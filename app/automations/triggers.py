"""
Trigger evaluation logic for automations.

Supports the following trigger types:
- time: Trigger at specific time(s) of day
- battery: Trigger based on battery state of charge
- flow: Trigger based on power flow (solar, grid, battery, home usage)
- price: Trigger based on electricity price thresholds
- grid: Trigger when grid status changes (off-grid/on-grid)
- weather: Trigger based on weather conditions
- ev: Trigger based on EV charging state (connected, charging, SoC)
- ocpp: Trigger based on OCPP charger state (connected, charging, energy)
- solar_forecast: Trigger based on solar forecast (today/tomorrow above/below kWh threshold)
"""

import logging
from dataclasses import dataclass
from datetime import datetime, time as dt_time, timedelta
from typing import Dict, Any, Optional

from app import db
from app.models import AutomationTrigger, User

_LOGGER = logging.getLogger(__name__)


@dataclass
class TriggerResult:
    """Result of trigger evaluation."""
    triggered: bool
    reason: str = ""


def evaluate_trigger(
    trigger: AutomationTrigger,
    current_state: Dict[str, Any],
    user: User
) -> TriggerResult:
    """
    Evaluate a trigger against current state.

    Args:
        trigger: The trigger to evaluate
        current_state: Current system state (battery %, prices, etc.)
        user: The user who owns the automation

    Returns:
        TriggerResult indicating if trigger condition is met
    """
    # Check time window constraint first (applies to all trigger types)
    if not _is_within_time_window(trigger, current_state):
        return TriggerResult(triggered=False, reason="Outside time window")

    # Evaluate based on trigger type
    if trigger.trigger_type == 'time':
        return _evaluate_time_trigger(trigger, current_state, user)
    elif trigger.trigger_type == 'battery':
        return _evaluate_battery_trigger(trigger, current_state)
    elif trigger.trigger_type == 'flow':
        return _evaluate_flow_trigger(trigger, current_state)
    elif trigger.trigger_type == 'price':
        return _evaluate_price_trigger(trigger, current_state)
    elif trigger.trigger_type == 'grid':
        return _evaluate_grid_trigger(trigger, current_state)
    elif trigger.trigger_type == 'weather':
        return _evaluate_weather_trigger(trigger, current_state)
    elif trigger.trigger_type == 'ev':
        return _evaluate_ev_trigger(trigger, current_state, user)
    elif trigger.trigger_type == 'ocpp':
        return _evaluate_ocpp_trigger(trigger, current_state, user)
    elif trigger.trigger_type == 'solar_forecast':
        return _evaluate_solar_forecast_trigger(trigger, current_state)
    else:
        _LOGGER.warning(f"Unknown trigger type: {trigger.trigger_type}")
        return TriggerResult(triggered=False, reason=f"Unknown trigger type: {trigger.trigger_type}")


def _is_within_time_window(trigger: AutomationTrigger, current_state: Dict[str, Any]) -> bool:
    """Check if current time is within the optional time window (uses user's local timezone)."""
    if not trigger.time_window_start and not trigger.time_window_end:
        return True  # No window constraint

    current_datetime = current_state.get('current_time', datetime.now())
    current_time = current_datetime.time()
    user_timezone = current_state.get('user_timezone', 'UTC')

    start = trigger.time_window_start
    end = trigger.time_window_end

    if not start or not end:
        return True  # Incomplete window, allow

    # Handle overnight windows (e.g., 22:00 to 06:00)
    if start <= end:
        # Normal window (e.g., 09:00 to 17:00)
        is_within = start <= current_time <= end
    else:
        # Overnight window (e.g., 22:00 to 06:00)
        is_within = current_time >= start or current_time <= end

    _LOGGER.debug(
        f"Time window check: {current_time.strftime('%H:%M')} ({user_timezone}) "
        f"window={start.strftime('%H:%M')}-{end.strftime('%H:%M')}, within={is_within}"
    )

    return is_within


