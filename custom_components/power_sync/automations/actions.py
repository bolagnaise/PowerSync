"""
Action execution logic for HA automations.

Supported actions:
- set_backup_reserve: Set battery backup reserve percentage (Tesla/Sigenergy)
- preserve_charge: Prevent battery discharge (Tesla: set export to "never", Sigenergy: set discharge to 0)
- set_operation_mode: Set Powerwall operation mode (Tesla only)
- force_discharge: Force battery discharge for a duration (Tesla/Sigenergy)
- force_charge: Force battery charge for a duration (Tesla/Sigenergy)
- curtail_inverter: Curtail AC-coupled solar inverter
- restore_inverter: Restore inverter to normal operation
- send_notification: Send push notification to user
- set_grid_export: Set grid export rule (Tesla only)
- set_grid_charging: Enable/disable grid charging (Tesla only)
- restore_normal: Restore normal battery operation
- set_charge_rate: Set charge rate limit (Sigenergy only)
- set_discharge_rate: Set discharge rate limit (Sigenergy only)
- set_export_limit: Set export power limit (Sigenergy only)

EV Actions (Tesla Fleet/Teslemetry, Tesla BLE, OCPP, generic HA, Zaptec, or HA-native chargers):
- start_ev_charging: Start charging an EV
- stop_ev_charging: Stop charging an EV
- set_ev_charge_limit: Set EV charge limit percentage
- set_ev_charging_amps: Set EV charging amperage
- start_ev_charging_dynamic: Start dynamic EV charging that adjusts amps based on battery/grid
- stop_ev_charging_dynamic: Stop dynamic EV charging and cancel the adjustment timer
"""

import logging
import asyncio
from typing import List, Dict, Any, Optional, Callable

from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers import entity_registry as er, device_registry as dr
from homeassistant.helpers.event import async_track_time_interval, async_track_point_in_time
from datetime import timedelta, datetime, time as dt_time
from homeassistant.util import dt as dt_util

from ..const import (
    DOMAIN,
    CONF_EV_PROVIDER,
    EV_PROVIDER_FLEET_API,
    EV_PROVIDER_TESLA_BLE,
    EV_PROVIDER_TESLEMETRY_BT,
    EV_PROVIDER_BOTH,
    CONF_TESLA_BLE_ENTITY_PREFIX,
    DEFAULT_TESLA_BLE_ENTITY_PREFIX,
    TESLA_BLE_SWITCH_CHARGER,
    TESLA_BLE_NUMBER_CHARGING_AMPS,
    TESLA_BLE_NUMBER_CHARGING_LIMIT,
    TESLA_BLE_BUTTON_WAKE_UP,
    TESLA_BLE_BINARY_ASLEEP,
    TESLA_BLE_BINARY_STATUS,
    TESLEMETRY_BT_SWITCH_CHARGE,
    TESLEMETRY_BT_NUMBER_CHARGE_AMPS,
)
from ..solar_surplus_config import (
    DEFAULT_SOLAR_SURPLUS_MIN_BATTERY_SOC,
    get_solar_surplus_min_battery_soc,
)

_LOGGER = logging.getLogger(__name__)

# Tesla integrations supported for EV control via Fleet API
from ..const import TESLA_INTEGRATIONS
TESLA_EV_INTEGRATIONS = TESLA_INTEGRATIONS

# Global lock to prevent concurrent wake/charging attempts
_ev_wake_lock: Dict[str, bool] = {}  # vehicle_id -> is_waking

PRE_CHARGE_WAKE_ENTITY_KEYS = (
    "pre_charge_wake_entity",
    "ev_wake_entity",
    "wake_entity",
)
OCPP_MIN_CHARGE_AMPS = 6
PRE_CHARGE_WAKE_DURATION_KEYS = (
    "pre_charge_wake_duration_seconds",
    "pre_charge_wake_wait_seconds",
    "ev_wake_duration_seconds",
    "wake_duration_seconds",
)
PRE_CHARGE_WAKE_DONE_KEY = "_pre_charge_wake_done_entity"
DEFAULT_PRE_CHARGE_WAKE_DURATION_SECONDS = 5
MAX_PRE_CHARGE_WAKE_DURATION_SECONDS = 120

# API credit exhaustion tracking - prevents retry loops when Teslemetry credits are depleted
_api_credit_exhausted: Dict[str, datetime] = {}  # "teslemetry" -> exhaustion_timestamp
API_CREDIT_COOLDOWN_MINUTES = 15  # Wait before retrying after credit exhaustion

# Error messages that indicate API credit/payment issues
API_CREDIT_ERROR_PATTERNS = [
    "payment is required",
    "insufficient command credits",
    "insufficient credits",
    "payment required",
    "credits exhausted",
]


def _is_api_credit_error(error_message: str) -> bool:
    """Check if an error message indicates API credit exhaustion."""
    error_lower = str(error_message).lower()
    return any(pattern in error_lower for pattern in API_CREDIT_ERROR_PATTERNS)


def _mark_api_credits_exhausted(api_name: str = "teslemetry") -> None:
    """Mark that API credits are exhausted, triggering cooldown."""
    _api_credit_exhausted[api_name] = datetime.now()
    _LOGGER.warning(
        f"🚫 {api_name.title()} API credits exhausted. "
        f"Commands will be blocked for {API_CREDIT_COOLDOWN_MINUTES} minutes. "
        f"Please top up your API credits."
    )


def _is_api_credit_available(api_name: str = "teslemetry") -> bool:
    """Check if API credits are available (not in cooldown period)."""
    if api_name not in _api_credit_exhausted:
        return True

    exhausted_at = _api_credit_exhausted[api_name]
    cooldown_end = exhausted_at + timedelta(minutes=API_CREDIT_COOLDOWN_MINUTES)

    if datetime.now() >= cooldown_end:
        # Cooldown expired, clear the exhaustion flag
        del _api_credit_exhausted[api_name]
        _LOGGER.info(f"✅ {api_name.title()} API credit cooldown expired, retrying commands")
        return True

    remaining = (cooldown_end - datetime.now()).total_seconds() / 60
    _LOGGER.debug(
        f"🚫 {api_name.title()} API credits exhausted, {remaining:.1f} minutes remaining in cooldown"
    )
    return False


def _coerce_positive_int(value: Any, default: Optional[int] = None) -> Optional[int]:
    """Return a positive integer from user/config input, or default when invalid."""
    try:
        result = int(float(value))
    except (TypeError, ValueError):
        return default
    return result if result > 0 else default


def _kw_from_power_state(state: Any) -> float:
    """Return a power state as kW, accepting W or kW entities."""
    if not state or state.state in ("unknown", "unavailable", ""):
        return 0.0
    try:
        power = float(state.state or 0)
    except (TypeError, ValueError):
        return 0.0

    unit = str((getattr(state, "attributes", {}) or {}).get("unit_of_measurement", "")).strip().lower()
    if unit in ("w", "watt", "watts"):
        return max(0.0, power / 1000.0)
    if unit in ("kw", "kilowatt", "kilowatts"):
        return max(0.0, power)
    return max(0.0, power / 1000.0 if abs(power) > 100 else power)


def _is_sigenergy(config_entry: ConfigEntry) -> bool:
    """Check if this is a Sigenergy system."""
    from ..const import CONF_SIGENERGY_STATION_ID
    return bool(config_entry.data.get(CONF_SIGENERGY_STATION_ID))


async def _get_tesla_ev_entity(
    hass: HomeAssistant,
    entity_pattern: str,
    vehicle_vin: Optional[str] = None
) -> Optional[str]:
    """
    Find a Tesla EV entity by pattern.

    Args:
        hass: Home Assistant instance
        entity_pattern: Pattern to match (e.g., "button.*charge_start", "number.*charge_limit")
        vehicle_vin: Optional VIN to filter by specific vehicle

    Returns:
        Entity ID if found, None otherwise
    """
    import re

    entity_registry = er.async_get(hass)
    device_registry = dr.async_get(hass)

    # EV-specific entity patterns that only vehicles have (not energy products)
    ev_entity_markers = [
        r"button\..*_charge",  # charge_start, force_data_update
        r"switch\..*_charge$",  # charger switch
        r"number\..*_charge_limit",  # charge limit
        r"number\..*_charging_amps",  # charging amps
        r"sensor\..*_battery_level$",  # vehicle battery (not Powerwall)
        r"device_tracker\.",  # vehicle location
    ]
    ev_marker_patterns = [re.compile(p, re.IGNORECASE) for p in ev_entity_markers]

    def _device_has_ev_entities(device_id: str) -> bool:
        """Check if device has EV-specific entities."""
        for entity in entity_registry.entities.values():
            if entity.device_id == device_id:
                for marker in ev_marker_patterns:
                    if marker.search(entity.entity_id):
                        return True
        return False

    # Find devices from Tesla integrations
    tesla_devices = []
    all_domains_found = set()
    all_tesla_domain_devices = []  # Track all devices from Tesla domains

    for device in device_registry.devices.values():
        for identifier in device.identifiers:
            # Use index access instead of tuple unpacking (identifiers can have >2 values)
            if len(identifier) < 2:
                continue
            domain = identifier[0]
            identifier_value = identifier[1]
            all_domains_found.add(domain)
            if domain in TESLA_EV_INTEGRATIONS:
                id_str = str(identifier_value)
                id_len = len(id_str)
                is_all_digit = id_str.isdigit()
                _LOGGER.debug(f"Tesla domain device: {device.name}, domain={domain}, id={id_str}, len={id_len}, all_digit={is_all_digit}")

                all_tesla_domain_devices.append((device, domain, id_str))

                # Check if it's a vehicle (VIN is 17 chars, not all numeric)
                if id_len == 17 and not is_all_digit:
                    if vehicle_vin is None or id_str == vehicle_vin:
                        tesla_devices.append(device)
                        _LOGGER.info(f"Found Tesla EV vehicle by VIN: {device.name}, VIN: {id_str}")
                        break
                    else:
                        _LOGGER.debug(f"Tesla device VIN format OK but doesn't match filter: {device.name}, VIN={id_str}, filter={vehicle_vin}")
                else:
                    _LOGGER.debug(f"Tesla device skipped VIN check (not VIN format): {device.name}, id={id_str} (len={id_len}, all_digit={is_all_digit})")

    # Fallback: if no VIN-based devices found, check for devices with EV entities
    if not tesla_devices and all_tesla_domain_devices:
        _LOGGER.debug(f"No VIN-based devices found, checking {len(all_tesla_domain_devices)} Tesla domain devices for EV entities")
        for device, domain, id_str in all_tesla_domain_devices:
            if _device_has_ev_entities(device.id):
                tesla_devices.append(device)
                _LOGGER.info(f"Found Tesla EV device by entity detection: {device.name}, domain={domain}, id={id_str}")
                break

    if not tesla_devices:
        _LOGGER.warning(f"No Tesla EV devices found. Looking for domains {TESLA_EV_INTEGRATIONS}, found domains: {sorted(all_domains_found)}")
        if all_tesla_domain_devices:
            _LOGGER.warning(f"Found {len(all_tesla_domain_devices)} Tesla domain devices but none matched VIN format or had EV entities:")
            for device, domain, id_str in all_tesla_domain_devices[:5]:
                _LOGGER.warning(f"  - {device.name}: domain={domain}, id={id_str}")
        return None

    # Use first vehicle if no specific VIN provided
    target_device = tesla_devices[0]
    _LOGGER.debug(f"Found Tesla EV device: {target_device.name} (id: {target_device.id})")

    # Find matching entity for this device
    pattern = re.compile(entity_pattern, re.IGNORECASE)
    device_entities = []
    for entity in entity_registry.entities.values():
        if entity.device_id == target_device.id:
            device_entities.append(entity.entity_id)
            if pattern.match(entity.entity_id):
                _LOGGER.debug(f"Found matching entity: {entity.entity_id}")
                return entity.entity_id

    _LOGGER.warning(f"No entity matching pattern '{entity_pattern}' found for Tesla EV")
    if device_entities:
        _LOGGER.debug(f"Available entities for device: {device_entities[:20]}")  # Log first 20
    return None


def _get_vehicle_name_from_vin(hass: HomeAssistant, vehicle_vin: str) -> str:
    """
    Look up the friendly name of a Tesla vehicle from its VIN.

    Args:
        hass: Home Assistant instance
        vehicle_vin: The vehicle's VIN

    Returns:
        The vehicle's friendly name (e.g., "TESSY") or a truncated VIN if not found
    """
    device_registry = dr.async_get(hass)

    for device in device_registry.devices.values():
        for identifier in device.identifiers:
            if len(identifier) < 2:
                continue
            domain = identifier[0]
            identifier_value = identifier[1]
            if domain in TESLA_EV_INTEGRATIONS:
                id_str = str(identifier_value)
                # VIN is 17 chars, not all numeric
                if len(id_str) == 17 and not id_str.isdigit():
                    if id_str == vehicle_vin or vehicle_vin == DEFAULT_VEHICLE_ID:
                        return device.name or id_str[:8]

    return ""


def _is_vehicle_charge_complete(hass: HomeAssistant, vehicle_vin: str) -> bool:
    """
    Check if a Tesla vehicle has reached its charge target (charging state is 'complete').

    Args:
        hass: Home Assistant instance
        vehicle_vin: The vehicle's VIN

    Returns:
        True if the vehicle's charging state is 'complete'
    """
    import re

    if not vehicle_vin or vehicle_vin == DEFAULT_VEHICLE_ID:
        return False

    # Check Teslemetry/Fleet sensor.*_charging_state
    for state in hass.states.async_all():
        match = re.match(r"sensor\.(\w+)_charging_state$", state.entity_id)
        if match:
            candidate = match.group(1)
            # Match by VIN (case-insensitive)
            if candidate.upper() == vehicle_vin.upper():
                if state.state and state.state.lower() in ("complete", "stopped"):
                    return True
    return False


async def _get_observed_ev_power_kw(
    hass: HomeAssistant,
    vehicle_id: str,
    params: dict,
    *,
    allow_wall_connector_fallback: bool = False,
) -> float:
    """Return measured EV charging power for dynamic surplus control."""
    power_entity_keys = (
        "charger_power_entity",
        "charger_power_sensor",
        "charger_power_sensor_entity",
        "ev_power_entity",
        "ev_power_sensor",
        "power_entity",
    )
    for key in power_entity_keys:
        entity_id = params.get(key)
        if entity_id:
            power_kw = _kw_from_power_state(hass.states.get(str(entity_id)))
            if power_kw > 0.05:
                return power_kw

    charger_type = params.get("charger_type", "tesla")
    if charger_type == "tesla" and vehicle_id != DEFAULT_VEHICLE_ID:
        if allow_wall_connector_fallback:
            wall_power_kw = 0.0
            for state in hass.states.async_all("sensor"):
                entity_id = state.entity_id.lower()
                if "wall_connector" not in entity_id or "power" not in entity_id:
                    continue
                if any(token in entity_id for token in ("voltage", "current", "energy", "frequency")):
                    continue
                wall_power_kw = max(wall_power_kw, _kw_from_power_state(state))
            if wall_power_kw > 0.05:
                return wall_power_kw

        try:
            entity = await _get_tesla_ev_entity(
                hass,
                r"sensor\..*(charger_power|charging_power|charge_power)$",
                vehicle_id,
            )
            if entity:
                power_kw = _kw_from_power_state(hass.states.get(entity))
                if power_kw > 0.05:
                    return power_kw
        except Exception as err:
            _LOGGER.debug("Solar surplus EV: could not read Tesla EV power entity: %s", err)

    return 0.0


