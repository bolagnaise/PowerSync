"""
Action execution logic for HA automations.

Supported actions:
- set_backup_reserve: Set battery backup reserve percentage (Tesla only)
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
TESLA_EV_INTEGRATIONS = ["tesla_fleet", "teslemetry"]


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

    # Find devices from Tesla integrations
    tesla_devices = []
    for device in device_registry.devices.values():
        for identifier in device.identifiers:
            # Use index access instead of tuple unpacking (identifiers can have >2 values)
            if len(identifier) < 2:
                continue
            domain = identifier[0]
            identifier_value = identifier[1]
            if domain in TESLA_EV_INTEGRATIONS:
                # Check if it's a vehicle (VIN is 17 chars, non-numeric)
                if len(str(identifier_value)) == 17 and not str(identifier_value).isdigit():
                    if vehicle_vin is None or identifier_value == vehicle_vin:
                        tesla_devices.append(device)
                        break

    if not tesla_devices:
        _LOGGER.warning("No Tesla EV devices found in device registry (looking for tesla_fleet/teslemetry integrations)")
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


async def _wake_tesla_ev(hass: HomeAssistant, vehicle_vin: Optional[str] = None) -> bool:
    """
    Wake up a Tesla vehicle before sending commands.

    Args:
        hass: Home Assistant instance
        vehicle_vin: Optional VIN to filter by specific vehicle

    Returns:
        True if wake command sent successfully
    """
    # Find the wake up button entity
    wake_entity = await _get_tesla_ev_entity(
        hass,
        r"button\..*wake(_up)?$",
        vehicle_vin
    )

    if not wake_entity:
        _LOGGER.warning("Could not find Tesla wake button entity")
        return False

    try:
        await hass.services.async_call(
            "button",
            "press",
            {"entity_id": wake_entity},
            blocking=True,
        )
        _LOGGER.info(f"Sent wake command to Tesla EV: {wake_entity}")
        # Wait a moment for vehicle to wake
        import asyncio
        await asyncio.sleep(3)
        return True
    except Exception as e:
        _LOGGER.error(f"Failed to wake Tesla EV: {e}")
        return False


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

    if hass.states.get(amps_entity) is None:
        _LOGGER.error(f"Tesla BLE charging amps entity not found: {amps_entity}")
        return False

    try:
        await _wake_tesla_ble(hass, ble_prefix)
        await hass.services.async_call(
            "number",
            "set_value",
            {"entity_id": amps_entity, "value": amps},
            blocking=True,
        )
        _LOGGER.info(f"Set EV charging amps to {amps}A via Tesla BLE: {amps_entity}")
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
    """Set battery backup reserve percentage (Tesla only)."""
    if _is_sigenergy(config_entry):
        _LOGGER.warning("set_backup_reserve not supported for Sigenergy")
        return False

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

    # Get registered push tokens
    push_tokens = hass.data.get(DOMAIN, {}).get("push_tokens", {})
    if not push_tokens:
        _LOGGER.warning("📱 No push tokens registered, skipping push notification")
        return

    _LOGGER.info(f"📱 Found {len(push_tokens)} registered push token(s)")

    # Prepare messages for Expo Push API
    messages = []
    skipped_tokens = 0
    for token_data in push_tokens.values():
        token = token_data.get("token")
        platform = token_data.get("platform", "unknown")
        device = token_data.get("device_name", "unknown")
        if token and token.startswith("ExponentPushToken"):
            messages.append({
                "to": token,
                "title": title,
                "body": message,
                "sound": "default",
                "priority": "high",
            })
            _LOGGER.debug(f"📱 Including token for {device} ({platform})")
        else:
            skipped_tokens += 1
            _LOGGER.warning(f"📱 Skipping non-Expo token for {device} ({platform}): {token[:30] if token else 'None'}...")

    if not messages:
        _LOGGER.warning(f"📱 No valid Expo push tokens found (skipped {skipped_tokens} invalid tokens)")
        return

    # Send to Expo Push API
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://exp.host/--/api/v2/push/send",
                json=messages,
                headers={"Content-Type": "application/json"},
            ) as response:
                if response.status == 200:
                    result = await response.json()
                    _LOGGER.info(f"📱 Push notification sent to {len(messages)} device(s)")
                else:
                    text = await response.text()
                    _LOGGER.error(f"Expo Push API error: {response.status} - {text}")
    except Exception as e:
        _LOGGER.error(f"Failed to send Expo push notification: {e}")


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
            await _wake_tesla_ev(hass, vehicle_vin)
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
                await _send_expo_push(hass, "PowerSync", "EV charging stopped - time window ended")
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
            await _wake_tesla_ev(hass, vehicle_vin)
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
        charge_limit_entity = await _get_tesla_ev_entity(
            hass,
            r"number\..*charge_limit$",
            vehicle_vin
        )

        if not charge_limit_entity:
            _LOGGER.error("Could not find Tesla charge_limit number entity")
            return False

        try:
            await _wake_tesla_ev(hass, vehicle_vin)
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

    # Clamp to valid range (1-48A typical, but allow up to 80A for some chargers)
    # Note: Tesla BLE max is typically 15A
    amps = max(1, min(80, int(amps)))

    # Try BLE first if configured (BLE max is typically 15A)
    ble_amps = min(amps, 15)  # BLE charger has lower max
    if ev_provider in (EV_PROVIDER_TESLA_BLE, EV_PROVIDER_BOTH):
        if _is_ble_available(hass, ble_prefix):
            result = await _set_ev_charging_amps_ble(hass, ble_prefix, ble_amps)
            if result or ev_provider == EV_PROVIDER_TESLA_BLE:
                return result

    # Use Fleet API
    if ev_provider in (EV_PROVIDER_FLEET_API, EV_PROVIDER_BOTH):
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
            await _wake_tesla_ev(hass, vehicle_vin)
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
            return False

    return False


# =============================================================================
# Dynamic EV Charging (adjusts amps based on battery discharge and grid import)
# =============================================================================

# Global storage for dynamic EV charging state per config entry
_dynamic_ev_state: Dict[str, Dict[str, Any]] = {}

# Global storage for regular EV charging scheduled stop (for stop_outside_window)
_ev_scheduled_stop: Dict[str, Any] = {}


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


async def _dynamic_ev_update(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    entry_id: str,
) -> None:
    """Periodic update function for dynamic EV charging.

    Uses same logic as the HA automation "Tesla Dynamic Charge Control for PW3":
    - Maintains a target battery charge rate (e.g., 5kW into battery)
    - Falls back to grid headroom if battery can't meet target
    - Adjusts EV amps based on available power
    - Stops charging if outside time window (when stop_outside_window is enabled)

    Battery power convention: Positive = discharging, Negative = charging
    Grid power convention: Positive = importing, Negative = exporting
    """
    state = _dynamic_ev_state.get(entry_id)
    if not state or not state.get("active"):
        return

    params = state.get("params", {})

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
                await _send_expo_push(hass, "PowerSync", "EV charging stopped - time window ended")
                return

    # target_battery_charge_kw: How much we want the battery to charge (positive = charging into battery)
    # e.g., 5.0 means we want 5kW going INTO the battery
    target_battery_charge_kw = params.get("target_battery_charge_kw", 5.0)
    max_grid_import_kw = params.get("max_grid_import_kw", 12.5)
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
    # - If battery has surplus (deficit > 0.1), use that surplus
    # - Otherwise, use grid headroom
    if battery_deficit_kw > 0.1:
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

    _LOGGER.debug(
        f"Dynamic EV: battery={battery_power_kw:.1f}kW (target={target_battery_power_kw:.1f}kW), "
        f"deficit={battery_deficit_kw:.1f}kW, grid={grid_power_kw:.1f}kW (max={max_grid_import_kw:.1f}kW), "
        f"headroom={grid_headroom_kw:.1f}kW, available={available_power_kw:.1f}kW, "
        f"current={current_amps}A, target={new_amps}A"
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


async def _action_start_ev_charging_dynamic(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    params: Dict[str, Any],
    context: Optional[Dict[str, Any]] = None
) -> bool:
    """
    Start dynamic EV charging that adjusts charge rate based on battery/grid.

    Uses same logic as HA automation "Tesla Dynamic Charge Control for PW3":
    - Maintains target battery charge rate while EV charges
    - Falls back to grid headroom if battery can't meet target

    Parameters:
        target_battery_charge_kw: Target battery charge rate in kW (default 5.0)
            e.g., 5.0 means maintain 5kW charging INTO the Powerwall
        max_grid_import_kw: Max grid import allowed (default 12.5)
        min_charge_amps: Minimum EV charge amps (default 5)
        max_charge_amps: Maximum EV charge amps (default 32)
        voltage: Assumed charging voltage for calculations (default 240)
        stop_outside_window: If True, stop charging when outside time window (default False)
        vehicle_vin: Optional VIN to filter by specific vehicle
    """
    from ..const import DOMAIN

    entry_id = config_entry.entry_id

    # Get parameters with defaults (support both old and new parameter names)
    target_battery_charge_kw = params.get(
        "target_battery_charge_kw",
        params.get("max_battery_discharge_kw", 5.0)  # Fallback for old param name
    )
    max_grid_import_kw = params.get("max_grid_import_kw", 12.5)
    min_charge_amps = params.get("min_charge_amps", 5)
    max_charge_amps = params.get("max_charge_amps", 32)
    voltage = params.get("voltage", 240)
    start_amps = params.get("start_amps", max_charge_amps)
    stop_outside_window = params.get("stop_outside_window", False)

    # Get time window from context (passed from automation trigger)
    time_window_start = context.get("time_window_start") if context else None
    time_window_end = context.get("time_window_end") if context else None
    timezone = context.get("timezone", "UTC") if context else "UTC"

    _LOGGER.info(
        f"⚡ Starting dynamic EV charging: target_battery_charge={target_battery_charge_kw}kW, "
        f"max_grid_import={max_grid_import_kw}kW, amps={min_charge_amps}-{max_charge_amps}A"
        f"{', stop_outside_window=' + str(time_window_start) + '-' + str(time_window_end) if stop_outside_window and time_window_start else ''}"
    )

    # Stop any existing dynamic charging for this entry
    await _action_stop_ev_charging_dynamic(hass, config_entry, {})

    # Start EV charging first
    start_success = await _action_start_ev_charging(hass, config_entry, params, context)
    if not start_success:
        _LOGGER.error("Dynamic EV: Failed to start EV charging")
        return False

    # Set initial amps
    amps_success = await _action_set_ev_charging_amps(
        hass, config_entry, {"amps": start_amps}
    )
    if not amps_success:
        _LOGGER.warning(f"Dynamic EV: Failed to set initial amps to {start_amps}A")

    # Create the periodic update callback
    async def periodic_update(now) -> None:
        await _dynamic_ev_update(hass, config_entry, entry_id)

    # Schedule the periodic update (every 30 seconds)
    cancel_timer = async_track_time_interval(
        hass,
        periodic_update,
        timedelta(seconds=30),
    )

    # Store state (including time window for stop_outside_window feature)
    _dynamic_ev_state[entry_id] = {
        "active": True,
        "params": {
            "target_battery_charge_kw": target_battery_charge_kw,
            "max_grid_import_kw": max_grid_import_kw,
            "min_charge_amps": min_charge_amps,
            "max_charge_amps": max_charge_amps,
            "voltage": voltage,
            "stop_outside_window": stop_outside_window,
            "time_window_start": time_window_start,
            "time_window_end": time_window_end,
            "timezone": timezone,
        },
        "current_amps": start_amps,
        "cancel_timer": cancel_timer,
    }

    # Also store in hass.data for access from other places
    if DOMAIN in hass.data and entry_id in hass.data[DOMAIN]:
        hass.data[DOMAIN][entry_id]["dynamic_ev_state"] = _dynamic_ev_state[entry_id]

    _LOGGER.info(f"⚡ Dynamic EV charging started (update every 30s)")
    return True


async def _action_stop_ev_charging_dynamic(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    params: Dict[str, Any]
) -> bool:
    """
    Stop dynamic EV charging and cancel the adjustment timer.

    Parameters:
        stop_charging: If True (default), also stop the EV charging. If False, just stop adjustments.
    """
    from ..const import DOMAIN

    entry_id = config_entry.entry_id
    stop_charging = params.get("stop_charging", True)

    state = _dynamic_ev_state.get(entry_id)
    if state:
        # Cancel the timer
        cancel_timer = state.get("cancel_timer")
        if cancel_timer:
            cancel_timer()
            _LOGGER.debug("Dynamic EV: Cancelled periodic timer")

        state["active"] = False
        del _dynamic_ev_state[entry_id]

        # Remove from hass.data
        if DOMAIN in hass.data and entry_id in hass.data[DOMAIN]:
            hass.data[DOMAIN][entry_id].pop("dynamic_ev_state", None)

        _LOGGER.info("⚡ Dynamic EV charging stopped")

    # Stop EV charging if requested
    if stop_charging:
        return await _action_stop_ev_charging(hass, config_entry, params)

    return True