def _evaluate_time_trigger(
    trigger: AutomationTrigger,
    current_state: Dict[str, Any],
    user: User
) -> TriggerResult:
    """
    Evaluate time-based trigger.

    Triggers once per day at the specified time (with 1-minute tolerance).
    Respects repeat_days (0=Sun, 1=Mon, ..., 6=Sat).
    Uses user's local timezone from their site configuration.
    """
    if not trigger.time_of_day:
        return TriggerResult(triggered=False, reason="No time_of_day set")

    now = current_state.get('current_time', datetime.now())
    user_timezone = current_state.get('user_timezone', 'UTC')
    current_time = now.time()
    current_day = now.weekday()  # Monday=0, Sunday=6

    # Log timezone for debugging
    _LOGGER.debug(
        f"Time trigger evaluation for user {user.id}: "
        f"timezone={user_timezone}, local_time={now.strftime('%Y-%m-%d %H:%M:%S')}, "
        f"trigger_time={trigger.time_of_day.strftime('%H:%M')}"
    )

    # Convert to our format (Sunday=0)
    day_of_week = (current_day + 1) % 7

    # Check if today is in repeat_days
    if trigger.repeat_days:
        allowed_days = [int(d) for d in trigger.repeat_days.split(',') if d.strip().isdigit()]
        if day_of_week not in allowed_days:
            return TriggerResult(triggered=False, reason=f"Day {day_of_week} not in repeat_days")

    # Check if current time matches trigger time (within 1 minute tolerance)
    trigger_time = trigger.time_of_day

    # Create trigger datetime in user's timezone
    if hasattr(now, 'tzinfo') and now.tzinfo is not None:
        # now is timezone-aware, combine with same timezone
        trigger_datetime = datetime.combine(now.date(), trigger_time, tzinfo=now.tzinfo)
    else:
        trigger_datetime = datetime.combine(now.date(), trigger_time)

    time_diff = abs((now - trigger_datetime).total_seconds())

    if time_diff <= 60:  # Within 1 minute
        # Check if we already triggered recently (prevent duplicate triggers)
        if trigger.last_evaluated_at:
            # last_evaluated_at is stored as naive UTC, so compare in a consistent way
            last_eval_utc = trigger.last_evaluated_at
            if hasattr(now, 'tzinfo') and now.tzinfo is not None:
                # Convert now to UTC for comparison
                from zoneinfo import ZoneInfo
                now_utc = now.astimezone(ZoneInfo('UTC')).replace(tzinfo=None)
                since_last = (now_utc - last_eval_utc).total_seconds()
            else:
                since_last = (now - last_eval_utc).total_seconds()

            if since_last < 300:  # 5 minutes
                return TriggerResult(triggered=False, reason="Already triggered recently")

        # Update last evaluated time (store as UTC)
        trigger.last_evaluated_at = datetime.utcnow()
        db.session.commit()

        _LOGGER.info(
            f"Time trigger fired for user {user.id} at {now.strftime('%H:%M')} {user_timezone} "
            f"(target: {trigger_time.strftime('%H:%M')})"
        )

        return TriggerResult(
            triggered=True,
            reason=f"Time trigger at {trigger_time.strftime('%H:%M')} ({user_timezone})"
        )

    return TriggerResult(triggered=False, reason="Not yet time")