async def _wake_tesla_ev(
    hass: HomeAssistant,
    vehicle_vin: Optional[str] = None,
    wait_timeout: int = 45,
    max_retries: int = 3,
) -> bool:
    """
    Wake up a Tesla vehicle before sending commands.

    Tesla vehicles can take 15-60 seconds to wake from deep sleep.
    This function sends the wake command, waits, and verifies the car is awake.

    Args:
        hass: Home Assistant instance
        vehicle_vin: Optional VIN to filter by specific vehicle
        wait_timeout: Maximum seconds to wait for car to wake (default 45)
        max_retries: Number of wake command retries (default 3)

    Returns:
        True if vehicle is awake (or timeout reached), False if wake failed completely
    """
    import asyncio

    # Check if API credits are exhausted (cooldown period active)
    if not _is_api_credit_available("teslemetry"):
        _LOGGER.warning("Skipping Tesla wake - API credits exhausted, in cooldown period")
        return False

    # Generate a lock key (use VIN if available, otherwise generic)
    lock_key = vehicle_vin or "default"

    # Check if wake is already in progress for this vehicle
    if _ev_wake_lock.get(lock_key, False):
        _LOGGER.info(f"Wake already in progress for Tesla EV (key={lock_key}), waiting for it to complete")
        # Wait up to wait_timeout for the other wake to complete
        wait_start = asyncio.get_event_loop().time()
        while _ev_wake_lock.get(lock_key, False):
            if (asyncio.get_event_loop().time() - wait_start) > wait_timeout:
                _LOGGER.warning(f"Timed out waiting for existing wake to complete")
                return True  # Proceed anyway
            await asyncio.sleep(2)
        _LOGGER.info(f"Previous wake completed, proceeding")
        return True

    # Acquire lock
    _ev_wake_lock[lock_key] = True
    _LOGGER.debug(f"Acquired wake lock for Tesla EV (key={lock_key})")

    try:
        # Find the wake up button entity
        wake_entity = await _get_tesla_ev_entity(
            hass,
            r"button\..*wake(_up)?$",
            vehicle_vin
        )

        if not wake_entity:
            _LOGGER.warning("Could not find Tesla wake button entity")
            return False

        # Try multiple entity patterns to verify wake status
        # Different Tesla integrations use different entity naming conventions
        # Order matters - more specific patterns first to avoid matching wrong entities
        status_patterns = [
            # Teslemetry uses binary_sensor.*_status (on=online, off=offline)
            (r"binary_sensor\..*_status$", "binary", "on"),
            # Other integrations use binary_sensor.*_asleep (off=awake)
            (r"binary_sensor\..*_asleep$", "binary", "off"),
            (r"binary_sensor\..*asleep$", "binary", "off"),
            # Fallback: sensor.*_vehicle_state (avoid shift_state which is drive gear)
            (r"sensor\..*_vehicle_state$", "state", "online"),
            (r"sensor\..*vehicle_state$", "state", "online"),
        ]

        status_entity = None
        status_type = None
        awake_value = None

        for pattern, stype, awake_val in status_patterns:
            entity = await _get_tesla_ev_entity(hass, pattern, vehicle_vin)
            if entity:
                status_entity = entity
                status_type = stype
                awake_value = awake_val
                _LOGGER.info(f"Found Tesla status entity: {entity} (type={stype}, awake_value={awake_val})")
                break

        if not status_entity:
            _LOGGER.warning(
                "Could not find Tesla status/asleep sensor. "
                "Will send wake command but cannot verify wake completion. "
                "Tried patterns: binary_sensor.*_status, binary_sensor.*_asleep, "
                "sensor.*_vehicle_state"
            )

        def _is_awake() -> bool:
            """Check if the vehicle is currently awake."""
            if not status_entity:
                return False
            state = hass.states.get(status_entity)
            if not state:
                return False
            if status_type == "binary":
                return state.state == awake_value
            else:
                return state.state.lower() == awake_value.lower()

        # Check if already awake
        if _is_awake():
            _LOGGER.debug(f"Tesla EV is already awake ({status_entity})")
            return True

        # Send wake command with retries
        for attempt in range(1, max_retries + 1):
            try:
                _LOGGER.info(f"Sending wake command to Tesla EV (attempt {attempt}/{max_retries}): {wake_entity}")
                await hass.services.async_call(
                    "button",
                    "press",
                    {"entity_id": wake_entity},
                    blocking=True,
                )
            except Exception as e:
                _LOGGER.warning(f"Wake command attempt {attempt} failed: {e}")

                # Check if this is a credit/payment error - if so, stop retrying
                if _is_api_credit_error(str(e)):
                    _mark_api_credits_exhausted("teslemetry")
                    _LOGGER.error(f"Failed to wake Tesla EV - API credits exhausted")
                    return False

                if attempt == max_retries:
                    _LOGGER.error(f"Failed to wake Tesla EV after {max_retries} attempts")
                    # Still return True to attempt the charging command anyway
                    return True
                await asyncio.sleep(5)
                continue

            # If we don't have a status sensor, just wait a fixed time
            if not status_entity:
                _LOGGER.info(f"No status sensor available, waiting {wait_timeout // max_retries}s before proceeding")
                await asyncio.sleep(wait_timeout // max_retries)
                if attempt == max_retries:
                    return True
                continue

            # Wait for car to wake up (poll every 3 seconds)
            start_time = asyncio.get_event_loop().time()
            poll_interval = 3
            wake_timeout_per_attempt = wait_timeout // max_retries

            while (asyncio.get_event_loop().time() - start_time) < wake_timeout_per_attempt:
                await asyncio.sleep(poll_interval)
                elapsed = int(asyncio.get_event_loop().time() - start_time)

                if _is_awake():
                    _LOGGER.info(f"Tesla EV is now awake after {elapsed}s ({status_entity})")
                    await asyncio.sleep(2)  # Extra buffer for readiness
                    return True

                _LOGGER.debug(f"Waiting for Tesla EV to wake... {elapsed}s elapsed")

            _LOGGER.info(f"Wake attempt {attempt} timed out after {wake_timeout_per_attempt}s, will retry...")

        _LOGGER.warning(f"Tesla EV wake timed out after {wait_timeout}s total, attempting command anyway")
        return True

    finally:
        # Always release the lock
        _ev_wake_lock[lock_key] = False
        _LOGGER.debug(f"Released wake lock for Tesla EV (key={lock_key})")


def _get_ev_config(config_entry: ConfigEntry) -> dict:
    """Get EV configuration from config entry."""
    return {
        "ev_provider": config_entry.options.get(CONF_EV_PROVIDER, EV_PROVIDER_FLEET_API),
        "ble_prefix": config_entry.options.get(
            CONF_TESLA_BLE_ENTITY_PREFIX, DEFAULT_TESLA_BLE_ENTITY_PREFIX
        ),
    }


def _resolve_ble_prefix_for_vehicle(
    hass: HomeAssistant, config_entry: ConfigEntry, vehicle_vin: str | None
) -> str:
    """Get the correct BLE prefix for a specific vehicle.

    If vehicle_vin is a BLE identifier (ble_*), extract the prefix from it.
    Otherwise fall back to the first configured BLE prefix.
    """
    if vehicle_vin and vehicle_vin.startswith("ble_"):
        return vehicle_vin[4:]  # "ble_joanna_model_3_local" → "joanna_model_3_local"

    # Fall back to first configured prefix
    raw = config_entry.options.get(CONF_TESLA_BLE_ENTITY_PREFIX, DEFAULT_TESLA_BLE_ENTITY_PREFIX)
    prefixes = [p.strip() for p in raw.split(",") if p.strip()]
    return prefixes[0] if prefixes else DEFAULT_TESLA_BLE_ENTITY_PREFIX


def _is_ble_available(hass: HomeAssistant, ble_prefix: str) -> bool:
    """Check if Tesla BLE entities are available."""
    charger_entity = TESLA_BLE_SWITCH_CHARGER.format(prefix=ble_prefix)
    state = hass.states.get(charger_entity)
    return state is not None


async def _wake_tesla_ble(hass: HomeAssistant, ble_prefix: str, wait_timeout: int = 30) -> bool:
    """Wake up Tesla via BLE and wait for it to be awake.

    Args:
        hass: Home Assistant instance
        ble_prefix: The BLE entity prefix (e.g., "tesla_ble")
        wait_timeout: Maximum seconds to wait for car to wake up (default 30)
    """
    import asyncio

    wake_entity = TESLA_BLE_BUTTON_WAKE_UP.format(prefix=ble_prefix)
    asleep_entity = TESLA_BLE_BINARY_ASLEEP.format(prefix=ble_prefix)

    state = hass.states.get(wake_entity)
    if state is None:
        _LOGGER.warning(f"Tesla BLE wake entity not found: {wake_entity}")
        return False

    # Check if already awake
    asleep_state = hass.states.get(asleep_entity)
    if asleep_state and asleep_state.state == "off":
        _LOGGER.debug("Tesla BLE: Car is already awake")
        return True

    try:
        await hass.services.async_call(
            "button",
            "press",
            {"entity_id": wake_entity},
            blocking=True,
        )
        _LOGGER.info(f"Sent wake command via Tesla BLE: {wake_entity}")

        # Wait for car to wake up (poll every 2 seconds)
        start_time = asyncio.get_event_loop().time()
        while (asyncio.get_event_loop().time() - start_time) < wait_timeout:
            await asyncio.sleep(2)

            asleep_state = hass.states.get(asleep_entity)
            if asleep_state and asleep_state.state == "off":
                _LOGGER.info(f"Tesla BLE: Car is now awake after {int(asyncio.get_event_loop().time() - start_time)}s")
                # Give it a bit more time to be fully ready
                await asyncio.sleep(2)
                return True

            # Also check status entity as fallback
            status_entity = TESLA_BLE_BINARY_STATUS.format(prefix=ble_prefix)
            status_state = hass.states.get(status_entity)
            if status_state and status_state.state == "on":
                _LOGGER.info(f"Tesla BLE: Car is online after {int(asyncio.get_event_loop().time() - start_time)}s")
                await asyncio.sleep(2)
                return True

        _LOGGER.warning(f"Tesla BLE: Timed out waiting for car to wake after {wait_timeout}s")
        # Still return True to attempt the command anyway
        return True
    except Exception as e:
        _LOGGER.error(f"Failed to wake Tesla via BLE: {e}")
        return False


async def _start_ev_charging_ble(hass: HomeAssistant, ble_prefix: str) -> bool:
    """Start EV charging via Tesla BLE."""
    charger_entity = TESLA_BLE_SWITCH_CHARGER.format(prefix=ble_prefix)

    if hass.states.get(charger_entity) is None:
        _LOGGER.error(f"Tesla BLE charger entity not found: {charger_entity}")
        return False

    try:
        await _wake_tesla_ble(hass, ble_prefix)
        await hass.services.async_call(
            "switch",
            "turn_on",
            {"entity_id": charger_entity},
            blocking=True,
        )
        _LOGGER.info(f"Started EV charging via Tesla BLE: {charger_entity}")
        return True
    except Exception as e:
        err_str = str(e).lower()
        if "complete" in err_str:
            _LOGGER.info(f"EV charging is complete (at target SOC) via BLE — skipping start")
        else:
            _LOGGER.error(f"Failed to start EV charging via BLE: {e}")
        return False


async def _stop_ev_charging_ble(hass: HomeAssistant, ble_prefix: str) -> bool:
    """Stop EV charging via Tesla BLE."""
    charger_entity = TESLA_BLE_SWITCH_CHARGER.format(prefix=ble_prefix)

    if hass.states.get(charger_entity) is None:
        _LOGGER.error(f"Tesla BLE charger entity not found: {charger_entity}")
        return False

    try:
        await _wake_tesla_ble(hass, ble_prefix)
        await hass.services.async_call(
            "switch",
            "turn_off",
            {"entity_id": charger_entity},
            blocking=True,
        )
        _LOGGER.info(f"Stopped EV charging via Tesla BLE: {charger_entity}")
        return True
    except Exception as e:
        _LOGGER.error(f"Failed to stop EV charging via BLE: {e}")
        return False


async def _set_ev_charge_limit_ble(
    hass: HomeAssistant, ble_prefix: str, percent: int
) -> bool:
    """Set EV charge limit via Tesla BLE."""
    limit_entity = TESLA_BLE_NUMBER_CHARGING_LIMIT.format(prefix=ble_prefix)

    if hass.states.get(limit_entity) is None:
        _LOGGER.error(f"Tesla BLE charge limit entity not found: {limit_entity}")
        return False

    try:
        await _wake_tesla_ble(hass, ble_prefix)
        await hass.services.async_call(
            "number",
            "set_value",
            {"entity_id": limit_entity, "value": percent},
            blocking=True,
        )
        _LOGGER.info(f"Set EV charge limit to {percent}% via Tesla BLE: {limit_entity}")
        return True
    except Exception as e:
        _LOGGER.error(f"Failed to set EV charge limit via BLE: {e}")
        return False


async def _set_ev_charging_amps_ble(
    hass: HomeAssistant,
    ble_prefix: str,
    amps: int,
    *,
    allow_stale_entity_max_override: bool = False,
    configured_max_amps: Optional[int] = None,
) -> bool:
    """Set EV charging amps via Tesla BLE."""
    amps_entity = TESLA_BLE_NUMBER_CHARGING_AMPS.format(prefix=ble_prefix)

    state = hass.states.get(amps_entity)
    if state is None:
        _LOGGER.error(f"Tesla BLE charging amps entity not found: {amps_entity}")
        return False

    # Cap amps to entity's min/max range. Tesla integrations can report a stale
    # 16A max while idle, so solar-surplus callers may opt into the configured
    # app/home-power max instead.
    entity_min = _coerce_positive_int(state.attributes.get("min"), 0) or 0
    entity_max = _coerce_positive_int(state.attributes.get("max"), int(amps)) or int(amps)
    effective_max = entity_max
    if (
        allow_stale_entity_max_override
        and configured_max_amps is not None
        and configured_max_amps > entity_max
    ):
        effective_max = configured_max_amps
        _LOGGER.debug(
            "BLE amps using configured max %dA over entity max %dA",
            configured_max_amps,
            entity_max,
        )
    capped_amps = max(entity_min, min(effective_max, int(amps)))
    if capped_amps != amps:
        _LOGGER.debug(f"BLE amps capped from {amps}A to {capped_amps}A (entity range: {entity_min}-{entity_max})")

    try:
        await _wake_tesla_ble(hass, ble_prefix)
        await hass.services.async_call(
            "number",
            "set_value",
            {"entity_id": amps_entity, "value": capped_amps},
            blocking=True,
        )
        _LOGGER.info(f"Set EV charging amps to {capped_amps}A via Tesla BLE: {amps_entity}")
        return True
    except Exception as e:
        _LOGGER.error(f"Failed to set EV charging amps via BLE: {e}")
        return False


# =============================================================================
# Teslemetry Bluetooth helpers (native HA integration: tesla_bluetooth)
# =============================================================================


def _resolve_teslemetry_bt_prefix(hass: HomeAssistant) -> str | None:
    """Auto-detect Teslemetry Bluetooth entity prefix (VIN).

    Scans for sensor.*_charging_state entities where the prefix is a 17-char
    alphanumeric VIN (distinguishes from ESPHome BLE which contains 'ble').
    """
    import re
    for state in hass.states.async_all():
        match = re.match(r"sensor\.(\w+)_charging_state$", state.entity_id)
        if match:
            candidate = match.group(1)
            if len(candidate) == 17 and candidate.isalnum():
                charge_switch = f"switch.{candidate}_charge"
                if hass.states.get(charge_switch) is not None:
                    return candidate
    return None


def _is_teslemetry_bt_available(hass: HomeAssistant, tbt_prefix: str | None) -> bool:
    """Check if Teslemetry Bluetooth entities are available."""
    if not tbt_prefix:
        return False
    charge_entity = TESLEMETRY_BT_SWITCH_CHARGE.format(prefix=tbt_prefix)
    state = hass.states.get(charge_entity)
    return state is not None


async def _start_ev_charging_teslemetry_bt(hass: HomeAssistant, tbt_prefix: str) -> bool:
    """Start EV charging via Teslemetry Bluetooth."""
    entity_id = TESLEMETRY_BT_SWITCH_CHARGE.format(prefix=tbt_prefix)
    if hass.states.get(entity_id) is None:
        _LOGGER.error(f"Teslemetry BT charge entity not found: {entity_id}")
        return False
    try:
        await hass.services.async_call(
            "switch", "turn_on",
            {"entity_id": entity_id},
            blocking=True,
        )
        _LOGGER.info(f"Started EV charging via Teslemetry BT: {entity_id}")
        return True
    except Exception as e:
        err_str = str(e).lower()
        if "complete" in err_str:
            _LOGGER.info(f"EV charging is complete (at target SOC) via Teslemetry BT — skipping start")
        else:
            _LOGGER.error(f"Failed to start EV charging via Teslemetry BT: {e}")
        return False


async def _stop_ev_charging_teslemetry_bt(hass: HomeAssistant, tbt_prefix: str) -> bool:
    """Stop EV charging via Teslemetry Bluetooth."""
    entity_id = TESLEMETRY_BT_SWITCH_CHARGE.format(prefix=tbt_prefix)
    if hass.states.get(entity_id) is None:
        _LOGGER.error(f"Teslemetry BT charge entity not found: {entity_id}")
        return False
    try:
        await hass.services.async_call(
            "switch", "turn_off",
            {"entity_id": entity_id},
            blocking=True,
        )
        _LOGGER.info(f"Stopped EV charging via Teslemetry BT: {entity_id}")
        return True
    except Exception as e:
        _LOGGER.error(f"Failed to stop EV charging via Teslemetry BT: {e}")
        return False


async def _set_ev_charging_amps_teslemetry_bt(
    hass: HomeAssistant,
    tbt_prefix: str,
    amps: int,
    *,
    allow_stale_entity_max_override: bool = False,
    configured_max_amps: Optional[int] = None,
) -> bool:
    """Set EV charging amps via Teslemetry Bluetooth."""
    entity_id = TESLEMETRY_BT_NUMBER_CHARGE_AMPS.format(prefix=tbt_prefix)
    state = hass.states.get(entity_id)
    if state is None:
        _LOGGER.error(f"Teslemetry BT charge amps entity not found: {entity_id}")
        return False
    min_val = _coerce_positive_int(state.attributes.get("min"), 0) or 0
    max_val = _coerce_positive_int(state.attributes.get("max"), 32) or 32
    effective_max = max_val
    if (
        allow_stale_entity_max_override
        and configured_max_amps is not None
        and configured_max_amps > max_val
    ):
        effective_max = configured_max_amps
        _LOGGER.debug(
            "Teslemetry BT amps using configured max %dA over entity max %dA",
            configured_max_amps,
            max_val,
        )
    capped = max(min_val, min(effective_max, int(amps)))
    if capped != amps:
        _LOGGER.debug(f"Teslemetry BT amps capped from {amps}A to {capped}A (range: {min_val}-{max_val})")
    try:
        await hass.services.async_call(
            "number", "set_value",
            {"entity_id": entity_id, "value": capped},
            blocking=True,
        )
        _LOGGER.info(f"Set EV charging amps to {capped}A via Teslemetry BT: {entity_id}")
        return True
    except Exception as e:
        _LOGGER.error(f"Failed to set EV charging amps via Teslemetry BT: {e}")
        return False


async def _get_sigenergy_controller(config_entry: ConfigEntry) -> Optional["SigenergyController"]:
    """Get a Sigenergy controller for Modbus operations.

    Returns:
        SigenergyController instance or None if not configured
    """
    from ..const import (
        CONF_SIGENERGY_MODBUS_HOST,
        CONF_SIGENERGY_MODBUS_PORT,
        CONF_SIGENERGY_MODBUS_SLAVE_ID,
        CONF_SIGENERGY_EXPORT_LIMIT_KW,
    )
    from ..inverters.sigenergy import SigenergyController

    # Check both data and options for Modbus settings
    modbus_host = config_entry.options.get(
        CONF_SIGENERGY_MODBUS_HOST,
        config_entry.data.get(CONF_SIGENERGY_MODBUS_HOST)
    )
    if not modbus_host:
        _LOGGER.warning("Sigenergy Modbus host not configured")
        return None

    modbus_port = config_entry.options.get(
        CONF_SIGENERGY_MODBUS_PORT,
        config_entry.data.get(CONF_SIGENERGY_MODBUS_PORT, 502)
    )
    modbus_slave_id = config_entry.options.get(
        CONF_SIGENERGY_MODBUS_SLAVE_ID,
        config_entry.data.get(CONF_SIGENERGY_MODBUS_SLAVE_ID, 1)
    )
    export_limit_kw = config_entry.data.get(CONF_SIGENERGY_EXPORT_LIMIT_KW)

    return SigenergyController(
        host=modbus_host,
        port=modbus_port,
        slave_id=modbus_slave_id,
        max_export_limit_kw=export_limit_kw,
    )


async def execute_actions(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    actions: List[Dict[str, Any]],
    context: Optional[Dict[str, Any]] = None
) -> bool:
    """
    Execute a list of automation actions.

    Args:
        hass: Home Assistant instance
        config_entry: Config entry for this integration
        actions: List of action dicts to execute
        context: Optional context with time_window_start, time_window_end, timezone

    Returns:
        True if at least one action executed successfully
    """
    success_count = 0

    for action in actions:
        try:
            action_type = action.get("action_type")
            params = action.get("parameters", {})
            if isinstance(params, str):
                import json
                params = json.loads(params) if params else {}

            result = await _execute_single_action(hass, config_entry, action_type, params, context)
            if result:
                success_count += 1
                _LOGGER.info(f"Executed action '{action_type}'")
            elif result is None:
                _LOGGER.debug(f"Action '{action_type}' skipped (not applicable for this system)")
            else:
                _LOGGER.warning(f"Action '{action_type}' returned False")
                try:
                    await _send_expo_push(hass, "⚠️ Automation Action Failed", f"Action '{action_type}' did not complete successfully")
                except Exception:
                    pass
        except Exception as e:
            _LOGGER.error(f"Error executing action '{action.get('action_type')}': {e}")
            try:
                await _send_expo_push(hass, "⚠️ Automation Error", f"Action '{action.get('action_type')}' failed: {e}")
            except Exception:
                pass

    return success_count > 0


def _ev_action_loadpoint_id(params: Dict[str, Any]) -> str:
    """Return the loadpoint id for direct EV start/stop automation actions."""
    vehicle_id = params.get("vehicle_id") or params.get("vehicle_vin")
    if vehicle_id:
        return str(vehicle_id)

    charger_type = params.get("charger_type")
    if charger_type == "generic":
        return "generic_ev"
    if charger_type == "ocpp":
        charger_id = str(params.get("ocpp_charger_id") or "ocpp_charger")
        return charger_id if charger_id.startswith("ocpp_") else f"ocpp_{charger_id}"
    if charger_type == "zaptec":
        return "zaptec_standalone"
    if charger_type == "sigenergy":
        return "sigenergy_charger"

    return DEFAULT_VEHICLE_ID


def _session_energy_tracked_by_charger_poll(params: Dict[str, Any]) -> bool:
    """Return true when a charger poll provides authoritative session metering."""
    return str(params.get("charger_type") or "").lower() == "ocpp"


def _sigenergy_charger_config(config_entry: ConfigEntry, params: Dict[str, Any]) -> dict:
    """Resolve Sigenergy EV charger Modbus connection details."""
    from ..const import (
        CONF_SIGENERGY_CHARGER_HOST,
        CONF_SIGENERGY_CHARGER_PORT,
        CONF_SIGENERGY_CHARGER_SLAVE_ID,
        CONF_SIGENERGY_CHARGER_TYPE,
        CONF_SIGENERGY_MODBUS_HOST,
        DEFAULT_SIGENERGY_CHARGER_PORT,
        DEFAULT_SIGENERGY_CHARGER_SLAVE_ID,
        SIGENERGY_CHARGER_EVAC,
    )

    opts = {**getattr(config_entry, "data", {}), **getattr(config_entry, "options", {})}
    host = (
        params.get("sigenergy_charger_host")
        or opts.get(CONF_SIGENERGY_CHARGER_HOST)
        or opts.get(CONF_SIGENERGY_MODBUS_HOST)
        or ""
    )
    return {
        "host": str(host).strip(),
        "port": int(
            params.get("sigenergy_charger_port")
            or opts.get(CONF_SIGENERGY_CHARGER_PORT)
            or DEFAULT_SIGENERGY_CHARGER_PORT
        ),
        "slave_id": int(
            params.get("sigenergy_charger_slave_id")
            or opts.get(CONF_SIGENERGY_CHARGER_SLAVE_ID)
            or DEFAULT_SIGENERGY_CHARGER_SLAVE_ID
        ),
        "charger_type": str(
            params.get("sigenergy_charger_type")
            or opts.get(CONF_SIGENERGY_CHARGER_TYPE)
            or SIGENERGY_CHARGER_EVAC
        ).lower(),
    }


def _new_sigenergy_charger(config: dict):
    """Return a configured Sigenergy EV charger controller."""
    from ..sigenergy_charger import SigenergyEVChargerController

    return SigenergyEVChargerController(**config)


async def _start_sigenergy_charger(
    config_entry: ConfigEntry,
    params: Dict[str, Any],
    amps: int | None = None,
) -> bool:
    config = _sigenergy_charger_config(config_entry, params)
    if not config["host"]:
        _LOGGER.error("Sigenergy charger start: no Modbus host configured")
        return False

    controller = _new_sigenergy_charger(config)
    try:
        return await controller.start_charging(amps=amps)
    finally:
        await controller.disconnect()


async def _stop_sigenergy_charger(config_entry: ConfigEntry, params: Dict[str, Any]) -> bool:
    config = _sigenergy_charger_config(config_entry, params)
    if not config["host"]:
        _LOGGER.error("Sigenergy charger stop: no Modbus host configured")
        return False

    controller = _new_sigenergy_charger(config)
    try:
        return await controller.stop_charging()
    finally:
        await controller.disconnect()


async def _set_sigenergy_charger_amps(
    config_entry: ConfigEntry,
    params: Dict[str, Any],
    amps: int,
) -> bool:
    config = _sigenergy_charger_config(config_entry, params)
    if not config["host"]:
        _LOGGER.error("Sigenergy charger set amps: no Modbus host configured")
        return False

    controller = _new_sigenergy_charger(config)
    try:
        return await controller.set_charging_amps(amps)
    finally:
        await controller.disconnect()




async def _execute_single_action(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    action_type: str,
    params: Dict[str, Any],
    context: Optional[Dict[str, Any]] = None
) -> bool:
    """
    Execute a single action.

    Args:
        hass: Home Assistant instance
        config_entry: Config entry
        action_type: Type of action to execute
        params: Action parameters
        context: Optional context with time_window_start, time_window_end, timezone

    Returns:
        True if action executed successfully
    """
    if action_type == "set_backup_reserve":
        return await _action_set_backup_reserve(hass, config_entry, params)
    elif action_type == "preserve_charge":
        return await _action_preserve_charge(hass, config_entry)
    elif action_type == "set_operation_mode":
        return await _action_set_operation_mode(hass, config_entry, params)
    elif action_type == "force_discharge":
        return await _action_force_discharge(hass, config_entry, params)
    elif action_type == "force_charge":
        return await _action_force_charge(hass, config_entry, params)
    elif action_type == "curtail_inverter":
        return await _action_curtail_inverter(hass, config_entry, params)
    elif action_type == "restore_inverter":
        return await _action_restore_inverter(hass, config_entry)
    elif action_type == "send_notification":
        return await _action_send_notification(hass, params)
    elif action_type == "set_grid_export":
        return await _action_set_grid_export(hass, config_entry, params)
    elif action_type == "set_grid_charging":
        return await _action_set_grid_charging(hass, config_entry, params)
    elif action_type == "set_storm_watch":
        return await _action_set_storm_watch(hass, config_entry, params)
    elif action_type == "set_off_grid_ev_reserve":
        return await _action_set_off_grid_ev_reserve(hass, config_entry, params)
    elif action_type == "set_vpp_enrollment":
        return await _action_set_vpp_enrollment(hass, config_entry, params)
    elif action_type == "restore_normal":
        return await _action_restore_normal(hass, config_entry)
    elif action_type == "set_amber_forecast_type":
        return await _action_set_amber_forecast_type(hass, config_entry, params)
    elif action_type == "set_charge_rate":
        return await _action_set_charge_rate(hass, config_entry, params)
    elif action_type == "set_discharge_rate":
        return await _action_set_discharge_rate(hass, config_entry, params)
    elif action_type == "set_export_limit":
        return await _action_set_export_limit(hass, config_entry, params)
    # EV Charging Actions (pass context for time window support)
    elif action_type == "start_ev_charging":
        success = await _action_start_ev_charging(hass, config_entry, params, context)
        if success and not params.get("skip_ownership"):
            await record_manual_ev_charging_session(
                hass,
                config_entry,
                _ev_action_loadpoint_id(params),
                params,
                reason=params.get("reason", "Manual automation start"),
            )
        return success
    elif action_type == "stop_ev_charging":
        success = await _action_stop_ev_charging(hass, config_entry, params)
        if success and not params.get("skip_ownership"):
            await clear_tracked_ev_charging_session(
                hass,
                config_entry,
                _ev_action_loadpoint_id(params),
                reason=params.get("reason", "Manual automation stop"),
            )
        return success
    elif action_type == "set_ev_charge_limit":
        return await _action_set_ev_charge_limit(hass, config_entry, params)
    elif action_type == "set_ev_charging_amps":
        return await _action_set_ev_charging_amps(hass, config_entry, params)
    elif action_type == "start_ev_charging_dynamic":
        return await _action_start_ev_charging_dynamic(hass, config_entry, params, context)
    elif action_type == "stop_ev_charging_dynamic":
        return await _action_stop_ev_charging_dynamic(hass, config_entry, params)
    elif action_type == "enable_optimizer":
        return await _action_enable_optimizer(hass, config_entry)
    elif action_type == "disable_optimizer":
        return await _action_disable_optimizer(hass, config_entry)
    elif action_type == "powerwall_go_off_grid":
        return await _action_powerwall_off_grid(hass, config_entry, "go_off_grid")
    elif action_type == "powerwall_reconnect_grid":
        return await _action_powerwall_off_grid(hass, config_entry, "reconnect")
    else:
        _LOGGER.warning(f"Unknown action type: {action_type}")
        return False


async def _action_powerwall_off_grid(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    action: str,
) -> bool:
    """Dispatch an automation into the Powerwall local off-grid service.

    Wraps ``power_sync.powerwall_go_off_grid`` / ``powerwall_reconnect_grid``
    with a single retry. Raises no exceptions — returns False on failure so
    the automation engine can surface a notification.
    """
    from ..const import DOMAIN
    service = (
        "powerwall_go_off_grid" if action == "go_off_grid" else "powerwall_reconnect_grid"
    )
    try:
        await hass.services.async_call(DOMAIN, service, {}, blocking=True)
        return True
    except Exception as e:
        _LOGGER.error(f"powerwall local {action} failed: {e}")
        return False


async def _action_set_backup_reserve(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    params: Dict[str, Any]
) -> bool:
    """Set battery backup reserve percentage.

    Supports both Tesla Powerwall and SigEnergy systems.
    """
    from ..const import DOMAIN, SERVICE_SET_BACKUP_RESERVE

    # Accept both "percent" and "reserve_percent" for flexibility
    reserve_percent = params.get("percent") or params.get("reserve_percent")
    if reserve_percent is None:
        _LOGGER.error("set_backup_reserve: missing percent parameter")
        return False

    # Clamp to valid range
    reserve_percent = max(0, min(100, int(reserve_percent)))

    for attempt in range(10):
        try:
            await hass.services.async_call(
                DOMAIN,
                SERVICE_SET_BACKUP_RESERVE,
                {"percent": reserve_percent},
                blocking=True,
            )
            if attempt > 0:
                _LOGGER.info(f"set_backup_reserve succeeded on attempt {attempt + 1}")
            return True
        except Exception as e:
            delay = min(60, 5 * (2 ** attempt))  # 5, 10, 20, 40, 60, 60...
            _LOGGER.warning(f"set_backup_reserve attempt {attempt + 1}/10 failed: {e} — retrying in {delay}s")
            await asyncio.sleep(delay)
    _LOGGER.error(f"Failed to set backup reserve to {reserve_percent}% after 10 attempts")
    return False


async def _action_preserve_charge(
    hass: HomeAssistant,
    config_entry: ConfigEntry
) -> bool:
    """Prevent battery discharge."""
    if _is_sigenergy(config_entry):
        # Sigenergy: Set discharge rate limit to 0 to prevent discharge
        controller = await _get_sigenergy_controller(config_entry)
        if not controller:
            _LOGGER.error("preserve_charge: Sigenergy Modbus not configured")
            return False
        try:
            result = await controller.set_discharge_rate_limit(0)
            if result:
                _LOGGER.info("Sigenergy: Set discharge rate to 0 (preserve charge)")
            return result
        except Exception as e:
            _LOGGER.error(f"Failed to preserve charge (Sigenergy): {e}")
            return False
        finally:
            await controller.disconnect()

    from ..const import CONF_BATTERY_SYSTEM, BATTERY_SYSTEM_TESLA, DOMAIN, SERVICE_SET_GRID_EXPORT
    if config_entry.data.get(CONF_BATTERY_SYSTEM) != BATTERY_SYSTEM_TESLA:
        _LOGGER.debug("preserve_charge via grid export not supported for non-Tesla systems")
        return None

    try:
        await hass.services.async_call(
            DOMAIN,
            SERVICE_SET_GRID_EXPORT,
            {"rule": "never"},
            blocking=True,
        )
        return True
    except Exception as e:
        _LOGGER.error(f"Failed to preserve charge: {e}")
        return False


async def _action_set_operation_mode(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    params: Dict[str, Any]
) -> bool:
    """Set battery operation mode (Tesla only)."""
    if _is_sigenergy(config_entry):
        _LOGGER.warning("set_operation_mode not supported for Sigenergy")
        return False

    from ..const import DOMAIN, SERVICE_SET_OPERATION_MODE

    mode = params.get("mode")
    if not mode:
        _LOGGER.error("set_operation_mode: missing mode parameter")
        return False

    valid_modes = ["self_consumption", "autonomous", "backup"]
    if mode not in valid_modes:
        _LOGGER.error(f"set_operation_mode: invalid mode '{mode}'")
        return False

    for attempt in range(10):
        try:
            await hass.services.async_call(
                DOMAIN,
                SERVICE_SET_OPERATION_MODE,
                {"mode": mode},
                blocking=True,
            )
            if attempt > 0:
                _LOGGER.info(f"set_operation_mode succeeded on attempt {attempt + 1}")
            return True
        except Exception as e:
            delay = min(60, 5 * (2 ** attempt))  # 5, 10, 20, 40, 60, 60...
            _LOGGER.warning(f"set_operation_mode attempt {attempt + 1}/10 failed: {e} — retrying in {delay}s")
            await asyncio.sleep(delay)
    _LOGGER.error(f"Failed to set operation mode to {mode} after 10 attempts")
    return False


async def _action_force_discharge(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    params: Dict[str, Any]
) -> bool:
    """Force battery discharge for a specified duration."""
    # Web app stores as "minutes", mobile app as "duration_minutes", HA automations as "duration"
    duration = params.get("duration") or params.get("duration_minutes") or params.get("minutes", 30)

    if _is_sigenergy(config_entry):
        # Sigenergy: Enable Remote EMS + force discharge mode
        controller = await _get_sigenergy_controller(config_entry)
        if not controller:
            _LOGGER.error("force_discharge: Sigenergy Modbus not configured")
            return False
        try:
            power_kw = params.get("power_w", 10000) / 1000 if params.get("power_w") else 10.0
            result = await controller.force_discharge(power_kw)
            if result:
                # Also restore export limit to allow discharge to grid
                await controller.restore_export_limit()
                _LOGGER.info(f"Sigenergy: Force discharge activated at {power_kw}kW for {duration} minutes")
                return True
            else:
                _LOGGER.error("Sigenergy force_discharge() failed")
                return False
        except Exception as e:
            _LOGGER.error(f"Failed to force discharge (Sigenergy): {e}")
            return False
        finally:
            await controller.disconnect()

    from ..const import DOMAIN, SERVICE_FORCE_DISCHARGE

    try:
        service_data: Dict[str, Any] = {"duration": duration}
        power_w = params.get("power_w")
        if power_w is not None:
            service_data["power_w"] = int(power_w)
        await hass.services.async_call(
            DOMAIN,
            SERVICE_FORCE_DISCHARGE,
            service_data,
            blocking=True,
        )
        return True
    except Exception as e:
        _LOGGER.error(f"Failed to activate force discharge: {e}")
        return False


async def _action_force_charge(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    params: Dict[str, Any]
) -> bool:
    """Force battery charge for a specified duration."""
    # Web app stores as "minutes", mobile app as "duration_minutes", HA automations as "duration"
    duration = params.get("duration") or params.get("duration_minutes") or params.get("minutes", 60)

    if _is_sigenergy(config_entry):
        # Sigenergy: Enable Remote EMS + force charge mode
        controller = await _get_sigenergy_controller(config_entry)
        if not controller:
            _LOGGER.error("force_charge: Sigenergy Modbus not configured")
            return False
        try:
            power_kw = params.get("power_w", 10000) / 1000 if params.get("power_w") else 10.0
            result = await controller.force_charge(power_kw)
            if result:
                _LOGGER.info(f"Sigenergy: Force charge activated at {power_kw}kW for {duration} minutes")
                return True
            else:
                _LOGGER.error("Sigenergy force_charge() failed")
                return False
        except Exception as e:
            _LOGGER.error(f"Failed to force charge (Sigenergy): {e}")
            return False
        finally:
            await controller.disconnect()

    from ..const import DOMAIN, SERVICE_FORCE_CHARGE

    try:
        service_data: Dict[str, Any] = {"duration": duration}
        power_w = params.get("power_w")
        if power_w is not None:
            service_data["power_w"] = int(power_w)
        await hass.services.async_call(
            DOMAIN,
            SERVICE_FORCE_CHARGE,
            service_data,
            blocking=True,
        )
        return True
    except Exception as e:
        _LOGGER.error(f"Failed to activate force charge: {e}")
        return False


async def _action_enable_optimizer(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
) -> bool:
    """Enable the LP optimizer via opt_coordinator.set_settings().

    Uses the same code path as the mobile app to avoid triggering a full
    integration reload. The set_settings() method sets _skip_reload before
    updating the config entry, so the options listener skips the reload.
    """
    from ..const import DOMAIN

    try:
        entry_data = hass.data.get(DOMAIN, {}).get(config_entry.entry_id, {})
        opt_coordinator = entry_data.get("optimization_coordinator")
        if opt_coordinator:
            await opt_coordinator.set_settings({"enabled": True})
            _LOGGER.info("Optimizer enabled via automation action (set_settings path)")
        else:
            # Fallback: no coordinator yet, update config directly with skip flag
            entry_data["_skip_reload"] = True
            from ..const import CONF_OPTIMIZATION_ENABLED, CONF_OPTIMIZATION_PROVIDER, OPT_PROVIDER_POWERSYNC
            new_data = dict(config_entry.data)
            new_options = dict(config_entry.options)
            new_data[CONF_OPTIMIZATION_PROVIDER] = OPT_PROVIDER_POWERSYNC
            new_options[CONF_OPTIMIZATION_ENABLED] = True
            hass.config_entries.async_update_entry(config_entry, data=new_data, options=new_options)
            _LOGGER.info("Optimizer enabled via automation action (direct config path)")
        return True
    except Exception as e:
        _LOGGER.error(f"Failed to enable optimizer: {e}")
        return False


async def _action_disable_optimizer(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
) -> bool:
    """Disable the LP optimizer and restore normal battery operation.

    Uses opt_coordinator.set_settings() to avoid triggering a full
    integration reload (same path as the mobile app).
    """
    from ..const import DOMAIN, SERVICE_RESTORE_NORMAL

    try:
        entry_data = hass.data.get(DOMAIN, {}).get(config_entry.entry_id, {})
        opt_coordinator = entry_data.get("optimization_coordinator")
        if opt_coordinator:
            await opt_coordinator.set_settings({"enabled": False})
            _LOGGER.info("Optimizer disabled via automation action (set_settings path)")
        else:
            # Fallback: no coordinator, update config directly with skip flag
            entry_data["_skip_reload"] = True
            from ..const import CONF_OPTIMIZATION_ENABLED, CONF_OPTIMIZATION_PROVIDER, OPT_PROVIDER_NATIVE
            new_data = dict(config_entry.data)
            new_options = dict(config_entry.options)
            new_data[CONF_OPTIMIZATION_PROVIDER] = OPT_PROVIDER_NATIVE
            new_options[CONF_OPTIMIZATION_ENABLED] = False
            hass.config_entries.async_update_entry(config_entry, data=new_data, options=new_options)
            _LOGGER.info("Optimizer disabled via automation action (direct config path)")
        # Restore normal battery operation so the battery isn't stuck in a forced mode
        try:
            await hass.services.async_call(DOMAIN, SERVICE_RESTORE_NORMAL, {}, blocking=True)
        except Exception as restore_err:
            _LOGGER.warning(f"disable_optimizer: restore_normal failed (non-fatal): {restore_err}")
        return True
    except Exception as e:
        _LOGGER.error(f"Failed to disable optimizer: {e}")
        return False


async def _action_curtail_inverter(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    params: Dict[str, Any]
) -> bool:
    """Curtail AC-coupled solar inverter (works for both Tesla and Sigenergy)."""
    from ..const import DOMAIN, SERVICE_CURTAIL_INVERTER

    # Service expects "mode": "load_following" or "shutdown"
    # Accept both "mode" and "curtailment_mode" for flexibility
    # Default to "load_following" (limit to home load / zero-export)
    mode = params.get("mode") or params.get("curtailment_mode", "load_following")

    try:
        await hass.services.async_call(
            DOMAIN,
            SERVICE_CURTAIL_INVERTER,
            {"mode": mode},
            blocking=True,
        )
        return True
    except Exception as e:
        _LOGGER.error(f"Failed to curtail inverter: {e}")
        return False


async def _action_send_notification(
    hass: HomeAssistant,
    params: Dict[str, Any]
) -> bool:
    """Send push notification via Expo Push API to the PowerSync mobile app."""
    message = params.get("message", "Automation triggered")
    title = params.get("title", "PowerSync")

    try:
        # Send push notification to PowerSync app via Expo Push API
        await _send_expo_push(hass, title, message)
        return True
    except Exception as e:
        _LOGGER.error(f"Failed to send notification: {e}")
        return False


async def _send_expo_push(hass: HomeAssistant, title: str, message: str) -> None:
    """Send push notification via Expo Push API."""
    from ..const import DOMAIN
    import aiohttp

    _LOGGER.info(f"📱 PUSH DEBUG: Attempting to send notification - Title: '{title}', Message: '{message}'")

    # Get registered push tokens
    push_tokens = hass.data.get(DOMAIN, {}).get("push_tokens", {})
    if not push_tokens:
        _LOGGER.warning("📱 PUSH DEBUG: No push tokens registered in hass.data[DOMAIN]['push_tokens'], skipping notification")
        return

    _LOGGER.info(f"📱 PUSH DEBUG: Found {len(push_tokens)} registered push token(s)")

    # Prepare messages for Expo Push API
    messages = []
    skipped_tokens = 0
    for device_id, token_data in push_tokens.items():
        token = token_data.get("token")
        platform = token_data.get("platform", "unknown")
        device = token_data.get("device_name", "unknown")
        registered_at = token_data.get("registered_at", "unknown")
        _LOGGER.info(f"📱 PUSH DEBUG: Token entry - device_id={device_id}, platform={platform}, device={device}, registered_at={registered_at}")
        _LOGGER.info(f"📱 PUSH DEBUG: Token value = {token[:50] if token else 'None'}...")

        if token and token.startswith("ExponentPushToken"):
            messages.append({
                "to": token,
                "title": title,
                "body": message,
                "sound": "default",
                "priority": "high",
                "channelId": "default",  # Android channel ID
            })
            _LOGGER.info(f"📱 PUSH DEBUG: Including token for {device} ({platform})")
        else:
            skipped_tokens += 1
            _LOGGER.warning(f"📱 PUSH DEBUG: Skipping non-Expo token for {device} ({platform}): {token[:30] if token else 'None'}...")

    if not messages:
        _LOGGER.warning(f"📱 PUSH DEBUG: No valid Expo push tokens found (skipped {skipped_tokens} invalid tokens)")
        return

    _LOGGER.info(f"📱 PUSH DEBUG: Sending {len(messages)} message(s) to Expo Push API")

    # Send to Expo Push API
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://exp.host/--/api/v2/push/send",
                json=messages,
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "Accept-Encoding": "gzip, deflate",
                },
            ) as response:
                response_text = await response.text()
                _LOGGER.info(f"📱 PUSH DEBUG: Expo API response status: {response.status}")
                _LOGGER.info(f"📱 PUSH DEBUG: Expo API response body: {response_text}")

                if response.status == 200:
                    try:
                        result = await response.json()
                        # Check individual ticket status
                        data = result.get("data", [])
                        for i, ticket in enumerate(data):
                            status = ticket.get("status")
                            ticket_id = ticket.get("id", "no-id")
                            if status == "ok":
                                _LOGGER.info(f"📱 PUSH DEBUG: Ticket {i+1}/{len(data)} - SUCCESS (id={ticket_id})")
                            else:
                                # Error in ticket
                                error_msg = ticket.get("message", "unknown error")
                                error_details = ticket.get("details", {})
                                _LOGGER.error(f"📱 PUSH DEBUG: Ticket {i+1}/{len(data)} - FAILED: {error_msg}")
                                _LOGGER.error(f"📱 PUSH DEBUG: Error details: {error_details}")
                                # Common errors:
                                # - DeviceNotRegistered: FCM token is invalid/expired
                                # - MessageTooBig: Payload too large
                                # - MessageRateExceeded: Too many messages
                                # - MismatchSenderId: FCM sender ID mismatch
                                # - InvalidCredentials: FCM credentials not configured in Expo
                                if "InvalidCredentials" in str(error_details) or "InvalidCredentials" in error_msg:
                                    _LOGGER.error("📱 PUSH DEBUG: ⚠️ FCM credentials may not be configured in Expo! "
                                                "Upload google-services.json to Expo for Android push notifications.")
                                if "DeviceNotRegistered" in str(error_details) or "DeviceNotRegistered" in error_msg:
                                    _LOGGER.error("📱 PUSH DEBUG: ⚠️ Device token is no longer valid. "
                                                "App may need to re-register for push notifications.")
                    except Exception as parse_err:
                        _LOGGER.error(f"📱 PUSH DEBUG: Failed to parse Expo response: {parse_err}")
                else:
                    _LOGGER.error(f"📱 PUSH DEBUG: Expo Push API HTTP error: {response.status} - {response_text}")
    except Exception as e:
        _LOGGER.error(f"📱 PUSH DEBUG: Exception sending Expo push notification: {e}", exc_info=True)


