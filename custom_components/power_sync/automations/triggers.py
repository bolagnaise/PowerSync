"""
Trigger evaluation logic for HA automations.

Supports the following trigger types:
- time: Trigger at specific time(s) of day
- battery: Trigger based on battery state of charge
- flow: Trigger based on power flow
- price: Trigger based on electricity price thresholds
- grid: Trigger when grid status changes
- weather: Trigger based on weather conditions
- solar_forecast: Trigger based on solar forecast (today/tomorrow above/below kWh threshold)
- ev: Trigger based on EV charging state (connected, disconnected, charging, SOC)
- ocpp: Trigger based on OCPP charger state
"""

import logging
from dataclasses import dataclass
from datetime import datetime, time as dt_time, timedelta
from typing import Dict, Any, Optional, List, TYPE_CHECKING

if TYPE_CHECKING:
    from . import AutomationStore

_LOGGER = logging.getLogger(__name__)


@dataclass
class TriggerResult:
    """Result of trigger evaluation."""
    triggered: bool
    reason: str = ""


def evaluate_trigger(
    trigger: Dict[str, Any],
    current_state: Dict[str, Any],
    last_evaluated_value: Optional[float],
    store: "AutomationStore",
    automation_id: int
) -> TriggerResult:
    """
    Evaluate a trigger against current state.

    Args:
        trigger: Trigger configuration dict
        current_state: Current system state
        last_evaluated_value: Last evaluated value for edge detection
        store: AutomationStore for updating state
        automation_id: ID of the automation being evaluated

    Returns:
        TriggerResult indicating if trigger condition is met
    """
    # Check time window constraint first
    in_time_window = _is_within_time_window(trigger, current_state)
    trigger_type = trigger.get("trigger_type")

    # Sentinel value to mark "was outside time window"
    OUTSIDE_WINDOW_SENTINEL = -999999.0

    if not in_time_window:
        # Mark that we're outside the time window
        # For EV triggers, preserve state bits but clear window flag
        if trigger_type == "ev" and last_evaluated_value is not None:
            new_value = float(int(last_evaluated_value) & 3)  # Keep bits 0-1, clear bit 2
            store.update_trigger_state(automation_id, new_value)
        else:
            # For other triggers, use sentinel value
            store.update_trigger_state(automation_id, OUTSIDE_WINDOW_SENTINEL)
        return TriggerResult(triggered=False, reason="Outside time window")

    # Check if we just entered the time window (for non-EV triggers)
    # EV triggers handle this internally with bit 2
    just_entered_window = (
        last_evaluated_value == OUTSIDE_WINDOW_SENTINEL
        if trigger_type != "ev" else False
    )

    if trigger_type == "time":
        return _evaluate_time_trigger(trigger, current_state, store, automation_id)
    elif trigger_type == "battery":
        return _evaluate_battery_trigger(trigger, current_state, last_evaluated_value, store, automation_id, just_entered_window)
    elif trigger_type == "flow":
        return _evaluate_flow_trigger(trigger, current_state, last_evaluated_value, store, automation_id, just_entered_window)
    elif trigger_type == "price":
        return _evaluate_price_trigger(trigger, current_state, last_evaluated_value, store, automation_id, just_entered_window)
    elif trigger_type == "grid":
        return _evaluate_grid_trigger(trigger, current_state, last_evaluated_value, store, automation_id)
    elif trigger_type == "weather":
        return _evaluate_weather_trigger(trigger, current_state, last_evaluated_value, store, automation_id)
    elif trigger_type == "solar_forecast":
        return _evaluate_solar_forecast_trigger(trigger, current_state, last_evaluated_value, store, automation_id)
    elif trigger_type == "ev":
        return _evaluate_ev_trigger(trigger, current_state, last_evaluated_value, store, automation_id)
    elif trigger_type == "ocpp":
        return _evaluate_ocpp_trigger(trigger, current_state, last_evaluated_value, store, automation_id)
    else:
        _LOGGER.warning(f"Unknown trigger type: {trigger_type}")
        return TriggerResult(triggered=False, reason=f"Unknown trigger type: {trigger_type}")


def _is_within_time_window(trigger: Dict[str, Any], current_state: Dict[str, Any]) -> bool:
    """Check if current time is within the optional time window (uses user's local timezone)."""
    start_str = trigger.get("time_window_start")
    end_str = trigger.get("time_window_end")

    if not start_str or not end_str:
        return True

    try:
        start = datetime.strptime(start_str, "%H:%M").time()
        end = datetime.strptime(end_str, "%H:%M").time()
    except ValueError:
        return True

    current_datetime = current_state.get("current_time", datetime.now())
    current_time = current_datetime.time()
    user_timezone = current_state.get("user_timezone", "UTC")

    # Handle overnight windows
    if start <= end:
        is_within = start <= current_time <= end
    else:
        is_within = current_time >= start or current_time <= end

    _LOGGER.debug(
        f"Time window check: {current_time.strftime('%H:%M')} ({user_timezone}) "
        f"window={start.strftime('%H:%M')}-{end.strftime('%H:%M')}, within={is_within}"
    )

    return is_within