def _evaluate_battery_trigger(
    trigger: AutomationTrigger,
    current_state: Dict[str, Any]
) -> TriggerResult:
    """
    Evaluate battery state trigger.

    Conditions:
    - charged_up_to: Battery reaches or exceeds threshold
    - discharged_down_to: Battery drops to or below threshold
    - discharged_to_reserve: Battery reaches backup reserve level
    """
    battery_percent = current_state.get('battery_percent')
    if battery_percent is None:
        return TriggerResult(triggered=False, reason="Battery percent unavailable")

    condition = trigger.battery_condition
    threshold = trigger.battery_threshold
    backup_reserve = current_state.get('backup_reserve')

    # Get last evaluated value for edge detection
    last_value = trigger.last_evaluated_value

    if condition == 'charged_up_to':
        if threshold is None:
            return TriggerResult(triggered=False, reason="No threshold set")

        # Trigger when crossing threshold upward
        if battery_percent >= threshold:
            if last_value is not None and last_value < threshold:
                _update_last_value(trigger, battery_percent)
                return TriggerResult(
                    triggered=True,
                    reason=f"Battery charged to {battery_percent}% (threshold: {threshold}%)"
                )
            elif last_value is None:
                # First evaluation, check if already at or above threshold
                _update_last_value(trigger, battery_percent)
                # Don't trigger on first evaluation if already above threshold
                return TriggerResult(triggered=False, reason="Initial state above threshold")

        _update_last_value(trigger, battery_percent)
        return TriggerResult(triggered=False, reason=f"Battery at {battery_percent}%")

    elif condition == 'discharged_down_to':
        if threshold is None:
            return TriggerResult(triggered=False, reason="No threshold set")

        # Trigger when crossing threshold downward
        if battery_percent <= threshold:
            if last_value is not None and last_value > threshold:
                _update_last_value(trigger, battery_percent)
                return TriggerResult(
                    triggered=True,
                    reason=f"Battery discharged to {battery_percent}% (threshold: {threshold}%)"
                )
            elif last_value is None:
                _update_last_value(trigger, battery_percent)
                return TriggerResult(triggered=False, reason="Initial state below threshold")

        _update_last_value(trigger, battery_percent)
        return TriggerResult(triggered=False, reason=f"Battery at {battery_percent}%")

    elif condition == 'discharged_to_reserve':
        if backup_reserve is None:
            return TriggerResult(triggered=False, reason="Backup reserve unavailable")

        # Trigger when battery reaches backup reserve (with 1% tolerance)
        if battery_percent <= backup_reserve + 1:
            if last_value is not None and last_value > backup_reserve + 1:
                _update_last_value(trigger, battery_percent)
                return TriggerResult(
                    triggered=True,
                    reason=f"Battery discharged to reserve ({battery_percent}%, reserve: {backup_reserve}%)"
                )
            elif last_value is None:
                _update_last_value(trigger, battery_percent)
                return TriggerResult(triggered=False, reason="Initial state at reserve")

        _update_last_value(trigger, battery_percent)
        return TriggerResult(triggered=False, reason=f"Battery at {battery_percent}%")

    return TriggerResult(triggered=False, reason=f"Unknown battery condition: {condition}")


def _evaluate_flow_trigger(
    trigger: AutomationTrigger,
    current_state: Dict[str, Any]
) -> TriggerResult:
    """
    Evaluate power flow trigger.

    Sources: home_usage, solar, grid_import, grid_export, battery_charge, battery_discharge
    Transitions: rises_above, drops_below
    """
    source = trigger.flow_source
    transition = trigger.flow_transition
    threshold_kw = trigger.flow_threshold_kw

    if not source or not transition or threshold_kw is None:
        return TriggerResult(triggered=False, reason="Incomplete flow trigger config")

    # Map source to state key
    source_map = {
        'home_usage': 'home_usage_kw',
        'solar': 'solar_power_kw',
        'grid_import': 'grid_import_kw',
        'grid_export': 'grid_export_kw',
        'battery_charge': 'battery_charge_kw',
        'battery_discharge': 'battery_discharge_kw',
    }

    state_key = source_map.get(source)
    if not state_key:
        return TriggerResult(triggered=False, reason=f"Unknown flow source: {source}")

    current_value = current_state.get(state_key)
    if current_value is None:
        return TriggerResult(triggered=False, reason=f"{source} value unavailable")

    last_value = trigger.last_evaluated_value

    if transition == 'rises_above':
        if current_value >= threshold_kw:
            if last_value is not None and last_value < threshold_kw:
                _update_last_value(trigger, current_value)
                return TriggerResult(
                    triggered=True,
                    reason=f"{source} rose to {current_value:.2f}kW (threshold: {threshold_kw}kW)"
                )
            elif last_value is None:
                _update_last_value(trigger, current_value)
                return TriggerResult(triggered=False, reason="Initial state above threshold")

    elif transition == 'drops_below':
        if current_value <= threshold_kw:
            if last_value is not None and last_value > threshold_kw:
                _update_last_value(trigger, current_value)
                return TriggerResult(
                    triggered=True,
                    reason=f"{source} dropped to {current_value:.2f}kW (threshold: {threshold_kw}kW)"
                )
            elif last_value is None:
                _update_last_value(trigger, current_value)
                return TriggerResult(triggered=False, reason="Initial state below threshold")

    _update_last_value(trigger, current_value)
    return TriggerResult(triggered=False, reason=f"{source} at {current_value:.2f}kW")