async def _action_set_grid_export(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    params: Dict[str, Any]
) -> bool:
    """Set grid export rule (Tesla only)."""
    from ..const import CONF_BATTERY_SYSTEM, BATTERY_SYSTEM_TESLA, DOMAIN, SERVICE_SET_GRID_EXPORT
    if config_entry.data.get(CONF_BATTERY_SYSTEM) != BATTERY_SYSTEM_TESLA:
        _LOGGER.debug("set_grid_export not supported for non-Tesla systems")
        return None

    # Accept both "rule" and "grid_export_rule" for flexibility
    rule = params.get("rule") or params.get("grid_export_rule")
    if not rule:
        _LOGGER.error("set_grid_export: missing rule parameter")
        return False

    valid_rules = ["never", "pv_only", "battery_ok"]
    if rule not in valid_rules:
        _LOGGER.error(f"set_grid_export: invalid rule '{rule}'")
        return False

    try:
        await hass.services.async_call(
            DOMAIN,
            SERVICE_SET_GRID_EXPORT,
            {"rule": rule},
            blocking=True,
        )
        return True
    except Exception as e:
        _LOGGER.error(f"Failed to set grid export: {e}")
        return False


async def _action_set_grid_charging(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    params: Dict[str, Any]
) -> bool:
    """Enable or disable grid charging (Tesla only)."""
    if _is_sigenergy(config_entry):
        _LOGGER.warning("set_grid_charging not supported for Sigenergy")
        return False

    from ..const import DOMAIN, SERVICE_SET_GRID_CHARGING

    enabled = params.get("enabled", True)

    try:
        await hass.services.async_call(
            DOMAIN,
            SERVICE_SET_GRID_CHARGING,
            {"enabled": enabled},
            blocking=True,
        )
        return True
    except Exception as e:
        _LOGGER.error(f"Failed to set grid charging: {e}")
        return False


async def _action_set_storm_watch(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    params: Dict[str, Any],
) -> bool:
    """Enable or disable Tesla Storm Watch via the set_storm_watch service."""
    from ..const import DOMAIN

    enabled = bool(params.get("enabled", True))
    try:
        await hass.services.async_call(
            DOMAIN, "set_storm_watch", {"enabled": enabled}, blocking=True,
        )
        return True
    except Exception as e:
        _LOGGER.error(f"Failed to set Storm Watch: {e}")
        return False


async def _action_set_off_grid_ev_reserve(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    params: Dict[str, Any],
) -> bool:
    """Set off-grid vehicle charging reserve percent."""
    from ..const import DOMAIN

    percent = params.get("off_grid_ev_reserve_percent")
    if percent is None:
        percent = params.get("percent")
    if percent is None:
        _LOGGER.error("set_off_grid_ev_reserve: missing percent parameter")
        return False
    try:
        percent = int(percent)
    except (ValueError, TypeError):
        _LOGGER.error("set_off_grid_ev_reserve: invalid percent %r", percent)
        return False
    percent = max(0, min(100, percent))

    try:
        await hass.services.async_call(
            DOMAIN, "set_off_grid_ev_reserve", {"percent": percent}, blocking=True,
        )
        return True
    except Exception as e:
        _LOGGER.error(f"Failed to set off-grid EV reserve: {e}")
        return False


async def _action_set_vpp_enrollment(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    params: Dict[str, Any],
) -> bool:
    """Enroll or unenroll the site in a Tesla VPP / grid-services program."""
    from ..const import DOMAIN

    program_id = params.get("vpp_program_id") or params.get("program_id")
    if not program_id:
        _LOGGER.error("set_vpp_enrollment: missing program_id parameter")
        return False
    # Automation UI reuses the `enabled` key for clarity alongside storm_watch/grid_charging
    enrolled = params.get("enrolled")
    if enrolled is None:
        enrolled = params.get("enabled", True)
    enrolled = bool(enrolled)

    try:
        await hass.services.async_call(
            DOMAIN,
            "set_vpp_enrollment",
            {"program_id": str(program_id), "enrolled": enrolled},
            blocking=True,
        )
        return True
    except Exception as e:
        _LOGGER.error(f"Failed to set VPP enrollment: {e}")
        return False


async def _action_set_amber_forecast_type(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    params: Dict[str, Any]
) -> bool:
    """Switch the Amber forecast type (predicted/low/high).

    Updates the config entry options and triggers a TOU sync so the new
    forecast type takes effect immediately.
    """
    from ..const import DOMAIN, CONF_AMBER_FORECAST_TYPE

    forecast_type = params.get("forecast_type", "predicted")
    if forecast_type not in ("predicted", "low", "high"):
        _LOGGER.error("Invalid Amber forecast type: %s (must be predicted, low, or high)", forecast_type)
        return False

    try:
        new_options = dict(config_entry.options)
        new_options[CONF_AMBER_FORECAST_TYPE] = forecast_type
        hass.config_entries.async_update_entry(config_entry, options=new_options)
        _LOGGER.info("Amber forecast type changed to '%s' via automation", forecast_type)

        # Trigger a TOU sync so the new forecast type takes effect immediately
        try:
            await hass.services.async_call(DOMAIN, "sync_tou", {}, blocking=True)
        except Exception:
            pass  # Non-critical — next scheduled sync will pick it up

        return True
    except Exception as e:
        _LOGGER.error("Failed to set Amber forecast type: %s", e)
        return False


async def _action_restore_normal(
    hass: HomeAssistant,
    config_entry: ConfigEntry
) -> bool:
    """Restore normal battery operation (cancel force charge/discharge)."""
    if _is_sigenergy(config_entry):
        # Sigenergy: Disable Remote EMS to return to native EMS
        controller = await _get_sigenergy_controller(config_entry)
        if not controller:
            _LOGGER.error("restore_normal: Sigenergy Modbus not configured")
            return False
        try:
            result = await controller.restore_normal()
            if result:
                _LOGGER.info("Sigenergy: Restored normal operation (Remote EMS disabled)")
                return True
            else:
                _LOGGER.error("Sigenergy restore_normal() failed")
                return False
        except Exception as e:
            _LOGGER.error(f"Failed to restore normal (Sigenergy): {e}")
            return False
        finally:
            await controller.disconnect()

    from ..const import DOMAIN, SERVICE_RESTORE_NORMAL

    try:
        await hass.services.async_call(
            DOMAIN,
            SERVICE_RESTORE_NORMAL,
            {},
            blocking=True,
        )
        return True
    except Exception as e:
        _LOGGER.error(f"Failed to restore normal: {e}")
        return False


async def _action_set_charge_rate(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    params: Dict[str, Any]
) -> bool:
    """Set charge rate limit (Sigenergy only)."""
    if not _is_sigenergy(config_entry):
        _LOGGER.warning("set_charge_rate only supported for Sigenergy")
        return False

    # Accept both "rate" and "rate_limit_kw" for flexibility
    rate_kw = params.get("rate") or params.get("rate_limit_kw")
    if rate_kw is None:
        _LOGGER.error("set_charge_rate: missing rate parameter")
        return False

    controller = await _get_sigenergy_controller(config_entry)
    if not controller:
        _LOGGER.error("set_charge_rate: Sigenergy Modbus not configured")
        return False

    try:
        # Clamp to valid range (0-10 kW typical)
        rate_kw = max(0, min(10, float(rate_kw)))
        result = await controller.set_charge_rate_limit(rate_kw)
        if result:
            _LOGGER.info(f"Set charge rate limit to {rate_kw} kW")
        return result
    except Exception as e:
        _LOGGER.error(f"Failed to set charge rate: {e}")
        return False
    finally:
        await controller.disconnect()


async def _action_set_discharge_rate(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    params: Dict[str, Any]
) -> bool:
    """Set discharge rate limit (Sigenergy only)."""
    if not _is_sigenergy(config_entry):
        _LOGGER.warning("set_discharge_rate only supported for Sigenergy")
        return False

    # Accept both "rate" and "rate_limit_kw" for flexibility
    rate_kw = params.get("rate") or params.get("rate_limit_kw")
    if rate_kw is None:
        _LOGGER.error("set_discharge_rate: missing rate parameter")
        return False

    controller = await _get_sigenergy_controller(config_entry)
    if not controller:
        _LOGGER.error("set_discharge_rate: Sigenergy Modbus not configured")
        return False

    try:
        # Clamp to valid range (0-10 kW typical)
        rate_kw = max(0, min(10, float(rate_kw)))
        result = await controller.set_discharge_rate_limit(rate_kw)
        if result:
            _LOGGER.info(f"Set discharge rate limit to {rate_kw} kW")
        return result
    except Exception as e:
        _LOGGER.error(f"Failed to set discharge rate: {e}")
        return False
    finally:
        await controller.disconnect()


async def _action_set_export_limit(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    params: Dict[str, Any]
) -> bool:
    """Set export power limit (Sigenergy only)."""
    if not _is_sigenergy(config_entry):
        _LOGGER.warning("set_export_limit only supported for Sigenergy")
        return False

    # Accept both "limit" and "export_limit_kw" for flexibility
    # None means unlimited
    limit_kw = params.get("limit") or params.get("export_limit_kw")

    controller = await _get_sigenergy_controller(config_entry)
    if not controller:
        _LOGGER.error("set_export_limit: Sigenergy Modbus not configured")
        return False

    try:
        if limit_kw is None:
            # Unlimited export
            result = await controller.restore_export_limit()
            _LOGGER.info("Restored unlimited export")
        else:
            # Clamp to valid range (0-10 kW typical)
            limit_kw = max(0, min(10, float(limit_kw)))
            result = await controller.set_export_limit(limit_kw)
            _LOGGER.info(f"Set export limit to {limit_kw} kW")
        return result
    except Exception as e:
        _LOGGER.error(f"Failed to set export limit: {e}")
        return False
    finally:
        await controller.disconnect()


async def _action_restore_inverter(
    hass: HomeAssistant,
    config_entry: ConfigEntry
) -> bool:
    """Restore inverter to normal operation."""
    from ..const import DOMAIN, SERVICE_RESTORE_INVERTER

    try:
        await hass.services.async_call(
            DOMAIN,
            SERVICE_RESTORE_INVERTER,
            {},
            blocking=True,
        )
        return True
    except Exception as e:
        _LOGGER.error(f"Failed to restore inverter: {e}")
        return False


# =============================================================================
# EV Charging Actions (Tesla Fleet/Teslemetry or Tesla BLE via Home Assistant)
# =============================================================================


def _pre_charge_wake_entity(params: Dict[str, Any]) -> str:
    """Return the configured pre-charge wake entity, if any."""
    for key in PRE_CHARGE_WAKE_ENTITY_KEYS:
        value = str(params.get(key) or "").strip()
        if value:
            return value
    return ""


def _has_pre_charge_wake(params: Dict[str, Any]) -> bool:
    """Return whether a charger start should run a wake sequence first."""
    return bool(_pre_charge_wake_entity(params))


def _pre_charge_wake_duration(params: Dict[str, Any]) -> int:
    """Return the wake hold duration in seconds, clamped to a safe range."""
    raw_value = None
    for key in PRE_CHARGE_WAKE_DURATION_KEYS:
        if params.get(key) is not None:
            raw_value = params.get(key)
            break

    if raw_value is None:
        raw_value = DEFAULT_PRE_CHARGE_WAKE_DURATION_SECONDS

    try:
        seconds = int(float(raw_value))
    except (TypeError, ValueError):
        seconds = DEFAULT_PRE_CHARGE_WAKE_DURATION_SECONDS

    return max(0, min(MAX_PRE_CHARGE_WAKE_DURATION_SECONDS, seconds))


def _split_pre_charge_wake_service(
    entity_domain: str,
    configured: Any,
    default_domain: str,
    default_service: str,
) -> tuple[str, str]:
    """Resolve a wake service override into HA service domain/name."""
    value = str(configured or "").strip()
    if not value:
        return default_domain, default_service

    if "." in value:
        service_domain, service = value.split(".", 1)
    else:
        service_domain, service = entity_domain, value

    return service_domain.strip(), service.strip()


def _pre_charge_wake_service_data(params: Dict[str, Any], key: str) -> dict:
    """Return optional extra service data for the wake service."""
    value = params.get(key)
    return dict(value) if isinstance(value, dict) else {}


def _default_pre_charge_wake_services(
    entity_domain: str,
    params: Dict[str, Any],
) -> tuple[tuple[str, str], tuple[str, str] | None]:
    """Return default on/off service calls for a wake entity."""
    if entity_domain == "button":
        on_default = ("button", "press")
        off_default = None
    elif entity_domain in ("script", "scene"):
        on_default = (entity_domain, "turn_on")
        off_default = None
    else:
        on_default = (entity_domain, "turn_on")
        off_default = (entity_domain, "turn_off")

    on_service = _split_pre_charge_wake_service(
        entity_domain,
        params.get("pre_charge_wake_on_service"),
        on_default[0],
        on_default[1],
    )

    off_override = params.get("pre_charge_wake_off_service")
    if str(off_override or "").strip().lower() in ("none", "skip", "disabled"):
        off_service = None
    elif off_default is None and not off_override:
        off_service = None
    else:
        fallback = off_default or (entity_domain, "turn_off")
        off_service = _split_pre_charge_wake_service(
            entity_domain,
            off_override,
            fallback[0],
            fallback[1],
        )

    return on_service, off_service


async def _run_pre_charge_wake_sequence(
    hass: HomeAssistant,
    params: Dict[str, Any],
    charger_type: str,
) -> bool:
    """Run an optional EV wake action before enabling a non-Tesla charger."""
    entity_id = _pre_charge_wake_entity(params)
    if not entity_id:
        return True

    if params.get(PRE_CHARGE_WAKE_DONE_KEY) == entity_id:
        return True

    if "." not in entity_id:
        _LOGGER.error("Pre-charge wake entity is invalid: %s", entity_id)
        return False

    state = hass.states.get(entity_id)
    if state is None:
        _LOGGER.error("Pre-charge wake entity not found: %s", entity_id)
        return False

    entity_domain = entity_id.split(".", 1)[0]
    on_service, off_service = _default_pre_charge_wake_services(entity_domain, params)
    duration_seconds = _pre_charge_wake_duration(params)

    try:
        on_data = {"entity_id": entity_id}
        on_data.update(_pre_charge_wake_service_data(params, "pre_charge_wake_on_service_data"))
        await hass.services.async_call(
            on_service[0],
            on_service[1],
            on_data,
            blocking=True,
        )
        _LOGGER.info(
            "Pre-charge wake: called %s.%s for %s before %s start",
            on_service[0],
            on_service[1],
            entity_id,
            charger_type,
        )

        if duration_seconds > 0:
            await asyncio.sleep(duration_seconds)

        if off_service is not None:
            off_data = {"entity_id": entity_id}
            off_data.update(_pre_charge_wake_service_data(params, "pre_charge_wake_off_service_data"))
            await hass.services.async_call(
                off_service[0],
                off_service[1],
                off_data,
                blocking=True,
            )
            _LOGGER.info(
                "Pre-charge wake: called %s.%s for %s",
                off_service[0],
                off_service[1],
                entity_id,
            )

        params[PRE_CHARGE_WAKE_DONE_KEY] = entity_id
        return True
    except Exception as err:
        _LOGGER.error("Pre-charge wake failed for %s: %s", entity_id, err)
        return False


def _ocpp_charger_ready_for_wake(
    hass: HomeAssistant,
    charger_id: str,
) -> tuple[bool, str | None]:
    """Return whether an OCPP connector appears to have a vehicle plugged in."""
    connector_state = hass.states.get(f"sensor.{charger_id}_status_connector")
    if not connector_state or connector_state.state in ("unavailable", "unknown"):
        return True, None

    if connector_state.state.lower() in ("available", "disconnected"):
        return False, "Vehicle is not plugged in"

    return True, None


def _ocpp_charger_base_and_connector(charger_id: str) -> tuple[str, int | None]:
    """Return HACS OCPP base charge-point id and optional connector id."""
    from .ocpp_status import split_hacs_ocpp_connector_prefix

    return split_hacs_ocpp_connector_prefix(str(charger_id))


async def _call_hacs_ocpp_charger_state(
    hass: HomeAssistant,
    charger_id: str,
    service_name: str,
    state: bool,
) -> Optional[bool]:
    """Call HACS OCPP CentralSystem directly when available.

    The HA switch service path is optimistic from PowerSync's perspective: it
    does not return the charge point's RemoteStart/RemoteStop result. The
    CentralSystem API does, so prefer it when present.
    """
    base_id, connector_id = _ocpp_charger_base_and_connector(charger_id)
    target_connector = connector_id or 1
    found_api = False

    for central_system in (hass.data.get("ocpp") or {}).values():
        if not hasattr(central_system, "set_charger_state"):
            continue
        found_api = True
        try:
            success = await central_system.set_charger_state(
                base_id,
                service_name,
                state,
                connector_id=target_connector,
            )
            if success:
                return True
        except Exception as err:
            _LOGGER.error(
                "OCPP charger %s %s failed through HACS OCPP API: %s",
                charger_id,
                service_name,
                err,
            )

    if found_api:
        return False
    return None