def _evaluate_time_trigger(
    trigger: Dict[str, Any],
    current_state: Dict[str, Any],
    store: "AutomationStore",
    automation_id: int
) -> TriggerResult:
    """
    Evaluate time-based trigger.

    Uses user's local timezone from current_state for accurate time matching.
    """
    time_str = trigger.get("time_of_day")
    if not time_str:
        return TriggerResult(triggered=False, reason="No time_of_day set")

    try:
        trigger_time = datetime.strptime(time_str, "%H:%M").time()
    except ValueError:
        return TriggerResult(triggered=False, reason="Invalid time format")

    now = current_state.get("current_time", datetime.now())
    user_timezone = current_state.get("user_timezone", "UTC")
    current_time = now.time()

    # Log timezone for debugging
    _LOGGER.debug(
        f"Time trigger evaluation: timezone={user_timezone}, "
        f"local_time={now.strftime('%Y-%m-%d %H:%M:%S')}, trigger_time={time_str}"
    )

    # Check repeat days (0=Sun, 1=Mon, ..., 6=Sat)
    repeat_days = trigger.get("repeat_days")
    if repeat_days:
        day_of_week = (now.weekday() + 1) % 7  # Convert to Sunday=0
        allowed_days = [int(d) for d in repeat_days.split(",") if d.strip().isdigit()]
        if day_of_week not in allowed_days:
            return TriggerResult(triggered=False, reason=f"Day {day_of_week} not in repeat_days")

    # Check if current time matches (within 1 minute tolerance)
    # Create trigger datetime in user's timezone
    if hasattr(now, 'tzinfo') and now.tzinfo is not None:
        trigger_datetime = datetime.combine(now.date(), trigger_time, tzinfo=now.tzinfo)
    else:
        trigger_datetime = datetime.combine(now.date(), trigger_time)

    # Only trigger at or after the target time (within 60s window for polling interval)
    time_diff_seconds = (now - trigger_datetime).total_seconds()

    if 0 <= time_diff_seconds <= 60:
        # Check if already triggered recently
        auto = store.get_by_id(automation_id)
        if auto and auto.get("last_evaluated_at"):
            try:
                last_eval = datetime.fromisoformat(auto["last_evaluated_at"])
                # Compare in UTC for consistency
                if hasattr(now, 'tzinfo') and now.tzinfo is not None:
                    from zoneinfo import ZoneInfo
                    now_utc = now.astimezone(ZoneInfo('UTC')).replace(tzinfo=None)
                    since_last = (now_utc - last_eval).total_seconds()
                else:
                    since_last = (now - last_eval).total_seconds()

                if since_last < 300:
                    return TriggerResult(triggered=False, reason="Already triggered recently")
            except ValueError:
                pass

        store.update_trigger_state(automation_id, 1.0)

        _LOGGER.info(
            f"Time trigger fired at {now.strftime('%H:%M')} {user_timezone} "
            f"(target: {trigger_time.strftime('%H:%M')})"
        )

        return TriggerResult(
            triggered=True,
            reason=f"Time trigger at {trigger_time.strftime('%H:%M')} ({user_timezone})"
        )

    return TriggerResult(triggered=False, reason="Not yet time")


def _evaluate_battery_trigger(
    trigger: Dict[str, Any],
    current_state: Dict[str, Any],
    last_value: Optional[float],
    store: "AutomationStore",
    automation_id: int,
    just_entered_window: bool = False
) -> TriggerResult:
    """Evaluate battery state trigger."""
    battery_percent = current_state.get("battery_percent")
    if battery_percent is None:
        return TriggerResult(triggered=False, reason="Battery percent unavailable")

    condition = trigger.get("battery_condition")
    threshold = trigger.get("battery_threshold")
    backup_reserve = current_state.get("backup_reserve")

    if condition == "charged_up_to":
        if threshold is None:
            return TriggerResult(triggered=False, reason="No threshold set")

        if battery_percent >= threshold:
            # Trigger if: crossed threshold OR just entered time window while above
            if (last_value is not None and last_value < threshold) or just_entered_window:
                store.update_trigger_state(automation_id, battery_percent)
                reason = f"Battery charged to {battery_percent}% (threshold: {threshold}%)"
                if just_entered_window:
                    reason = f"Battery already at {battery_percent}% (entered time window)"
                return TriggerResult(triggered=True, reason=reason)
            elif last_value is None:
                store.update_trigger_state(automation_id, battery_percent)

        store.update_trigger_state(automation_id, battery_percent)
        return TriggerResult(triggered=False, reason=f"Battery at {battery_percent}%")

    elif condition == "discharged_down_to":
        if threshold is None:
            return TriggerResult(triggered=False, reason="No threshold set")

        if battery_percent <= threshold:
            # Trigger if: crossed threshold OR just entered time window while below
            if (last_value is not None and last_value > threshold) or just_entered_window:
                store.update_trigger_state(automation_id, battery_percent)
                reason = f"Battery discharged to {battery_percent}% (threshold: {threshold}%)"
                if just_entered_window:
                    reason = f"Battery already at {battery_percent}% (entered time window)"
                return TriggerResult(triggered=True, reason=reason)
            elif last_value is None:
                store.update_trigger_state(automation_id, battery_percent)

        store.update_trigger_state(automation_id, battery_percent)
        return TriggerResult(triggered=False, reason=f"Battery at {battery_percent}%")

    elif condition == "discharged_to_reserve":
        if backup_reserve is None:
            return TriggerResult(triggered=False, reason="Backup reserve unavailable")

        if battery_percent <= backup_reserve + 1:
            # Trigger if: crossed threshold OR just entered time window while at reserve
            if (last_value is not None and last_value > backup_reserve + 1) or just_entered_window:
                store.update_trigger_state(automation_id, battery_percent)
                reason = f"Battery at reserve ({battery_percent}%, reserve: {backup_reserve}%)"
                if just_entered_window:
                    reason = f"Battery already at reserve {battery_percent}% (entered time window)"
                return TriggerResult(triggered=True, reason=reason)
            elif last_value is None:
                store.update_trigger_state(automation_id, battery_percent)

        store.update_trigger_state(automation_id, battery_percent)
        return TriggerResult(triggered=False, reason=f"Battery at {battery_percent}%")

    return TriggerResult(triggered=False, reason=f"Unknown condition: {condition}")


