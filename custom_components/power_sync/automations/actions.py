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

EV Actions (Tesla Fleet/Teslemetry or Tesla BLE):
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

from ..const import (
    DOMAIN,
    CONF_EV_PROVIDER,
    EV_PROVIDER_FLEET_API,
    EV_PROVIDER_TESLA_BLE,
    EV_PROVIDER_BOTH,
    CONF_TESLA_BLE_ENTITY_PREFIX,
    DEFAULT_TESLA_BLE_ENTITY_PREFIX,
    TESLA_BLE_SWITCH_CHARGER,
    TESLA_BLE_NUMBER_CHARGING_AMPS,
    TESLA_BLE_NUMBER_CHARGING_LIMIT,
    TESLA_BLE_BUTTON_WAKE_UP,
    TESLA_BLE_BINARY_ASLEEP,
    TESLA_BLE_BINARY_STATUS,
)

_LOGGER = logging.getLogger(__name__)

# Tesla integrations supported for EV control via Fleet API
from ..const import TESLA_INTEGRATIONS
TESLA_EV_INTEGRATIONS = TESLA_INTEGRATIONS

# Global lock to prevent concurrent wake/charging attempts
_ev_wake_lock: Dict[str, bool] = {}  # vehicle_id -> is_waking

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
                if len(id_str) == 17 and not id_str.isdigit() and id_str == vehicle_vin:
                    return device.name or vehicle_vin[:8]

    # Fallback to truncated VIN
    return vehicle_vin[:8] if len(vehicle_vin) > 8 else vehicle_vin


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
    hass: HomeAssistant, ble_prefix: str, amps: int
) -> bool:
    """Set EV charging amps via Tesla BLE."""
    amps_entity = TESLA_BLE_NUMBER_CHARGING_AMPS.format(prefix=ble_prefix)

    state = hass.states.get(amps_entity)
    if state is None:
        _LOGGER.error(f"Tesla BLE charging amps entity not found: {amps_entity}")
        return False

    # Cap amps to entity's min/max range
    entity_min = state.attributes.get("min", 0)
    entity_max = state.attributes.get("max", amps)
    capped_amps = max(int(entity_min), min(int(entity_max), int(amps)))
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