async def _start_ocpp_charging(hass: HomeAssistant, charger_id: str) -> bool:
    """Start charging on an OCPP charger via its HA switch entity.

    When the connector is in "Finishing" (the post-stop state some chargers
    sit in until the cable is unplugged), a plain turn_on can be a no-op:
    HACS lbbrhzn/ocpp's charge_control switch caches its is_on state and the
    underlying RemoteStartTransaction never gets sent. Toggle off→on first
    in that case to force a fresh RemoteStartTransaction.
    """
    entity_id = f"switch.{charger_id}_charge_control"
    state = hass.states.get(entity_id)
    if not state:
        _LOGGER.error("OCPP start: entity %s not found", entity_id)
        return False

    connector_state = hass.states.get(f"sensor.{charger_id}_status_connector")
    needs_reset = (
        connector_state is not None
        and connector_state.state.lower() == "finishing"
    )
    if str(state.state).lower() == "on" and not needs_reset:
        _LOGGER.debug(
            "OCPP charger %s: %s already on; skipping duplicate start",
            charger_id,
            entity_id,
        )
        return True

    direct_result = await _call_hacs_ocpp_charger_state(
        hass,
        str(charger_id),
        "service_charge_start",
        True,
    )
    if direct_result is True:
        _LOGGER.info("OCPP charger %s: start charging via HACS OCPP API", charger_id)
        return True
    if direct_result is False:
        _LOGGER.warning("OCPP charger %s rejected remote start", charger_id)
        return False

    try:
        if needs_reset:
            try:
                await hass.services.async_call(
                    "switch", "turn_off", {"entity_id": entity_id}, blocking=True
                )
                await asyncio.sleep(1)
            except Exception as off_err:
                _LOGGER.debug("OCPP pre-start reset turn_off failed for %s: %s", charger_id, off_err)
        await hass.services.async_call("switch", "turn_on", {"entity_id": entity_id}, blocking=True)
        if needs_reset:
            _LOGGER.info(
                "OCPP charger %s: start charging via %s (reset from Finishing)",
                charger_id, entity_id,
            )
        else:
            _LOGGER.info("OCPP charger %s: start charging via %s", charger_id, entity_id)
        return True
    except Exception as e:
        _LOGGER.error("OCPP start charging failed for %s: %s", charger_id, e)
        return False


async def _stop_ocpp_charging(hass: HomeAssistant, charger_id: str) -> bool:
    """Stop charging on an OCPP charger via its HA switch entity."""
    entity_id = f"switch.{charger_id}_charge_control"
    state = hass.states.get(entity_id)
    if not state:
        _LOGGER.error("OCPP stop: entity %s not found", entity_id)
        return False
    direct_result = await _call_hacs_ocpp_charger_state(
        hass,
        str(charger_id),
        "service_charge_stop",
        False,
    )
    if direct_result is True:
        _LOGGER.info("OCPP charger %s: stop charging via HACS OCPP API", charger_id)
        return True
    if direct_result is False:
        _LOGGER.warning("OCPP charger %s rejected remote stop", charger_id)
        return False

    try:
        await hass.services.async_call("switch", "turn_off", {"entity_id": entity_id}, blocking=True)
        _LOGGER.info("OCPP charger %s: stop charging via %s", charger_id, entity_id)
        return True
    except Exception as e:
        _LOGGER.error("OCPP stop charging failed for %s: %s", charger_id, e)
        return False


def _find_ocpp_current_limit_entity(hass: HomeAssistant, charger_id: str) -> Optional[str]:
    """Find a HACS OCPP number entity that can set a charger's current limit."""
    charger_key = str(charger_id).lower()
    current_keys = (
        "maximum_current",
        "max_current",
        "current_limit",
        "charging_current",
        "charge_current",
        "current",
        "amps",
    )

    def _matches(entity_id: str) -> bool:
        entity_lower = entity_id.lower()
        if not entity_lower.startswith("number."):
            return False
        object_id = entity_lower.split(".", 1)[1]
        if not object_id.startswith(f"{charger_key}_"):
            return False
        return any(key in object_id for key in current_keys)

    candidates: List[str] = []

    try:
        entity_reg = er.async_get(hass)
        for entity in entity_reg.entities.values():
            if getattr(entity, "platform", None) != "ocpp":
                continue
            if _matches(entity.entity_id):
                candidates.append(entity.entity_id)
    except Exception:
        pass

    if not candidates:
        try:
            for entity_id in hass.states.async_entity_ids("number"):
                if _matches(entity_id):
                    candidates.append(entity_id)
        except Exception:
            pass

    if not candidates:
        return None

    def _priority(entity_id: str) -> int:
        object_id = entity_id.lower().split(".", 1)[1]
        for idx, key in enumerate(current_keys):
            if object_id.endswith(key) or f"_{key}_" in object_id:
                return idx
        return len(current_keys)

    return sorted(set(candidates), key=_priority)[0]


def _generic_charger_ready_for_start(
    hass: HomeAssistant,
    params: Dict[str, Any],
) -> tuple[bool, str | None]:
    """Return whether a generic charger appears to have a vehicle connected."""
    status_entity = params.get("charger_status_entity")
    if not status_entity:
        return True, None

    state = hass.states.get(status_entity)
    if not state or state.state in ("unavailable", "unknown"):
        return True, None

    status_lower = state.state.lower()
    if status_lower not in ("available", "disconnected"):
        return True, None

    car_present_states = {
        "preparing",
        "charging",
        "suspendedev",
        "suspendedevse",
        "suspended_ev",
        "suspended_evse",
        "finishing",
    }
    car_on_connector = any(
        s.state.lower() in car_present_states
        for s in hass.states.async_all()
        if s.entity_id.startswith("sensor.")
        and s.entity_id.endswith("_status_connector")
        and s.state not in ("unavailable", "unknown")
    )
    if car_on_connector:
        _LOGGER.debug(
            "Generic charger: %s=%s but connector shows car present",
            status_entity,
            state.state,
        )
        return True, None

    return False, "Vehicle is not plugged in"


def _get_zaptec_standalone(
    hass: HomeAssistant,
    config_entry: ConfigEntry | None,
) -> Optional[Dict[str, Any]]:
    """Return the configured Zaptec standalone client and cached charger state."""
    from ..const import (
        CONF_ZAPTEC_CHARGER_ID,
        CONF_ZAPTEC_INSTALLATION_ID_CLOUD,
        CONF_ZAPTEC_STANDALONE_ENABLED,
        CONF_ZAPTEC_USERNAME,
    )

    candidates = [config_entry] if config_entry is not None else []
    try:
        for entry in hass.config_entries.async_entries(DOMAIN):
            if entry is not config_entry:
                candidates.append(entry)
    except Exception:
        pass

    for entry in candidates:
        opts = {**getattr(entry, "data", {}), **getattr(entry, "options", {})}
        if not (
            opts.get(CONF_ZAPTEC_STANDALONE_ENABLED)
            and opts.get(CONF_ZAPTEC_USERNAME)
        ):
            continue

        entry_data = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
        client = entry_data.get("zaptec_client")
        charger_id = opts.get(CONF_ZAPTEC_CHARGER_ID, "")
        if not client or not charger_id:
            continue

        return {
            "client": client,
            "charger_id": charger_id,
            "installation_id": opts.get(CONF_ZAPTEC_INSTALLATION_ID_CLOUD, ""),
            "cached_state": entry_data.get("zaptec_cached_state", {}),
        }

    return None


def _zaptec_state_value(cached_state: Dict[str, Any], key: str, default: Any = None) -> Any:
    """Read Zaptec cached state defensively; tests use simple dict stubs."""
    value = cached_state.get(key, default)
    return default if value is None else value


async def _set_zaptec_charging_amps(
    hass: HomeAssistant,
    config_entry: ConfigEntry | None,
    amps: int,
) -> bool:
    """Set Zaptec standalone installation current."""
    zaptec = _get_zaptec_standalone(hass, config_entry)
    if not zaptec:
        _LOGGER.error("Zaptec set amps: standalone charger is not configured")
        return False

    installation_id = zaptec.get("installation_id")
    if not installation_id:
        _LOGGER.error("Zaptec set amps: no installation ID configured")
        return False

    target_amps = max(0, min(80, int(amps)))
    try:
        await zaptec["client"].set_installation_current(installation_id, target_amps)
        _LOGGER.info("Zaptec charger set to %dA", target_amps)
        return True
    except Exception as e:
        _LOGGER.error("Zaptec set amps failed: %s", e)
        return False


async def _start_zaptec_charging(
    hass: HomeAssistant,
    config_entry: ConfigEntry | None,
    amps: Optional[int] = None,
    params: Dict[str, Any] | None = None,
) -> bool:
    """Start Zaptec standalone charging with state-aware command selection."""
    zaptec = _get_zaptec_standalone(hass, config_entry)
    if not zaptec:
        _LOGGER.error("Zaptec start: standalone charger is not configured")
        return False

    cached_state = zaptec.get("cached_state") or {}
    charger_mode = str(_zaptec_state_value(cached_state, "charger_operation_mode", "")).lower()
    try:
        power_w = float(_zaptec_state_value(cached_state, "total_charge_power_w", 0) or 0)
    except (TypeError, ValueError):
        power_w = 0
    cable_locked = bool(_zaptec_state_value(cached_state, "cable_locked", False))

    if charger_mode not in ("connected_waiting", "charging") and power_w <= 50 and not cable_locked:
        _LOGGER.warning("Zaptec start: vehicle is not plugged in")
        return False

    target_amps = int(amps) if amps is not None else None

    if charger_mode == "charging":
        if target_amps is not None:
            await _set_zaptec_charging_amps(hass, config_entry, target_amps)
        _LOGGER.info("Zaptec charger is already charging")
        return True

    if params is not None and not await _run_pre_charge_wake_sequence(hass, params, "zaptec"):
        return False

    if charger_mode == "connected_waiting":
        if not await _set_zaptec_charging_amps(hass, config_entry, target_amps or 16):
            _LOGGER.warning("Zaptec start: waiting charger could not be assigned current")
            return False
        _LOGGER.info("Zaptec charger waiting: set installation current instead of resume")
        return True

    try:
        if target_amps is not None and zaptec.get("installation_id"):
            await _set_zaptec_charging_amps(hass, config_entry, target_amps)
        await zaptec["client"].resume_charging(zaptec["charger_id"])
        _LOGGER.info("Zaptec charger resumed via Cloud API")
        return True
    except Exception as e:
        _LOGGER.error("Zaptec start charging failed: %s", e)
        return False


async def _stop_zaptec_charging(
    hass: HomeAssistant,
    config_entry: ConfigEntry | None,
) -> bool:
    """Stop Zaptec standalone charging, treating already-idle states as success."""
    zaptec = _get_zaptec_standalone(hass, config_entry)
    if not zaptec:
        _LOGGER.error("Zaptec stop: standalone charger is not configured")
        return False

    cached_state = zaptec.get("cached_state") or {}
    charger_mode = str(_zaptec_state_value(cached_state, "charger_operation_mode", "")).lower()
    try:
        power_w = float(_zaptec_state_value(cached_state, "total_charge_power_w", 0) or 0)
    except (TypeError, ValueError):
        power_w = 0

    if charger_mode in ("connected_waiting", "disconnected", "") and power_w <= 50:
        _LOGGER.info(
            "Zaptec charger already in %s mode, skipping stop command",
            charger_mode or "unknown",
        )
        return True

    try:
        await zaptec["client"].stop_charging(zaptec["charger_id"])
        _LOGGER.info("Zaptec charger stopped via Cloud API")
        return True
    except Exception as e:
        _LOGGER.error("Zaptec stop charging failed: %s", e)
        return False


async def _action_start_ev_charging(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    params: Dict[str, Any],
    context: Optional[Dict[str, Any]] = None
) -> bool:
    """
    Start EV charging via Tesla, OCPP, generic, or Zaptec charger.

    Dispatches based on charger_type parameter. Tesla uses BLE/Fleet API,
    OCPP uses HA switch entities, generic uses configured switch entity,
    Zaptec uses the standalone Cloud client.

    Parameters:
        stop_outside_window: If True, schedule charging to stop at end of time window
    """
    charger_type = params.get("charger_type", "tesla")

    # OCPP charger: use HA switch entity
    if charger_type == "ocpp":
        ocpp_charger_id = params.get("ocpp_charger_id")
        if not ocpp_charger_id:
            _LOGGER.error("OCPP start: no charger ID configured")
            return False
        if _has_pre_charge_wake(params):
            entity_id = f"switch.{ocpp_charger_id}_charge_control"
            if not hass.states.get(entity_id):
                _LOGGER.error("OCPP start: entity %s not found", entity_id)
                return False
            ready, block_reason = _ocpp_charger_ready_for_wake(hass, str(ocpp_charger_id))
            if not ready:
                _LOGGER.warning("OCPP pre-charge wake blocked: %s", block_reason)
                return False
            if not await _run_pre_charge_wake_sequence(hass, params, "ocpp"):
                return False
        return await _start_ocpp_charging(hass, ocpp_charger_id)

    # Zaptec standalone charger: use configured Cloud API client
    if charger_type == "zaptec":
        amps = params.get("amps")
        if amps is None:
            amps = params.get("charging_amps")
        return await _start_zaptec_charging(
            hass,
            config_entry,
            int(amps) if amps is not None else None,
            params,
        )

    # Sigenergy EVAC/EVDC direct Modbus charger.
    if charger_type == "sigenergy":
        amps = params.get("amps")
        if amps is None:
            amps = params.get("charging_amps")
        return await _start_sigenergy_charger(
            config_entry,
            params,
            int(amps) if amps is not None else None,
        )

    # Generic charger: use configured switch entity
    if charger_type == "generic":
        ready, block_reason = _generic_charger_ready_for_start(hass, params)
        if not ready:
            _LOGGER.warning("Generic charger start blocked: %s", block_reason)
            return False

        switch_entity = (params.get("charger_switch_entity") or "").strip()
        if not switch_entity:
            _LOGGER.error("Generic charger start: no switch entity configured")
            return False
        if "." not in switch_entity:
            _LOGGER.error("Generic charger start: invalid switch entity %s", switch_entity)
            return False
        if not await _run_pre_charge_wake_sequence(hass, params, "generic"):
            return False
        try:
            await hass.services.async_call("switch", "turn_on", {"entity_id": switch_entity}, blocking=True)
            _LOGGER.info("Generic charger started via %s", switch_entity)
            return True
        except Exception as e:
            _LOGGER.error("Generic charger start failed: %s", e)
            return False

    # HA-native charger integrations: Wallbox, Easee, native Zaptec, ev_charger,
    # and similar entities that expose service-domain start/stop methods.
    if _is_ha_native_charger_type(charger_type):
        amps = params.get("amps")
        if amps is None:
            amps = params.get("charging_amps")
        if amps is not None:
            amps_ok = await _set_ha_native_charging_amps(hass, params, int(amps))
            if not amps_ok:
                _LOGGER.debug(
                    "HA-native charger start will continue although current limit update failed"
                )
        if not await _run_pre_charge_wake_sequence(hass, params, str(charger_type)):
            return False
        return await _start_ha_native_charger(hass, params)

    # Tesla charger: existing logic below
    ev_config = _get_ev_config(config_entry)
    ev_provider = ev_config["ev_provider"]
    vehicle_vin = params.get("vehicle_vin")
    ble_prefix = _resolve_ble_prefix_for_vehicle(hass, config_entry, vehicle_vin)
    stop_outside_window = params.get("stop_outside_window", False)

    # Get time window from context
    time_window_start = context.get("time_window_start") if context else None
    time_window_end = context.get("time_window_end") if context else None
    timezone = context.get("timezone", "UTC") if context else "UTC"

    charging_started = False

    # Try Teslemetry Bluetooth first if configured
    if ev_provider in (EV_PROVIDER_TESLEMETRY_BT, EV_PROVIDER_BOTH):
        tbt_prefix = _resolve_teslemetry_bt_prefix(hass)
        if _is_teslemetry_bt_available(hass, tbt_prefix):
            result = await _start_ev_charging_teslemetry_bt(hass, tbt_prefix)
            if result:
                charging_started = True
            elif ev_provider == EV_PROVIDER_TESLEMETRY_BT:
                return False

    # Try ESPHome BLE if configured
    if not charging_started and ev_provider in (EV_PROVIDER_TESLA_BLE, EV_PROVIDER_BOTH):
        if _is_ble_available(hass, ble_prefix):
            result = await _start_ev_charging_ble(hass, ble_prefix)
            if result:
                charging_started = True
            elif ev_provider == EV_PROVIDER_TESLA_BLE:
                return False

    # Use Fleet API
    if not charging_started and ev_provider in (EV_PROVIDER_FLEET_API, EV_PROVIDER_BOTH):
        # Tesla Fleet uses switch.X_charge, not button.X_charge_start
        charge_switch_entity = await _get_tesla_ev_entity(
            hass,
            r"switch\..*_charge$",
            vehicle_vin
        )

        if not charge_switch_entity:
            _LOGGER.error("Could not find Tesla charge switch entity (switch.*_charge)")
            return False

        try:
            # Check API credits before attempting
            if not _is_api_credit_available("teslemetry"):
                _LOGGER.warning("Skipping EV charging start - API credits exhausted, in cooldown period")
                return False

            wake_success = await _wake_tesla_ev(hass, vehicle_vin)
            if not wake_success:
                _LOGGER.warning("Wake failed (possibly due to API credits), skipping charge command")
                return False

            await hass.services.async_call(
                "switch",
                "turn_on",
                {"entity_id": charge_switch_entity},
                blocking=True,
            )
            _LOGGER.info(f"Started EV charging via {charge_switch_entity}")
            charging_started = True
        except Exception as e:
            err_str = str(e).lower()
            # If the car is already charging, treat as success
            if "is_charging" in err_str or "already" in err_str:
                _LOGGER.info(f"EV is already charging — proceeding with session")
                charging_started = True
            elif "complete" in err_str:
                # Vehicle has reached its charge limit — not an error
                _LOGGER.info(f"EV charging is complete (at target SOC) — skipping start")
                return False
            elif "disconnected" in str(e).lower() or "not_plugged" in str(e).lower():
                _LOGGER.info(f"EV not plugged in — skipping start charge")
                return False
            else:
                _LOGGER.error(f"Failed to start EV charging: {e}")

                # Check if this is a credit/payment error
                if _is_api_credit_error(str(e)):
                    _mark_api_credits_exhausted("teslemetry")

                return False

    if not charging_started:
        return False

    # Schedule stop at end of time window if requested
    if stop_outside_window and time_window_end:
        entry_id = config_entry.entry_id

        # Cancel any existing scheduled stop
        if entry_id in _ev_scheduled_stop:
            cancel_func = _ev_scheduled_stop[entry_id].get("cancel")
            if cancel_func:
                cancel_func()
            del _ev_scheduled_stop[entry_id]

        # Calculate when the window ends
        end_datetime = _get_window_end_datetime(time_window_end, time_window_start, timezone)
        if end_datetime:
            async def stop_charging_at_window_end(now) -> None:
                """Stop charging when time window ends."""
                _LOGGER.info(f"⏰ Time window ended, stopping EV charging")
                stop_success = await _action_stop_ev_charging(hass, config_entry, params)
                if stop_success and not params.get("skip_ownership"):
                    await clear_tracked_ev_charging_session(
                        hass,
                        config_entry,
                        _ev_action_loadpoint_id(params),
                        reason="time window ended",
                    )
                # Send notification that charging stopped
                await _send_expo_push(hass, "EV Charging", "Stopped - time window ended")
                # Clean up the scheduled stop entry
                if entry_id in _ev_scheduled_stop:
                    del _ev_scheduled_stop[entry_id]

            cancel_func = async_track_point_in_time(
                hass,
                stop_charging_at_window_end,
                end_datetime,
            )

            _ev_scheduled_stop[entry_id] = {
                "cancel": cancel_func,
                "end_time": end_datetime,
            }
            _LOGGER.info(f"⚡ EV charging started, will stop at {end_datetime.strftime('%H:%M')}")
        else:
            _LOGGER.warning("Could not parse time window for scheduled stop")

    return True


async def _action_stop_ev_charging(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    params: Dict[str, Any]
) -> bool:
    """
    Stop EV charging via Tesla, OCPP, generic, or Zaptec charger.

    Dispatches based on charger_type parameter.
    Also cancels any scheduled stop from stop_outside_window.
    """
    entry_id = config_entry.entry_id

    # Cancel any scheduled stop
    if entry_id in _ev_scheduled_stop:
        cancel_func = _ev_scheduled_stop[entry_id].get("cancel")
        if cancel_func:
            cancel_func()
            _LOGGER.debug("Cancelled scheduled EV charging stop")
        del _ev_scheduled_stop[entry_id]

    charger_type = params.get("charger_type", "tesla")

    # OCPP charger
    if charger_type == "ocpp":
        ocpp_charger_id = params.get("ocpp_charger_id")
        if not ocpp_charger_id:
            _LOGGER.error("OCPP stop: no charger ID configured")
            return False
        return await _stop_ocpp_charging(hass, ocpp_charger_id)

    # Zaptec standalone charger
    if charger_type == "zaptec":
        return await _stop_zaptec_charging(hass, config_entry)

    # Sigenergy EVAC/EVDC direct Modbus charger
    if charger_type == "sigenergy":
        return await _stop_sigenergy_charger(config_entry, params)

    # Generic charger
    if charger_type == "generic":
        switch_entity = (params.get("charger_switch_entity") or "").strip()
        if not switch_entity:
            _LOGGER.error("Generic charger stop: no switch entity configured")
            return False
        if "." not in switch_entity:
            _LOGGER.error("Generic charger stop: invalid switch entity %s", switch_entity)
            return False
        try:
            await hass.services.async_call("switch", "turn_off", {"entity_id": switch_entity}, blocking=True)
            _LOGGER.info("Generic charger stopped via %s", switch_entity)
            return True
        except Exception as e:
            _LOGGER.error("Generic charger stop failed: %s", e)
            return False

    # HA-native charger integrations
    if _is_ha_native_charger_type(charger_type):
        return await _stop_ha_native_charger(hass, params)

    # Tesla charger: existing logic below
    ev_config = _get_ev_config(config_entry)
    ev_provider = ev_config["ev_provider"]
    vehicle_vin = params.get("vehicle_vin")
    ble_prefix = _resolve_ble_prefix_for_vehicle(hass, config_entry, vehicle_vin)

    charging_entity = await _get_tesla_ev_entity(hass, r"sensor\..*_charging$", vehicle_vin)
    if charging_entity:
        charging_state = hass.states.get(charging_entity)
        if charging_state and charging_state.state not in ("unavailable", "unknown"):
            state_lower = charging_state.state.lower()
            if state_lower and state_lower != "charging":
                _LOGGER.info("EV is not charging (state: %s) - treating stop as complete", state_lower)
                return True

    # Try Teslemetry Bluetooth first if configured
    if ev_provider in (EV_PROVIDER_TESLEMETRY_BT, EV_PROVIDER_BOTH):
        tbt_prefix = _resolve_teslemetry_bt_prefix(hass)
        if _is_teslemetry_bt_available(hass, tbt_prefix):
            result = await _stop_ev_charging_teslemetry_bt(hass, tbt_prefix)
            if result or ev_provider == EV_PROVIDER_TESLEMETRY_BT:
                return result

    # Try ESPHome BLE if configured
    if ev_provider in (EV_PROVIDER_TESLA_BLE, EV_PROVIDER_BOTH):
        if _is_ble_available(hass, ble_prefix):
            result = await _stop_ev_charging_ble(hass, ble_prefix)
            if result or ev_provider == EV_PROVIDER_TESLA_BLE:
                return result

    # Use Fleet API
    if ev_provider in (EV_PROVIDER_FLEET_API, EV_PROVIDER_BOTH):
        # Check API credits before attempting
        if not _is_api_credit_available("teslemetry"):
            _LOGGER.warning("Skipping EV charging stop - API credits exhausted, in cooldown period")
            return False

        # Tesla Fleet uses switch.X_charge, not button.X_charge_stop
        charge_switch_entity = await _get_tesla_ev_entity(
            hass,
            r"switch\..*_charge$",
            vehicle_vin
        )

        if not charge_switch_entity:
            _LOGGER.error("Could not find Tesla charge switch entity (switch.*_charge)")
            return False

        try:
            wake_success = await _wake_tesla_ev(hass, vehicle_vin)
            if not wake_success:
                _LOGGER.warning("Wake failed (possibly due to API credits), skipping stop charge command")
                return False

            await hass.services.async_call(
                "switch",
                "turn_off",
                {"entity_id": charge_switch_entity},
                blocking=True,
            )
            _LOGGER.info(f"Stopped EV charging via {charge_switch_entity}")
            return True
        except Exception as e:
            _LOGGER.error(f"Failed to stop EV charging: {e}")

            # Check if this is a credit/payment error
            if _is_api_credit_error(str(e)):
                _mark_api_credits_exhausted("teslemetry")

            return False

    return False


async def _action_set_ev_charge_limit(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    params: Dict[str, Any]
) -> bool:
    """
    Set EV charge limit percentage via Tesla Fleet/Teslemetry or Tesla BLE.
    OCPP, generic, and Zaptec chargers don't support vehicle charge limits — returns True (no-op).
    """
    charger_type = params.get("charger_type", "tesla")
    if charger_type in ("ocpp", "generic", "zaptec"):
        _LOGGER.info("set_ev_charge_limit: not supported for %s chargers (no-op)", charger_type)
        return True

    ev_config = _get_ev_config(config_entry)
    ev_provider = ev_config["ev_provider"]
    vehicle_vin = params.get("vehicle_vin")
    ble_prefix = _resolve_ble_prefix_for_vehicle(hass, config_entry, vehicle_vin)

    # Accept multiple parameter names for flexibility
    percent = params.get("percent") or params.get("limit") or params.get("charge_limit_percent")
    if percent is None:
        _LOGGER.error("set_ev_charge_limit: missing percent parameter")
        return False

    # Clamp to valid range (50-100%)
    percent = max(50, min(100, int(percent)))

    # Teslemetry BT doesn't support charge limit — skip to BLE/Fleet API

    # Try ESPHome BLE if configured
    if ev_provider in (EV_PROVIDER_TESLA_BLE, EV_PROVIDER_BOTH):
        if _is_ble_available(hass, ble_prefix):
            result = await _set_ev_charge_limit_ble(hass, ble_prefix, percent)
            if result or ev_provider == EV_PROVIDER_TESLA_BLE:
                return result

    # Use Fleet API (also fallback for Teslemetry BT which lacks charge limit)
    if ev_provider in (EV_PROVIDER_FLEET_API, EV_PROVIDER_TESLEMETRY_BT, EV_PROVIDER_BOTH):
        # Check API credits before attempting
        if not _is_api_credit_available("teslemetry"):
            _LOGGER.debug("Skipping set EV charge limit - API credits exhausted, in cooldown period")
            return False

        charge_limit_entity = await _get_tesla_ev_entity(
            hass,
            r"number\..*charge_limit$",
            vehicle_vin
        )

        if not charge_limit_entity:
            _LOGGER.error("Could not find Tesla charge_limit number entity")
            return False

        try:
            wake_success = await _wake_tesla_ev(hass, vehicle_vin)
            if not wake_success:
                _LOGGER.debug("Wake failed (possibly due to API credits), skipping set charge limit command")
                return False

            await hass.services.async_call(
                "number",
                "set_value",
                {"entity_id": charge_limit_entity, "value": percent},
                blocking=True,
            )
            _LOGGER.info(f"Set EV charge limit to {percent}% via {charge_limit_entity}")
            return True
        except Exception as e:
            _LOGGER.error(f"Failed to set EV charge limit: {e}")

            # Check if this is a credit/payment error
            if _is_api_credit_error(str(e)):
                _mark_api_credits_exhausted("teslemetry")

            return False

    return False