def _evaluate_price_trigger(
    trigger: AutomationTrigger,
    current_state: Dict[str, Any]
) -> TriggerResult:
    """
    Evaluate price trigger.

    Price types: import, export
    Transitions: rises_above, drops_below
    """
    price_type = trigger.price_type
    transition = trigger.price_transition
    threshold = trigger.price_threshold

    if not price_type or not transition or threshold is None:
        return TriggerResult(triggered=False, reason="Incomplete price trigger config")

    # Get current price
    price_key = 'import_price' if price_type == 'import' else 'export_price'
    current_price = current_state.get(price_key)

    if current_price is None:
        return TriggerResult(triggered=False, reason=f"{price_type} price unavailable")

    last_value = trigger.last_evaluated_value

    if transition == 'rises_above':
        if current_price >= threshold:
            if last_value is not None and last_value < threshold:
                _update_last_value(trigger, current_price)
                return TriggerResult(
                    triggered=True,
                    reason=f"{price_type} price rose to ${current_price:.4f}/kWh (threshold: ${threshold:.4f})"
                )
            elif last_value is None:
                _update_last_value(trigger, current_price)
                return TriggerResult(triggered=False, reason="Initial state above threshold")

    elif transition == 'drops_below':
        if current_price <= threshold:
            if last_value is not None and last_value > threshold:
                _update_last_value(trigger, current_price)
                return TriggerResult(
                    triggered=True,
                    reason=f"{price_type} price dropped to ${current_price:.4f}/kWh (threshold: ${threshold:.4f})"
                )
            elif last_value is None:
                _update_last_value(trigger, current_price)
                return TriggerResult(triggered=False, reason="Initial state below threshold")

    _update_last_value(trigger, current_price)
    return TriggerResult(triggered=False, reason=f"{price_type} price at ${current_price:.4f}/kWh")


def _evaluate_grid_trigger(
    trigger: AutomationTrigger,
    current_state: Dict[str, Any]
) -> TriggerResult:
    """
    Evaluate grid status trigger.

    Conditions: off_grid, on_grid
    """
    condition = trigger.grid_condition
    if not condition:
        return TriggerResult(triggered=False, reason="No grid condition set")

    current_status = current_state.get('grid_status', 'on_grid')

    # Use last_evaluated_value to track state (1.0 = on_grid, 0.0 = off_grid)
    last_value = trigger.last_evaluated_value
    current_value = 1.0 if current_status == 'on_grid' else 0.0

    if condition == 'off_grid':
        # Trigger when grid goes down
        if current_status == 'off_grid':
            if last_value is not None and last_value == 1.0:  # Was on_grid
                _update_last_value(trigger, current_value)
                return TriggerResult(triggered=True, reason="System went off-grid")
            elif last_value is None:
                _update_last_value(trigger, current_value)
                return TriggerResult(triggered=False, reason="Initial state off-grid")

    elif condition == 'on_grid':
        # Trigger when grid comes back
        if current_status == 'on_grid':
            if last_value is not None and last_value == 0.0:  # Was off_grid
                _update_last_value(trigger, current_value)
                return TriggerResult(triggered=True, reason="System back on grid")
            elif last_value is None:
                _update_last_value(trigger, current_value)
                return TriggerResult(triggered=False, reason="Initial state on-grid")

    _update_last_value(trigger, current_value)
    return TriggerResult(triggered=False, reason=f"Grid status: {current_status}")