def _evaluate_flow_trigger(
    trigger: Dict[str, Any],
    current_state: Dict[str, Any],
    last_value: Optional[float],
    store: "AutomationStore",
    automation_id: int,
    just_entered_window: bool = False
) -> TriggerResult:
    """Evaluate power flow trigger."""
    source = trigger.get("flow_source")
    transition = trigger.get("flow_transition")
    threshold_kw = trigger.get("flow_threshold_kw")

    if not source or not transition or threshold_kw is None:
        return TriggerResult(triggered=False, reason="Incomplete flow trigger config")

    source_map = {
        "home_usage": "home_usage_kw",
        "solar": "solar_power_kw",
        "grid_import": "grid_import_kw",
        "grid_export": "grid_export_kw",
        "battery_charge": "battery_charge_kw",
        "battery_discharge": "battery_discharge_kw",
    }

    state_key = source_map.get(source)
    if not state_key:
        return TriggerResult(triggered=False, reason=f"Unknown flow source: {source}")

    current_value = current_state.get(state_key)
    if current_value is None:
        return TriggerResult(triggered=False, reason=f"{source} value unavailable")

    if transition == "rises_above":
        if current_value >= threshold_kw:
            # Trigger if: crossed threshold OR just entered time window while above
            if (last_value is not None and last_value < threshold_kw) or just_entered_window:
                store.update_trigger_state(automation_id, current_value)
                reason = f"{source} rose to {current_value:.2f}kW (threshold: {threshold_kw}kW)"
                if just_entered_window:
                    reason = f"{source} already at {current_value:.2f}kW (entered time window)"
                return TriggerResult(triggered=True, reason=reason)
            elif last_value is None:
                store.update_trigger_state(automation_id, current_value)

    elif transition == "drops_below":
        if current_value <= threshold_kw:
            # Trigger if: crossed threshold OR just entered time window while below
            if (last_value is not None and last_value > threshold_kw) or just_entered_window:
                store.update_trigger_state(automation_id, current_value)
                reason = f"{source} dropped to {current_value:.2f}kW (threshold: {threshold_kw}kW)"
                if just_entered_window:
                    reason = f"{source} already at {current_value:.2f}kW (entered time window)"
                return TriggerResult(triggered=True, reason=reason)
            elif last_value is None:
                store.update_trigger_state(automation_id, current_value)

    store.update_trigger_state(automation_id, current_value)
    return TriggerResult(triggered=False, reason=f"{source} at {current_value:.2f}kW")


def _evaluate_price_trigger(
    trigger: Dict[str, Any],
    current_state: Dict[str, Any],
    last_value: Optional[float],
    store: "AutomationStore",
    automation_id: int,
    just_entered_window: bool = False
) -> TriggerResult:
    """Evaluate price trigger."""
    price_type = trigger.get("price_type")
    transition = trigger.get("price_transition")
    threshold = trigger.get("price_threshold")

    if not price_type or not transition or threshold is None:
        return TriggerResult(triggered=False, reason="Incomplete price trigger config")

    price_key = "import_price" if price_type == "import" else "export_price"
    current_price = current_state.get(price_key)

    if current_price is None:
        return TriggerResult(triggered=False, reason=f"{price_type} price unavailable")

    if transition == "rises_above":
        if current_price >= threshold:
            # Trigger if: price crossed threshold OR just entered time window while above
            if (last_value is not None and last_value < threshold) or just_entered_window:
                store.update_trigger_state(automation_id, current_price)
                reason = f"{price_type} price rose to ${current_price:.4f}/kWh"
                if just_entered_window:
                    reason = f"{price_type} price already above ${threshold:.4f}/kWh (entered time window)"
                return TriggerResult(triggered=True, reason=reason)
            elif last_value is None:
                store.update_trigger_state(automation_id, current_price)

    elif transition == "drops_below":
        if current_price <= threshold:
            # Trigger if: price crossed threshold OR just entered time window while below
            if (last_value is not None and last_value > threshold) or just_entered_window:
                store.update_trigger_state(automation_id, current_price)
                reason = f"{price_type} price dropped to ${current_price:.4f}/kWh"
                if just_entered_window:
                    reason = f"{price_type} price already below ${threshold:.4f}/kWh (entered time window)"
                return TriggerResult(triggered=True, reason=reason)
            elif last_value is None:
                store.update_trigger_state(automation_id, current_price)

    store.update_trigger_state(automation_id, current_price)
    return TriggerResult(triggered=False, reason=f"{price_type} price at ${current_price:.4f}/kWh")