async def _action_set_ev_charging_amps(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    params: Dict[str, Any]
) -> bool:
    """
    Set EV charging amperage via Tesla, OCPP, generic, or Zaptec charger.
    """
    # Accept both "amps" and "charging_amps" for flexibility. Preserve 0A
    # values for charger APIs that use amps=0 as a pause/stop command.
    amps = params.get("amps")
    if amps is None:
        amps = params.get("charging_amps")
    if amps is None:
        _LOGGER.error("set_ev_charging_amps: missing amps parameter")
        return False

    charger_type = params.get("charger_type", "tesla")

    # OCPP charger
    if charger_type == "ocpp":
        ocpp_charger_id = params.get("ocpp_charger_id")
        if not ocpp_charger_id:
            _LOGGER.error("OCPP set amps: no charger ID configured")
            return False
        return await _set_ocpp_charging_amps(hass, ocpp_charger_id, int(amps))

    # Zaptec standalone charger
    if charger_type == "zaptec":
        return await _set_zaptec_charging_amps(hass, config_entry, int(amps))

    # Sigenergy EVAC direct Modbus charger
    if charger_type == "sigenergy":
        return await _set_sigenergy_charger_amps(config_entry, params, int(amps))

    # HA-native charger integrations
    if _is_ha_native_charger_type(charger_type):
        return await _set_ha_native_charging_amps(hass, params, int(amps))

    # Generic charger
    if charger_type == "generic":
        amps_entity = params.get("charger_amps_entity")
        if not amps_entity:
            _LOGGER.error("Generic charger set amps: no amps entity configured")
            return False
        try:
            await hass.services.async_call("number", "set_value", {"entity_id": amps_entity, "value": int(amps)}, blocking=True)
            _LOGGER.info("Generic charger set to %dA via %s", amps, amps_entity)
            return True
        except Exception as e:
            _LOGGER.error("Generic charger set amps failed: %s", e)
            return False

    # Tesla charger: existing logic below
    ev_config = _get_ev_config(config_entry)
    ev_provider = ev_config["ev_provider"]
    vehicle_vin = params.get("vehicle_vin")
    ble_prefix = _resolve_ble_prefix_for_vehicle(hass, config_entry, vehicle_vin)
    configured_max_amps = _coerce_positive_int(params.get("max_charge_amps"))
    allow_stale_entity_max_override = bool(
        params.get("allow_stale_entity_max_override")
    )

    # Clamp to valid range (5-48A typical, but allow up to 80A for some chargers)
    # Note: Tesla vehicles refuse charging below 5A, so we enforce 5A minimum
    # Tesla BLE supports same 5-32A range as cloud API
    amps = max(5, min(80, int(amps)))
    if configured_max_amps is not None:
        amps = min(configured_max_amps, amps)

    # Try Teslemetry Bluetooth first if configured
    if ev_provider in (EV_PROVIDER_TESLEMETRY_BT, EV_PROVIDER_BOTH):
        tbt_prefix = _resolve_teslemetry_bt_prefix(hass)
        if _is_teslemetry_bt_available(hass, tbt_prefix):
            result = await _set_ev_charging_amps_teslemetry_bt(
                hass,
                tbt_prefix,
                amps,
                allow_stale_entity_max_override=allow_stale_entity_max_override,
                configured_max_amps=configured_max_amps,
            )
            if result or ev_provider == EV_PROVIDER_TESLEMETRY_BT:
                return result

    # Try ESPHome BLE if configured (BLE supports same 5-32A range as cloud API)
    ble_amps = amps
    if ev_provider in (EV_PROVIDER_TESLA_BLE, EV_PROVIDER_BOTH):
        if _is_ble_available(hass, ble_prefix):
            result = await _set_ev_charging_amps_ble(
                hass,
                ble_prefix,
                ble_amps,
                allow_stale_entity_max_override=allow_stale_entity_max_override,
                configured_max_amps=configured_max_amps,
            )
            if result or ev_provider == EV_PROVIDER_TESLA_BLE:
                return result

    # Use Fleet API
    if ev_provider in (EV_PROVIDER_FLEET_API, EV_PROVIDER_BOTH):
        # Check API credits before attempting
        if not _is_api_credit_available("teslemetry"):
            _LOGGER.debug("Skipping set EV charging amps - API credits exhausted, in cooldown period")
            return False

        # Tesla Fleet uses charge_current, some versions use charging_amps
        charging_amps_entity = await _get_tesla_ev_entity(
            hass,
            r"number\..*(charging_amps|charge_current)$",
            vehicle_vin
        )

        if not charging_amps_entity:
            _LOGGER.error("Could not find Tesla charge_current/charging_amps number entity")
            return False

        try:
            # Check entity's actual min/max limits and clamp accordingly
            entity_state = hass.states.get(charging_amps_entity)
            if entity_state:
                entity_min = _coerce_positive_int(entity_state.attributes.get("min"), 5) or 5
                entity_max = _coerce_positive_int(entity_state.attributes.get("max"), 32) or 32
                effective_max = entity_max
                if (
                    allow_stale_entity_max_override
                    and configured_max_amps is not None
                    and configured_max_amps > entity_max
                ):
                    effective_max = configured_max_amps
                    _LOGGER.debug(
                        "Tesla charging amps using configured max %dA over entity max %dA",
                        configured_max_amps,
                        entity_max,
                    )
                original_amps = amps
                amps = max(entity_min, min(effective_max, amps))
                if amps != original_amps:
                    _LOGGER.info(
                        f"Clamped charging amps from {original_amps}A to {amps}A "
                        f"(entity range: {entity_min}-{entity_max}A)"
                    )

            wake_success = await _wake_tesla_ev(hass, vehicle_vin)
            if not wake_success:
                _LOGGER.debug("Wake failed (possibly due to API credits), skipping set amps command")
                return False

            await hass.services.async_call(
                "number",
                "set_value",
                {"entity_id": charging_amps_entity, "value": amps},
                blocking=True,
            )
            _LOGGER.info(f"Set EV charging amps to {amps}A via {charging_amps_entity}")
            return True
        except Exception as e:
            _LOGGER.error(f"Failed to set EV charging amps: {e}")

            # Check if this is a credit/payment error
            if _is_api_credit_error(str(e)):
                _mark_api_credits_exhausted("teslemetry")

            return False

    return False


# =============================================================================
# Dynamic EV Charging (adjusts amps based on battery discharge and grid import)
# =============================================================================

# Global storage for dynamic EV charging state per config entry
# Structure: { entry_id: { vehicle_id: { state... }, ... }, ... }
_dynamic_ev_state: Dict[str, Dict[str, Dict[str, Any]]] = {}

# Lock to prevent duplicate dynamic EV charging sessions from concurrent triggers
_start_dynamic_lock = asyncio.Lock()

# Global storage for regular EV charging scheduled stop (for stop_outside_window)
_ev_scheduled_stop: Dict[str, Any] = {}

# Default vehicle ID for single-vehicle setups
DEFAULT_VEHICLE_ID = "_default"

# Internal charger type for HA-native charger integrations that expose their
# own service domains rather than the generic switch/number model.
HA_NATIVE_CHARGER_TYPES = {
    "ha_native",
    "native",
    "ev_charger",
    "wallbox",
    "easee",
    "zaptec_native",
}


def _is_ha_native_charger_type(charger_type: Any) -> bool:
    """Return whether params refer to a HA-native charger integration."""
    return str(charger_type or "").lower() in HA_NATIVE_CHARGER_TYPES


def _ha_native_charger_entity(params: Dict[str, Any]) -> str:
    """Return the HA entity id used by a HA-native charger adapter."""
    return str(
        params.get("charger_entity_id")
        or params.get("entity_id")
        or params.get("charger_switch_entity")
        or ""
    ).strip()


def _ha_native_charger_domain(params: Dict[str, Any], entity_id: str) -> str:
    """Return the HA service domain for a native charger entity."""
    configured_domain = str(params.get("charger_domain") or "").strip()
    if configured_domain:
        return configured_domain
    if "." in entity_id:
        return entity_id.split(".", 1)[0]
    charger_type = str(params.get("charger_type") or "").strip()
    if charger_type in HA_NATIVE_CHARGER_TYPES and charger_type not in ("ha_native", "native", "zaptec_native"):
        return charger_type
    if charger_type == "zaptec_native":
        return "zaptec"
    return "homeassistant"


def _ha_native_charger_amps_entity(hass: HomeAssistant, params: Dict[str, Any], entity_id: str) -> str:
    """Find a number entity that controls HA-native charger amps."""
    explicit_entity = str(params.get("charger_amps_entity") or "").strip()
    if explicit_entity:
        return explicit_entity

    candidates: list[str] = []
    if entity_id:
        candidates.append(
            entity_id.replace("switch.", "number.").replace("_charger", "_charging_amps")
        )
        candidates.extend(
            entity_id.replace("switch.", "number.") + suffix
            for suffix in ("_amps", "_charging_amps", "_current", "_charging_current")
        )

    for number_entity in candidates:
        if hass.states.get(number_entity):
            return number_entity
    return ""


async def _set_ha_native_charging_amps(
    hass: HomeAssistant,
    params: Dict[str, Any],
    amps: int,
) -> bool:
    """Set charging current for HA-native charger integrations."""
    entity_id = _ha_native_charger_entity(params)
    domain = _ha_native_charger_domain(params, entity_id)

    if not entity_id and domain != "zaptec":
        _LOGGER.error("HA-native charger set amps: no charger entity configured")
        return False

    try:
        if domain == "wallbox":
            await hass.services.async_call(
                "wallbox",
                "set_charging_current",
                {"entity_id": entity_id, "charging_current": amps},
                blocking=True,
            )
            _LOGGER.debug("Set Wallbox charging amps to %dA", amps)
            return True

        if domain == "easee":
            await hass.services.async_call(
                "easee",
                "set_charger_dynamic_limit",
                {"entity_id": entity_id, "current": amps},
                blocking=True,
            )
            _LOGGER.debug("Set Easee charging amps to %dA", amps)
            return True

        if domain == "zaptec":
            installation_id = str(
                params.get("zaptec_installation_id")
                or params.get("zaptec_installation_id_cloud")
                or ""
            ).strip()
            if installation_id:
                await hass.services.async_call(
                    "zaptec",
                    "limit_current",
                    {"device_id": installation_id, "available_current": amps},
                    blocking=True,
                )
                _LOGGER.debug("Set HA Zaptec charging amps to %dA", amps)
                return True

        if domain == "ocpp":
            await hass.services.async_call(
                "ocpp",
                "set_charge_rate",
                {"entity_id": entity_id, "limit_amps": amps},
                blocking=True,
            )
            _LOGGER.debug("Set native OCPP charging amps to %dA", amps)
            return True

        amps_entity = _ha_native_charger_amps_entity(hass, params, entity_id)
        if amps_entity:
            await hass.services.async_call(
                "number",
                "set_value",
                {"entity_id": amps_entity, "value": amps},
                blocking=True,
            )
            _LOGGER.debug("Set HA-native charger amps via %s to %dA", amps_entity, amps)
            return True
    except Exception as err:
        _LOGGER.error("Failed to set HA-native charger amps: %s", err)
        return False

    _LOGGER.debug("No HA-native charger amp control found for %s", entity_id or domain)
    return False


async def _start_ha_native_charger(
    hass: HomeAssistant,
    params: Dict[str, Any],
) -> bool:
    """Start charging through a HA-native charger integration."""
    entity_id = _ha_native_charger_entity(params)
    domain = _ha_native_charger_domain(params, entity_id)

    if not entity_id:
        _LOGGER.error("HA-native charger start: no charger entity configured")
        return False

    try:
        if domain == "switch":
            service_domain = "switch"
            service = "turn_on"
        elif domain in ("ev_charger", "ocpp", "wallbox", "easee", "zaptec"):
            service_domain = domain
            service = "resume_charging" if domain == "zaptec" else "start_charging"
        else:
            service_domain = "homeassistant"
            service = "turn_on"

        await hass.services.async_call(
            service_domain,
            service,
            {"entity_id": entity_id},
            blocking=True,
        )
        _LOGGER.info("Started HA-native charger via %s.%s for %s", service_domain, service, entity_id)
        return True
    except Exception as err:
        _LOGGER.error("HA-native charger start failed: %s", err)
        return False


async def _stop_ha_native_charger(
    hass: HomeAssistant,
    params: Dict[str, Any],
) -> bool:
    """Stop charging through a HA-native charger integration."""
    entity_id = _ha_native_charger_entity(params)
    domain = _ha_native_charger_domain(params, entity_id)

    if not entity_id:
        _LOGGER.error("HA-native charger stop: no charger entity configured")
        return False

    try:
        if domain == "switch":
            service_domain = "switch"
            service = "turn_off"
        elif domain in ("ev_charger", "ocpp", "wallbox", "easee", "zaptec"):
            service_domain = domain
            service = "stop_charging"
        else:
            service_domain = "homeassistant"
            service = "turn_off"

        await hass.services.async_call(
            service_domain,
            service,
            {"entity_id": entity_id},
            blocking=True,
        )
        _LOGGER.info("Stopped HA-native charger via %s.%s for %s", service_domain, service, entity_id)
        return True
    except Exception as err:
        _LOGGER.error("HA-native charger stop failed: %s", err)
        return False


async def record_manual_ev_charging_session(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    vehicle_id: Optional[str] = None,
    params: Optional[Dict[str, Any]] = None,
    reason: str = "Manual charging",
) -> None:
    """Record a user-started EV session so automation modes do not fight it."""
    from ..const import DOMAIN

    entry_id = config_entry.entry_id
    resolved_vehicle_id = vehicle_id or DEFAULT_VEHICLE_ID

    # If another PowerSync mode owned this loadpoint, release its timers and
    # session bookkeeping without sending another physical stop command.
    await clear_tracked_ev_charging_session(
        hass,
        config_entry,
        resolved_vehicle_id,
        reason="manual override",
    )

    full_params = {
        "dynamic_mode": "manual",
        "owner_mode": "manual",
        "vehicle_id": resolved_vehicle_id,
        "vehicle_vin": None if resolved_vehicle_id == DEFAULT_VEHICLE_ID else resolved_vehicle_id,
        **(params or {}),
    }

    if entry_id not in _dynamic_ev_state:
        _dynamic_ev_state[entry_id] = {}

    session_id = None
    try:
        from .ev_charging_session import get_session_manager
        session_manager = get_session_manager()
        if session_manager:
            session = await session_manager.start_session(
                vehicle_id=resolved_vehicle_id,
                mode="manual",
            )
            session_id = session.id
    except Exception as e:
        _LOGGER.debug("Manual EV: could not start session tracking: %s", e)

    _dynamic_ev_state[entry_id][resolved_vehicle_id] = {
        "active": True,
        "params": full_params,
        "current_amps": 0,
        "target_amps": 0,
        "cancel_timer": None,
        "priority": 0,
        "paused": False,
        "paused_reason": None,
        "charging_started": True,
        "entity_max_rechecked": True,
        "allocated_surplus_kw": 0,
        "reason": reason,
        "vehicle_name": full_params.get("vehicle_name"),
        "session_id": session_id,
    }
    from .ev_ownership import claim_ev_ownership
    _dynamic_ev_state[entry_id][resolved_vehicle_id]["ownership"] = claim_ev_ownership(
        hass,
        config_entry,
        resolved_vehicle_id,
        owner_mode="manual",
        session_id=session_id,
        reason=reason,
        command="start",
        extra={
            "charger_type": full_params.get("charger_type", "tesla"),
            "source_mode": full_params.get("source_mode"),
            "duration_minutes": full_params.get("duration_minutes"),
            "expires_at": full_params.get("expires_at"),
            "quick_control": full_params.get("quick_control"),
        },
    )

    if DOMAIN in hass.data and entry_id in hass.data[DOMAIN]:
        hass.data[DOMAIN][entry_id]["dynamic_ev_state"] = _dynamic_ev_state[entry_id]

    _LOGGER.info("Manual EV charging session recorded for %s", resolved_vehicle_id)


async def clear_tracked_ev_charging_session(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    vehicle_id: Optional[str] = None,
    reason: str = "manual stop",
) -> None:
    """Clear PowerSync EV ownership without sending a physical stop command."""
    await _action_stop_ev_charging_dynamic(
        hass,
        config_entry,
        {
            "vehicle_id": vehicle_id or DEFAULT_VEHICLE_ID,
            "stop_charging": False,
            "manual_stop": True,
            "stop_reason": reason,
        },
    )


def _parallel_battery_reserve_kw(
    live_status: dict,
    config: dict,
    method: str,
    battery_charge_kw: float,
) -> float:
    """Return the extra battery charge reserve to withhold from EV surplus."""
    if not config.get("allow_parallel_charging", False):
        return 0.0

    try:
        max_charge_kw = max(0.0, float(config.get("max_battery_charge_rate_kw", 5.0) or 0.0))
    except (TypeError, ValueError):
        max_charge_kw = 5.0
    if max_charge_kw <= 0:
        return 0.0

    battery_soc = live_status.get("battery_soc")
    try:
        battery_soc = float(battery_soc) if battery_soc is not None else None
    except (TypeError, ValueError):
        battery_soc = None

    min_soc = get_solar_surplus_min_battery_soc(config)
    battery_below_min = battery_soc is not None and battery_soc < min_soc
    battery_currently_charging = battery_charge_kw > 0.05
    if not battery_below_min and not battery_currently_charging:
        return 0.0

    if method == "direct":
        return max(0.0, max_charge_kw - battery_charge_kw)
    return max_charge_kw


def _calculate_solar_surplus(live_status: dict, current_ev_power_kw: float, config: dict) -> float:
    """
    Calculate available solar surplus for EV charging.

    Two methods are supported:
    - direct: surplus = solar - load - battery_charge - buffer
    - grid_based (AmpPilot style): surplus = -grid + current_ev + battery_charge
    When parallel charging is enabled, the configured battery charge rate is
    reserved before EV amps are calculated.

    Args:
        live_status: Dict with solar_power, grid_power, battery_power, load_power (all in W)
        current_ev_power_kw: Current EV charging power in kW (sum of all EVs currently charging)
        config: Dict with surplus_calculation method and household_buffer_kw

    Returns:
        Available surplus in kW (always >= 0)
    """
    # Convert from W to kW
    solar_kw = (live_status.get("solar_power") or 0) / 1000
    grid_kw = (live_status.get("grid_power") or 0) / 1000  # Positive = import, Negative = export
    battery_kw = (live_status.get("battery_power") or 0) / 1000  # Positive = discharge, Negative = charge
    load_kw = (live_status.get("load_power") or 0) / 1000
    buffer_kw = config.get("household_buffer_kw", 0.5)

    method = config.get("surplus_calculation", "grid_based")

    if method == "grid_based":
        # AmpPilot style: what's being exported + EV power + battery charge
        # If grid_kw is negative (exporting), -grid_kw is positive (available surplus)
        # Add back current EV power (since we want to know what's available if EV wasn't charging)
        # Add battery charging power (we can redirect it to EV instead)
        battery_charge_kw = max(0, -battery_kw)  # Only count when charging (negative = charging)
        battery_discharge_kw = max(0, battery_kw)  # Positive = discharging
        # Subtract battery discharge: grid export from battery isn't solar surplus
        surplus = -grid_kw + current_ev_power_kw + battery_charge_kw - battery_discharge_kw
        battery_reserve_kw = _parallel_battery_reserve_kw(
            live_status, config, method, battery_charge_kw
        )
        available_kw = max(0, surplus - buffer_kw - battery_reserve_kw)
        _LOGGER.debug(
            f"Surplus calc (grid_based): grid={grid_kw:.2f}kW, ev={current_ev_power_kw:.2f}kW, "
            f"bat_charge={battery_charge_kw:.2f}kW, bat_discharge={battery_discharge_kw:.2f}kW → "
            f"raw={surplus:.2f}kW, buffer={buffer_kw:.2f}kW, "
            f"battery_reserve={battery_reserve_kw:.2f}kW, available={available_kw:.2f}kW"
        )
    else:  # direct method
        # Direct calculation: what solar is producing minus what's being used
        # IMPORTANT: If load sensor includes EV power (e.g., mobile connector), we need to
        # subtract it to get the "real" household load, then calculate true surplus
        battery_charge_kw = max(0, -battery_kw)
        real_household_load_kw = load_kw - current_ev_power_kw  # Remove EV from house load
        surplus = solar_kw - real_household_load_kw - battery_charge_kw
        battery_reserve_kw = _parallel_battery_reserve_kw(
            live_status, config, method, battery_charge_kw
        )
        available_kw = max(0, surplus - buffer_kw - battery_reserve_kw)
        _LOGGER.debug(
            f"Surplus calc (direct): solar={solar_kw:.2f}kW, load={load_kw:.2f}kW (real={real_household_load_kw:.2f}kW), "
            f"bat_charge={battery_charge_kw:.2f}kW → raw={surplus:.2f}kW, "
            f"buffer={buffer_kw:.2f}kW, battery_reserve={battery_reserve_kw:.2f}kW, "
            f"available={available_kw:.2f}kW"
        )

    # Apply buffer and ensure non-negative
    return available_kw


def _is_ev_charging_from_solar(grid_power_kw: float, ev_power_kw: float) -> bool:
    """Determine if EV is charging primarily from solar.

    If less than 20% of EV power is coming from the grid, consider it solar.
    """
    if ev_power_kw <= 0:
        return False
    return grid_power_kw < (ev_power_kw * 0.2)


def _get_current_ev_prices(hass, entry_id: str) -> tuple:
    """Get current import/export prices with multi-source fallback.

    Returns:
        (import_price_cents, export_price_cents) tuple with defaults of (30.0, 8.0)
    """
    from .ev_pricing import get_current_ev_prices

    return get_current_ev_prices(hass, entry_id)


def get_price_recommendation(
    import_price_cents: float,
    export_price_cents: float,
    surplus_kw: float,
    battery_soc: float,
    min_battery_soc: float = DEFAULT_SOLAR_SURPLUS_MIN_BATTERY_SOC,
    prefer_export_threshold_cents: float = 15.0,
) -> dict:
    """
    Get a charging recommendation based on current prices and surplus.

    Logic:
    - If surplus > 0 and battery SoC >= min: recommend charge (free solar)
    - If export price > threshold and surplus > 0: recommend export (sell high)
    - If import price < export price (negative spread): recommend charge (arbitrage)
    - If import price very low (< 5c): recommend charge (cheap grid)
    - Otherwise: recommend wait

    Args:
        import_price_cents: Current import price in cents/kWh
        export_price_cents: Current export price (feed-in tariff) in cents/kWh
        surplus_kw: Current solar surplus in kW
        battery_soc: Current battery SoC (0-100)
        min_battery_soc: Home battery must be at least this % before EV charging (default 80)
        prefer_export_threshold_cents: Export price above which to prefer selling (default 15c)

    Returns:
        Dictionary with recommendation, reason, and prices
    """
    recommendation = "wait"
    reason = "No clear advantage to charge now"

    # Check for solar surplus first (best option - free energy)
    if surplus_kw >= 1.0 and battery_soc >= min_battery_soc:
        recommendation = "charge"
        reason = f"Solar surplus available ({surplus_kw:.1f}kW) - free charging!"
    elif surplus_kw >= 1.0 and battery_soc < min_battery_soc:
        recommendation = "wait"
        reason = f"Solar surplus available but battery only at {battery_soc:.0f}% (need {min_battery_soc}%)"
    # Check if export price is high enough to prefer selling
    elif surplus_kw > 0 and export_price_cents >= prefer_export_threshold_cents:
        recommendation = "export"
        reason = f"High export rate ({export_price_cents:.1f}c/kWh) - sell to grid instead"
    # Check for arbitrage opportunity (import < export is rare but possible)
    elif import_price_cents < export_price_cents:
        recommendation = "charge"
        reason = f"Arbitrage: import ({import_price_cents:.1f}c) cheaper than export ({export_price_cents:.1f}c)"
    # Check for very cheap grid power (off-peak)
    elif import_price_cents < 10:
        recommendation = "charge"
        reason = f"Very cheap grid power ({import_price_cents:.1f}c/kWh) - good time to charge"
    # Check for cheap-ish grid power
    elif import_price_cents < 20:
        recommendation = "charge"
        reason = f"Reasonable grid price ({import_price_cents:.1f}c/kWh)"
    # High import price - wait for cheaper
    elif import_price_cents > 35:
        recommendation = "wait"
        reason = f"High grid price ({import_price_cents:.1f}c/kWh) - wait for cheaper rates"
    else:
        recommendation = "wait"
        reason = f"Grid at {import_price_cents:.1f}c/kWh - consider waiting for solar or off-peak"

    return {
        "import_price_cents": round(import_price_cents, 2),
        "export_price_cents": round(export_price_cents, 2),
        "surplus_kw": round(surplus_kw, 2),
        "recommendation": recommendation,
        "reason": reason,
    }