def _evaluate_weather_trigger(
    trigger: AutomationTrigger,
    current_state: Dict[str, Any]
) -> TriggerResult:
    """
    Evaluate weather trigger.

    Conditions: sunny, partly_sunny, cloudy
    """
    condition = trigger.weather_condition
    if not condition:
        return TriggerResult(triggered=False, reason="No weather condition set")

    current_weather = current_state.get('weather')
    if not current_weather:
        return TriggerResult(triggered=False, reason="Weather data unavailable")

    # Map weather condition to numeric value for edge detection
    weather_values = {'sunny': 3.0, 'partly_sunny': 2.0, 'cloudy': 1.0}
    current_value = weather_values.get(current_weather, 0.0)
    target_value = weather_values.get(condition, 0.0)

    last_value = trigger.last_evaluated_value

    if current_weather == condition:
        if last_value is not None and last_value != target_value:
            _update_last_value(trigger, current_value)
            return TriggerResult(triggered=True, reason=f"Weather changed to {condition}")
        elif last_value is None:
            _update_last_value(trigger, current_value)
            return TriggerResult(triggered=False, reason=f"Initial weather: {condition}")

    _update_last_value(trigger, current_value)
    return TriggerResult(triggered=False, reason=f"Current weather: {current_weather}")


def _update_last_value(trigger: AutomationTrigger, value: float) -> None:
    """Update trigger's last evaluated value."""
    trigger.last_evaluated_value = value
    trigger.last_evaluated_at = datetime.utcnow()
    db.session.commit()


def _evaluate_ev_trigger(
    trigger: AutomationTrigger,
    current_state: Dict[str, Any],
    user: User
) -> TriggerResult:
    """
    Evaluate EV charging trigger.

    Conditions:
    - connected: EV is plugged in
    - disconnected: EV is unplugged
    - charging_starts: EV starts charging
    - charging_stops: EV stops charging
    - soc_reaches: EV battery reaches threshold
    """
    from app.models import TeslaVehicle

    condition = trigger.ev_condition
    if not condition:
        return TriggerResult(triggered=False, reason="No EV condition set")

    # Get EV state from current_state
    ev_state = current_state.get('ev_vehicles', [])
    if not ev_state:
        return TriggerResult(triggered=False, reason="No EV data available")

    # Filter to specific vehicle if set
    target_vehicle_id = trigger.ev_vehicle_id
    if target_vehicle_id:
        ev_state = [v for v in ev_state if v.get('id') == target_vehicle_id]
        if not ev_state:
            return TriggerResult(triggered=False, reason=f"Vehicle {target_vehicle_id} not found")

    # Check each vehicle for the trigger condition
    for vehicle in ev_state:
        vehicle_name = vehicle.get('display_name', 'EV')
        vehicle_id = vehicle.get('id')
        is_plugged_in = vehicle.get('is_plugged_in', False)
        charging_state = vehicle.get('charging_state', '')
        battery_level = vehicle.get('battery_level')

        # Create a unique key for this trigger + vehicle combination
        state_key = f"ev_{vehicle_id}_{condition}"

        # Use last_evaluated_value to track state:
        # For connected/disconnected: 1.0 = plugged in, 0.0 = not plugged in
        # For charging_starts/stops: 1.0 = charging, 0.0 = not charging
        # For soc_reaches: actual battery level
        last_value = trigger.last_evaluated_value

        if condition == 'connected':
            # Trigger when EV gets plugged in
            current_value = 1.0 if is_plugged_in else 0.0
            if is_plugged_in:
                if last_value is not None and last_value == 0.0:  # Was disconnected
                    _update_last_value(trigger, current_value)
                    return TriggerResult(triggered=True, reason=f"{vehicle_name} connected")
                elif last_value is None:
                    _update_last_value(trigger, current_value)
                    return TriggerResult(triggered=False, reason="Initial state connected")
            _update_last_value(trigger, current_value)

        elif condition == 'disconnected':
            # Trigger when EV gets unplugged
            current_value = 1.0 if is_plugged_in else 0.0
            if not is_plugged_in:
                if last_value is not None and last_value == 1.0:  # Was connected
                    _update_last_value(trigger, current_value)
                    return TriggerResult(triggered=True, reason=f"{vehicle_name} disconnected")
                elif last_value is None:
                    _update_last_value(trigger, current_value)
                    return TriggerResult(triggered=False, reason="Initial state disconnected")
            _update_last_value(trigger, current_value)

        elif condition == 'charging_starts':
            # Trigger when EV starts charging
            is_charging = charging_state == 'Charging'
            current_value = 1.0 if is_charging else 0.0
            if is_charging:
                if last_value is not None and last_value == 0.0:  # Was not charging
                    _update_last_value(trigger, current_value)
                    return TriggerResult(triggered=True, reason=f"{vehicle_name} started charging")
                elif last_value is None:
                    _update_last_value(trigger, current_value)
                    return TriggerResult(triggered=False, reason="Initial state charging")
            _update_last_value(trigger, current_value)

        elif condition == 'charging_stops':
            # Trigger when EV stops charging
            is_charging = charging_state == 'Charging'
            current_value = 1.0 if is_charging else 0.0
            if not is_charging and is_plugged_in:  # Stopped but still plugged in
                if last_value is not None and last_value == 1.0:  # Was charging
                    _update_last_value(trigger, current_value)
                    return TriggerResult(triggered=True, reason=f"{vehicle_name} stopped charging")
                elif last_value is None:
                    _update_last_value(trigger, current_value)
                    return TriggerResult(triggered=False, reason="Initial state not charging")
            _update_last_value(trigger, current_value)

        elif condition == 'soc_reaches':
            # Trigger when EV battery reaches threshold
            threshold = trigger.ev_soc_threshold
            if threshold is None:
                return TriggerResult(triggered=False, reason="No SoC threshold set")

            if battery_level is None:
                continue  # Skip this vehicle, try next

            current_value = float(battery_level)
            if battery_level >= threshold:
                if last_value is not None and last_value < threshold:
                    _update_last_value(trigger, current_value)
                    return TriggerResult(
                        triggered=True,
                        reason=f"{vehicle_name} reached {battery_level}% (threshold: {threshold}%)"
                    )
                elif last_value is None:
                    _update_last_value(trigger, current_value)
                    return TriggerResult(triggered=False, reason="Initial state above threshold")
            _update_last_value(trigger, current_value)

    return TriggerResult(triggered=False, reason=f"No EV matched condition: {condition}")