def _evaluate_grid_trigger(
    trigger: Dict[str, Any],
    current_state: Dict[str, Any],
    last_value: Optional[float],
    store: "AutomationStore",
    automation_id: int
) -> TriggerResult:
    """Evaluate grid status trigger."""
    condition = trigger.get("grid_condition")
    if not condition:
        return TriggerResult(triggered=False, reason="No grid condition set")

    current_status = current_state.get("grid_status", "on_grid")
    current_value = 1.0 if current_status == "on_grid" else 0.0

    if condition == "off_grid":
        if current_status == "off_grid":
            if last_value is not None and last_value == 1.0:
                store.update_trigger_state(automation_id, current_value)
                return TriggerResult(triggered=True, reason="System went off-grid")
            elif last_value is None:
                store.update_trigger_state(automation_id, current_value)

    elif condition == "on_grid":
        if current_status == "on_grid":
            if last_value is not None and last_value == 0.0:
                store.update_trigger_state(automation_id, current_value)
                return TriggerResult(triggered=True, reason="System back on grid")
            elif last_value is None:
                store.update_trigger_state(automation_id, current_value)

    store.update_trigger_state(automation_id, current_value)
    return TriggerResult(triggered=False, reason=f"Grid status: {current_status}")


def _evaluate_weather_trigger(
    trigger: Dict[str, Any],
    current_state: Dict[str, Any],
    last_value: Optional[float],
    store: "AutomationStore",
    automation_id: int
) -> TriggerResult:
    """Evaluate weather trigger."""
    condition = trigger.get("weather_condition")
    if not condition:
        return TriggerResult(triggered=False, reason="No weather condition set")

    current_weather = current_state.get("weather")
    if not current_weather:
        return TriggerResult(triggered=False, reason="Weather data unavailable")

    weather_values = {"sunny": 3.0, "partly_sunny": 2.0, "cloudy": 1.0}
    current_value = weather_values.get(current_weather, 0.0)
    target_value = weather_values.get(condition, 0.0)

    if current_weather == condition:
        if last_value is not None and last_value != target_value:
            store.update_trigger_state(automation_id, current_value)
            return TriggerResult(triggered=True, reason=f"Weather changed to {condition}")
        elif last_value is None:
            store.update_trigger_state(automation_id, current_value)

    store.update_trigger_state(automation_id, current_value)
    return TriggerResult(triggered=False, reason=f"Current weather: {current_weather}")


def _evaluate_solar_forecast_trigger(
    trigger: Dict[str, Any],
    current_state: Dict[str, Any],
    last_value: Optional[float],
    store: "AutomationStore",
    automation_id: int
) -> TriggerResult:
    """
    Evaluate solar forecast trigger.

    Periods: today, tomorrow
    Conditions: above, below (threshold in kWh)

    This trigger fires once per day when the forecast meets the condition.
    Uses date encoding in last_value to prevent re-triggering same day.
    """
    period = trigger.get("solar_forecast_period")
    condition = trigger.get("solar_forecast_condition")
    threshold_kwh = trigger.get("solar_forecast_threshold_kwh")

    if not period or not condition or threshold_kwh is None:
        return TriggerResult(triggered=False, reason="Incomplete solar forecast trigger config")

    # Get solar forecast from current state
    solcast = current_state.get("solcast_forecast", {})
    if not solcast:
        return TriggerResult(triggered=False, reason="Solar forecast data unavailable")

    # Get the forecast value based on period
    if period == "today":
        forecast_kwh = solcast.get("today_forecast_kwh") or solcast.get("today_kwh")
    elif period == "tomorrow":
        forecast_kwh = solcast.get("tomorrow_kwh")
    else:
        return TriggerResult(triggered=False, reason=f"Unknown forecast period: {period}")

    if forecast_kwh is None:
        return TriggerResult(triggered=False, reason=f"No {period} forecast available")

    # Check if we already triggered today
    now = current_state.get("current_time", datetime.now())
    current_date_encoded = int(now.strftime("%Y%m%d"))

    # We encode the date into last_value to prevent re-triggering same day
    last_date_encoded = int(last_value) if last_value is not None else 0

    if last_date_encoded == current_date_encoded:
        return TriggerResult(
            triggered=False,
            reason=f"Already evaluated {period} forecast today ({forecast_kwh:.1f} kWh)"
        )

    # Evaluate the condition
    triggered = False
    if condition == "above":
        triggered = forecast_kwh >= threshold_kwh
    elif condition == "below":
        triggered = forecast_kwh <= threshold_kwh
    else:
        return TriggerResult(triggered=False, reason=f"Unknown condition: {condition}")

    # Update state with date encoding
    store.update_trigger_state(automation_id, float(current_date_encoded))

    if triggered:
        return TriggerResult(
            triggered=True,
            reason=f"{period.capitalize()} solar forecast {forecast_kwh:.1f} kWh is {condition} {threshold_kwh:.1f} kWh"
        )

    return TriggerResult(
        triggered=False,
        reason=f"{period.capitalize()} forecast {forecast_kwh:.1f} kWh (threshold: {condition} {threshold_kwh:.1f} kWh)"
    )