def _distribute_surplus(entry_id: str, vehicle_id: str, total_surplus_kw: float, strategy: str) -> float:
    """
    Distribute available surplus between multiple vehicles based on strategy.

    Args:
        entry_id: Config entry ID
        vehicle_id: The vehicle requesting its allocation
        total_surplus_kw: Total available surplus in kW
        strategy: Distribution strategy (even, priority_first, priority_only)

    Returns:
        Allocated surplus for this vehicle in kW
    """
    vehicles = _dynamic_ev_state.get(entry_id, {})
    active_vehicles = [
        (vid, v) for vid, v in vehicles.items()
        if v.get("active") and not v.get("paused")
    ]

    if len(active_vehicles) <= 1:
        _LOGGER.debug(f"Distribute surplus: single vehicle {vehicle_id[:8]}... gets all {total_surplus_kw:.2f}kW")
        return total_surplus_kw

    # Get current vehicle info
    my_vehicle = vehicles.get(vehicle_id, {})
    my_priority = my_vehicle.get("priority", 1)
    my_params = my_vehicle.get("params", {})

    vehicle_names = [(vid[:8], v.get("priority", 1)) for vid, v in active_vehicles]
    _LOGGER.debug(f"Distribute surplus: {len(active_vehicles)} vehicles active: {vehicle_names}, strategy={strategy}")

    allocated = 0.0

    if strategy == "even":
        # Split evenly between all active vehicles
        allocated = total_surplus_kw / len(active_vehicles)

    elif strategy == "priority_first":
        # Highest priority (lowest number) gets first allocation up to max
        # Sort by priority (1 = highest priority)
        sorted_vehicles = sorted(active_vehicles, key=lambda x: x[1].get("priority", 1))

        if my_priority == 1:
            # Primary vehicle gets first allocation up to max
            max_kw = (my_params.get("max_charge_amps", 32) * my_params.get("voltage", 240)) / 1000
            allocated = min(total_surplus_kw, max_kw)
        else:
            # Secondary vehicles get the remainder
            remaining = total_surplus_kw
            for vid, v in sorted_vehicles:
                if v.get("priority", 1) < my_priority:
                    v_params = v.get("params", {})
                    v_max_kw = (v_params.get("max_charge_amps", 32) * v_params.get("voltage", 240)) / 1000
                    remaining = max(0, remaining - min(remaining, v_max_kw))
            allocated = remaining

    elif strategy == "priority_only":
        # Only the highest priority vehicle gets surplus
        if my_priority == 1:
            allocated = total_surplus_kw
        else:
            allocated = 0

    else:
        allocated = total_surplus_kw

    _LOGGER.debug(
        f"Distribute surplus: {vehicle_id[:8]}... (priority={my_priority}) "
        f"gets {allocated:.2f}kW of {total_surplus_kw:.2f}kW ({strategy})"
    )
    return allocated


def _dynamic_ev_vehicle_vin(vehicle_id: str, params: Dict[str, Any]) -> Optional[str]:
    """Return the specific vehicle identifier used by plug-status checks."""
    return (
        params.get("vehicle_vin")
        or params.get("vehicle_id")
        or (vehicle_id if vehicle_id != DEFAULT_VEHICLE_ID else None)
    )


async def _clear_ble_dynamic_session_if_unplugged(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    vehicle_id: str,
    params: Dict[str, Any],
) -> bool:
    """Clear stale BLE dynamic sessions when the vehicle is definitively unplugged."""
    vehicle_vin = _dynamic_ev_vehicle_vin(vehicle_id, params)
    if not (vehicle_vin and str(vehicle_vin).startswith("ble_")):
        return False

    try:
        from .ev_charging_planner import is_ev_plugged_in

        plugged_in = await is_ev_plugged_in(hass, config_entry, vehicle_vin=vehicle_vin)
    except Exception as err:
        _LOGGER.debug("Dynamic EV: could not verify BLE plug state for %s: %s", vehicle_id, err)
        return False

    if plugged_in:
        return False

    _LOGGER.info(
        "⚡ Dynamic EV: clearing stale BLE session for %s because vehicle is not plugged in",
        vehicle_id,
    )
    await _action_stop_ev_charging_dynamic(
        hass,
        config_entry,
        {
            "vehicle_id": vehicle_id,
            "stop_charging": False,
            "stop_reason": "vehicle unplugged",
        },
    )
    return True


async def _solar_surplus_switch_to_next_vehicle(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    entry_id: str,
    completed_vehicle_id: str,
    current_params: Dict[str, Any],
) -> None:
    """
    When a vehicle finishes charging in solar surplus mode, stop its session
    and try to start surplus charging on the next available vehicle.
    """
    from .ev_charging_planner import discover_all_tesla_vehicles, is_ev_plugged_in

    completed_name = _get_vehicle_name_from_vin(hass, completed_vehicle_id) or completed_vehicle_id[:8]

    # Stop the completed vehicle's dynamic session (don't send stop command — it's already done)
    await _action_stop_ev_charging_dynamic(
        hass, config_entry,
        {
            "vehicle_id": completed_vehicle_id,
            "stop_charging": False,
            "stop_reason": f"charge complete",
        }
    )

    # Send notification about completion
    try:
        await _send_expo_push(
            hass, "EV Charging",
            f"{completed_name} charge complete — checking other vehicles"
        )
    except Exception:
        pass

    # Discover all Tesla vehicles and find one that's plugged in and not complete
    try:
        all_vehicles = await discover_all_tesla_vehicles(hass, config_entry)
    except Exception as e:
        _LOGGER.warning(f"Solar surplus: Could not discover vehicles for fallback: {e}")
        return

    for vehicle in all_vehicles:
        vin = vehicle["vin"]
        name = vehicle["name"]

        # Skip the completed vehicle
        if vin == completed_vehicle_id:
            continue

        # Check if plugged in
        plugged_in = await is_ev_plugged_in(hass, config_entry, vehicle_vin=vin)
        if not plugged_in:
            _LOGGER.debug(f"Solar surplus fallback: {name} ({vin[:8]}...) not plugged in, skipping")
            continue

        # Check if charge is already complete
        if _is_vehicle_charge_complete(hass, vin):
            _LOGGER.debug(f"Solar surplus fallback: {name} ({vin[:8]}...) already complete, skipping")
            continue

        # Found a candidate — start surplus charging with same params but new VIN
        _LOGGER.info(
            f"⚡ Solar surplus EV: Switching from {completed_name} to {name} ({vin[:8]}...)"
        )
        new_params = dict(current_params)
        new_params["vehicle_vin"] = vin
        new_params["vehicle_name"] = name

        try:
            await _action_start_ev_charging_dynamic(hass, config_entry, new_params, context=None)
            await _send_expo_push(
                hass, "EV Charging",
                f"Solar surplus switching to {name}"
            )
        except Exception as e:
            _LOGGER.error(f"Solar surplus fallback: Failed to start {name}: {e}")
        return

    _LOGGER.info(f"⚡ Solar surplus EV: No other vehicles available for charging")


async def _set_vehicle_amps(
    hass: HomeAssistant,
    config_entry: ConfigEntry | None,
    vehicle_id: str,
    amps: int,
    params: dict
) -> bool:
    """
    Set charging amps for any charger type (Tesla, OCPP, generic HA entities, Zaptec).

    Args:
        hass: Home Assistant instance
        config_entry: Config entry
        vehicle_id: Vehicle identifier (VIN for Tesla, charger ID for OCPP)
        amps: Target charging amperage (0 = stop charging)
        params: Charger parameters including charger_type

    Returns:
        True if successful
    """
    charger_type = params.get("charger_type", "tesla")

    if charger_type == "tesla":
        if amps == 0:
            return await _action_stop_ev_charging(hass, config_entry, {"vehicle_vin": vehicle_id})
        return await _action_set_ev_charging_amps(hass, config_entry, {
            "amps": amps,
            "vehicle_vin": vehicle_id if vehicle_id != DEFAULT_VEHICLE_ID else None,
            "max_charge_amps": params.get("max_charge_amps"),
            "allow_stale_entity_max_override": params.get(
                "allow_stale_entity_max_override",
                False,
            ),
        })

    elif charger_type == "ocpp":
        ocpp_charger_id = params.get("ocpp_charger_id")
        if not ocpp_charger_id:
            _LOGGER.error("OCPP charger ID not configured")
            return False
        if amps == 0:
            return await _stop_ocpp_charging(hass, ocpp_charger_id)
        # Set amps then ensure charger is on (idempotent)
        amps_ok = True
        if params.get("_ocpp_current_limit_unsupported"):
            _LOGGER.debug(
                "OCPP charger %s: blocking managed start to %sA after previous current-limit rejection",
                ocpp_charger_id,
                amps,
            )
            return False
        else:
            amps_ok = await _set_ocpp_charging_amps(hass, ocpp_charger_id, amps)
            if not amps_ok:
                params["_ocpp_current_limit_unsupported"] = True
                _LOGGER.warning(
                    "OCPP charger %s: current limit update to %sA failed; refusing managed start",
                    ocpp_charger_id,
                    amps,
                )
                return False
        if _has_pre_charge_wake(params):
            ready, block_reason = _ocpp_charger_ready_for_wake(hass, str(ocpp_charger_id))
            if not ready:
                _LOGGER.warning("OCPP charger wake/start blocked: %s", block_reason)
                return False
            if not await _run_pre_charge_wake_sequence(hass, params, "ocpp"):
                return False
        start_ok = await _start_ocpp_charging(hass, ocpp_charger_id)
        if not amps_ok:
            _LOGGER.debug(
                "OCPP charger %s start command %s even though current limit update failed",
                ocpp_charger_id, "succeeded" if start_ok else "failed",
            )
        return start_ok

    elif charger_type == "zaptec":
        if amps == 0:
            return await _stop_zaptec_charging(hass, config_entry)
        return await _start_zaptec_charging(hass, config_entry, amps, params)

    elif charger_type == "sigenergy":
        if config_entry is None:
            _LOGGER.error("Sigenergy charger control requires a config entry")
            return False
        if amps == 0:
            return await _stop_sigenergy_charger(config_entry, params)
        config = _sigenergy_charger_config(config_entry, params)
        if config["charger_type"] == "evdc":
            _LOGGER.debug(
                "Sigenergy EVDC does not expose writable amps; starting charger without current limit"
            )
            return await _start_sigenergy_charger(config_entry, params)
        if not await _set_sigenergy_charger_amps(config_entry, params, amps):
            return False
        return await _start_sigenergy_charger(config_entry, params)

    elif _is_ha_native_charger_type(charger_type):
        if amps == 0:
            return await _stop_ha_native_charger(hass, params)

        amps_ok = await _set_ha_native_charging_amps(hass, params, amps)
        if not await _run_pre_charge_wake_sequence(hass, params, str(charger_type)):
            return False
        start_ok = await _start_ha_native_charger(hass, params)
        if not amps_ok:
            _LOGGER.debug(
                "HA-native charger start command %s even though current limit update failed",
                "succeeded" if start_ok else "failed",
            )
        return start_ok

    elif charger_type == "generic":
        # Use HA service calls to switch and number entities
        # Supports two modes:
        #   1) Switch + optional amps: switch on/off to start/stop, amps to set rate
        #   2) Amps-only (no switch): set amps to 0 to pause, >0 to charge
        #      (e.g. Evnex pauses at <=5A — the min_charge_amps floor handles this)
        switch_entity = params.get("charger_switch_entity")
        amps_entity = params.get("charger_amps_entity")

        try:
            if amps == 0:
                # Stop/pause charging
                if amps_entity:
                    await hass.services.async_call(
                        "number", "set_value",
                        {"entity_id": amps_entity, "value": 0},
                        blocking=True
                    )
                if switch_entity:
                    await hass.services.async_call(
                        "switch", "turn_off",
                        {"entity_id": switch_entity},
                        blocking=True
                    )
            else:
                # Set amps and ensure charger is on
                ready, block_reason = _generic_charger_ready_for_start(hass, params)
                if not ready:
                    _LOGGER.warning("Generic charger set amps blocked: %s", block_reason)
                    return False
                if not await _run_pre_charge_wake_sequence(hass, params, "generic"):
                    return False
                if amps_entity:
                    await hass.services.async_call(
                        "number", "set_value",
                        {"entity_id": amps_entity, "value": amps},
                        blocking=True
                    )
                if switch_entity:
                    await hass.services.async_call(
                        "switch", "turn_on",
                        {"entity_id": switch_entity},
                        blocking=True
                    )
            _LOGGER.info(f"Set generic charger to {amps}A via {amps_entity or switch_entity}")
            return True
        except Exception as e:
            _LOGGER.error(f"Failed to set generic charger amps: {e}")
            return False

    _LOGGER.warning(f"Unknown charger type: {charger_type}")
    return False


def _effective_min_charge_amps(params: dict) -> int:
    """Return the minimum current the selected charger can actually accept."""
    configured_min = _coerce_positive_int(params.get("min_charge_amps"), 5) or 5
    charger_type = params.get("charger_type", "tesla")
    if charger_type == "tesla":
        # Tesla charge-current entities clamp to at least 5A. Solar surplus may
        # be configured lower, but hysteresis must use the hardware floor.
        return max(configured_min, 5)
    if charger_type == "ocpp":
        # OCPP AC charging follows the EVSE/J1772 6A floor. Some HACS OCPP
        # entities expose a stale or capability range below that; do not let
        # dynamic control chase invalid 0-5A targets.
        return max(configured_min, OCPP_MIN_CHARGE_AMPS)
    if charger_type == "sigenergy":
        return max(configured_min, OCPP_MIN_CHARGE_AMPS)
    return configured_min


async def _set_ocpp_charging_amps(hass: HomeAssistant, charger_id: int, amps: int) -> bool:
    """Set charging amps for an OCPP charger."""
    from ..const import DOMAIN

    charger_id = str(charger_id)
    base_id, connector_id = _ocpp_charger_base_and_connector(charger_id)
    server_found = False
    hacs_server_found = False

    for central_system in (hass.data.get("ocpp") or {}).values():
        if not hasattr(central_system, "set_max_charge_rate_amps"):
            continue
        hacs_server_found = True
        try:
            kwargs = {}
            if connector_id is not None:
                kwargs["connector_id"] = connector_id
            success = await central_system.set_max_charge_rate_amps(
                base_id,
                float(amps),
                **kwargs,
            )
            if success:
                _LOGGER.info("Set OCPP charger %s to %dA via HACS OCPP API", charger_id, amps)
                return True
        except Exception as e:
            _LOGGER.error("Failed to set OCPP charging amps through HACS OCPP API: %s", e)

    if hacs_server_found:
        _LOGGER.warning(
            "OCPP charger %s current limit rejected by HACS OCPP/charge point",
            charger_id,
        )
        return False

    try:
        # Find the OCPP charger controller in hass.data
        for entry_id, entry_data in hass.data.get(DOMAIN, {}).items():
            if isinstance(entry_data, dict) and "ocpp_server" in entry_data:
                ocpp_server = entry_data["ocpp_server"]
                if hasattr(ocpp_server, "set_charging_profile"):
                    server_found = True
                    success = await ocpp_server.set_charging_profile(charger_id, amps)
                    if success:
                        _LOGGER.info(f"Set OCPP charger {charger_id} to {amps}A")
                        return True

    except Exception as e:
        _LOGGER.error(f"Failed to set OCPP charging amps through server: {e}")

    current_entity = _find_ocpp_current_limit_entity(hass, charger_id)
    if current_entity:
        try:
            target_amps = max(OCPP_MIN_CHARGE_AMPS, int(amps))
            entity_state = hass.states.get(current_entity)
            if entity_state:
                entity_min = entity_state.attributes.get("min")
                entity_max = entity_state.attributes.get("max")
                if entity_max is not None and int(entity_max) < OCPP_MIN_CHARGE_AMPS:
                    _LOGGER.warning(
                        "OCPP charger %s current-limit entity %s reports max=%sA below the 6A EVSE minimum; treating current limiting as unsupported",
                        charger_id,
                        current_entity,
                        entity_max,
                    )
                    return False
                if entity_min is not None:
                    target_amps = max(int(entity_min), target_amps)
                if entity_max is not None:
                    target_amps = min(int(entity_max), target_amps)

            await hass.services.async_call(
                "number",
                "set_value",
                {"entity_id": current_entity, "value": target_amps},
                blocking=True,
            )
            _LOGGER.info(
                "Set OCPP charger %s to %dA via %s",
                charger_id, target_amps, current_entity,
            )
            return True
        except Exception as e:
            _LOGGER.error("Failed to set OCPP amps via %s: %s", current_entity, e)

    if server_found:
        _LOGGER.warning(
            "OCPP charger %s current limit not updated: server command failed and no current-limit number entity was found",
            charger_id,
        )
    else:
        _LOGGER.warning(
            "OCPP charger %s current limit not updated: no OCPP server or current-limit number entity found",
            charger_id,
        )
    return False


def _parse_time_window(time_str: str) -> Optional[dt_time]:
    """Parse time string (HH:MM) to time object."""
    if not time_str:
        return None
    try:
        parts = time_str.split(":")
        return dt_time(int(parts[0]), int(parts[1]))
    except (ValueError, IndexError):
        return None


def _get_window_end_datetime(
    window_end_str: str,
    window_start_str: str,
    timezone_str: str
) -> Optional[datetime]:
    """Calculate the datetime when the time window ends.

    Handles windows that cross midnight (e.g., 22:00 - 06:00).
    Returns None if parsing fails.
    """
    from zoneinfo import ZoneInfo

    window_end = _parse_time_window(window_end_str)
    window_start = _parse_time_window(window_start_str)

    if not window_end:
        return None

    try:
        tz = ZoneInfo(timezone_str)
    except Exception:
        tz = ZoneInfo("UTC")

    now = datetime.now(tz)
    today = now.date()

    # Create end datetime for today
    end_datetime = datetime.combine(today, window_end, tzinfo=tz)

    # Handle cross-midnight windows
    if window_start and window_end < window_start:
        # Window crosses midnight (e.g., 22:00 - 06:00)
        # If we're past midnight (current time < start time), end is today
        # If we're before midnight (current time >= start time), end is tomorrow
        if now.time() >= window_start:
            # We're in the first part of the window (before midnight)
            # End is tomorrow
            from datetime import timedelta as td
            end_datetime = end_datetime + td(days=1)

    # If end time has already passed today, move to tomorrow
    if end_datetime <= now:
        from datetime import timedelta as td
        end_datetime = end_datetime + td(days=1)

    return end_datetime


def _is_inside_time_window(
    window_start_str: str,
    window_end_str: str,
    timezone_str: str
) -> bool:
    """Check if current time is inside the specified time window."""
    from zoneinfo import ZoneInfo

    window_start = _parse_time_window(window_start_str)
    window_end = _parse_time_window(window_end_str)

    if not window_start or not window_end:
        return True  # No window defined, always inside

    try:
        tz = ZoneInfo(timezone_str)
    except Exception:
        tz = ZoneInfo("UTC")

    now = datetime.now(tz).time()

    # Handle cross-midnight windows
    if window_end < window_start:
        # Window crosses midnight (e.g., 22:00 - 06:00)
        return now >= window_start or now < window_end
    else:
        # Normal window (e.g., 06:00 - 22:00)
        return window_start <= now < window_end


async def _get_tesla_live_status(hass: HomeAssistant, config_entry: ConfigEntry) -> Optional[Dict[str, Any]]:
    """Get live status from Tesla API for battery and grid power.

    Returns:
        Dict with battery_power, grid_power, solar_power, load_power, battery_soc
        - battery_power: Positive = discharging, Negative = charging
        - grid_power: Positive = importing, Negative = exporting
    """
    from ..const import DOMAIN
    from .live_status import coordinator_data_to_ev_live_status

    entry_data = hass.data.get(DOMAIN, {}).get(config_entry.entry_id, {})

    # Try coordinator data first (cached, no API call needed)
    for coord_key in ("tesla_coordinator", "sigenergy_coordinator", "sungrow_coordinator"):
        coordinator = entry_data.get(coord_key)
        if coordinator and coordinator.data:
            return coordinator_data_to_ev_live_status(coordinator.data)

    # Fall back to direct API call
    token_getter = entry_data.get("token_getter")
    site_id = entry_data.get("site_id")

    if not token_getter or not site_id:
        _LOGGER.debug("No Tesla token getter or site_id available")
        return None

    try:
        current_token, current_provider = token_getter()
        if not current_token:
            return None

        import aiohttp

        if current_provider == "teslemetry":
            url = f"https://api.teslemetry.com/api/energy_sites/{site_id}/live_status"
        else:
            url = f"https://fleet-api.prd.na.vn.cloud.tesla.com/api/1/energy_sites/{site_id}/live_status"

        headers = {"Authorization": f"Bearer {current_token}"}

        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as response:
                if response.status == 200:
                    data = await response.json()
                    site_status = data.get("response", {})
                    return {
                        "battery_soc": site_status.get("percentage_charged"),
                        "grid_power": site_status.get("grid_power"),  # Positive = importing
                        "solar_power": site_status.get("solar_power"),
                        "battery_power": site_status.get("battery_power"),  # Positive = discharging
                        "load_power": site_status.get("load_power"),
                    }
                else:
                    _LOGGER.debug(f"Failed to get live_status: {response.status}")
                    return None
    except Exception as e:
        _LOGGER.debug(f"Error getting Tesla live status: {e}")
        return None