def _evaluate_ocpp_trigger(
    trigger: AutomationTrigger,
    current_state: Dict[str, Any],
    user: User
) -> TriggerResult:
    """
    Evaluate OCPP charger trigger.

    Conditions:
    - connected: Charger connects to central system
    - disconnected: Charger disconnects from central system
    - charging_starts: Charging session starts
    - charging_stops: Charging session stops
    - energy_reaches: Energy delivered reaches threshold (kWh)
    - available: Charger becomes available
    - faulted: Charger reports a fault
    """
    from app.models import OCPPCharger

    condition = trigger.ocpp_condition
    if not condition:
        return TriggerResult(triggered=False, reason="No OCPP condition set")

    # Get OCPP charger state from current_state
    ocpp_chargers = current_state.get('ocpp_chargers', [])
    if not ocpp_chargers:
        # Try to get from database if not in current_state
        chargers = OCPPCharger.query.filter_by(user_id=user.id).all()
        ocpp_chargers = [{
            'id': c.id,
            'charger_id': c.charger_id,
            'display_name': c.display_name or c.charger_id,
            'is_connected': c.is_connected,
            'status': c.status,
            'current_transaction_id': c.current_transaction_id,
            'current_energy_kwh': c.current_energy_kwh,
        } for c in chargers]

    if not ocpp_chargers:
        return TriggerResult(triggered=False, reason="No OCPP chargers found")

    # Filter to specific charger if set
    target_charger_id = trigger.ocpp_charger_id
    if target_charger_id:
        ocpp_chargers = [c for c in ocpp_chargers if c.get('id') == target_charger_id]
        if not ocpp_chargers:
            return TriggerResult(triggered=False, reason=f"Charger {target_charger_id} not found")

    # Check each charger for the trigger condition
    for charger in ocpp_chargers:
        charger_name = charger.get('display_name', charger.get('charger_id', 'Charger'))
        charger_id = charger.get('id')
        is_connected = charger.get('is_connected', False)
        status = charger.get('status', 'Unavailable')
        current_transaction_id = charger.get('current_transaction_id')
        current_energy_kwh = charger.get('current_energy_kwh', 0.0)

        # State tracking:
        # For connected/disconnected: 1.0 = connected, 0.0 = not connected
        # For charging_starts/stops: 1.0 = has active transaction, 0.0 = no transaction
        # For available/faulted: use status string hash
        # For energy_reaches: actual energy value
        last_value = trigger.last_evaluated_value

        if condition == 'connected':
            # Trigger when charger connects to central system
            current_value = 1.0 if is_connected else 0.0
            if is_connected:
                if last_value is not None and last_value == 0.0:
                    _update_last_value(trigger, current_value)
                    return TriggerResult(triggered=True, reason=f"{charger_name} connected")
                elif last_value is None:
                    _update_last_value(trigger, current_value)
                    return TriggerResult(triggered=False, reason="Initial state connected")
            _update_last_value(trigger, current_value)

        elif condition == 'disconnected':
            # Trigger when charger disconnects from central system
            current_value = 1.0 if is_connected else 0.0
            if not is_connected:
                if last_value is not None and last_value == 1.0:
                    _update_last_value(trigger, current_value)
                    return TriggerResult(triggered=True, reason=f"{charger_name} disconnected")
                elif last_value is None:
                    _update_last_value(trigger, current_value)
                    return TriggerResult(triggered=False, reason="Initial state disconnected")
            _update_last_value(trigger, current_value)

        elif condition == 'charging_starts':
            # Trigger when a charging session starts (transaction begins)
            has_transaction = current_transaction_id is not None
            current_value = 1.0 if has_transaction else 0.0
            if has_transaction:
                if last_value is not None and last_value == 0.0:
                    _update_last_value(trigger, current_value)
                    return TriggerResult(triggered=True, reason=f"{charger_name} started charging")
                elif last_value is None:
                    _update_last_value(trigger, current_value)
                    return TriggerResult(triggered=False, reason="Initial state charging")
            _update_last_value(trigger, current_value)

        elif condition == 'charging_stops':
            # Trigger when a charging session stops (transaction ends)
            has_transaction = current_transaction_id is not None
            current_value = 1.0 if has_transaction else 0.0
            if not has_transaction:
                if last_value is not None and last_value == 1.0:
                    _update_last_value(trigger, current_value)
                    return TriggerResult(triggered=True, reason=f"{charger_name} stopped charging")
                elif last_value is None:
                    _update_last_value(trigger, current_value)
                    return TriggerResult(triggered=False, reason="Initial state not charging")
            _update_last_value(trigger, current_value)

        elif condition == 'energy_reaches':
            # Trigger when energy delivered reaches threshold
            threshold = trigger.ocpp_energy_threshold
            if threshold is None:
                return TriggerResult(triggered=False, reason="No energy threshold set")

            if current_energy_kwh is None:
                continue  # Skip this charger, try next

            current_value = float(current_energy_kwh)
            if current_energy_kwh >= threshold:
                if last_value is not None and last_value < threshold:
                    _update_last_value(trigger, current_value)
                    return TriggerResult(
                        triggered=True,
                        reason=f"{charger_name} delivered {current_energy_kwh:.1f}kWh (threshold: {threshold}kWh)"
                    )
                elif last_value is None:
                    _update_last_value(trigger, current_value)
                    return TriggerResult(triggered=False, reason="Initial state above threshold")
            _update_last_value(trigger, current_value)

        elif condition == 'available':
            # Trigger when charger becomes available
            is_available = status == 'Available'
            current_value = 1.0 if is_available else 0.0
            if is_available:
                if last_value is not None and last_value == 0.0:
                    _update_last_value(trigger, current_value)
                    return TriggerResult(triggered=True, reason=f"{charger_name} is now available")
                elif last_value is None:
                    _update_last_value(trigger, current_value)
                    return TriggerResult(triggered=False, reason="Initial state available")
            _update_last_value(trigger, current_value)

        elif condition == 'faulted':
            # Trigger when charger reports a fault
            is_faulted = status == 'Faulted'
            current_value = 1.0 if is_faulted else 0.0
            if is_faulted:
                if last_value is not None and last_value == 0.0:
                    _update_last_value(trigger, current_value)
                    return TriggerResult(triggered=True, reason=f"{charger_name} reported a fault")
                elif last_value is None:
                    _update_last_value(trigger, current_value)
                    return TriggerResult(triggered=False, reason="Initial state faulted")
            _update_last_value(trigger, current_value)

    return TriggerResult(triggered=False, reason=f"No OCPP charger matched condition: {condition}")


