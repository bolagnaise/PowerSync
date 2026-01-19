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
"""

import logging
from typing import List, Dict, Any, Optional

from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers import entity_registry as er, device_registry as dr

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
        _LOGGER.warning("No Tesla EV devices found")
        return None

    # Use first vehicle if no specific VIN provided
    target_device = tesla_devices[0]

    # Find matching entity for this device
    pattern = re.compile(entity_pattern, re.IGNORECASE)
    for entity in entity_registry.entities.values():
        if entity.device_id == target_device.id:
            if pattern.match(entity.entity_id):
                return entity.entity_id

    _LOGGER.warning(f"No entity matching pattern '{entity_pattern}' found for Tesla EV")
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


async def _wake_tesla_ble(hass: HomeAssistant, ble_prefix: str) -> bool:
    """Wake up Tesla via BLE."""
    wake_entity = TESLA_BLE_BUTTON_WAKE_UP.format(prefix=ble_prefix)
    state = hass.states.get(wake_entity)

    if state is None:
        _LOGGER.warning(f"Tesla BLE wake entity not found: {wake_entity}")
        return False

    try:
        await hass.services.async_call(
            "button",
            "press",
            {"entity_id": wake_entity},
            blocking=True,
        )
        _LOGGER.info(f"Sent wake command via Tesla BLE: {wake_entity}")
        import asyncio
        await asyncio.sleep(2)
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
    actions: List[Dict[str, Any]]
) -> bool:
    """
    Execute a list of automation actions.

    Args:
        hass: Home Assistant instance
        config_entry: Config entry for this integration
        actions: List of action dicts to execute

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

            result = await _execute_single_action(hass, config_entry, action_type, params)
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
    params: Dict[str, Any]
) -> bool:
    """
    Execute a single action.

    Args:
        hass: Home Assistant instance
        config_entry: Config entry
        action_type: Type of action to execute
        params: Action parameters

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
    # EV Charging Actions
    elif action_type == "start_ev_charging":
        return await _action_start_ev_charging(hass, config_entry, params)
    elif action_type == "stop_ev_charging":
        return await _action_stop_ev_charging(hass, config_entry, params)
    elif action_type == "set_ev_charge_limit":
        return await _action_set_ev_charge_limit(hass, config_entry, params)
    elif action_type == "set_ev_charging_amps":
        return await _action_set_ev_charging_amps(hass, config_entry, params)
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
    """Send notification via persistent notification and Expo Push."""
    message = params.get("message", "Automation triggered")
    title = params.get("title", "PowerSync")

    try:
        # Send persistent notification (shows in HA UI)
        await hass.services.async_call(
            "persistent_notification",
            "create",
            {
                "title": title,
                "message": message,
            },
            blocking=True,
        )

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
        _LOGGER.debug("No push tokens registered, skipping push notification")
        return

    # Prepare messages for Expo Push API
    messages = []
    for token_data in push_tokens.values():
        token = token_data.get("token")
        if token and token.startswith("ExponentPushToken"):
            messages.append({
                "to": token,
                "title": title,
                "body": message,
                "sound": "default",
                "priority": "high",
            })

    if not messages:
        _LOGGER.debug("No valid Expo push tokens found")
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
    params: Dict[str, Any]
) -> bool:
    """
    Start EV charging via Tesla Fleet/Teslemetry or Tesla BLE.

    Uses BLE if configured as primary or both, falls back to Fleet API.
    """
    ev_config = _get_ev_config(config_entry)
    ev_provider = ev_config["ev_provider"]
    ble_prefix = ev_config["ble_prefix"]
    vehicle_vin = params.get("vehicle_vin")

    # Try BLE first if configured
    if ev_provider in (EV_PROVIDER_TESLA_BLE, EV_PROVIDER_BOTH):
        if _is_ble_available(hass, ble_prefix):
            result = await _start_ev_charging_ble(hass, ble_prefix)
            if result or ev_provider == EV_PROVIDER_TESLA_BLE:
                return result
            # Fall through to Fleet API if BLE failed and both are configured

    # Use Fleet API
    if ev_provider in (EV_PROVIDER_FLEET_API, EV_PROVIDER_BOTH):
        charge_start_entity = await _get_tesla_ev_entity(
            hass,
            r"button\..*charge_start$",
            vehicle_vin
        )

        if not charge_start_entity:
            _LOGGER.error("Could not find Tesla charge_start button entity")
            return False

        try:
            await _wake_tesla_ev(hass, vehicle_vin)
            await hass.services.async_call(
                "button",
                "press",
                {"entity_id": charge_start_entity},
                blocking=True,
            )
            _LOGGER.info(f"Started EV charging via {charge_start_entity}")
            return True
        except Exception as e:
            _LOGGER.error(f"Failed to start EV charging: {e}")
            return False

    return False


async def _action_stop_ev_charging(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    params: Dict[str, Any]
) -> bool:
    """
    Stop EV charging via Tesla Fleet/Teslemetry or Tesla BLE.

    Uses BLE if configured as primary or both, falls back to Fleet API.
    """
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
        charge_stop_entity = await _get_tesla_ev_entity(
            hass,
            r"button\..*charge_stop$",
            vehicle_vin
        )

        if not charge_stop_entity:
            _LOGGER.error("Could not find Tesla charge_stop button entity")
            return False

        try:
            await _wake_tesla_ev(hass, vehicle_vin)
            await hass.services.async_call(
                "button",
                "press",
                {"entity_id": charge_stop_entity},
                blocking=True,
            )
            _LOGGER.info(f"Stopped EV charging via {charge_stop_entity}")
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
        charging_amps_entity = await _get_tesla_ev_entity(
            hass,
            r"number\..*charging_amps$",
            vehicle_vin
        )

        if not charging_amps_entity:
            _LOGGER.error("Could not find Tesla charging_amps number entity")
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
