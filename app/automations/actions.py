"""
Action execution logic for automations.

Supported actions:
- set_backup_reserve: Set battery backup reserve percentage (Tesla)
- preserve_charge: Prevent battery discharge
- set_operation_mode: Set Powerwall operation mode (Tesla)
- force_discharge: Force battery discharge for a duration
- force_charge: Force battery charge for a duration
- curtail_inverter: Curtail AC-coupled solar inverter
- restore_inverter: Restore inverter to normal operation
- send_notification: Send push notification to user
- set_grid_export: Set grid export rule (Tesla)
- set_grid_charging: Enable/disable grid charging (Tesla)
- restore_normal: Restore normal battery operation
- set_charge_rate: Set charge rate limit (Sigenergy)
- set_discharge_rate: Set discharge rate limit (Sigenergy)
- set_export_limit: Set export power limit (Sigenergy)
- start_ev_charging: Start EV charging (Tesla Fleet API)
- stop_ev_charging: Stop EV charging (Tesla Fleet API)
- set_ev_charge_limit: Set EV charge limit percentage (Tesla Fleet API)
- set_ev_charging_amps: Set EV charging amperage (Tesla Fleet API)
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
    elif action_type == 'restore_inverter':
        return _action_restore_inverter(user)
    elif action_type == 'send_notification':
        return _action_send_notification(params, user)
    elif action_type == 'set_grid_export':
        return _action_set_grid_export(params, user)
    elif action_type == 'set_grid_charging':
        return _action_set_grid_charging(params, user)
    elif action_type == 'restore_normal':
        return _action_restore_normal(user)
    elif action_type == 'set_charge_rate':
        return _action_set_charge_rate(params, user)
    elif action_type == 'set_discharge_rate':
        return _action_set_discharge_rate(params, user)
    elif action_type == 'set_export_limit':
        return _action_set_export_limit(params, user)
    elif action_type == 'start_ev_charging':
        return _action_start_ev_charging(params, user)
    elif action_type == 'stop_ev_charging':
        return _action_stop_ev_charging(params, user)
    elif action_type == 'set_ev_charge_limit':
        return _action_set_ev_charge_limit(params, user)
    elif action_type == 'set_ev_charging_amps':
        return _action_set_ev_charging_amps(params, user)
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

    # Support both "mode"/"curtailment_mode" (load_following/shutdown) and "power_limit_w"
    mode = params.get('mode') or params.get('curtailment_mode', 'load_following')
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


def _action_set_grid_export(params: Dict[str, Any], user: User) -> bool:
    """Set grid export rule (Tesla only)."""
    # Accept both "rule" and "grid_export_rule" for flexibility
    rule = params.get('rule') or params.get('grid_export_rule')
    if not rule:
        _LOGGER.error("set_grid_export: missing rule parameter")
        return False

    valid_rules = ['never', 'pv_only', 'battery_ok']
    if rule not in valid_rules:
        _LOGGER.error(f"set_grid_export: invalid rule '{rule}'")
        return False

    if user.battery_system != 'tesla':
        _LOGGER.warning("set_grid_export only supported for Tesla")
        return False

    from app.api_clients import get_tesla_client
    client = get_tesla_client(user)
    if not client:
        return False

    try:
        result = client.set_grid_export(user.tesla_energy_site_id, rule)
        if result:
            user.current_export_rule = rule
            user.current_export_rule_updated = datetime.utcnow()
            db.session.commit()
        return result is not None
    except Exception as e:
        _LOGGER.error(f"Failed to set grid export: {e}")
        return False


def _action_set_grid_charging(params: Dict[str, Any], user: User) -> bool:
    """Enable or disable grid charging (Tesla only)."""
    enabled = params.get('enabled', True)

    if user.battery_system != 'tesla':
        _LOGGER.warning("set_grid_charging only supported for Tesla")
        return False

    from app.api_clients import get_tesla_client
    client = get_tesla_client(user)
    if not client:
        return False

    try:
        result = client.set_grid_charging(user.tesla_energy_site_id, enabled)
        return result is not None
    except Exception as e:
        _LOGGER.error(f"Failed to set grid charging: {e}")
        return False


def _action_restore_normal(user: User) -> bool:
    """Restore normal battery operation (cancel force charge/discharge)."""
    try:
        # Clear force charge/discharge states
        user.manual_charge_active = False
        user.manual_charge_expires_at = None
        user.manual_discharge_active = False
        user.manual_discharge_expires_at = None

        if user.battery_system == 'tesla':
            from app.api_clients import get_tesla_client
            from app.models import SavedTOUProfile

            client = get_tesla_client(user)
            if not client:
                return False

            # Restore saved tariff if available
            if user.manual_charge_saved_tariff_id:
                saved = SavedTOUProfile.query.get(user.manual_charge_saved_tariff_id)
                if saved:
                    import json as json_module
                    tariff = json_module.loads(saved.tariff_json)
                    client.set_tariff(user.tesla_energy_site_id, tariff)
                user.manual_charge_saved_tariff_id = None

            if user.manual_discharge_saved_tariff_id:
                saved = SavedTOUProfile.query.get(user.manual_discharge_saved_tariff_id)
                if saved:
                    import json as json_module
                    tariff = json_module.loads(saved.tariff_json)
                    client.set_tariff(user.tesla_energy_site_id, tariff)
                user.manual_discharge_saved_tariff_id = None

            # Restore backup reserve if saved
            if user.manual_charge_saved_backup_reserve is not None:
                client.set_backup_reserve(user.tesla_energy_site_id, user.manual_charge_saved_backup_reserve)
                user.manual_charge_saved_backup_reserve = None

        elif user.battery_system == 'sigenergy':
            from app.sigenergy_modbus import get_sigenergy_modbus_client
            client = get_sigenergy_modbus_client(user)
            if client:
                # Restore default rate limits (max rates)
                client.set_charge_rate_limit(10.0)
                client.set_discharge_rate_limit(10.0)
                client.restore_export_limit()

        db.session.commit()
        _LOGGER.info(f"Restored normal operation for user {user.id}")
        return True

    except Exception as e:
        _LOGGER.error(f"Failed to restore normal operation: {e}")
        db.session.rollback()
        return False


def _action_set_charge_rate(params: Dict[str, Any], user: User) -> bool:
    """Set charge rate limit (Sigenergy only)."""
    # Accept both "rate" and "rate_limit_kw" for flexibility
    rate_kw = params.get('rate') or params.get('rate_limit_kw')
    if rate_kw is None:
        _LOGGER.error("set_charge_rate: missing rate parameter")
        return False

    if user.battery_system != 'sigenergy':
        _LOGGER.warning("set_charge_rate only supported for Sigenergy")
        return False

    from app.sigenergy_modbus import get_sigenergy_modbus_client

    try:
        client = get_sigenergy_modbus_client(user)
        if not client:
            _LOGGER.error("Sigenergy Modbus not configured")
            return False

        # Clamp to valid range (0-10 kW typical)
        rate_kw = max(0, min(10, float(rate_kw)))
        result = client.set_charge_rate_limit(rate_kw)
        _LOGGER.info(f"Set charge rate limit to {rate_kw} kW")
        return result
    except Exception as e:
        _LOGGER.error(f"Failed to set charge rate: {e}")
        return False


def _action_set_discharge_rate(params: Dict[str, Any], user: User) -> bool:
    """Set discharge rate limit (Sigenergy only)."""
    # Accept both "rate" and "rate_limit_kw" for flexibility
    rate_kw = params.get('rate') or params.get('rate_limit_kw')
    if rate_kw is None:
        _LOGGER.error("set_discharge_rate: missing rate parameter")
        return False

    if user.battery_system != 'sigenergy':
        _LOGGER.warning("set_discharge_rate only supported for Sigenergy")
        return False

    from app.sigenergy_modbus import get_sigenergy_modbus_client

    try:
        client = get_sigenergy_modbus_client(user)
        if not client:
            _LOGGER.error("Sigenergy Modbus not configured")
            return False

        # Clamp to valid range (0-10 kW typical)
        rate_kw = max(0, min(10, float(rate_kw)))
        result = client.set_discharge_rate_limit(rate_kw)
        _LOGGER.info(f"Set discharge rate limit to {rate_kw} kW")
        return result
    except Exception as e:
        _LOGGER.error(f"Failed to set discharge rate: {e}")
        return False


def _action_set_export_limit(params: Dict[str, Any], user: User) -> bool:
    """Set export power limit (Sigenergy only)."""
    # Accept both "limit" and "export_limit_kw" for flexibility
    # None means unlimited
    limit_kw = params.get('limit') or params.get('export_limit_kw')

    if user.battery_system != 'sigenergy':
        _LOGGER.warning("set_export_limit only supported for Sigenergy")
        return False

    from app.sigenergy_modbus import get_sigenergy_modbus_client

    try:
        client = get_sigenergy_modbus_client(user)
        if not client:
            _LOGGER.error("Sigenergy Modbus not configured")
            return False

        if limit_kw is None:
            # Unlimited export
            result = client.restore_export_limit()
            _LOGGER.info("Restored unlimited export")
        else:
            # Clamp to valid range (0-10 kW typical)
            limit_kw = max(0, min(10, float(limit_kw)))
            result = client.set_export_limit(limit_kw)
            _LOGGER.info(f"Set export limit to {limit_kw} kW")
        return result
    except Exception as e:
        _LOGGER.error(f"Failed to set export limit: {e}")
        return False


def _action_restore_inverter(user: User) -> bool:
    """Restore inverter to normal operation."""
    if not user.inverter_curtailment_enabled:
        _LOGGER.warning("Inverter curtailment not enabled for user")
        return False

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

        result = controller.restore()

        if result:
            user.inverter_last_state = 'normal'
            user.inverter_last_state_updated = datetime.utcnow()
            user.inverter_power_limit_w = None
            db.session.commit()

        _LOGGER.info("Restored inverter to normal operation")
        return result

    except Exception as e:
        _LOGGER.error(f"Failed to restore inverter: {e}")
        return False


# =============================================================================
# EV Charging Actions (Tesla Fleet API)
# =============================================================================

def _get_ev_vehicle_and_client(params: Dict[str, Any], user: User):
    """
    Get the target vehicle and Fleet API client.

    Args:
        params: Action parameters (may contain vehicle_id)
        user: The user

    Returns:
        Tuple of (TeslaVehicle, TeslaFleetClient) or (None, None) if not available
    """
    from app.models import TeslaVehicle
    from app.ev.tesla_fleet import get_fleet_client_for_user

    # Get Fleet API client
    client = get_fleet_client_for_user(user)
    if not client:
        _LOGGER.error("Fleet API not configured for user")
        return None, None

    # Get target vehicle
    vehicle_id = params.get('vehicle_id')
    if vehicle_id:
        vehicle = TeslaVehicle.query.filter_by(user_id=user.id, id=vehicle_id).first()
    else:
        # Use first vehicle with automations enabled
        vehicle = TeslaVehicle.query.filter_by(
            user_id=user.id,
            enable_automations=True
        ).first()

    if not vehicle:
        _LOGGER.error("No EV vehicle found for action")
        return None, None

    return vehicle, client


def _action_start_ev_charging(params: Dict[str, Any], user: User) -> bool:
    """Start EV charging via Tesla Fleet API."""
    vehicle, client = _get_ev_vehicle_and_client(params, user)
    if not vehicle or not client:
        return False

    try:
        # Check if vehicle is plugged in
        if not vehicle.is_plugged_in:
            _LOGGER.warning(f"Cannot start charging - {vehicle.display_name} is not plugged in")
            return False

        # Wake up vehicle if needed
        if not vehicle.is_online:
            _LOGGER.info(f"Waking up {vehicle.display_name}...")
            client.wake_up_vehicle(vehicle.vehicle_id)
            # Give it a moment to wake
            import time
            time.sleep(5)

        # Start charging
        result = client.charge_start(vehicle.vehicle_id)
        if result:
            vehicle.charging_state = 'Charging'
            vehicle.data_updated_at = datetime.utcnow()
            db.session.commit()
            _LOGGER.info(f"Started charging {vehicle.display_name}")
            return True
        return False

    except Exception as e:
        _LOGGER.error(f"Failed to start EV charging: {e}")
        return False


def _action_stop_ev_charging(params: Dict[str, Any], user: User) -> bool:
    """Stop EV charging via Tesla Fleet API."""
    vehicle, client = _get_ev_vehicle_and_client(params, user)
    if not vehicle or not client:
        return False

    try:
        # Wake up vehicle if needed
        if not vehicle.is_online:
            _LOGGER.info(f"Waking up {vehicle.display_name}...")
            client.wake_up_vehicle(vehicle.vehicle_id)
            import time
            time.sleep(5)

        # Stop charging
        result = client.charge_stop(vehicle.vehicle_id)
        if result:
            vehicle.charging_state = 'Stopped'
            vehicle.data_updated_at = datetime.utcnow()
            db.session.commit()
            _LOGGER.info(f"Stopped charging {vehicle.display_name}")
            return True
        return False

    except Exception as e:
        _LOGGER.error(f"Failed to stop EV charging: {e}")
        return False


def _action_set_ev_charge_limit(params: Dict[str, Any], user: User) -> bool:
    """Set EV charge limit percentage via Tesla Fleet API."""
    vehicle, client = _get_ev_vehicle_and_client(params, user)
    if not vehicle or not client:
        return False

    # Accept both "percent" and "limit" for flexibility
    percent = params.get('percent') or params.get('limit')
    if percent is None:
        _LOGGER.error("set_ev_charge_limit: missing percent parameter")
        return False

    # Clamp to valid range (50-100%)
    percent = max(50, min(100, int(percent)))

    try:
        # Wake up vehicle if needed
        if not vehicle.is_online:
            _LOGGER.info(f"Waking up {vehicle.display_name}...")
            client.wake_up_vehicle(vehicle.vehicle_id)
            import time
            time.sleep(5)

        # Set charge limit
        result = client.set_charge_limit(vehicle.vehicle_id, percent)
        if result:
            vehicle.charge_limit_soc = percent
            vehicle.data_updated_at = datetime.utcnow()
            db.session.commit()
            _LOGGER.info(f"Set charge limit to {percent}% for {vehicle.display_name}")
            return True
        return False

    except Exception as e:
        _LOGGER.error(f"Failed to set EV charge limit: {e}")
        return False


def _action_set_ev_charging_amps(params: Dict[str, Any], user: User) -> bool:
    """Set EV charging amperage via Tesla Fleet API."""
    vehicle, client = _get_ev_vehicle_and_client(params, user)
    if not vehicle or not client:
        return False

    # Accept both "amps" and "charging_amps" for flexibility
    amps = params.get('amps') or params.get('charging_amps')
    if amps is None:
        _LOGGER.error("set_ev_charging_amps: missing amps parameter")
        return False

    # Clamp to valid range (typically 1-48A)
    amps = max(1, min(48, int(amps)))

    try:
        # Wake up vehicle if needed
        if not vehicle.is_online:
            _LOGGER.info(f"Waking up {vehicle.display_name}...")
            client.wake_up_vehicle(vehicle.vehicle_id)
            import time
            time.sleep(5)

        # Set charging amps
        result = client.set_charging_amps(vehicle.vehicle_id, amps)
        if result:
            vehicle.charge_current_request = amps
            vehicle.data_updated_at = datetime.utcnow()
            db.session.commit()
            _LOGGER.info(f"Set charging amps to {amps}A for {vehicle.display_name}")
            return True
        return False

    except Exception as e:
        _LOGGER.error(f"Failed to set EV charging amps: {e}")
        return False