def _evaluate_solar_forecast_trigger(
    trigger: AutomationTrigger,
    current_state: Dict[str, Any]
) -> TriggerResult:
    """
    Evaluate solar forecast trigger.

    Periods: today, tomorrow
    Conditions: above, below (threshold in kWh)

    This trigger fires once per day when the forecast meets the condition.
    Uses edge detection to avoid repeated triggering.
    """
    period = trigger.solar_forecast_period
    condition = trigger.solar_forecast_condition
    threshold_kwh = trigger.solar_forecast_threshold_kwh

    if not period or not condition or threshold_kwh is None:
        return TriggerResult(triggered=False, reason="Incomplete solar forecast trigger config")

    # Get solar forecast from current state
    solcast = current_state.get('solcast_forecast', {})
    if not solcast:
        return TriggerResult(triggered=False, reason="Solar forecast data unavailable")

    # Get the forecast value based on period
    if period == 'today':
        forecast_kwh = solcast.get('today_forecast_kwh') or solcast.get('today_kwh')
    elif period == 'tomorrow':
        forecast_kwh = solcast.get('tomorrow_kwh')
    else:
        return TriggerResult(triggered=False, reason=f"Unknown forecast period: {period}")

    if forecast_kwh is None:
        return TriggerResult(triggered=False, reason=f"No {period} forecast available")

    # Check if we already triggered today (use date as part of state tracking)
    now = current_state.get('current_time', datetime.now())
    today_str = now.strftime('%Y-%m-%d')

    # We encode the date into the last_evaluated_value to prevent re-triggering same day
    # Format: YYYYMMDD.forecast_kwh (e.g., 20260119.25.5)
    last_value = trigger.last_evaluated_value
    last_date_encoded = int(last_value) if last_value is not None else 0
    last_date_str = str(last_date_encoded) if last_date_encoded > 20000000 else None

    current_date_encoded = int(now.strftime('%Y%m%d'))

    # Check if already triggered today
    if last_date_str and last_date_str == now.strftime('%Y%m%d'):
        return TriggerResult(
            triggered=False,
            reason=f"Already evaluated {period} forecast today ({forecast_kwh:.1f} kWh)"
        )

    # Evaluate the condition
    triggered = False
    if condition == 'above':
        triggered = forecast_kwh >= threshold_kwh
    elif condition == 'below':
        triggered = forecast_kwh <= threshold_kwh
    else:
        return TriggerResult(triggered=False, reason=f"Unknown condition: {condition}")

    # Update last evaluated (encode date to prevent re-triggering)
    _update_last_value(trigger, float(current_date_encoded))

    if triggered:
        return TriggerResult(
            triggered=True,
            reason=f"{period.capitalize()} solar forecast {forecast_kwh:.1f} kWh is {condition} {threshold_kwh:.1f} kWh"
        )

    return TriggerResult(
        triggered=False,
        reason=f"{period.capitalize()} forecast {forecast_kwh:.1f} kWh (threshold: {condition} {threshold_kwh:.1f} kWh)"
    )