def _evaluate_ev_trigger(
    trigger: Dict[str, Any],
    current_state: Dict[str, Any],
    last_value: Optional[float],
    store: "AutomationStore",
    automation_id: int
) -> TriggerResult:
    """
    Evaluate EV charging trigger.

    Conditions:
    - connected: EV plugged in
    - disconnected: EV unplugged
    - charging_starts: Charging begins
    - charging_stops: Charging ends
    - soc_reaches: EV battery reaches threshold

    Edge detection triggers on:
    1. State change (e.g., unplugged -> plugged in)
    2. Time window entry while condition is already met
    3. Initial evaluation if trigger_on_initial is True
    """
    condition = trigger.get("ev_condition")
    if not condition:
        return TriggerResult(triggered=False, reason="No EV condition set")

    # Option to trigger on initial evaluation if condition is already met
    trigger_on_initial = trigger.get("trigger_on_initial", False)

    # Get EV state from current_state
    ev_state = current_state.get("ev_state", {})

    _LOGGER.debug(
        f"EV trigger evaluation: condition={condition}, last_value={last_value}, "
        f"trigger_on_initial={trigger_on_initial}, ev_state={ev_state}"
    )
    is_plugged_in = ev_state.get("is_plugged_in", False)
    is_charging = ev_state.get("is_charging", False)
    battery_level = ev_state.get("battery_level")
    charging_state = ev_state.get("charging_state", "").lower()
    location = ev_state.get("location", "unknown")

    # Check location requirement
    ev_location = trigger.get("ev_location", "any")
    if ev_location and ev_location != "any":
        if ev_location == "home" and location != "home":
            return TriggerResult(triggered=False, reason=f"EV not at home (location: {location})")
        elif ev_location == "work" and location != "work":
            return TriggerResult(triggered=False, reason=f"EV not at work (location: {location})")
        elif ev_location == "other" and location in ("home", "work", "unknown", "not_home"):
            return TriggerResult(triggered=False, reason=f"EV not at other location (location: {location})")

    # Encode state for edge detection
    # Bits: 0=plugged_in, 1=charging, 2=was_in_time_window
    # Bit 2 tracks whether we were in the time window on last evaluation
    # This allows triggering when entering time window while condition is already met
    was_in_window = (int(last_value) & 4) != 0 if last_value is not None else False
    current_value = float(
        (1 if is_plugged_in else 0) +
        (2 if is_charging else 0) +
        4  # Always set bit 2 since we're now in time window (check happens before this)
    )

    # Check if we just entered the time window
    just_entered_window = not was_in_window and last_value is not None

    if condition == "connected":
        if is_plugged_in:
            # Trigger if: state changed OR just entered time window OR initial with trigger_on_initial
            was_plugged_in = (int(last_value) & 1) != 0 if last_value is not None else False
            is_initial = last_value is None
            if (last_value is not None and not was_plugged_in) or just_entered_window or (is_initial and trigger_on_initial):
                store.update_trigger_state(automation_id, current_value)
                if is_initial and trigger_on_initial:
                    reason = "EV already plugged in (initial trigger)"
                elif just_entered_window:
                    reason = "EV already plugged in (entered time window)"
                else:
                    reason = "EV plugged in"
                return TriggerResult(triggered=True, reason=reason)
            elif is_initial:
                store.update_trigger_state(automation_id, current_value)

    elif condition == "disconnected":
        if not is_plugged_in:
            # Trigger if: state changed OR just entered time window OR initial with trigger_on_initial
            was_plugged_in = (int(last_value) & 1) != 0 if last_value is not None else False
            is_initial = last_value is None
            if (last_value is not None and was_plugged_in) or just_entered_window or (is_initial and trigger_on_initial):
                store.update_trigger_state(automation_id, current_value)
                if is_initial and trigger_on_initial:
                    reason = "EV already unplugged (initial trigger)"
                elif just_entered_window:
                    reason = "EV already unplugged (entered time window)"
                else:
                    reason = "EV unplugged"
                return TriggerResult(triggered=True, reason=reason)
            elif is_initial:
                store.update_trigger_state(automation_id, current_value)

    elif condition == "charging_starts":
        if is_charging or charging_state == "charging":
            # Trigger if: state changed OR just entered time window OR initial with trigger_on_initial
            was_charging = (int(last_value) & 2) != 0 if last_value is not None else False
            is_initial = last_value is None
            if (last_value is not None and not was_charging) or just_entered_window or (is_initial and trigger_on_initial):
                store.update_trigger_state(automation_id, current_value)
                if is_initial and trigger_on_initial:
                    reason = "EV already charging (initial trigger)"
                elif just_entered_window:
                    reason = "EV already charging (entered time window)"
                else:
                    reason = "EV charging started"
                return TriggerResult(triggered=True, reason=reason)
            elif is_initial:
                store.update_trigger_state(automation_id, current_value)

    elif condition == "charging_stops":
        if not is_charging and charging_state != "charging":
            # Trigger if: state changed OR just entered time window OR initial with trigger_on_initial
            was_charging = (int(last_value) & 2) != 0 if last_value is not None else False
            is_initial = last_value is None
            if (last_value is not None and was_charging) or just_entered_window or (is_initial and trigger_on_initial):
                store.update_trigger_state(automation_id, current_value)
                if is_initial and trigger_on_initial:
                    reason = "EV already stopped (initial trigger)"
                elif just_entered_window:
                    reason = "EV already stopped (entered time window)"
                else:
                    reason = "EV charging stopped"
                return TriggerResult(triggered=True, reason=reason)
            elif is_initial:
                store.update_trigger_state(automation_id, current_value)

    elif condition == "soc_reaches":
        threshold = trigger.get("ev_soc_threshold")
        if threshold is None:
            return TriggerResult(triggered=False, reason="No SOC threshold set")
        if battery_level is None:
            return TriggerResult(triggered=False, reason="EV battery level unavailable")

        # Use battery_level as last_value for edge detection
        if battery_level >= threshold:
            if last_value is not None and last_value < threshold:
                store.update_trigger_state(automation_id, float(battery_level))
                return TriggerResult(
                    triggered=True,
                    reason=f"EV battery reached {battery_level}% (threshold: {threshold}%)"
                )
            elif last_value is None:
                store.update_trigger_state(automation_id, float(battery_level))

        store.update_trigger_state(automation_id, float(battery_level))
        return TriggerResult(triggered=False, reason=f"EV battery at {battery_level}%")

    store.update_trigger_state(automation_id, current_value)
    return TriggerResult(triggered=False, reason=f"EV condition '{condition}' not met")


