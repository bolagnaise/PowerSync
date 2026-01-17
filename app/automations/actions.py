"""
Action execution logic for automations.

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
import json
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional

from app import db
from app.models import AutomationAction, User

_LOGGER = logging.getLogger(__name__)


def execute_actions(actions: List[AutomationAction], user: User) -> bool:
    """
    Execute a list of automation actions.

    Args:
        actions: List of actions to execute
        user: The user who owns the automation

    Returns:
        True if at least one action executed successfully
    """
    success_count = 0

    for action in actions:
        try:
            params = json.loads(action.parameters) if action.parameters else {}
            result = _execute_single_action(action.action_type, params, user)
            if result:
                success_count += 1
                _LOGGER.info(f"Executed action '{action.action_type}' for user {user.id}")
            else:
                _LOGGER.warning(f"Action '{action.action_type}' returned False for user {user.id}")
        except json.JSONDecodeError:
            _LOGGER.error(f"Invalid JSON in action parameters: {action.parameters}")
        except Exception as e:
            _LOGGER.error(f"Error executing action '{action.action_type}': {e}")

    return success_count > 0


def _execute_single_action(action_type: str, params: Dict[str, Any], user: User) -> bool:
    """
    Execute a single action.

    Args:
        action_type: Type of action to execute
        params: Action parameters
        user: The user

    Returns:
        True if action executed successfully
    """
    if action_type == 'set_backup_reserve':
        return _action_set_backup_reserve(params, user)
    elif action_type == 'preserve_charge':
        return _action_preserve_charge(user)
    elif action_type == 'set_operation_mode':
        return _action_set_operation_mode(params, user)
    elif action_type == 'force_discharge':
        return _action_force_discharge(params, user)
    elif action_type == 'force_charge':
        return _action_force_charge(params, user)
    elif action_type == 'curtail_inverter':
        return _action_curtail_inverter(params, user)
    elif action_type == 'send_notification':
        return _action_send_notification(params, user)
    else:
        _LOGGER.warning(f"Unknown action type: {action_type}")
        return False


def _action_set_backup_reserve(params: Dict[str, Any], user: User) -> bool:
    """Set battery backup reserve percentage."""
    # Accept both "percent" and "reserve_percent" for flexibility
    reserve_percent = params.get('percent') or params.get('reserve_percent')
    if reserve_percent is None:
        _LOGGER.error("set_backup_reserve: missing percent parameter")
        return False

    # Clamp to valid range
    reserve_percent = max(0, min(100, int(reserve_percent)))

    if user.battery_system == 'tesla':
        from app.api_clients import get_tesla_client
        client = get_tesla_client(user)
        if client:
            try:
                result = client.set_backup_reserve(user.tesla_energy_site_id, reserve_percent)
                return result is not None
            except Exception as e:
                _LOGGER.error(f"Failed to set backup reserve: {e}")
                return False
    elif user.battery_system == 'sigenergy':
        # Sigenergy doesn't have backup reserve concept in the same way
        _LOGGER.warning("set_backup_reserve not supported for Sigenergy")
        return False

    return False


def _action_preserve_charge(user: User) -> bool:
    """Prevent battery discharge by setting export rule to 'never'."""
    if user.battery_system == 'tesla':
        from app.api_clients import get_tesla_client
        client = get_tesla_client(user)
        if client:
            try:
                # Set grid export to 'never' to prevent discharge
                result = client.set_grid_export(user.tesla_energy_site_id, 'never')
                if result:
                    user.current_export_rule = 'never'
                    user.current_export_rule_updated = datetime.utcnow()
                    db.session.commit()
                return result is not None
            except Exception as e:
                _LOGGER.error(f"Failed to preserve charge: {e}")
                return False
    elif user.battery_system == 'sigenergy':
        from app.sigenergy_modbus import get_sigenergy_modbus_client
        try:
            client = get_sigenergy_modbus_client(user)
            if not client:
                _LOGGER.error("Sigenergy Modbus not configured")
                return False
            # Set discharge rate limit to 0 to prevent discharge
            result = client.set_discharge_rate_limit(0)
            return result
        except Exception as e:
            _LOGGER.error(f"Failed to preserve charge (Sigenergy): {e}")
            return False

    return False


def _action_set_operation_mode(params: Dict[str, Any], user: User) -> bool:
    """Set battery operation mode."""
    mode = params.get('mode')
    if not mode:
        _LOGGER.error("set_operation_mode: missing mode parameter")
        return False

    valid_modes = ['self_consumption', 'autonomous', 'backup']
    if mode not in valid_modes:
        _LOGGER.error(f"set_operation_mode: invalid mode '{mode}'")
        return False

    if user.battery_system == 'tesla':
        from app.api_clients import get_tesla_client
        client = get_tesla_client(user)
        if client:
            try:
                result = client.set_operation_mode(user.tesla_energy_site_id, mode)
                return result is not None
            except Exception as e:
                _LOGGER.error(f"Failed to set operation mode: {e}")
                return False
    elif user.battery_system == 'sigenergy':
        # Sigenergy has different modes - map as best we can
        _LOGGER.warning("set_operation_mode: Sigenergy mode mapping not fully implemented")
        return False

    return False


def _action_force_discharge(params: Dict[str, Any], user: User) -> bool:
    """Force battery discharge for a specified duration."""
    # Accept both "duration" and "duration_minutes" for flexibility
    duration_minutes = params.get('duration') or params.get('duration_minutes', 30)

    if user.battery_system == 'tesla':
        from app.api_clients import get_tesla_client
        from app.models import SavedTOUProfile
        import json as json_module

        client = get_tesla_client(user)
        if not client:
            return False

        try:
            # Save current tariff first
            current_tariff = client.get_tariff(user.tesla_energy_site_id)
            if current_tariff:
                # Save as backup
                saved_profile = SavedTOUProfile(
                    user_id=user.id,
                    name=f"Auto-saved before force discharge ({datetime.utcnow().strftime('%Y-%m-%d %H:%M')})",
                    source_type='automation',
                    tariff_json=json_module.dumps(current_tariff),
                    fetched_from_tesla_at=datetime.utcnow()
                )
                db.session.add(saved_profile)
                db.session.flush()
                user.manual_discharge_saved_tariff_id = saved_profile.id

            # Set force discharge state
            user.manual_discharge_active = True
            user.manual_discharge_expires_at = datetime.utcnow() + timedelta(minutes=duration_minutes)

            # Set operation mode to self_consumption with high export prices
            client.set_operation_mode(user.tesla_energy_site_id, 'autonomous')

            # Set grid export to battery_ok to allow discharge
            client.set_grid_export(user.tesla_energy_site_id, 'battery_ok')

            db.session.commit()
            _LOGGER.info(f"Force discharge activated for {duration_minutes} minutes")
            return True

        except Exception as e:
            _LOGGER.error(f"Failed to activate force discharge: {e}")
            db.session.rollback()
            return False

    elif user.battery_system == 'sigenergy':
        from app.sigenergy_modbus import get_sigenergy_modbus_client
        try:
            client = get_sigenergy_modbus_client(user)
            if not client:
                _LOGGER.error("Sigenergy Modbus not configured")
                return False
            # Set high discharge rate limit and remove export limit to allow discharge
            client.set_discharge_rate_limit(10.0)  # 10kW max discharge
            client.restore_export_limit()  # Remove export limit
            user.manual_discharge_active = True
            user.manual_discharge_expires_at = datetime.utcnow() + timedelta(minutes=duration_minutes)
            db.session.commit()
            _LOGGER.info(f"Force discharge activated for Sigenergy ({duration_minutes} minutes)")
            return True
        except Exception as e:
            _LOGGER.error(f"Failed to force discharge (Sigenergy): {e}")
            return False

    return False


def _action_force_charge(params: Dict[str, Any], user: User) -> bool:
    """Force battery charge for a specified duration."""
    # Accept both "duration" and "duration_minutes" for flexibility
    duration_minutes = params.get('duration') or params.get('duration_minutes', 60)
    target_percent = params.get('target_percent') or params.get('percent', 100)

    if user.battery_system == 'tesla':
        from app.api_clients import get_tesla_client
        from app.models import SavedTOUProfile
        import json as json_module

        client = get_tesla_client(user)
        if not client:
            return False

        try:
            # Get current status to save backup reserve
            status = client.get_site_live_status(user.tesla_energy_site_id)
            if status:
                user.manual_charge_saved_backup_reserve = status.get('backup_reserve_percent', 20)

            # Save current tariff
            current_tariff = client.get_tariff(user.tesla_energy_site_id)
            if current_tariff:
                saved_profile = SavedTOUProfile(
                    user_id=user.id,
                    name=f"Auto-saved before force charge ({datetime.utcnow().strftime('%Y-%m-%d %H:%M')})",
                    source_type='automation',
                    tariff_json=json_module.dumps(current_tariff),
                    fetched_from_tesla_at=datetime.utcnow()
                )
                db.session.add(saved_profile)
                db.session.flush()
                user.manual_charge_saved_tariff_id = saved_profile.id

            # Set force charge state
            user.manual_charge_active = True
            user.manual_charge_expires_at = datetime.utcnow() + timedelta(minutes=duration_minutes)

            # Set backup reserve to target (forces charging)
            client.set_backup_reserve(user.tesla_energy_site_id, target_percent)

            # Enable grid charging
            client.set_grid_charging(user.tesla_energy_site_id, True)

            db.session.commit()
            _LOGGER.info(f"Force charge activated for {duration_minutes} minutes (target: {target_percent}%)")
            return True

        except Exception as e:
            _LOGGER.error(f"Failed to activate force charge: {e}")
            db.session.rollback()
            return False

    elif user.battery_system == 'sigenergy':
        from app.sigenergy_modbus import get_sigenergy_modbus_client
        try:
            client = get_sigenergy_modbus_client(user)
            if not client:
                _LOGGER.error("Sigenergy Modbus not configured")
                return False
            # Set high charge rate limit and zero discharge to force charging
            client.set_charge_rate_limit(10.0)  # 10kW max charge
            client.set_discharge_rate_limit(0)  # Prevent discharge
            user.manual_charge_active = True
            user.manual_charge_expires_at = datetime.utcnow() + timedelta(minutes=duration_minutes)
            db.session.commit()
            _LOGGER.info(f"Force charge activated for Sigenergy ({duration_minutes} minutes)")
            return True
        except Exception as e:
            _LOGGER.error(f"Failed to force charge (Sigenergy): {e}")
            return False

    return False


def _action_curtail_inverter(params: Dict[str, Any], user: User) -> bool:
    """Curtail AC-coupled solar inverter."""
    if not user.inverter_curtailment_enabled:
        _LOGGER.warning("Inverter curtailment not enabled for user")
        return False

    # Support both "mode" (load_following/shutdown) and "power_limit_w"
    mode = params.get('mode', 'load_following')
    power_limit_w = params.get('power_limit_w')

    # If mode is shutdown, set power_limit_w to 0
    if mode == 'shutdown' and power_limit_w is None:
        power_limit_w = 0
    elif power_limit_w is None:
        power_limit_w = 0  # Default to full curtailment

    from app.inverters import get_inverter_controller

    try:
        controller = get_inverter_controller(
            brand=user.inverter_brand,
            host=user.inverter_host,
            port=user.inverter_port or 502,
            slave_id=user.inverter_slave_id or 1,
            model=user.inverter_model,
            token=user.inverter_token,
            load_following=user.fronius_load_following,
            enphase_username=user.enphase_username,
            enphase_password=user.enphase_password,
            enphase_serial=user.enphase_serial,
        )

        if not controller:
            _LOGGER.error("Failed to create inverter controller")
            return False

        if power_limit_w == 0:
            # Full curtailment
            result = controller.curtail()
        else:
            # Partial curtailment
            result = controller.set_power_limit(power_limit_w)

        if result:
            user.inverter_last_state = 'curtailed'
            user.inverter_last_state_updated = datetime.utcnow()
            user.inverter_power_limit_w = power_limit_w
            db.session.commit()

        return result

    except Exception as e:
        _LOGGER.error(f"Failed to curtail inverter: {e}")
        return False


def _action_send_notification(params: Dict[str, Any], user: User) -> bool:
    """Send push notification to user."""
    message = params.get('message', 'Automation triggered')
    title = params.get('title', 'PowerSync Automation')

    from app.push_notifications import send_push_notification

    if not user.apns_device_token:
        _LOGGER.warning(f"Cannot send notification - user {user.id} has no device token registered")
        return False

    try:
        success = send_push_notification(user.apns_device_token, title, message, {
            "type": "automation_action",
            "title": title,
            "message": message
        })
        return success
    except Exception as e:
        _LOGGER.error(f"Failed to send notification: {e}")
        return False