async def _get_sigenergy_controller(config_entry: ConfigEntry) -> Optional["SigenergyController"]:
    """Get a Sigenergy controller for Modbus operations.

    Returns:
        SigenergyController instance or None if not configured
    """
    from ..const import (
        CONF_SIGENERGY_MODBUS_HOST,
        CONF_SIGENERGY_MODBUS_PORT,
        CONF_SIGENERGY_MODBUS_SLAVE_ID,
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

    return SigenergyController(
        host=modbus_host,
        port=modbus_port,
        slave_id=modbus_slave_id,
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
            else:
                _LOGGER.warning(f"Action '{action_type}' returned False")
        except Exception as e:
            _LOGGER.error(f"Error executing action '{action.get('action_type')}': {e}")

    return success_count > 0


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
    elif action_type == "restore_normal":
        return await _action_restore_normal(hass, config_entry)
    elif action_type == "set_charge_rate":
        return await _action_set_charge_rate(hass, config_entry, params)
    elif action_type == "set_discharge_rate":
        return await _action_set_discharge_rate(hass, config_entry, params)
    elif action_type == "set_export_limit":
        return await _action_set_export_limit(hass, config_entry, params)
    # EV Charging Actions (pass context for time window support)
    elif action_type == "start_ev_charging":
        return await _action_start_ev_charging(hass, config_entry, params, context)
    elif action_type == "stop_ev_charging":
        return await _action_stop_ev_charging(hass, config_entry, params)
    elif action_type == "set_ev_charge_limit":
        return await _action_set_ev_charge_limit(hass, config_entry, params)
    elif action_type == "set_ev_charging_amps":
        return await _action_set_ev_charging_amps(hass, config_entry, params)
    elif action_type == "start_ev_charging_dynamic":
        return await _action_start_ev_charging_dynamic(hass, config_entry, params, context)
    elif action_type == "stop_ev_charging_dynamic":
        return await _action_stop_ev_charging_dynamic(hass, config_entry, params)
    else:
        _LOGGER.warning(f"Unknown action type: {action_type}")
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

    try:
        await hass.services.async_call(
            DOMAIN,
            SERVICE_SET_BACKUP_RESERVE,
            {"percent": reserve_percent},
            blocking=True,
        )
        return True
    except Exception as e:
        _LOGGER.error(f"Failed to set backup reserve: {e}")
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

    from ..const import DOMAIN, SERVICE_SET_GRID_EXPORT

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

    try:
        await hass.services.async_call(
            DOMAIN,
            SERVICE_SET_OPERATION_MODE,
            {"mode": mode},
            blocking=True,
        )
        return True
    except Exception as e:
        _LOGGER.error(f"Failed to set operation mode: {e}")
        return False


async def _action_force_discharge(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    params: Dict[str, Any]
) -> bool:
    """Force battery discharge for a specified duration."""
    # Accept both "duration" and "duration_minutes" for flexibility
    duration = params.get("duration") or params.get("duration_minutes", 30)

    if _is_sigenergy(config_entry):
        # Sigenergy: Set high discharge rate and restore export limit
        controller = await _get_sigenergy_controller(config_entry)
        if not controller:
            _LOGGER.error("force_discharge: Sigenergy Modbus not configured")
            return False
        try:
            # Set high discharge rate (10kW max)
            discharge_result = await controller.set_discharge_rate_limit(10.0)
            # Restore export limit to allow discharge to grid
            export_result = await controller.restore_export_limit()
            if discharge_result and export_result:
                _LOGGER.info(f"Sigenergy: Force discharge activated for {duration} minutes")
                return True
            else:
                _LOGGER.warning(f"Sigenergy force discharge partial: discharge={discharge_result}, export={export_result}")
                return discharge_result or export_result
        except Exception as e:
            _LOGGER.error(f"Failed to force discharge (Sigenergy): {e}")
            return False
        finally:
            await controller.disconnect()

    from ..const import DOMAIN, SERVICE_FORCE_DISCHARGE

    try:
        await hass.services.async_call(
            DOMAIN,
            SERVICE_FORCE_DISCHARGE,
            {"duration": duration},
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
    # Accept both "duration" and "duration_minutes" for flexibility
    duration = params.get("duration") or params.get("duration_minutes", 60)

    if _is_sigenergy(config_entry):
        # Sigenergy: Set high charge rate and prevent discharge
        controller = await _get_sigenergy_controller(config_entry)
        if not controller:
            _LOGGER.error("force_charge: Sigenergy Modbus not configured")
            return False
        try:
            # Set high charge rate (10kW max)
            charge_result = await controller.set_charge_rate_limit(10.0)
            # Prevent discharge while charging
            discharge_result = await controller.set_discharge_rate_limit(0)
            if charge_result and discharge_result:
                _LOGGER.info(f"Sigenergy: Force charge activated for {duration} minutes")
                return True
            else:
                _LOGGER.warning(f"Sigenergy force charge partial: charge={charge_result}, discharge={discharge_result}")
                return charge_result or discharge_result
        except Exception as e:
            _LOGGER.error(f"Failed to force charge (Sigenergy): {e}")
            return False
        finally:
            await controller.disconnect()

    from ..const import DOMAIN, SERVICE_FORCE_CHARGE

    try:
        await hass.services.async_call(
            DOMAIN,
            SERVICE_FORCE_CHARGE,
            {"duration": duration},
            blocking=True,
        )
        return True
    except Exception as e:
        _LOGGER.error(f"Failed to activate force charge: {e}")
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
    if _is_sigenergy(config_entry):
        _LOGGER.warning("set_grid_export not supported for Sigenergy")
        return False

    from ..const import DOMAIN, SERVICE_SET_GRID_EXPORT

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


async def _action_restore_normal(
    hass: HomeAssistant,
    config_entry: ConfigEntry
) -> bool:
    """Restore normal battery operation (cancel force charge/discharge)."""
    if _is_sigenergy(config_entry):
        # Sigenergy: Restore default rate limits
        controller = await _get_sigenergy_controller(config_entry)
        if not controller:
            _LOGGER.error("restore_normal: Sigenergy Modbus not configured")
            return False
        try:
            # Restore default rates (max rates)
            charge_result = await controller.set_charge_rate_limit(10.0)
            discharge_result = await controller.set_discharge_rate_limit(10.0)
            export_result = await controller.restore_export_limit()
            if charge_result and discharge_result and export_result:
                _LOGGER.info("Sigenergy: Restored normal operation")
                return True
            return charge_result or discharge_result or export_result
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


async def _action_start_ev_charging(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    params: Dict[str, Any],
    context: Optional[Dict[str, Any]] = None
) -> bool:
    """
    Start EV charging via Tesla Fleet/Teslemetry or Tesla BLE.

    Uses BLE if configured as primary or both, falls back to Fleet API.

    Parameters:
        stop_outside_window: If True, schedule charging to stop at end of time window
    """
    ev_config = _get_ev_config(config_entry)
    ev_provider = ev_config["ev_provider"]
    ble_prefix = ev_config["ble_prefix"]
    vehicle_vin = params.get("vehicle_vin")
    stop_outside_window = params.get("stop_outside_window", False)

    # Get time window from context
    time_window_start = context.get("time_window_start") if context else None
    time_window_end = context.get("time_window_end") if context else None
    timezone = context.get("timezone", "UTC") if context else "UTC"

    charging_started = False

    # Try BLE first if configured
    if ev_provider in (EV_PROVIDER_TESLA_BLE, EV_PROVIDER_BOTH):
        if _is_ble_available(hass, ble_prefix):
            result = await _start_ev_charging_ble(hass, ble_prefix)
            if result:
                charging_started = True
            elif ev_provider == EV_PROVIDER_TESLA_BLE:
                return False
            # Fall through to Fleet API if BLE failed and both are configured

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
                await _action_stop_ev_charging(hass, config_entry, params)
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
    Stop EV charging via Tesla Fleet/Teslemetry or Tesla BLE.

    Uses BLE if configured as primary or both, falls back to Fleet API.
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

    ev_config = _get_ev_config(config_entry)
    ev_provider = ev_config["ev_provider"]
    ble_prefix = ev_config["ble_prefix"]
    vehicle_vin = params.get("vehicle_vin")

    # Try BLE first if configured
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
    """
    ev_config = _get_ev_config(config_entry)
    ev_provider = ev_config["ev_provider"]
    ble_prefix = ev_config["ble_prefix"]
    vehicle_vin = params.get("vehicle_vin")

    # Accept multiple parameter names for flexibility
    percent = params.get("percent") or params.get("limit") or params.get("charge_limit_percent")
    if percent is None:
        _LOGGER.error("set_ev_charge_limit: missing percent parameter")
        return False

    # Clamp to valid range (50-100%)
    percent = max(50, min(100, int(percent)))

    # Try BLE first if configured
    if ev_provider in (EV_PROVIDER_TESLA_BLE, EV_PROVIDER_BOTH):
        if _is_ble_available(hass, ble_prefix):
            result = await _set_ev_charge_limit_ble(hass, ble_prefix, percent)
            if result or ev_provider == EV_PROVIDER_TESLA_BLE:
                return result

    # Use Fleet API
    if ev_provider in (EV_PROVIDER_FLEET_API, EV_PROVIDER_BOTH):
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
    Set EV charging amperage via Tesla Fleet/Teslemetry or Tesla BLE.
    """
    ev_config = _get_ev_config(config_entry)
    ev_provider = ev_config["ev_provider"]
    ble_prefix = ev_config["ble_prefix"]
    vehicle_vin = params.get("vehicle_vin")

    # Accept both "amps" and "charging_amps" for flexibility
    amps = params.get("amps") or params.get("charging_amps")
    if amps is None:
        _LOGGER.error("set_ev_charging_amps: missing amps parameter")
        return False

    # Clamp to valid range (5-48A typical, but allow up to 80A for some chargers)
    # Note: Tesla vehicles refuse charging below 5A, so we enforce 5A minimum
    # Tesla BLE supports same 5-32A range as cloud API
    amps = max(5, min(80, int(amps)))

    # Try BLE first if configured (BLE supports same 5-32A range as cloud API)
    ble_amps = amps
    if ev_provider in (EV_PROVIDER_TESLA_BLE, EV_PROVIDER_BOTH):
        if _is_ble_available(hass, ble_prefix):
            result = await _set_ev_charging_amps_ble(hass, ble_prefix, ble_amps)
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
                entity_min = entity_state.attributes.get("min", 5)
                entity_max = entity_state.attributes.get("max", 32)
                original_amps = amps
                amps = max(entity_min, min(entity_max, amps))
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

# Global storage for regular EV charging scheduled stop (for stop_outside_window)
_ev_scheduled_stop: Dict[str, Any] = {}

# Default vehicle ID for single-vehicle setups
DEFAULT_VEHICLE_ID = "_default"


def _calculate_solar_surplus(live_status: dict, current_ev_power_kw: float, config: dict) -> float:
    """
    Calculate available solar surplus for EV charging.

    Two methods are supported:
    - direct: surplus = solar - load - battery_charge - buffer
    - grid_based (AmpPilot style): surplus = -grid + current_ev + battery_charge

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
        surplus = -grid_kw + current_ev_power_kw + battery_charge_kw
        _LOGGER.debug(
            f"Surplus calc (grid_based): grid={grid_kw:.2f}kW, ev={current_ev_power_kw:.2f}kW, "
            f"bat_charge={battery_charge_kw:.2f}kW → raw={surplus:.2f}kW, after_buffer={max(0, surplus - buffer_kw):.2f}kW"
        )
    else:  # direct method
        # Direct calculation: what solar is producing minus what's being used
        # IMPORTANT: If load sensor includes EV power (e.g., mobile connector), we need to
        # subtract it to get the "real" household load, then calculate true surplus
        battery_charge_kw = max(0, -battery_kw)
        real_household_load_kw = load_kw - current_ev_power_kw  # Remove EV from house load
        surplus = solar_kw - real_household_load_kw - battery_charge_kw
        _LOGGER.debug(
            f"Surplus calc (direct): solar={solar_kw:.2f}kW, load={load_kw:.2f}kW (real={real_household_load_kw:.2f}kW), "
            f"bat_charge={battery_charge_kw:.2f}kW → raw={surplus:.2f}kW, after_buffer={max(0, surplus - buffer_kw):.2f}kW"
        )

    # Apply buffer and ensure non-negative
    return max(0, surplus - buffer_kw)


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
    import_price = 30.0
    export_price = 8.0

    entry_data = hass.data.get(DOMAIN, {}).get(entry_id, {})

    # Try Amber coordinator first
    amber_coordinator = entry_data.get("amber_coordinator")
    if amber_coordinator and amber_coordinator.data:
        current_prices = amber_coordinator.data.get("current", [])
        for price in current_prices:
            if price.get("channelType") == "general":
                import_price = price.get("perKwh", 30.0)
            elif price.get("channelType") == "feedIn":
                export_price = abs(price.get("perKwh", 8.0))
        return import_price, export_price

    # Fallback to tariff_schedule (for Globird/AEMO VPP users)
    if entry_data.get("tariff_schedule"):
        tariff_schedule = entry_data.get("tariff_schedule", {})
        import_price = tariff_schedule.get("buy_price", 30.0)
        export_price = tariff_schedule.get("sell_price", 8.0)
        return import_price, export_price

    # Fallback to Sigenergy tariff (for Sigenergy users with Amber)
    if entry_data.get("sigenergy_tariff"):
        sigenergy_tariff = entry_data.get("sigenergy_tariff", {})
        buy_prices = sigenergy_tariff.get("buy_prices", [])
        sell_prices = sigenergy_tariff.get("sell_prices", [])
        if buy_prices:
            now = datetime.now()
            current_time = f"{now.hour:02d}:{30 if now.minute >= 30 else 0:02d}"
            for slot in buy_prices:
                if slot.get("timeRange", "").startswith(current_time):
                    import_price = slot.get("price", 30.0)
                    break
        if sell_prices:
            now = datetime.now()
            current_time = f"{now.hour:02d}:{30 if now.minute >= 30 else 0:02d}"
            for slot in sell_prices:
                if slot.get("timeRange", "").startswith(current_time):
                    export_price = slot.get("price", 8.0)
                    break
        return import_price, export_price

    # Fallback to stored current_prices
    price_data = entry_data.get("current_prices", {})
    if price_data:
        import_price = price_data.get("import_cents", 30.0)
        export_price = price_data.get("export_cents", 8.0)

    return import_price, export_price


def get_price_recommendation(
    import_price_cents: float,
    export_price_cents: float,
    surplus_kw: float,
    battery_soc: float,
    min_battery_soc: float = 80,
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


async def _set_vehicle_amps(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    vehicle_id: str,
    amps: int,
    params: dict
) -> bool:
    """
    Set charging amps for any charger type (Tesla, OCPP, generic HA entities).

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
            "vehicle_vin": vehicle_id if vehicle_id != DEFAULT_VEHICLE_ID else None
        })

    elif charger_type == "ocpp":
        ocpp_charger_id = params.get("ocpp_charger_id")
        if not ocpp_charger_id:
            _LOGGER.error("OCPP charger ID not configured")
            return False
        return await _set_ocpp_charging_amps(hass, ocpp_charger_id, amps)

    elif charger_type == "generic":
        # Use HA service calls to switch and number entities
        switch_entity = params.get("charger_switch_entity")
        amps_entity = params.get("charger_amps_entity")

        try:
            if amps == 0:
                # Turn off charger
                if switch_entity:
                    await hass.services.async_call(
                        "switch", "turn_off",
                        {"entity_id": switch_entity},
                        blocking=True
                    )
            else:
                # Set amps and ensure charger is on
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


async def _set_ocpp_charging_amps(hass: HomeAssistant, charger_id: int, amps: int) -> bool:
    """Set charging amps for an OCPP charger."""
    from ..const import DOMAIN

    try:
        # Find the OCPP charger controller in hass.data
        for entry_id, entry_data in hass.data.get(DOMAIN, {}).items():
            if isinstance(entry_data, dict) and "ocpp_server" in entry_data:
                ocpp_server = entry_data["ocpp_server"]
                if hasattr(ocpp_server, "set_charging_profile"):
                    success = await ocpp_server.set_charging_profile(charger_id, amps)
                    if success:
                        _LOGGER.info(f"Set OCPP charger {charger_id} to {amps}A")
                        return True

        _LOGGER.warning(f"OCPP server not found or charger {charger_id} not available")
        return False
    except Exception as e:
        _LOGGER.error(f"Failed to set OCPP charging amps: {e}")
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

    entry_data = hass.data.get(DOMAIN, {}).get(config_entry.entry_id, {})

    # Try coordinator data first (cached, no API call needed)
    for coord_key in ("tesla_coordinator", "sigenergy_coordinator", "sungrow_coordinator"):
        coordinator = entry_data.get(coord_key)
        if coordinator and coordinator.data:
            data = coordinator.data
            return {
                "battery_soc": data.get("battery_level"),
                "grid_power": (data.get("grid_power", 0) or 0) * 1000,
                "solar_power": (data.get("solar_power", 0) or 0) * 1000,
                "battery_power": (data.get("battery_power", 0) or 0) * 1000,
                "load_power": (data.get("load_power", 0) or 0) * 1000,
            }

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

    # Get live status
    live_status = await _get_tesla_live_status(hass, config_entry)
    if not live_status:
        _LOGGER.debug("Solar surplus EV: Could not get live status")
        return

    battery_soc = live_status.get("battery_soc") or 0

    # Battery priority check
    min_soc = params.get("min_battery_soc", 80)
    pause_soc = params.get("pause_below_soc", 70)

    # Parallel charging parameters
    allow_parallel = params.get("allow_parallel_charging", False)
    max_battery_charge_kw = params.get("max_battery_charge_rate_kw", 5.0)

    # For parallel charging check, we need to calculate surplus early
    # Calculate current EV power for THIS vehicle
    voltage = params.get("voltage", 240)
    current_amps = state.get("current_amps", 0)

    # Calculate TOTAL EV power from ALL active vehicles
    total_ev_power_kw = 0.0
    all_vehicles = _dynamic_ev_state.get(entry_id, {})
    for vid, v_state in all_vehicles.items():
        if v_state.get("active") and not v_state.get("paused"):
            v_amps = v_state.get("current_amps", 0)
            v_voltage = v_state.get("params", {}).get("voltage", 240)
            total_ev_power_kw += (v_amps * v_voltage) / 1000

    # Calculate raw surplus (before any parallel charging adjustments)
    raw_surplus_kw = _calculate_solar_surplus(live_status, total_ev_power_kw, params)

    # Check if parallel charging is possible (surplus exceeds what battery can absorb)
    parallel_charging_available = (
        allow_parallel and
        battery_soc < min_soc and
        raw_surplus_kw > max_battery_charge_kw
    )

    # Don't start charging until battery reaches min_soc (unless parallel charging is available)
    if not state.get("charging_started"):
        if battery_soc < min_soc:
            if parallel_charging_available:
                # Parallel charging: solar exceeds battery's max charge rate
                state["paused"] = False
                state["paused_reason"] = None
                state["parallel_charging_mode"] = True
                _LOGGER.info(
                    f"⚡ Solar surplus EV: Parallel charging enabled - surplus {raw_surplus_kw:.1f}kW > "
                    f"battery max {max_battery_charge_kw}kW (battery at {battery_soc:.0f}%)"
                )
            else:
                state["paused"] = True
                if allow_parallel:
                    state["paused_reason"] = (
                        f"Waiting for battery to reach {min_soc}% or surplus > {max_battery_charge_kw}kW "
                        f"(currently {battery_soc:.0f}%, surplus {raw_surplus_kw:.1f}kW)"
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
        # In parallel mode, we pause if surplus drops below battery max charge rate
        if state.get("parallel_charging_mode"):
            if raw_surplus_kw <= max_battery_charge_kw:
                if not state.get("paused"):
                    state["paused"] = True
                    state["paused_reason"] = (
                        f"Parallel charging paused - surplus {raw_surplus_kw:.1f}kW <= "
                        f"battery max {max_battery_charge_kw}kW"
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
                            vehicle_name = vehicle_id[:8] if len(vehicle_id) > 8 else vehicle_id
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
        can_resume = False
        if battery_soc >= min_soc:
            # Battery recovered - switch to normal mode
            can_resume = True
            state["parallel_charging_mode"] = False
            _LOGGER.info(f"⚡ Solar surplus EV: Resuming - battery at {battery_soc:.0f}%")
        elif state.get("parallel_charging_mode") and raw_surplus_kw > max_battery_charge_kw:
            # Parallel mode: surplus recovered
            can_resume = True
            _LOGGER.info(
                f"⚡ Solar surplus EV: Resuming parallel charging - surplus {raw_surplus_kw:.1f}kW > "
                f"battery max {max_battery_charge_kw}kW"
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
                            vehicle_name = vehicle_id[:8] if len(vehicle_id) > 8 else vehicle_id
                        await _send_expo_push(
                            hass,
                            "EV Charging",
                            f"{vehicle_name} resumed"
                        )
                    except Exception as e:
                        _LOGGER.debug(f"Could not send resume notification: {e}")

    # Calculate current EV power for THIS vehicle (for logging)
    current_ev_kw = (current_amps * voltage) / 1000

    # Determine available surplus for EV
    # In parallel charging mode, reserve max_battery_charge_kw for the battery
    if state.get("parallel_charging_mode") and battery_soc < min_soc:
        # Parallel mode: only use surplus beyond what battery can absorb
        surplus_kw = max(0, raw_surplus_kw - max_battery_charge_kw)
        _LOGGER.debug(
            f"Solar surplus EV (parallel mode): raw_surplus={raw_surplus_kw:.2f}kW, "
            f"battery_reserve={max_battery_charge_kw}kW, available_for_ev={surplus_kw:.2f}kW"
        )
    else:
        # Normal mode: use full surplus
        surplus_kw = raw_surplus_kw

    # Apply dual vehicle distribution strategy
    strategy = params.get("dual_vehicle_strategy", "priority_first")
    my_surplus_kw = _distribute_surplus(entry_id, vehicle_id, surplus_kw, strategy)

    # Convert to amps
    available_amps = (my_surplus_kw * 1000) / voltage

    # Apply constraints
    min_amps = params.get("min_charge_amps", 5)
    max_amps = params.get("max_charge_amps", 32)
    new_amps = int(round(max(0, min(max_amps, available_amps))))

    # Hysteresis: don't start unless we have sustained surplus
    sustained_minutes = params.get("sustained_surplus_minutes", 2)
    stop_delay_minutes = params.get("stop_delay_minutes", 5)

    if new_amps < min_amps:
        # Not enough surplus
        if current_amps > 0:
            # Track how long we've been below threshold
            low_surplus_start = state.get("low_surplus_start")
            if low_surplus_start is None:
                state["low_surplus_start"] = datetime.now()
            elif (datetime.now() - low_surplus_start).total_seconds() >= stop_delay_minutes * 60:
                # Stop charging after delay
                _LOGGER.info(f"⚡ Solar surplus EV: Stopping - insufficient surplus for {stop_delay_minutes} min")
                new_amps = 0
            else:
                # Keep current amps during grace period
                new_amps = current_amps
        else:
            new_amps = 0
    else:
        # Sufficient surplus - reset low surplus timer
        state["low_surplus_start"] = None

        if current_amps == 0:
            # Track how long we've had surplus before starting
            high_surplus_start = state.get("high_surplus_start")
            if high_surplus_start is None:
                state["high_surplus_start"] = datetime.now()
                new_amps = 0  # Don't set amps yet, wait for sustained surplus
            elif (datetime.now() - high_surplus_start).total_seconds() >= sustained_minutes * 60:
                # Start charging after sustained surplus
                _LOGGER.info(f"⚡ Solar surplus EV: Starting - sustained surplus for {sustained_minutes} min")

                # Send the actual start-charging command to the vehicle
                start_success = await _action_start_ev_charging(hass, config_entry, params, context=None)
                if not start_success:
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
                            vehicle_name = vehicle_id[:8] if len(vehicle_id) > 8 else vehicle_id
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
    if current_amps > 0:
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
                    amps=current_amps,
                    is_solar=is_solar,
                    import_price_cents=import_price,
                    export_price_cents=export_price,
                    battery_soc=int(battery_soc) if battery_soc else None,
                )
        except Exception as e:
            _LOGGER.debug(f"Could not update session: {e}")

    # Only update if change is significant (>= 1 amp)
    if abs(new_amps - current_amps) >= 1:
        _LOGGER.info(
            f"⚡ Solar surplus EV: {current_amps}A -> {new_amps}A "
            f"(surplus={my_surplus_kw:.1f}kW, battery={battery_soc:.0f}%)"
        )
        success = await _set_vehicle_amps(hass, config_entry, vehicle_id, new_amps, params)
        if success:
            state["current_amps"] = new_amps
            state["target_amps"] = new_amps

            # End session when transitioning to 0 amps (stopping charging)
            if new_amps == 0 and current_amps > 0:
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

    current_amps = state.get("current_amps", max_amps)

    # Get live status
    live_status = await _get_tesla_live_status(hass, config_entry)
    if not live_status:
        _LOGGER.debug("Dynamic EV: Could not get live status, keeping current amps")
        return

    # Convert to kW for cleaner math (matching HA automation)
    # battery_power: Positive = discharging, Negative = charging
    battery_power_kw = (live_status.get("battery_power", 0) or 0) / 1000
    grid_power_kw = (live_status.get("grid_power", 0) or 0) / 1000
    current_ev_power_kw = (current_amps * voltage) / 1000

    # Target battery power in same convention (negative = charging)
    # If target_battery_charge_kw = 5, we want battery_power = -5 kW
    target_battery_power_kw = -target_battery_charge_kw

    # Battery deficit: How much more the battery should be charging
    # Positive deficit = battery is charging MORE than target (surplus available for EV)
    # Negative deficit = battery isn't meeting charge target
    battery_deficit_kw = target_battery_power_kw - battery_power_kw

    # Grid headroom: How much more we could import before hitting limit
    grid_headroom_kw = max_grid_import_kw - grid_power_kw

    # Available power for EV adjustment:
    # - In no_grid_import mode: limit to inverter capacity and prevent grid imports
    # - If battery has surplus (deficit > 0.1), use that surplus
    # - Otherwise, use grid headroom
    if no_grid_import:
        # Exclude intentional home battery grid-charging from the grid import figure.
        # When the LP optimizer force-charges the home battery from grid, battery_power_kw
        # is negative (charging) and grid_power_kw includes that draw.  The EV should not
        # be throttled because of intentional battery charging — only because of the EV's
        # own grid draw and household load.
        battery_charging_kw = max(0.0, -battery_power_kw)  # positive when battery is charging
        ev_relevant_grid_kw = grid_power_kw - battery_charging_kw

        # Calculate home load (excluding EV and battery charging) from power balance
        home_load_kw = ev_relevant_grid_kw - current_ev_power_kw

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
    elif battery_deficit_kw > 0.1:
        available_power_kw = battery_deficit_kw
    else:
        available_power_kw = grid_headroom_kw

    # Convert available power to amps
    available_amps = (available_power_kw * 1000) / voltage

    # Calculate new target amps
    raw_new_amps = current_amps + available_amps
    new_amps = int(round(max(min_amps, min(max_amps, raw_new_amps))))

    # Clamp to 0 if below minimum (stop charging)
    if new_amps < min_amps:
        new_amps = 0

    # In no_grid_import mode, respond immediately to grid imports (don't wait for 1A threshold)
    # Use ev_relevant_grid_kw (excludes battery charging) to avoid throttling due to
    # intentional home battery grid-charging by the LP optimizer
    ev_grid_check = ev_relevant_grid_kw if no_grid_import else grid_power_kw
    if no_grid_import and ev_grid_check > grid_import_tolerance_kw:
        # We're importing (beyond battery charging) - reduce aggressively
        if new_amps < current_amps:
            _LOGGER.info(
                f"⚡ No-grid-import: Grid importing {grid_power_kw:.2f}kW "
                f"(battery_charging={battery_charging_kw:.2f}kW, "
                f"ev_relevant={ev_grid_check:.2f}kW, inverter_max={max_inverter_kw}kW), "
                f"reducing to {new_amps}A"
            )
            success = await _action_set_ev_charging_amps(hass, config_entry, {"amps": new_amps})
            if success:
                state["current_amps"] = new_amps
            return

    _LOGGER.debug(
        f"Dynamic EV: battery={battery_power_kw:.1f}kW (target={target_battery_power_kw:.1f}kW), "
        f"deficit={battery_deficit_kw:.1f}kW, grid={grid_power_kw:.1f}kW (max={max_grid_import_kw:.1f}kW), "
        f"headroom={grid_headroom_kw:.1f}kW, available={available_power_kw:.1f}kW, "
        f"current={current_amps}A, target={new_amps}A, no_grid_import={no_grid_import}"
    )

    # Only update if change is >= 1 amp (avoid constant micro-adjustments)
    if abs(new_amps - current_amps) >= 1:
        _LOGGER.info(
            f"⚡ Dynamic EV: Adjusting from {current_amps}A to {new_amps}A "
            f"(battery={battery_power_kw:.1f}kW, grid={grid_power_kw:.1f}kW, "
            f"available={available_power_kw:.1f}kW)"
        )
        success = await _action_set_ev_charging_amps(
            hass, config_entry, {"amps": new_amps}
        )
        if success:
            state["current_amps"] = new_amps
        else:
            _LOGGER.warning(f"Dynamic EV: Failed to set amps to {new_amps}A")

    # Update session tracking (battery target mode)
    try:
        from ..const import DOMAIN
        from .ev_charging_session import get_session_manager
        session_manager = get_session_manager()
        if session_manager:
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
        min_charge_amps: Minimum EV charge amps (default 5)
        max_charge_amps: Maximum EV charge amps (default 32)
        voltage: Assumed charging voltage (default 240)
        stop_outside_window: Stop when outside time window (default False)
        vehicle_vin: Optional VIN to filter by specific vehicle
        charger_type: "tesla", "ocpp", or "generic" (default tesla)
        priority: Vehicle priority for dual-vehicle setups (default 1)
    """
    from ..const import DOMAIN

    entry_id = config_entry.entry_id
    vehicle_id = params.get("vehicle_vin") or params.get("vehicle_id") or DEFAULT_VEHICLE_ID

    # Determine mode
    dynamic_mode = params.get("dynamic_mode", "battery_target")

    # Prevent duplicate solar surplus sessions for the same entry
    if dynamic_mode == "solar_surplus":
        entry_vehicles = _dynamic_ev_state.get(entry_id, {})
        for vid, v_state in entry_vehicles.items():
            if v_state.get("active") and v_state.get("params", {}).get("dynamic_mode") == "solar_surplus":
                _LOGGER.debug(f"Solar surplus session already active for vehicle {vid}, skipping duplicate")
                return True

    # Get common parameters with defaults
    min_charge_amps = params.get("min_charge_amps", 5)
    max_charge_amps = params.get("max_charge_amps", 32)
    voltage = params.get("voltage", 240)
    stop_outside_window = params.get("stop_outside_window", False)
    charger_type = params.get("charger_type", "tesla")
    priority = params.get("priority", 1)

    # Mode-specific parameters
    if dynamic_mode == "solar_surplus":
        mode_params = {
            "household_buffer_kw": params.get("household_buffer_kw", 0.5),
            "surplus_calculation": params.get("surplus_calculation", "grid_based"),
            "min_battery_soc": params.get("min_battery_soc", 80),
            "pause_below_soc": params.get("pause_below_soc", 70),
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
            f"min_soc={mode_params['min_battery_soc']}%, amps={min_charge_amps}-{max_charge_amps}A{parallel_info}"
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
        }
        start_amps = params.get("start_amps", max_charge_amps)
        no_grid_info = f", no_grid_import=enabled (inverter={mode_params['max_inverter_kw']}kW)" if no_grid_import else ""
        _LOGGER.info(
            f"⚡ Starting dynamic EV charging: target_battery_charge={target_battery_charge_kw}kW, "
            f"max_grid_import={mode_params['max_grid_import_kw']}kW, amps={min_charge_amps}-{max_charge_amps}A{no_grid_info}"
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
        start_success = await _action_start_ev_charging(hass, config_entry, params, context)
        if not start_success:
            _LOGGER.error("Dynamic EV: Failed to start EV charging")
            return False

        # Set initial amps
        amps_success = await _action_set_ev_charging_amps(
            hass, config_entry, {"amps": start_amps}
        )
        if not amps_success:
            # This is expected - Tesla reports lower max amps until charging actually starts
            _LOGGER.debug(f"Dynamic EV: Could not set initial amps to {start_amps}A (will adjust once charging starts)")

    # Create the periodic update callback for this vehicle
    async def periodic_update(now) -> None:
        await _dynamic_ev_update(hass, config_entry, entry_id, vehicle_id)

    # Schedule the periodic update (every 30 seconds)
    cancel_timer = async_track_time_interval(
        hass,
        periodic_update,
        timedelta(seconds=30),
    )

    # Build full params dict
    full_params = {
        "dynamic_mode": dynamic_mode,
        "vehicle_vin": params.get("vehicle_vin"),
        "vehicle_name": params.get("vehicle_name"),
        "min_charge_amps": min_charge_amps,
        "max_charge_amps": max_charge_amps,
        "voltage": voltage,
        "stop_outside_window": stop_outside_window,
        "time_window_start": time_window_start,
        "time_window_end": time_window_end,
        "timezone": timezone,
        "charger_type": charger_type,
        **mode_params,
        # Pass through generic charger entities if present
        "charger_switch_entity": params.get("charger_switch_entity"),
        "charger_amps_entity": params.get("charger_amps_entity"),
        "ocpp_charger_id": params.get("ocpp_charger_id"),
    }

    # Initialize entry-level state dict if needed
    if entry_id not in _dynamic_ev_state:
        _dynamic_ev_state[entry_id] = {}

    # Store vehicle-specific state
    # Read entity max for Tesla chargers to avoid over-reporting amps
    if charger_type == "tesla" and vehicle_id != DEFAULT_VEHICLE_ID:
        try:
            entity = await _get_tesla_ev_entity(
                hass, r"number\..*(charging_amps|charge_current)$", vehicle_id
            )
            if entity:
                entity_state = hass.states.get(entity)
                if entity_state:
                    entity_max = int(entity_state.attributes.get("max", max_charge_amps))
                    if entity_max < full_params.get("max_charge_amps", 32):
                        _LOGGER.info(f"Capping max_charge_amps to {entity_max}A (entity limit)")
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
                mode=dynamic_mode,
                start_soc=int(initial_soc) if initial_soc else None,
            )
            _dynamic_ev_state[entry_id][vehicle_id]["session_id"] = session.id
            _LOGGER.info(f"📊 Started charging session {session.id}")
    except Exception as e:
        _LOGGER.debug(f"Could not start session tracking: {e}")

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
                vehicle_name = vehicle_id[:8] if len(vehicle_id) > 8 else vehicle_id
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
        # Stop specific vehicle
        vehicle_ids_to_stop = [vehicle_id] if vehicle_id in vehicles else []
    else:
        # Stop all vehicles for this entry
        vehicle_ids_to_stop = list(vehicles.keys())

    for vid in vehicle_ids_to_stop:
        state = vehicles.get(vid)
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
        for vid_to_stop in vehicle_ids_to_stop:
            stop_params = dict(params)
            stop_params["vehicle_vin"] = vid_to_stop
            await _action_stop_ev_charging(hass, config_entry, stop_params)
        return True

    return True