def _evaluate_ocpp_trigger(
    trigger: Dict[str, Any],
    current_state: Dict[str, Any],
    last_value: Optional[float],
    store: "AutomationStore",
    automation_id: int
) -> TriggerResult:
    """
    Evaluate OCPP charger trigger.

    Conditions:
    - connected: Charger connected
    - disconnected: Charger disconnected
    - charging_starts: Charging session starts
    - charging_stops: Charging session stops
    - energy_reaches: Energy delivered reaches threshold
    - available: Charger becomes available
    - faulted: Charger reports fault
    """
    condition = trigger.get("ocpp_condition")
    if not condition:
        return TriggerResult(triggered=False, reason="No OCPP condition set")

    # Get OCPP state from current_state
    ocpp_state = current_state.get("ocpp_state", {})
    status = ocpp_state.get("status", "").lower()
    is_connected = ocpp_state.get("is_connected", False)
    is_charging = status == "charging"
    energy_kwh = ocpp_state.get("energy_kwh", 0)

    # Encode state for edge detection
    status_values = {"available": 1, "preparing": 2, "charging": 3, "finishing": 4, "faulted": 5}
    current_value = float(status_values.get(status, 0))

    if condition == "connected":
        if is_connected:
            if last_value == 0:
                store.update_trigger_state(automation_id, current_value)
                return TriggerResult(triggered=True, reason="OCPP charger connected")
            elif last_value is None:
                store.update_trigger_state(automation_id, current_value)

    elif condition == "disconnected":
        if not is_connected:
            if last_value is not None and last_value > 0:
                store.update_trigger_state(automation_id, current_value)
                return TriggerResult(triggered=True, reason="OCPP charger disconnected")
            elif last_value is None:
                store.update_trigger_state(automation_id, current_value)

    elif condition == "charging_starts":
        if is_charging:
            if last_value is not None and last_value != 3:
                store.update_trigger_state(automation_id, current_value)
                return TriggerResult(triggered=True, reason="OCPP charging started")
            elif last_value is None:
                store.update_trigger_state(automation_id, current_value)

    elif condition == "charging_stops":
        if not is_charging and status in ("available", "finishing"):
            if last_value == 3:
                store.update_trigger_state(automation_id, current_value)
                return TriggerResult(triggered=True, reason="OCPP charging stopped")
            elif last_value is None:
                store.update_trigger_state(automation_id, current_value)

    elif condition == "available":
        if status == "available":
            if last_value is not None and last_value != 1:
                store.update_trigger_state(automation_id, current_value)
                return TriggerResult(triggered=True, reason="OCPP charger available")
            elif last_value is None:
                store.update_trigger_state(automation_id, current_value)

    elif condition == "faulted":
        if status == "faulted":
            if last_value is not None and last_value != 5:
                store.update_trigger_state(automation_id, current_value)
                return TriggerResult(triggered=True, reason="OCPP charger faulted")
            elif last_value is None:
                store.update_trigger_state(automation_id, current_value)

    elif condition == "energy_reaches":
        threshold = trigger.get("ocpp_energy_threshold")
        if threshold is None:
            return TriggerResult(triggered=False, reason="No energy threshold set")

        if energy_kwh >= threshold:
            if last_value is not None and last_value < threshold:
                store.update_trigger_state(automation_id, float(energy_kwh))
                return TriggerResult(
                    triggered=True,
                    reason=f"OCPP energy reached {energy_kwh:.1f} kWh (threshold: {threshold} kWh)"
                )
            elif last_value is None:
                store.update_trigger_state(automation_id, float(energy_kwh))

        store.update_trigger_state(automation_id, float(energy_kwh))
        return TriggerResult(triggered=False, reason=f"OCPP energy at {energy_kwh:.1f} kWh")

    store.update_trigger_state(automation_id, current_value)
    return TriggerResult(triggered=False, reason=f"OCPP condition '{condition}' not met")


