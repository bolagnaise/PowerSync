"""
Action execution logic for HA automations.

Supported actions:
- set_backup_reserve: Set battery backup reserve percentage
- preserve_charge: Prevent battery discharge (set export to "never")
- set_operation_mode: Set Powerwall operation mode
- force_discharge: Force battery discharge for a duration
- force_charge: Force battery charge for a duration
- curtail_inverter: Curtail AC-coupled solar inverter
- send_notification: Send push notification to user
"""

import logging
from typing import List, Dict, Any

from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry

_LOGGER = logging.getLogger(__name__)


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
    elif action_type == "send_notification":
        return await _action_send_notification(hass, params)
    else:
        _LOGGER.warning(f"Unknown action type: {action_type}")
        return False


async def _action_set_backup_reserve(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    params: Dict[str, Any]
) -> bool:
    """Set battery backup reserve percentage."""
    from ..const import DOMAIN, SERVICE_SET_BACKUP_RESERVE

    reserve_percent = params.get("reserve_percent")
    if reserve_percent is None:
        _LOGGER.error("set_backup_reserve: missing reserve_percent parameter")
        return False

    # Clamp to valid range
    reserve_percent = max(0, min(100, int(reserve_percent)))

    try:
        await hass.services.async_call(
            DOMAIN,
            SERVICE_SET_BACKUP_RESERVE,
            {"reserve_percent": reserve_percent},
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
    """Prevent battery discharge by setting export rule to 'never'."""
    from ..const import DOMAIN, SERVICE_SET_GRID_EXPORT

    try:
        await hass.services.async_call(
            DOMAIN,
            SERVICE_SET_GRID_EXPORT,
            {"export_rule": "never"},
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
    """Set battery operation mode."""
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
    from ..const import DOMAIN, SERVICE_FORCE_DISCHARGE

    duration_minutes = params.get("duration_minutes", 30)

    try:
        await hass.services.async_call(
            DOMAIN,
            SERVICE_FORCE_DISCHARGE,
            {"duration_minutes": duration_minutes},
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
    from ..const import DOMAIN, SERVICE_FORCE_CHARGE

    duration_minutes = params.get("duration_minutes", 60)

    try:
        await hass.services.async_call(
            DOMAIN,
            SERVICE_FORCE_CHARGE,
            {"duration_minutes": duration_minutes},
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
    """Curtail AC-coupled solar inverter."""
    from ..const import DOMAIN, SERVICE_CURTAIL_INVERTER

    power_limit_w = params.get("power_limit_w", 0)  # 0 = full curtailment

    try:
        await hass.services.async_call(
            DOMAIN,
            SERVICE_CURTAIL_INVERTER,
            {"power_limit_w": power_limit_w},
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
    """Send persistent notification."""
    message = params.get("message", "Automation triggered")
    title = params.get("title", "PowerSync Automation")

    try:
        await hass.services.async_call(
            "persistent_notification",
            "create",
            {
                "title": title,
                "message": message,
            },
            blocking=True,
        )
        return True
    except Exception as e:
        _LOGGER.error(f"Failed to send notification: {e}")
        return False