async def _dynamic_ev_update_surplus(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    entry_id: str,
    vehicle_id: str,
) -> None:
    """
    Solar surplus mode update - adjusts EV charging amps based on available solar surplus.

    This mode prioritizes battery SoC and only charges the EV when there's excess solar
    that would otherwise be exported to the grid.
    """
    vehicles = _dynamic_ev_state.get(entry_id, {})
    state = vehicles.get(vehicle_id)
    if not state or not state.get("active"):
        return

    params = state.get("params", {})

    if await _clear_ble_dynamic_session_if_unplugged(
        hass, config_entry, vehicle_id, params
    ):
        return

    # Don't charge when vehicle is away from home
    try:
        from .ev_charging_planner import get_ev_location
        _vin = vehicle_id if vehicle_id != DEFAULT_VEHICLE_ID else None
        _location = await get_ev_location(hass, config_entry, _vin)
        if _location not in ("home", "unknown"):
            _current_amps = state.get("current_amps", 0)
            if _current_amps > 0:
                _LOGGER.info(f"⚡ Solar surplus EV: Stopping - vehicle not at home ({_location})")
                await _set_vehicle_amps(hass, config_entry, vehicle_id, 0, params)
                state["current_amps"] = 0
                state["target_amps"] = 0
                try:
                    from .ev_charging_session import get_session_manager
                    _sm = get_session_manager()
                    if _sm:
                        await _sm.end_session(vehicle_id=vehicle_id, reason="vehicle_away")
                except Exception:
                    pass
            state["high_surplus_start"] = None
            state["low_surplus_start"] = None
            return
    except Exception as e:
        _LOGGER.debug(f"Solar surplus EV: Could not check vehicle location: {e}")

    # Re-check Tesla entity max after charging starts (Tesla reports real max only when active)
    # The entity needs a few seconds to update after the car starts drawing power,
    # so we do this on the first update cycle after charging_started=True (10-30s later).
    if (state.get("charging_started") and not state.get("entity_max_rechecked")
            and params.get("charger_type") == "tesla"
            and vehicle_id != DEFAULT_VEHICLE_ID):
        try:
            entity = await _get_tesla_ev_entity(
                hass, r"number\..*(charging_amps|charge_current)$", vehicle_id
            )
            if entity:
                entity_state = hass.states.get(entity)
                if entity_state:
                    new_max = int(entity_state.attributes.get("max", 0))
                    old_max = params.get("max_charge_amps", 32)
                    if (
                        new_max > 0
                        and new_max < old_max
                        and params.get("allow_stale_entity_max_override")
                    ):
                        _LOGGER.debug(
                            "Solar surplus EV: ignoring Tesla entity max %dA below configured %dA",
                            new_max,
                            old_max,
                        )
                    elif (
                        new_max > 0
                        and new_max != old_max
                        and not params.get("allow_stale_entity_max_override")
                    ):
                        _LOGGER.info(
                            f"⚡ Solar surplus EV: Updated max_charge_amps {old_max}A -> {new_max}A "
                            f"(entity limit after charging started)"
                        )
                        params["max_charge_amps"] = new_max
        except Exception:
            pass
        state["entity_max_rechecked"] = True

    # Get live status
    live_status = await _get_tesla_live_status(hass, config_entry)
    if not live_status:
        _LOGGER.debug("Solar surplus EV: Could not get live status")
        return

    battery_soc = live_status.get("battery_soc") or 0

    # Battery priority check
    min_soc = get_solar_surplus_min_battery_soc(params)
    pause_soc = params.get("pause_below_soc", max(0, min_soc - 10))

    # Parallel charging parameters
    allow_parallel = params.get("allow_parallel_charging", False)
    max_battery_charge_kw = params.get("max_battery_charge_rate_kw", 5.0)

    # For parallel charging check, we need to calculate surplus early
    # Calculate current EV power for THIS vehicle
    voltage = params.get("voltage", 240)
    phases = params.get("phases", 1)
    current_amps = state.get("current_amps", 0)

    # Calculate TOTAL EV power from ALL active vehicles
    total_ev_power_kw = 0.0
    all_vehicles = _dynamic_ev_state.get(entry_id, {})
    active_vehicles = [
        (vid, v_state)
        for vid, v_state in all_vehicles.items()
        if v_state.get("active") and not v_state.get("paused")
    ]
    observed_current_power_kw = 0.0
    for vid, v_state in all_vehicles.items():
        if not v_state.get("active") or v_state.get("paused"):
            continue
        v_params = v_state.get("params", {})
        v_amps = v_state.get("current_amps", 0)
        v_voltage = v_params.get("voltage", 240)
        v_phases = v_params.get("phases", 1)
        commanded_power_kw = (v_amps * v_voltage * v_phases) / 1000
        observed_power_kw = await _get_observed_ev_power_kw(
            hass,
            vid,
            v_params,
            allow_wall_connector_fallback=len(active_vehicles) == 1,
        )
        if vid == vehicle_id:
            observed_current_power_kw = observed_power_kw
        total_ev_power_kw += max(commanded_power_kw, observed_power_kw)

    # Calculate available surplus after the household buffer and any configured
    # parallel battery reserve.
    raw_surplus_kw = _calculate_solar_surplus(live_status, total_ev_power_kw, params)

    # Check if parallel charging is possible below the battery floor. At this
    # point raw_surplus_kw is already the amount left for EV charging after
    # reserving max_battery_charge_kw for the battery.
    parallel_charging_available = (
        allow_parallel and
        battery_soc < min_soc and
        raw_surplus_kw > 0
    )

    # Don't start charging until battery reaches min_soc (unless parallel charging is available)
    if not state.get("charging_started"):
        if battery_soc < min_soc:
            if parallel_charging_available:
                state["paused"] = False
                state["paused_reason"] = None
                state["parallel_charging_mode"] = True
                _LOGGER.info(
                    f"⚡ Solar surplus EV: Parallel charging enabled - "
                    f"{raw_surplus_kw:.1f}kW remains after reserving "
                    f"{max_battery_charge_kw}kW for the battery (battery at {battery_soc:.0f}%)"
                )
            else:
                state["paused"] = True
                if allow_parallel:
                    state["paused_reason"] = (
                        f"Waiting for battery to reach {min_soc}% or surplus to remain after "
                        f"the {max_battery_charge_kw}kW battery reserve "
                        f"(currently {battery_soc:.0f}%, available {raw_surplus_kw:.1f}kW)"
                    )
                else:
                    state["paused_reason"] = f"Waiting for battery to reach {min_soc}% (currently {battery_soc:.0f}%)"
                _LOGGER.debug(f"Solar surplus EV: {state['paused_reason']}")
                return
        else:
            state["paused"] = False
            state["paused_reason"] = None
            state["parallel_charging_mode"] = False

    # Pause if battery drops below pause threshold (only in normal mode, not parallel)
    if state.get("charging_started") and battery_soc < pause_soc:
        # In parallel mode, pause once no EV surplus remains after battery reserve.
        if state.get("parallel_charging_mode"):
            if raw_surplus_kw <= 0:
                if not state.get("paused"):
                    state["paused"] = True
                    state["paused_reason"] = (
                        f"Parallel charging paused - no surplus remains after "
                        f"reserving {max_battery_charge_kw}kW for the battery"
                    )
                    _LOGGER.info(f"⚡ Solar surplus EV: {state['paused_reason']}")
                    await _set_vehicle_amps(hass, config_entry, vehicle_id, 0, params)
                    state["current_amps"] = 0
                return
        else:
            # Normal mode: pause below pause_soc threshold
            if not state.get("paused"):
                state["paused"] = True
                state["paused_reason"] = f"Battery dropped to {battery_soc:.0f}% (pause threshold: {pause_soc}%)"
                _LOGGER.info(f"⚡ Solar surplus EV: Pausing - {state['paused_reason']}")
                await _set_vehicle_amps(hass, config_entry, vehicle_id, 0, params)
                state["current_amps"] = 0

                # Send pause notification
                notify_on_error = params.get("notify_on_error", True)
                if notify_on_error:
                    try:
                        vehicle_name = params.get("vehicle_name")
                        if not vehicle_name and vehicle_id:
                            vehicle_name = _get_vehicle_name_from_vin(hass, vehicle_id)
                        if not vehicle_name:
                            vehicle_name = (vehicle_id[:8] if len(vehicle_id) > 8 else vehicle_id) if vehicle_id and vehicle_id != DEFAULT_VEHICLE_ID else "EV"
                        await _send_expo_push(
                            hass,
                            "EV Charging",
                            f"{vehicle_name} paused - battery low ({battery_soc:.0f}%)"
                        )
                    except Exception as e:
                        _LOGGER.debug(f"Could not send pause notification: {e}")
            return

    # Resume logic
    if state.get("paused"):
        stop_at_floor = params.get("stop_at_battery_floor", True)
        can_resume = False
        if battery_soc >= min_soc:
            # Battery recovered above start threshold - always resume
            can_resume = True
            state["parallel_charging_mode"] = False
            _LOGGER.info(f"⚡ Solar surplus EV: Resuming - battery at {battery_soc:.0f}%")
        elif stop_at_floor and battery_soc < pause_soc:
            # stop_at_battery_floor=True: don't resume until battery recovers above min_soc
            _LOGGER.debug(
                f"Solar surplus EV: Not resuming - stop_at_floor=True, "
                f"battery {battery_soc:.0f}% < floor {pause_soc}%"
            )
        elif state.get("parallel_charging_mode") and raw_surplus_kw > 0:
            # Parallel mode: surplus recovered
            can_resume = True
            _LOGGER.info(
                f"⚡ Solar surplus EV: Resuming parallel charging - "
                f"{raw_surplus_kw:.1f}kW remains after battery reserve"
            )

        if can_resume:
            was_paused = state.get("paused")
            state["paused"] = False
            state["paused_reason"] = None

            # Send resume notification
            if was_paused:
                notify_on_start = params.get("notify_on_start", True)
                if notify_on_start:
                    try:
                        vehicle_name = params.get("vehicle_name")
                        if not vehicle_name and vehicle_id:
                            vehicle_name = _get_vehicle_name_from_vin(hass, vehicle_id)
                        if not vehicle_name:
                            vehicle_name = (vehicle_id[:8] if len(vehicle_id) > 8 else vehicle_id) if vehicle_id and vehicle_id != DEFAULT_VEHICLE_ID else "EV"
                        await _send_expo_push(
                            hass,
                            "EV Charging",
                            f"{vehicle_name} resumed"
                        )
                    except Exception as e:
                        _LOGGER.debug(f"Could not send resume notification: {e}")

    # Calculate current EV power for THIS vehicle. Prefer measured charger
    # power so an already-active charge is treated as controllable load.
    current_ev_kw = max(
        (current_amps * voltage * phases) / 1000,
        observed_current_power_kw,
    )
    effective_current_amps = current_amps
    if current_amps <= 0 and observed_current_power_kw > 0.05:
        effective_current_amps = max(1, int(round((observed_current_power_kw * 1000) / (voltage * phases))))

    # Determine available surplus for EV. _calculate_solar_surplus already
    # applies the household buffer and any configured parallel battery reserve.
    if state.get("parallel_charging_mode") and battery_soc < min_soc:
        surplus_kw = raw_surplus_kw
        _LOGGER.debug(
            f"Solar surplus EV (parallel mode): available_after_battery_reserve={surplus_kw:.2f}kW"
        )
    else:
        surplus_kw = raw_surplus_kw

    # Apply dual vehicle distribution strategy
    strategy = params.get("dual_vehicle_strategy", "priority_first")
    my_surplus_kw = _distribute_surplus(entry_id, vehicle_id, surplus_kw, strategy)

    # Convert to amps (P = V × I × phases for AC charging)
    available_amps = (my_surplus_kw * 1000) / (voltage * phases)

    # Apply constraints
    min_amps = _effective_min_charge_amps(params)
    max_amps = params.get("max_charge_amps", 32)
    new_amps = int(round(max(0, min(max_amps, available_amps))))

    # Hysteresis: don't start unless we have sustained surplus
    sustained_minutes = params.get("sustained_surplus_minutes", 2)
    stop_delay_minutes = params.get("stop_delay_minutes", 5)

    if new_amps < min_amps:
        # Not enough surplus
        if effective_current_amps > 0:
            # Track how long we've been below threshold
            low_surplus_start = state.get("low_surplus_start")
            if low_surplus_start is None:
                state["low_surplus_start"] = datetime.now()
                new_amps = effective_current_amps
            elif (datetime.now() - low_surplus_start).total_seconds() >= stop_delay_minutes * 60:
                # Stop charging after delay
                _LOGGER.info(f"⚡ Solar surplus EV: Stopping - insufficient surplus for {stop_delay_minutes} min")
                new_amps = 0
            else:
                # Keep current amps during grace period
                new_amps = effective_current_amps
        else:
            new_amps = 0
    else:
        # Sufficient surplus - reset low surplus timer
        state["low_surplus_start"] = None

        if effective_current_amps == 0:
            # Track how long we've had surplus before starting
            high_surplus_start = state.get("high_surplus_start")
            if high_surplus_start is None:
                state["high_surplus_start"] = datetime.now()
                new_amps = 0  # Don't set amps yet, wait for sustained surplus
            elif (datetime.now() - high_surplus_start).total_seconds() >= sustained_minutes * 60:
                # Start charging after sustained surplus
                _LOGGER.info(f"⚡ Solar surplus EV: Starting - sustained surplus for {sustained_minutes} min")

                # Check if this vehicle's charge is already complete before trying
                if _is_vehicle_charge_complete(hass, vehicle_id):
                    _LOGGER.info(
                        f"⚡ Solar surplus EV: {vehicle_id[:8]}... is charge complete — "
                        f"looking for next vehicle"
                    )
                    await _solar_surplus_switch_to_next_vehicle(
                        hass, config_entry, entry_id, vehicle_id, params
                    )
                    return

                # Send the actual start-charging command to the vehicle
                start_success = await _action_start_ev_charging(hass, config_entry, params, context=None)
                if not start_success:
                    # Check if failure was due to charge complete
                    if _is_vehicle_charge_complete(hass, vehicle_id):
                        _LOGGER.info(
                            f"⚡ Solar surplus EV: {vehicle_id[:8]}... charge complete — "
                            f"looking for next vehicle"
                        )
                        await _solar_surplus_switch_to_next_vehicle(
                            hass, config_entry, entry_id, vehicle_id, params
                        )
                    else:
                        _LOGGER.warning("⚡ Solar surplus EV: Failed to send start charging command")
                    return

                state["charging_started"] = True

                # Send notification when solar charging actually begins
                notify_on_start = params.get("notify_on_start", True)
                if notify_on_start:
                    try:
                        vehicle_name = params.get("vehicle_name")
                        if not vehicle_name and vehicle_id:
                            vehicle_name = _get_vehicle_name_from_vin(hass, vehicle_id)
                        if not vehicle_name:
                            vehicle_name = (vehicle_id[:8] if len(vehicle_id) > 8 else vehicle_id) if vehicle_id and vehicle_id != DEFAULT_VEHICLE_ID else "EV"
                        await _send_expo_push(
                            hass,
                            "EV Charging",
                            f"{vehicle_name} started - {surplus_kw:.1f}kW solar"
                        )
                    except Exception as e:
                        _LOGGER.debug(f"Could not send solar start notification: {e}")
            else:
                # Don't start yet, waiting for sustained surplus
                new_amps = 0
        else:
            state["high_surplus_start"] = None

    # Store decision reason
    state["allocated_surplus_kw"] = my_surplus_kw
    state["reason"] = (
        f"Surplus: {surplus_kw:.1f}kW, Allocated: {my_surplus_kw:.1f}kW, "
        f"Battery: {battery_soc:.0f}%, Target: {new_amps}A"
    )

    # Update charging session with current power reading
    if effective_current_amps > 0 and not _session_energy_tracked_by_charger_poll(params):
        try:
            from .ev_charging_session import get_session_manager
            session_manager = get_session_manager()
            if session_manager:
                # Determine if we're charging from solar using grid import
                grid_power_kw = (live_status.get("grid_power") or 0) / 1000
                is_solar = _is_ev_charging_from_solar(grid_power_kw, current_ev_kw)

                import_price, export_price = _get_current_ev_prices(hass, config_entry.entry_id)

                await session_manager.update_session(
                    vehicle_id=vehicle_id,
                    power_kw=current_ev_kw,
                    amps=effective_current_amps,
                    is_solar=is_solar,
                    import_price_cents=import_price,
                    export_price_cents=export_price,
                    battery_soc=int(battery_soc) if battery_soc else None,
                )
        except Exception as e:
            _LOGGER.debug(f"Could not update session: {e}")

    # Only update if change is significant (>= 1 amp)
    if abs(new_amps - effective_current_amps) >= 1:
        _LOGGER.info(
            f"⚡ Solar surplus EV: {effective_current_amps}A -> {new_amps}A "
            f"(surplus={my_surplus_kw:.1f}kW, battery={battery_soc:.0f}%)"
        )
        success = await _set_vehicle_amps(hass, config_entry, vehicle_id, new_amps, params)
        if success:
            state["current_amps"] = new_amps
            state["target_amps"] = new_amps

            # End session when transitioning to 0 amps (stopping charging)
            if new_amps == 0 and effective_current_amps > 0:
                try:
                    from .ev_charging_session import get_session_manager
                    session_manager = get_session_manager()
                    if session_manager:
                        reason = "insufficient_surplus"
                        if state.get("paused"):
                            reason = "battery_low"
                        await session_manager.end_session(
                            vehicle_id=vehicle_id,
                            reason=reason,
                            end_soc=int(battery_soc) if battery_soc else None,
                        )
                        _LOGGER.debug(f"Solar surplus EV: Ended session for {vehicle_id} ({reason})")
                except Exception as e:
                    _LOGGER.debug(f"Could not end session: {e}")


def _get_home_power_settings(hass, config_entry) -> dict:
    """Return app-managed home power settings from automation storage."""
    try:
        from ..const import DOMAIN
        entry_data = hass.data.get(DOMAIN, {}).get(config_entry.entry_id, {})
        store = entry_data.get("automation_store")
        if store:
            stored = getattr(store, '_data', {}) or {}
            settings = stored.get("home_power_settings", {})
            if isinstance(settings, dict):
                return settings
    except Exception:
        pass
    return {}


def _get_home_power_max_charge_amps(hass, config_entry) -> Optional[int]:
    """Return configured per-phase max charger speed from Home Power settings."""
    settings = _get_home_power_settings(hass, config_entry)
    if not settings.get("max_charge_speed_enabled"):
        return None
    return _coerce_positive_int(settings.get("max_amps_per_phase"))


def _resolve_dynamic_max_charge_amps(
    hass,
    config_entry,
    params: dict,
    default: int = 32,
) -> tuple[int, str]:
    """Resolve dynamic EV max amps, giving Home Power max-speed priority."""
    home_max = _get_home_power_max_charge_amps(hass, config_entry)
    if home_max is not None:
        return home_max, "home_power"

    source = params.get("max_charge_amps_source")
    configured = _coerce_positive_int(params.get("max_charge_amps"), default)
    if source:
        return configured or default, str(source)
    if "max_charge_amps" in params:
        return configured or default, "params"
    return default, "default"


def _get_phases_from_config(hass, config_entry, params):
    """Get charging phases: from params, or fall back to home_power_settings."""
    if "phases" in params and params["phases"] in (1, 3):
        return params["phases"]
    settings = _get_home_power_settings(hass, config_entry)
    if settings:
        phase_type = settings.get("phase_type", "single")
        return 3 if phase_type == "three" else 1
    return 1


async def _dynamic_ev_update(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    entry_id: str,
    vehicle_id: str = DEFAULT_VEHICLE_ID,
) -> None:
    """Periodic update function for dynamic EV charging.

    Supports two modes:
    - battery_target: Maintains a target battery charge rate (e.g., 5kW into battery)
    - solar_surplus: Only charges EV when there's excess solar

    Battery power convention: Positive = discharging, Negative = charging
    Grid power convention: Positive = importing, Negative = exporting
    """
    vehicles = _dynamic_ev_state.get(entry_id, {})
    state = vehicles.get(vehicle_id)
    if not state or not state.get("active"):
        return

    params = state.get("params", {})

    # Check which mode we're in
    mode = params.get("dynamic_mode", "battery_target")
    if mode == "solar_surplus":
        await _dynamic_ev_update_surplus(hass, config_entry, entry_id, vehicle_id)
        return

    if await _clear_ble_dynamic_session_if_unplugged(
        hass, config_entry, vehicle_id, params
    ):
        return

    # Check time window if stop_outside_window is enabled
    stop_outside_window = params.get("stop_outside_window", False)
    if stop_outside_window:
        time_window_start = params.get("time_window_start")
        time_window_end = params.get("time_window_end")
        timezone = params.get("timezone", "UTC")

        if time_window_start and time_window_end:
            if not _is_inside_time_window(time_window_start, time_window_end, timezone):
                _LOGGER.info("⏰ Dynamic EV: Outside time window, stopping charging")
                await _action_stop_ev_charging_dynamic(hass, config_entry, {"stop_charging": True})
                # Send notification that charging stopped
                await _send_expo_push(hass, "EV Charging", "Stopped - time window ended")
                return

    # target_battery_charge_kw: How much we want the battery to charge (positive = charging into battery)
    # e.g., 5.0 means we want 5kW going INTO the battery
    target_battery_charge_kw = params.get("target_battery_charge_kw", 5.0)
    max_grid_import_kw = params.get("max_grid_import_kw", 12.5)

    # No grid import mode: prevent ALL grid imports by dynamically adjusting EV charge rate
    no_grid_import = params.get("no_grid_import", False)
    grid_import_tolerance_kw = params.get("grid_import_tolerance_kw", 0.1)  # 100W buffer
    max_inverter_kw = params.get("max_inverter_kw", 10.0)  # PW3=10kW, PW2=5kW per unit

    min_amps = params.get("min_charge_amps", 5)
    max_amps = params.get("max_charge_amps", 32)
    voltage = params.get("voltage", 240)
    phases = params.get("phases", 1)
    fixed_charge_amps = _coerce_positive_int(params.get("fixed_charge_amps"))

    # Hard inverter cap: even if reactive logic miscalculates, amps never exceed inverter capacity
    # Save original max_amps — restored later if battery depletes and grid takes over
    uncapped_max_amps = max_amps
    if no_grid_import:
        inverter_max_amps = int((max_inverter_kw * 1000) / (voltage * phases))
        if max_amps > inverter_max_amps:
            _LOGGER.debug(
                f"Dynamic EV: Capping max_amps {max_amps}A → {inverter_max_amps}A "
                f"(inverter={max_inverter_kw}kW, {phases}-phase)"
            )
            max_amps = inverter_max_amps

    current_amps = state.get("current_amps", max_amps)

    if fixed_charge_amps:
        fixed_amps = max(min_amps, min(max_amps, fixed_charge_amps))
        state["target_amps"] = fixed_amps
        if abs(fixed_amps - current_amps) >= 1:
            _LOGGER.info(
                f"⚡ Dynamic EV: Holding fixed charge rate {fixed_amps}A "
                f"(current={current_amps}A)"
            )
            success = await _set_vehicle_amps(hass, config_entry, vehicle_id, fixed_amps, params)
            if success:
                state["current_amps"] = fixed_amps
            else:
                _LOGGER.warning(f"Dynamic EV: Failed to set fixed amps to {fixed_amps}A")
        return

    # Get live status
    live_status = await _get_tesla_live_status(hass, config_entry)
    if not live_status:
        _LOGGER.debug("Dynamic EV: Could not get live status, keeping current amps")
        return

    # Convert to kW for cleaner math (matching HA automation)
    # battery_power: Positive = discharging, Negative = charging
    battery_power_kw = (live_status.get("battery_power", 0) or 0) / 1000
    grid_power_kw = (live_status.get("grid_power", 0) or 0) / 1000
    current_ev_power_kw = (current_amps * voltage * phases) / 1000
    battery_soc = live_status.get("battery_soc", 0) or 0

    # Target battery power in same convention (negative = charging)
    # If target_battery_charge_kw = 5, we want battery_power = -5 kW
    target_battery_power_kw = -target_battery_charge_kw

    # When battery is full (>=97%), it tapers charge rate naturally.
    # Don't treat this taper as a "deficit" — the battery isn't failing to charge,
    # it's done. Use grid headroom directly instead of penalizing the EV.
    battery_full = battery_soc >= 97.0

    # Battery deficit: How much more the battery should be charging
    # Positive deficit = battery is charging MORE than target (surplus available for EV)
    # Negative deficit = battery isn't meeting charge target
    battery_deficit_kw = target_battery_power_kw - battery_power_kw

    # Grid headroom: How much more we could import before hitting limit
    grid_headroom_kw = max_grid_import_kw - grid_power_kw

    # Available power for EV adjustment:
    # - In no_grid_import mode: limit to inverter capacity and prevent grid imports
    #   UNLESS battery has depleted (stopped discharging) — then allow grid import
    # - If battery has surplus (deficit > 0.1), use that surplus
    # - Otherwise, use grid headroom
    battery_depleted = False  # Track whether we bypassed no_grid_import due to battery depletion
    if no_grid_import:
        # Exclude intentional home battery grid-charging from the grid import figure.
        # When the LP optimizer force-charges the home battery from grid, battery_power_kw
        # is negative (charging) and grid_power_kw includes that draw.  The EV should not
        # be throttled because of intentional battery charging — only because of the EV's
        # own grid draw and household load.
        battery_charging_kw = max(0.0, -battery_power_kw)  # positive when battery is charging
        ev_relevant_grid_kw = grid_power_kw - battery_charging_kw

        # Check if battery has effectively depleted (stopped discharging).
        # When battery is not providing power (hit backup_reserve or LP set IDLE),
        # allow grid import — the battery can't supply EV power anymore.
        if battery_power_kw <= 0.1 and current_amps > 0:
            battery_depleted = True
            if not state.get("_battery_depleted_logged"):
                _LOGGER.info(
                    f"⚡ No-grid-import: Battery not discharging ({battery_power_kw:.1f}kW), "
                    f"allowing grid import for EV charging"
                )
                state["_battery_depleted_logged"] = True
            # Battery depleted — use grid headroom directly (ignore inverter cap)
            max_amps = uncapped_max_amps  # Remove inverter cap, charge at full charger rate
            available_power_kw = grid_headroom_kw
        else:
            # Battery is discharging — clear depleted flag if it was set
            if state.get("_battery_depleted_logged"):
                _LOGGER.info(
                    f"⚡ No-grid-import: Battery discharging again ({battery_power_kw:.1f}kW), "
                    f"re-engaging inverter capacity limit"
                )
                state["_battery_depleted_logged"] = False

            # Calculate home load using Tesla API's load_power (total behind-the-meter consumption)
            # load_power includes home + EV + everything; subtract EV estimate for home-only load
            load_power_kw = (live_status.get("load_power", 0) or 0) / 1000
            home_load_kw = max(0, load_power_kw - current_ev_power_kw)

            # Max power available from inverter for EV = inverter_capacity - home_load
            # This is proactive: we know the limit before hitting it
            inverter_headroom_kw = max_inverter_kw - max(0, home_load_kw)

            # Reactive adjustment based on current grid state (excluding battery charging)
            # ev_relevant_grid_kw: positive = importing for home+EV (bad), negative = exporting (good)
            grid_reactive_kw = -(ev_relevant_grid_kw + grid_import_tolerance_kw)

            # Use the more conservative of the two approaches
            # - inverter_headroom: proactive limit based on known capacity
            # - grid_reactive: reactive adjustment based on actual grid flow
            available_power_kw = min(inverter_headroom_kw, grid_reactive_kw + current_ev_power_kw) - current_ev_power_kw
    elif battery_full:
        # Battery is full — taper is natural, not a deficit. Use grid headroom directly.
        available_power_kw = grid_headroom_kw
    elif battery_deficit_kw > 0.1:
        # Battery has surplus beyond target — available for EV
        available_power_kw = battery_deficit_kw
    elif battery_deficit_kw < -0.2:
        # Battery is NOT meeting its charge target — EV is consuming too much.
        # Reduce EV amps by accounting for both the battery shortfall and grid headroom.
        # deficit is negative (e.g. -1.4kW means battery needs 1.4kW more),
        # grid_headroom is positive (e.g. 0.2kW still available on grid).
        # Net: if deficit=-1.4 and headroom=0.2, available = -1.4 + 0.2 = -1.2kW → reduce EV
        available_power_kw = battery_deficit_kw + grid_headroom_kw
    else:
        available_power_kw = grid_headroom_kw

    # Convert available power to amps (P = V × I × phases for AC charging)
    available_amps = (available_power_kw * 1000) / (voltage * phases)

    # Calculate new target amps
    raw_new_amps = current_amps + available_amps
    new_amps = int(round(max(min_amps, min(max_amps, raw_new_amps))))

    # Clamp to 0 if below minimum (stop charging)
    if new_amps < min_amps:
        new_amps = 0

    # In no_grid_import mode, respond immediately to grid imports (don't wait for 1A threshold)
    # Use ev_relevant_grid_kw (excludes battery charging) to avoid throttling due to
    # intentional home battery grid-charging by the LP optimizer
    # Skip this check if battery has depleted — grid import is expected in that case
    ev_grid_check = ev_relevant_grid_kw if no_grid_import else grid_power_kw
    if no_grid_import and not battery_depleted and ev_grid_check > grid_import_tolerance_kw:
        # We're importing (beyond battery charging) - reduce aggressively
        if new_amps < current_amps:
            _LOGGER.info(
                f"⚡ No-grid-import: Grid importing {grid_power_kw:.2f}kW "
                f"(battery_charging={battery_charging_kw:.2f}kW, "
                f"ev_relevant={ev_grid_check:.2f}kW, inverter_max={max_inverter_kw}kW), "
                f"reducing to {new_amps}A"
            )
            success = await _set_vehicle_amps(hass, config_entry, vehicle_id, new_amps, params)
            if success:
                state["current_amps"] = new_amps
            return

    _LOGGER.debug(
        f"Dynamic EV: battery={battery_power_kw:.1f}kW (target={target_battery_power_kw:.1f}kW), "
        f"deficit={battery_deficit_kw:.1f}kW, grid={grid_power_kw:.1f}kW (max={max_grid_import_kw:.1f}kW), "
        f"headroom={grid_headroom_kw:.1f}kW, available={available_power_kw:.1f}kW, "
        f"current={current_amps}A, target={new_amps}A, no_grid_import={no_grid_import}"
        f"{', battery_depleted=True' if battery_depleted else ''}"
        f"{', battery_full=True' if battery_full else ''}"
    )

    # Only update if change is >= 1 amp (avoid constant micro-adjustments)
    if abs(new_amps - current_amps) >= 1:
        _LOGGER.info(
            f"⚡ Dynamic EV: Adjusting from {current_amps}A to {new_amps}A "
            f"(battery={battery_power_kw:.1f}kW, grid={grid_power_kw:.1f}kW, "
            f"available={available_power_kw:.1f}kW)"
        )
        success = await _set_vehicle_amps(hass, config_entry, vehicle_id, new_amps, params)
        if success:
            state["current_amps"] = new_amps
        else:
            _LOGGER.warning(f"Dynamic EV: Failed to set amps to {new_amps}A")

    # Update session tracking (battery target mode)
    try:
        from ..const import DOMAIN
        from .ev_charging_session import get_session_manager
        session_manager = get_session_manager()
        if session_manager and not _session_energy_tracked_by_charger_poll(params):
            if current_amps > 0 and new_amps > 0:
                # Session update - still charging
                is_solar = _is_ev_charging_from_solar(grid_power_kw, current_ev_power_kw)
                import_price, export_price = _get_current_ev_prices(hass, entry_id)

                await session_manager.update_session(
                    vehicle_id=vehicle_id,
                    power_kw=current_ev_power_kw,
                    amps=current_amps,
                    is_solar=is_solar,
                    import_price_cents=import_price,
                    export_price_cents=export_price,
                )
            elif current_amps > 0 and new_amps == 0:
                # Session end - charging stopped
                live_soc = live_status.get("battery_soc")
                end_soc = int(live_soc) if live_soc else None
                await session_manager.end_session(
                    vehicle_id=vehicle_id,
                    reason="battery_target_stop",
                    end_soc=end_soc,
                )
                _LOGGER.debug(f"Dynamic EV: Ended session for {vehicle_id}")
    except Exception as e:
        _LOGGER.debug(f"Dynamic EV: Session tracking failed: {e}")