def evaluate_conditions(
    conditions: List[Dict[str, Any]],
    current_state: Dict[str, Any]
) -> TriggerResult:
    """
    Evaluate all conditions. All must be true for the result to be True.

    Args:
        conditions: List of condition configurations
        current_state: Current system state

    Returns:
        TriggerResult indicating if all conditions are met
    """
    if not conditions:
        return TriggerResult(triggered=True, reason="No conditions")

    for i, condition in enumerate(conditions):
        result = _evaluate_single_condition(condition, current_state)
        if not result.triggered:
            _LOGGER.debug(f"Condition {i+1} failed: {result.reason}")
            return TriggerResult(triggered=False, reason=f"Condition {i+1} not met: {result.reason}")

    return TriggerResult(triggered=True, reason="All conditions met")


def _evaluate_single_condition(
    condition: Dict[str, Any],
    current_state: Dict[str, Any]
) -> TriggerResult:
    """
    Evaluate a single condition (different from trigger - no edge detection).

    Conditions check current state without caring about transitions.
    """
    condition_type = condition.get("condition_type")

    if condition_type == "battery":
        return _evaluate_battery_condition(condition, current_state)
    elif condition_type == "flow":
        return _evaluate_flow_condition(condition, current_state)
    elif condition_type == "price":
        return _evaluate_price_condition(condition, current_state)
    elif condition_type == "grid":
        return _evaluate_grid_condition(condition, current_state)
    elif condition_type == "weather":
        return _evaluate_weather_condition(condition, current_state)
    elif condition_type == "ev":
        return _evaluate_ev_condition(condition, current_state)
    elif condition_type == "solar_forecast":
        return _evaluate_solar_forecast_condition(condition, current_state)
    else:
        return TriggerResult(triggered=False, reason=f"Unknown condition type: {condition_type}")


def _evaluate_battery_condition(condition: Dict[str, Any], current_state: Dict[str, Any]) -> TriggerResult:
    """Evaluate battery level condition (current state, no edge detection)."""
    battery_percent = current_state.get("battery_percent")
    if battery_percent is None:
        return TriggerResult(triggered=False, reason="Battery level unavailable")

    threshold = condition.get("battery_threshold", 50)
    battery_condition = condition.get("battery_condition", "charged_up_to")

    if battery_condition == "charged_up_to":  # Above
        if battery_percent >= threshold:
            return TriggerResult(triggered=True, reason=f"Battery at {battery_percent}% (>= {threshold}%)")
        return TriggerResult(triggered=False, reason=f"Battery at {battery_percent}% (< {threshold}%)")
    else:  # Below (discharged_down_to)
        if battery_percent <= threshold:
            return TriggerResult(triggered=True, reason=f"Battery at {battery_percent}% (<= {threshold}%)")
        return TriggerResult(triggered=False, reason=f"Battery at {battery_percent}% (> {threshold}%)")


def _evaluate_flow_condition(condition: Dict[str, Any], current_state: Dict[str, Any]) -> TriggerResult:
    """Evaluate power flow condition."""
    source = condition.get("flow_source", "solar")
    comparison = condition.get("flow_comparison", "above")
    threshold = condition.get("flow_threshold_kw", 0)

    # Map source to state key
    source_map = {
        "home_usage": "home_usage_kw",
        "solar": "solar_power_kw",
        "grid_import": "grid_import_kw",
        "grid_export": "grid_export_kw",
        "battery_charge": "battery_charge_kw",
        "battery_discharge": "battery_discharge_kw",
    }

    state_key = source_map.get(source)
    if not state_key:
        return TriggerResult(triggered=False, reason=f"Unknown flow source: {source}")

    value = current_state.get(state_key)
    if value is None:
        return TriggerResult(triggered=False, reason=f"{source} data unavailable")

    if comparison == "above":
        if value >= threshold:
            return TriggerResult(triggered=True, reason=f"{source} at {value:.1f} kW (>= {threshold} kW)")
        return TriggerResult(triggered=False, reason=f"{source} at {value:.1f} kW (< {threshold} kW)")
    else:  # below
        if value <= threshold:
            return TriggerResult(triggered=True, reason=f"{source} at {value:.1f} kW (<= {threshold} kW)")
        return TriggerResult(triggered=False, reason=f"{source} at {value:.1f} kW (> {threshold} kW)")


def _evaluate_price_condition(condition: Dict[str, Any], current_state: Dict[str, Any]) -> TriggerResult:
    """Evaluate price condition."""
    price_type = condition.get("price_type", "import")
    comparison = condition.get("price_comparison", "below")
    threshold = condition.get("price_threshold", 0)

    price = current_state.get("import_price" if price_type == "import" else "export_price")
    if price is None:
        return TriggerResult(triggered=False, reason=f"{price_type} price unavailable")

    if comparison == "above":
        if price >= threshold:
            return TriggerResult(triggered=True, reason=f"{price_type} price ${price:.2f} (>= ${threshold:.2f})")
        return TriggerResult(triggered=False, reason=f"{price_type} price ${price:.2f} (< ${threshold:.2f})")
    else:  # below
        if price <= threshold:
            return TriggerResult(triggered=True, reason=f"{price_type} price ${price:.2f} (<= ${threshold:.2f})")
        return TriggerResult(triggered=False, reason=f"{price_type} price ${price:.2f} (> ${threshold:.2f})")


def _evaluate_grid_condition(condition: Dict[str, Any], current_state: Dict[str, Any]) -> TriggerResult:
    """Evaluate grid status condition."""
    required_status = condition.get("grid_condition", "on_grid")
    current_status = current_state.get("grid_status", "on_grid")

    if current_status == required_status:
        return TriggerResult(triggered=True, reason=f"Grid is {current_status}")
    return TriggerResult(triggered=False, reason=f"Grid is {current_status}, need {required_status}")


def _evaluate_weather_condition(condition: Dict[str, Any], current_state: Dict[str, Any]) -> TriggerResult:
    """Evaluate weather condition."""
    required_weather = condition.get("weather_condition", "sunny")
    current_weather = current_state.get("weather")

    if current_weather is None:
        return TriggerResult(triggered=False, reason="Weather data unavailable")

    # Normalize weather strings
    current_weather_normalized = current_weather.lower().replace(" ", "_")
    if current_weather_normalized == required_weather:
        return TriggerResult(triggered=True, reason=f"Weather is {current_weather}")
    return TriggerResult(triggered=False, reason=f"Weather is {current_weather}, need {required_weather}")


def _evaluate_ev_condition(condition: Dict[str, Any], current_state: Dict[str, Any]) -> TriggerResult:
    """Evaluate EV condition."""
    ev_state = current_state.get("ev_state", {})

    # Check location first if specified
    ev_location = condition.get("ev_location", "any")
    location = ev_state.get("location", "unknown")
    if ev_location and ev_location != "any":
        if ev_location == "home" and location != "home":
            return TriggerResult(triggered=False, reason=f"EV not at home (location: {location})")
        elif ev_location == "work" and location != "work":
            return TriggerResult(triggered=False, reason=f"EV not at work (location: {location})")
        elif ev_location == "other" and location in ("home", "work", "unknown", "not_home"):
            return TriggerResult(triggered=False, reason=f"EV not at other location (location: {location})")

    # Check plugged in status
    ev_plugged_in = condition.get("ev_is_plugged_in")
    if ev_plugged_in is not None:
        is_plugged = ev_state.get("is_plugged_in", False)
        if ev_plugged_in and not is_plugged:
            return TriggerResult(triggered=False, reason="EV is not plugged in")
        if not ev_plugged_in and is_plugged:
            return TriggerResult(triggered=False, reason="EV is plugged in (expected unplugged)")

    # Check charging status
    ev_charging = condition.get("ev_is_charging")
    if ev_charging is not None:
        is_charging = ev_state.get("is_charging", False)
        if ev_charging and not is_charging:
            return TriggerResult(triggered=False, reason="EV is not charging")
        if not ev_charging and is_charging:
            return TriggerResult(triggered=False, reason="EV is charging (expected not charging)")

    # Check SOC
    soc_comparison = condition.get("ev_soc_comparison")
    if soc_comparison:
        threshold = condition.get("ev_soc_threshold", 50)
        battery_level = ev_state.get("battery_level")
        if battery_level is None:
            return TriggerResult(triggered=False, reason="EV battery level unavailable")

        if soc_comparison == "above":
            if battery_level < threshold:
                return TriggerResult(triggered=False, reason=f"EV battery at {battery_level}% (< {threshold}%)")
        else:  # below
            if battery_level > threshold:
                return TriggerResult(triggered=False, reason=f"EV battery at {battery_level}% (> {threshold}%)")

    return TriggerResult(triggered=True, reason="EV condition met")


def _evaluate_solar_forecast_condition(condition: Dict[str, Any], current_state: Dict[str, Any]) -> TriggerResult:
    """Evaluate solar forecast condition."""
    forecast = current_state.get("solcast_forecast", {})
    period = condition.get("solar_forecast_period", "today")
    comparison = condition.get("solar_forecast_comparison", "above")
    threshold = condition.get("solar_forecast_threshold_kwh", 0)

    forecast_value = forecast.get(f"{period}_kwh")
    if forecast_value is None:
        return TriggerResult(triggered=False, reason=f"Solar forecast for {period} unavailable")

    if comparison == "above":
        if forecast_value >= threshold:
            return TriggerResult(triggered=True, reason=f"{period} forecast {forecast_value:.0f} kWh (>= {threshold} kWh)")
        return TriggerResult(triggered=False, reason=f"{period} forecast {forecast_value:.0f} kWh (< {threshold} kWh)")
    else:  # below
        if forecast_value <= threshold:
            return TriggerResult(triggered=True, reason=f"{period} forecast {forecast_value:.0f} kWh (<= {threshold} kWh)")
        return TriggerResult(triggered=False, reason=f"{period} forecast {forecast_value:.0f} kWh (> {threshold} kWh)")