async def _action_start_ev_charging_dynamic(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    params: Dict[str, Any],
    context: Optional[Dict[str, Any]] = None
) -> bool:
    """
    Start dynamic EV charging that adjusts charge rate based on battery/grid or solar surplus.

    Supports two modes:
    - battery_target: Maintains target battery charge rate while EV charges
    - solar_surplus: Only charges EV when there's excess solar (battery priority)

    Parameters (battery_target mode):
        target_battery_charge_kw: Target battery charge rate in kW (default 5.0)
        max_grid_import_kw: Max grid import allowed (default 12.5)
        no_grid_import: If True, prevent ALL grid imports by dynamically adjusting
            EV charge rate. Useful for overnight Powerwall-to-EV charging. (default False)
        grid_import_tolerance_kw: Small buffer to prevent oscillation (default 0.1 = 100W)
        max_inverter_kw: Maximum inverter output capacity in kW (default 10.0 = single PW3).
            PW2: 5kW per unit, PW3: 10kW per unit. Used with no_grid_import to proactively
            limit EV charging based on available inverter headroom.

    Parameters (solar_surplus mode):
        household_buffer_kw: Buffer to keep from surplus (default 0.5)
        surplus_calculation: Method - "grid_based" or "direct" (default grid_based)
        min_battery_soc: Don't start/continue until home battery >= this % (default 80)
        pause_below_soc: Pause EV charging if home battery drops below this % (default 70)
        sustained_surplus_minutes: Wait time before starting (default 2)
        stop_delay_minutes: Wait time before stopping (default 5)
        dual_vehicle_strategy: "even", "priority_first", "priority_only" (default priority_first)
        allow_parallel_charging: Allow EV charging while battery is still charging if surplus
            exceeds max_battery_charge_rate_kw (default False)
        max_battery_charge_rate_kw: Maximum charge rate of your battery system in kW (default 5.0).
            Single PW2/PW3 = 5kW, dual = 10kW, triple = 15kW. When allow_parallel_charging is
            enabled, EV charging starts when solar exceeds this rate, even if battery isn't full.

    Common parameters:
        dynamic_mode: "battery_target" or "solar_surplus" (default battery_target)
        owner_mode: Business mode that owns the session, e.g. smart_schedule,
            price_level_recovery, scheduled, solar_surplus. Defaults to dynamic_mode.
        min_charge_amps: Minimum EV charge amps (default 5)
        max_charge_amps: Maximum EV charge amps (default 32)
        voltage: Assumed charging voltage (default 240)
        stop_outside_window: Stop when outside time window (default False)
        vehicle_vin: Optional VIN to filter by specific vehicle
        charger_type: "tesla", "ocpp", "generic", or "zaptec" (default tesla)
        priority: Vehicle priority for dual-vehicle setups (default 1)
    """
    from ..const import DOMAIN

    async with _start_dynamic_lock:
        return await _action_start_ev_charging_dynamic_locked(
            hass, config_entry, params, context
        )


async def _action_start_ev_charging_dynamic_locked(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    params: Dict[str, Any],
    context: Optional[Dict[str, Any]] = None,
) -> bool:
    """Inner implementation of dynamic EV charging start (caller holds _start_dynamic_lock)."""
    from ..const import DOMAIN

    entry_id = config_entry.entry_id
    vehicle_id = params.get("vehicle_vin") or params.get("vehicle_id") or DEFAULT_VEHICLE_ID

    # Determine mode
    dynamic_mode = params.get("dynamic_mode", "battery_target")
    owner_mode = params.get("owner_mode", dynamic_mode)
    allow_takeover = bool(params.get("allow_ownership_takeover", False))

    from .ev_ownership import (
        can_claim_ev_ownership,
        can_take_over_ev_ownership,
        claim_ev_ownership,
        record_ev_command,
    )

    entry_vehicles = _dynamic_ev_state.get(entry_id, {})

    def _same_loadpoint(candidate_id: str) -> bool:
        return (
            candidate_id == vehicle_id
            or candidate_id == DEFAULT_VEHICLE_ID
            or vehicle_id == DEFAULT_VEHICLE_ID
        )

    allowed, _lease_id, _lease, block_reason = can_claim_ev_ownership(
        hass,
        config_entry,
        vehicle_id,
        owner_mode=owner_mode,
        allow_takeover=allow_takeover,
    )
    if not allowed:
        reason = block_reason or "another EV mode owns this loadpoint"
        _LOGGER.info(
            "Dynamic EV: %s start blocked for %s because %s",
            owner_mode,
            vehicle_id,
            reason,
        )
        record_ev_command(
            hass,
            config_entry,
            vehicle_id,
            command=f"start_{owner_mode}",
            success=False,
            reason=reason,
        )
        return False

    # Legacy fallback for runtime state created before explicit ownership was
    # claimed. This keeps old dynamic sessions from being hijacked by another
    # automated mode during an in-place upgrade.
    for vid, v_state in entry_vehicles.items():
        if not v_state.get("active") or not _same_loadpoint(vid):
            continue
        existing_params = v_state.get("params") or {}
        existing_owner_mode = (
            existing_params.get("owner_mode")
            or existing_params.get("dynamic_mode")
            or "dynamic"
        )
        if not can_take_over_ev_ownership(
            existing_owner_mode,
            owner_mode,
            allow_takeover=allow_takeover,
        ):
            reason = f"{existing_owner_mode} already owns this loadpoint"
            _LOGGER.info(
                "Dynamic EV: %s start blocked for %s because legacy state says %s",
                owner_mode,
                vehicle_id,
                reason,
            )
            record_ev_command(
                hass,
                config_entry,
                vehicle_id,
                command=f"start_{owner_mode}",
                success=False,
                reason=reason,
            )
            return False

    # Prevent duplicate sessions for the same vehicle/mode
    # _default and a resolved VIN (e.g. LRWYHCEK3PC907290) refer to the same physical
    # vehicle in single-vehicle setups. Treat them as duplicates to prevent two update
    # loops fighting over the same car's charge current.
    for vid, v_state in entry_vehicles.items():
        if (
            v_state.get("active")
            and _same_loadpoint(vid)
            and v_state.get("params", {}).get("dynamic_mode") == dynamic_mode
        ):
            if vid == vehicle_id:
                existing_params = v_state.setdefault("params", {})
                existing_owner_mode = (
                    existing_params.get("owner_mode")
                    or existing_params.get("dynamic_mode")
                    or dynamic_mode
                )
                if can_take_over_ev_ownership(
                    existing_owner_mode,
                    owner_mode,
                    allow_takeover=allow_takeover,
                ):
                    existing_params["owner_mode"] = owner_mode
                    v_state["ownership"] = claim_ev_ownership(
                        hass,
                        config_entry,
                        vehicle_id,
                        owner_mode=owner_mode,
                        session_id=v_state.get("session_id"),
                        reason=v_state.get("reason") or None,
                        command=f"update_{owner_mode}",
                        extra={"charger_type": existing_params.get("charger_type", "tesla")},
                    )
                _LOGGER.debug(f"Dynamic session ({dynamic_mode}) already active for vehicle {vid}, skipping duplicate")
                return True
            # _default overlaps with any VIN (single-vehicle setup)
            if vid == DEFAULT_VEHICLE_ID or vehicle_id == DEFAULT_VEHICLE_ID:
                existing_params = v_state.setdefault("params", {})
                existing_owner_mode = (
                    existing_params.get("owner_mode")
                    or existing_params.get("dynamic_mode")
                    or dynamic_mode
                )
                if can_take_over_ev_ownership(
                    existing_owner_mode,
                    owner_mode,
                    allow_takeover=allow_takeover,
                ):
                    existing_params["owner_mode"] = owner_mode
                    v_state["ownership"] = claim_ev_ownership(
                        hass,
                        config_entry,
                        vid,
                        owner_mode=owner_mode,
                        session_id=v_state.get("session_id"),
                        reason=v_state.get("reason") or None,
                        command=f"update_{owner_mode}",
                        extra={"charger_type": existing_params.get("charger_type", "tesla")},
                    )
                _LOGGER.info(
                    f"Dynamic session ({dynamic_mode}) already active for {vid}, "
                    f"skipping duplicate start for {vehicle_id}"
                )
                return True

    # Get common parameters with defaults
    min_charge_amps = _effective_min_charge_amps(params)
    max_charge_amps, max_charge_amps_source = _resolve_dynamic_max_charge_amps(
        hass,
        config_entry,
        params,
    )
    voltage = params.get("voltage", 240)
    stop_outside_window = params.get("stop_outside_window", False)
    charger_type = params.get("charger_type", "tesla")
    priority = params.get("priority", 1)
    allow_stale_entity_max_override = bool(
        params.get("allow_stale_entity_max_override")
    )
    if dynamic_mode == "solar_surplus" and max_charge_amps_source != "default":
        allow_stale_entity_max_override = True

    # Resolve phases early for logging and start_amps capping
    resolved_phases = _get_phases_from_config(hass, config_entry, params)

    # Mode-specific parameters
    if dynamic_mode == "solar_surplus":
        min_battery_soc = get_solar_surplus_min_battery_soc(params)
        mode_params = {
            "household_buffer_kw": params.get("household_buffer_kw", 0.5),
            "surplus_calculation": params.get("surplus_calculation", "grid_based"),
            "min_battery_soc": min_battery_soc,
            "pause_below_soc": params.get("pause_below_soc", max(0, min_battery_soc - 10)),
            "sustained_surplus_minutes": params.get("sustained_surplus_minutes", 2),
            "stop_delay_minutes": params.get("stop_delay_minutes", 5),
            "dual_vehicle_strategy": params.get("dual_vehicle_strategy", "priority_first"),
            "allow_parallel_charging": params.get("allow_parallel_charging", False),
            "max_battery_charge_rate_kw": params.get("max_battery_charge_rate_kw", 5.0),
        }
        start_amps = 0  # Don't start immediately in solar surplus mode
        parallel_info = ""
        if mode_params["allow_parallel_charging"]:
            parallel_info = f", parallel_charging=enabled (battery_max={mode_params['max_battery_charge_rate_kw']}kW)"
        _LOGGER.info(
            f"⚡ Starting solar surplus EV charging: buffer={mode_params['household_buffer_kw']}kW, "
            f"min_soc={mode_params['min_battery_soc']}%, amps={min_charge_amps}-{max_charge_amps}A, "
            f"phases={resolved_phases}{parallel_info}"
        )
    else:
        # Battery target mode (existing behavior)
        target_battery_charge_kw = params.get(
            "target_battery_charge_kw",
            params.get("max_battery_discharge_kw", 5.0)  # Fallback for old param name
        )
        no_grid_import = params.get("no_grid_import", False)
        mode_params = {
            "target_battery_charge_kw": target_battery_charge_kw,
            "max_grid_import_kw": params.get("max_grid_import_kw", 12.5),
            "no_grid_import": no_grid_import,
            "grid_import_tolerance_kw": params.get("grid_import_tolerance_kw", 0.1),
            "max_inverter_kw": params.get("max_inverter_kw", 10.0),
            "fixed_charge_amps": params.get("fixed_charge_amps"),
        }
        start_amps = params.get("start_amps", max_charge_amps)
        if no_grid_import:
            inverter_max_amps = int((mode_params["max_inverter_kw"] * 1000) / (voltage * resolved_phases))
            start_amps = min(start_amps, inverter_max_amps)
        no_grid_info = f", no_grid_import=enabled (inverter={mode_params['max_inverter_kw']}kW)" if no_grid_import else ""
        _LOGGER.info(
            f"⚡ Starting dynamic EV charging: target_battery_charge={target_battery_charge_kw}kW, "
            f"max_grid_import={mode_params['max_grid_import_kw']}kW, amps={min_charge_amps}-{max_charge_amps}A, "
            f"phases={resolved_phases}{no_grid_info}"
        )

    # Get time window from context (passed from automation trigger)
    time_window_start = context.get("time_window_start") if context else None
    time_window_end = context.get("time_window_end") if context else None
    timezone = context.get("timezone", "UTC") if context else "UTC"

    # Stop any existing dynamic charging for this vehicle
    await _action_stop_ev_charging_dynamic(hass, config_entry, {"vehicle_id": vehicle_id})

    # For battery_target mode, start EV charging immediately
    # For solar_surplus mode, we wait for sufficient surplus before starting
    if dynamic_mode == "battery_target":
        if charger_type in ("ocpp", "generic", "zaptec") or _is_ha_native_charger_type(charger_type):
            start_success = await _set_vehicle_amps(
                hass, config_entry, vehicle_id, start_amps, params
            )
            if not start_success:
                _LOGGER.info(
                    "Dynamic EV: Could not set charger to %sA and start %s charging",
                    start_amps,
                    charger_type,
                )
                record_ev_command(
                    hass,
                    config_entry,
                    vehicle_id,
                    command=f"start_{owner_mode}",
                    success=False,
                    reason="current limit or physical start failed",
                )
                return False
        else:
            start_success = await _action_start_ev_charging(hass, config_entry, params, context)
            if not start_success:
                _LOGGER.info("Dynamic EV: Could not start EV charging (vehicle may be disconnected)")
                record_ev_command(
                    hass,
                    config_entry,
                    vehicle_id,
                    command=f"start_{owner_mode}",
                    success=False,
                    reason="physical start failed",
                )
                return False

            # Set initial amps through the charger abstraction so OCPP and generic
            # chargers do not fall back to the Tesla-only amperage path.
            amps_success = await _set_vehicle_amps(
                hass, config_entry, vehicle_id, start_amps, params
            )
            if not amps_success:
                # This is expected - Tesla reports lower max amps until charging actually starts
                _LOGGER.debug(f"Dynamic EV: Could not set initial amps to {start_amps}A (will adjust once charging starts)")

    # Create the periodic update callback for this vehicle
    async def periodic_update(now) -> None:
        await _dynamic_ev_update(hass, config_entry, entry_id, vehicle_id)

    # Use faster update interval for BLE (no API rate limits) vs Fleet API
    ev_config = _get_ev_config(config_entry)
    ble_prefix = ev_config.get("ble_prefix", "")
    use_ble = _is_ble_available(hass, ble_prefix) if ble_prefix else False
    tbt_prefix = _resolve_teslemetry_bt_prefix(hass)
    use_tbt = _is_teslemetry_bt_available(hass, tbt_prefix)
    use_bt = use_ble or use_tbt
    update_interval = 10 if use_bt else 30
    _LOGGER.debug(f"Dynamic EV update interval: {update_interval}s (BLE={use_ble}, teslemetry_bt={use_tbt})")

    cancel_timer = async_track_time_interval(
        hass,
        periodic_update,
        timedelta(seconds=update_interval),
    )

    # Build full params dict
    full_params = {
        "dynamic_mode": dynamic_mode,
        "owner_mode": owner_mode,
        "vehicle_vin": params.get("vehicle_vin"),
        "vehicle_name": params.get("vehicle_name"),
        "min_charge_amps": min_charge_amps,
        "max_charge_amps": max_charge_amps,
        "max_charge_amps_source": max_charge_amps_source,
        "allow_stale_entity_max_override": allow_stale_entity_max_override,
        "voltage": voltage,
        "phases": _get_phases_from_config(hass, config_entry, params),
        "stop_outside_window": stop_outside_window,
        "time_window_start": time_window_start,
        "time_window_end": time_window_end,
        "timezone": timezone,
        "charger_type": charger_type,
        **mode_params,
        # Pass through generic charger entities if present
        "charger_switch_entity": params.get("charger_switch_entity"),
        "charger_amps_entity": params.get("charger_amps_entity"),
        "charger_status_entity": params.get("charger_status_entity"),
        "charger_power_entity": params.get("charger_power_entity"),
        "ocpp_charger_id": params.get("ocpp_charger_id"),
    }

    # Initialize entry-level state dict if needed
    if entry_id not in _dynamic_ev_state:
        _dynamic_ev_state[entry_id] = {}

    # Store vehicle-specific state
    # Read entity max for Tesla chargers to avoid over-reporting amps
    # Note: Tesla reports max=16A when car is idle; real max (e.g. 32A for wall connector)
    # only appears once charging starts. We apply a preliminary cap here and re-check later.
    if charger_type == "tesla" and vehicle_id != DEFAULT_VEHICLE_ID:
        try:
            entity = await _get_tesla_ev_entity(
                hass, r"number\..*(charging_amps|charge_current)$", vehicle_id
            )
            if entity:
                entity_state = hass.states.get(entity)
                if entity_state:
                    entity_max = int(entity_state.attributes.get("max", max_charge_amps))
                    if (
                        entity_max < full_params.get("max_charge_amps", 32)
                        and full_params.get("allow_stale_entity_max_override")
                    ):
                        _LOGGER.debug(
                            "Skipping Tesla idle entity max cap %dA; "
                            "using configured max %dA from %s",
                            entity_max,
                            full_params.get("max_charge_amps", 32),
                            full_params.get("max_charge_amps_source", "params"),
                        )
                    elif entity_max < full_params.get("max_charge_amps", 32):
                        _LOGGER.info(
                            f"Preliminary cap: max_charge_amps to {entity_max}A "
                            f"(will re-check after charging starts)"
                        )
                        full_params["max_charge_amps"] = entity_max
        except Exception:
            pass

    _dynamic_ev_state[entry_id][vehicle_id] = {
        "active": True,
        "params": full_params,
        "current_amps": start_amps,
        "target_amps": start_amps,
        "cancel_timer": cancel_timer,
        "priority": priority,
        "paused": False,
        "paused_reason": None,
        "charging_started": dynamic_mode == "battery_target",  # Already started for battery_target
        "entity_max_rechecked": False,  # Re-check Tesla entity max after charging starts
        "allocated_surplus_kw": 0,
        "reason": "",
        "vehicle_name": full_params.get("vehicle_name"),
        "session_id": None,  # Will be set when session tracking starts
    }

    # Start charging session tracking
    try:
        from .ev_charging_session import get_session_manager
        session_manager = get_session_manager()
        if session_manager:
            # Get initial SoC if available
            initial_soc = None
            live_status = await _get_tesla_live_status(hass, config_entry)
            if live_status:
                initial_soc = live_status.get("battery_soc")

            session = await session_manager.start_session(
                vehicle_id=vehicle_id,
                mode=owner_mode,
                start_soc=int(initial_soc) if initial_soc else None,
            )
            _dynamic_ev_state[entry_id][vehicle_id]["session_id"] = session.id
            _LOGGER.info(f"📊 Started charging session {session.id}")
    except Exception as e:
        _LOGGER.debug(f"Could not start session tracking: {e}")

    session_id = _dynamic_ev_state[entry_id][vehicle_id].get("session_id")
    _dynamic_ev_state[entry_id][vehicle_id]["ownership"] = claim_ev_ownership(
        hass,
        config_entry,
        vehicle_id,
        owner_mode=owner_mode,
        session_id=session_id,
        reason=_dynamic_ev_state[entry_id][vehicle_id].get("reason") or None,
        command=f"start_{owner_mode}",
        extra={"charger_type": charger_type},
    )

    # Also store in hass.data for access from other places (for API endpoints)
    if DOMAIN in hass.data and entry_id in hass.data[DOMAIN]:
        hass.data[DOMAIN][entry_id]["dynamic_ev_state"] = _dynamic_ev_state[entry_id]

    mode_label = "solar surplus" if dynamic_mode == "solar_surplus" else "battery target"
    _LOGGER.info(f"⚡ Dynamic EV charging started ({mode_label} mode, vehicle={vehicle_id})")

    # Send push notification if enabled
    # For solar_surplus mode, skip the immediate notification — a notification will be
    # sent when charging actually starts (after conditions are met) in _dynamic_ev_update
    notify_on_start = params.get("notify_on_start", True)
    if notify_on_start and dynamic_mode != "solar_surplus":
        try:
            # Look up vehicle name from VIN, fallback to param or truncated VIN
            vehicle_name = params.get("vehicle_name")
            if not vehicle_name and vehicle_id:
                vehicle_name = _get_vehicle_name_from_vin(hass, vehicle_id)
            if not vehicle_name:
                vehicle_name = (vehicle_id[:8] if len(vehicle_id) > 8 else vehicle_id) if vehicle_id and vehicle_id != DEFAULT_VEHICLE_ID else "EV"
            await _send_expo_push(
                hass,
                "EV Charging",
                f"{vehicle_name} started"
            )
        except Exception as e:
            _LOGGER.debug(f"Could not send start notification: {e}")

    return True


async def _action_stop_ev_charging_dynamic(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    params: Dict[str, Any]
) -> bool:
    """
    Stop dynamic EV charging and cancel the adjustment timer.

    Parameters:
        vehicle_id: Specific vehicle to stop (default: stop all)
        stop_charging: If True (default), also stop the EV charging. If False, just stop adjustments.
    """
    from ..const import DOMAIN

    entry_id = config_entry.entry_id
    stop_charging = params.get("stop_charging", True)
    vehicle_id = params.get("vehicle_id") or params.get("vehicle_vin")

    vehicles = _dynamic_ev_state.get(entry_id, {})

    if vehicle_id:
        # Stop specific vehicle — also match _default ↔ VIN overlap
        if vehicle_id in vehicles:
            vehicle_ids_to_stop = [vehicle_id]
        elif vehicle_id != DEFAULT_VEHICLE_ID and DEFAULT_VEHICLE_ID in vehicles:
            vehicle_ids_to_stop = [DEFAULT_VEHICLE_ID]
        elif vehicle_id == DEFAULT_VEHICLE_ID:
            vehicle_ids_to_stop = list(vehicles.keys())  # Stop all (single vehicle)
        else:
            # Caller asked to stop a specific vehicle that isn't tracked in
            # dynamic state. Treat that as a cleanup no-op unless the caller
            # explicitly owns the session and asks to stop an untracked vehicle.
            if params.get("stop_untracked"):
                vehicle_ids_to_stop = [vehicle_id]
            else:
                _LOGGER.debug(
                    "Dynamic EV: no tracked session for %s; skipping downstream stop",
                    vehicle_id,
                )
                vehicle_ids_to_stop = []
    else:
        # Stop all vehicles for this entry
        vehicle_ids_to_stop = list(vehicles.keys())

    # Collect per-vehicle params before deleting state (needed for stop)
    vehicle_params: Dict[str, dict] = {}
    released_vehicle_ids: set[str] = set()

    for vid in vehicle_ids_to_stop:
        state = vehicles.get(vid)
        if state:
            vehicle_params[vid] = state.get("params", {})
        elif params.get("stop_untracked") and vid == vehicle_id:
            vehicle_params[vid] = params

        if state:

            # Cancel the timer
            cancel_timer = state.get("cancel_timer")
            if cancel_timer:
                cancel_timer()
                _LOGGER.debug(f"Dynamic EV: Cancelled periodic timer for {vid}")

            # End charging session tracking
            try:
                from .ev_charging_session import get_session_manager
                session_manager = get_session_manager()
                if session_manager and state.get("session_id"):
                    # Try to get current SoC for end_soc
                    end_soc = None
                    try:
                        live_status = hass.data.get(DOMAIN, {}).get(entry_id, {}).get("live_status", {})
                        if live_status.get("battery_soc"):
                            end_soc = int(live_status["battery_soc"])
                    except Exception:
                        pass

                    await session_manager.end_session(
                        vehicle_id=vid,
                        reason="manual" if params.get("manual_stop") else "stopped",
                        end_soc=end_soc,
                    )
                    _LOGGER.debug(f"Dynamic EV: Ended charging session for {vid}")
            except Exception as e:
                _LOGGER.warning(f"Dynamic EV: Failed to end session for {vid}: {e}")

            # Send stop notification if enabled
            notify_on_complete = state.get("params", {}).get("notify_on_complete", True)
            if notify_on_complete:
                try:
                    # Look up vehicle name from VIN, fallback to param or truncated VIN
                    vehicle_name = state.get("params", {}).get("vehicle_name")
                    if not vehicle_name and vid:
                        vehicle_name = _get_vehicle_name_from_vin(hass, vid)
                    if not vehicle_name:
                        vehicle_name = vid[:8] if len(vid) > 8 else vid
                    reason = params.get("stop_reason", "stopped")
                    await _send_expo_push(
                        hass,
                        "EV Charging",
                        f"{vehicle_name} {reason}"
                    )
                except Exception as e:
                    _LOGGER.debug(f"Could not send stop notification: {e}")

            state["active"] = False
            from .ev_ownership import release_ev_ownership
            release_ev_ownership(
                hass,
                config_entry,
                vid,
                reason=params.get("stop_reason", "manual" if params.get("manual_stop") else "stopped"),
                command="stop" if stop_charging else "release",
            )
            released_vehicle_ids.add(vid)
            del vehicles[vid]
            _LOGGER.info(f"⚡ Dynamic EV charging stopped for {vid}")

    # Clean up entry if no vehicles remain
    if not vehicles and entry_id in _dynamic_ev_state:
        del _dynamic_ev_state[entry_id]

    # Update hass.data
    if DOMAIN in hass.data and entry_id in hass.data[DOMAIN]:
        if _dynamic_ev_state.get(entry_id):
            hass.data[DOMAIN][entry_id]["dynamic_ev_state"] = _dynamic_ev_state[entry_id]
        else:
            hass.data[DOMAIN][entry_id].pop("dynamic_ev_state", None)

    # Stop EV charging if requested
    if stop_charging and vehicle_ids_to_stop:
        physical_stop_failed = False
        for vid_to_stop in vehicle_ids_to_stop:
            v_params = vehicle_params.get(vid_to_stop, {})
            charger_type = v_params.get("charger_type", "tesla")
            if charger_type in ("generic", "ocpp", "zaptec"):
                # Use _set_vehicle_amps which handles all charger types
                stop_success = await _set_vehicle_amps(hass, config_entry, vid_to_stop, 0, v_params)
            else:
                stop_params = dict(params)
                stop_params["vehicle_vin"] = vid_to_stop
                stop_success = await _action_stop_ev_charging(hass, config_entry, stop_params)
            if not stop_success:
                physical_stop_failed = True
            if params.get("stop_untracked") and vid_to_stop not in released_vehicle_ids:
                from .ev_ownership import release_ev_ownership
                release_ev_ownership(
                    hass,
                    config_entry,
                    vid_to_stop,
                    reason=params.get("stop_reason", "stopped"),
                    command="stop",
                    success=stop_success,
                )
        return not physical_stop_failed

    return True
