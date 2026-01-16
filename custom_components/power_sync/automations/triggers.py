"""
Trigger evaluation logic for HA automations.

Supports the following trigger types:
- time: Trigger at specific time(s) of day
- battery: Trigger based on battery state of charge
- flow: Trigger based on power flow
- price: Trigger based on electricity price thresholds
- grid: Trigger when grid status changes
- weather: Trigger based on weather conditions
"""

import logging
from dataclasses import dataclass
from datetime import datetime, time as dt_time, timedelta
from typing import Dict, Any, Optional, TYPE_CHECKING

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
    if not _is_within_time_window(trigger, current_state):
        return TriggerResult(triggered=False, reason="Outside time window")

    trigger_type = trigger.get("trigger_type")

    if trigger_type == "time":
        return _evaluate_time_trigger(trigger, current_state, store, automation_id)
    elif trigger_type == "battery":
        return _evaluate_battery_trigger(trigger, current_state, last_evaluated_value, store, automation_id)
    elif trigger_type == "flow":
        return _evaluate_flow_trigger(trigger, current_state, last_evaluated_value, store, automation_id)
    elif trigger_type == "price":
        return _evaluate_price_trigger(trigger, current_state, last_evaluated_value, store, automation_id)
    elif trigger_type == "grid":
        return _evaluate_grid_trigger(trigger, current_state, last_evaluated_value, store, automation_id)
    elif trigger_type == "weather":
        return _evaluate_weather_trigger(trigger, current_state, last_evaluated_value, store, automation_id)
    else:
        _LOGGER.warning(f"Unknown trigger type: {trigger_type}")
        return TriggerResult(triggered=False, reason=f"Unknown trigger type: {trigger_type}")


def _is_within_time_window(trigger: Dict[str, Any], current_state: Dict[str, Any]) -> bool:
    """Check if current time is within the optional time window."""
    start_str = trigger.get("time_window_start")
    end_str = trigger.get("time_window_end")

    if not start_str or not end_str:
        return True

    try:
        start = datetime.strptime(start_str, "%H:%M").time()
        end = datetime.strptime(end_str, "%H:%M").time()
    except ValueError:
        return True

    current_time = current_state.get("current_time", datetime.now()).time()

    # Handle overnight windows
    if start <= end:
        return start <= current_time <= end
    else:
        return current_time >= start or current_time <= end


def _evaluate_time_trigger(
    trigger: Dict[str, Any],
    current_state: Dict[str, Any],
    store: "AutomationStore",
    automation_id: int
) -> TriggerResult:
    """Evaluate time-based trigger."""
    time_str = trigger.get("time_of_day")
    if not time_str:
        return TriggerResult(triggered=False, reason="No time_of_day set")

    try:
        trigger_time = datetime.strptime(time_str, "%H:%M").time()
    except ValueError:
        return TriggerResult(triggered=False, reason="Invalid time format")

    now = current_state.get("current_time", datetime.now())
    current_time = now.time()

    # Check repeat days (0=Sun, 1=Mon, ..., 6=Sat)
    repeat_days = trigger.get("repeat_days")
    if repeat_days:
        day_of_week = (now.weekday() + 1) % 7  # Convert to Sunday=0
        allowed_days = [int(d) for d in repeat_days.split(",") if d.strip().isdigit()]
        if day_of_week not in allowed_days:
            return TriggerResult(triggered=False, reason=f"Day {day_of_week} not in repeat_days")

    # Check if current time matches (within 1 minute tolerance)
    trigger_datetime = datetime.combine(now.date(), trigger_time)
    time_diff = abs((now - trigger_datetime).total_seconds())

    if time_diff <= 60:
        # Check if already triggered recently
        auto = store.get_by_id(automation_id)
        if auto and auto.get("last_evaluated_at"):
            try:
                last_eval = datetime.fromisoformat(auto["last_evaluated_at"])
                if (now - last_eval).total_seconds() < 300:
                    return TriggerResult(triggered=False, reason="Already triggered recently")
            except ValueError:
                pass

        store.update_trigger_state(automation_id, 1.0)
        return TriggerResult(
            triggered=True,
            reason=f"Time trigger at {trigger_time.strftime('%H:%M')}"
        )

    return TriggerResult(triggered=False, reason="Not yet time")


def _evaluate_battery_trigger(
    trigger: Dict[str, Any],
    current_state: Dict[str, Any],
    last_value: Optional[float],
    store: "AutomationStore",
    automation_id: int
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
            if last_value is not None and last_value < threshold:
                store.update_trigger_state(automation_id, battery_percent)
                return TriggerResult(
                    triggered=True,
                    reason=f"Battery charged to {battery_percent}% (threshold: {threshold}%)"
                )
            elif last_value is None:
                store.update_trigger_state(automation_id, battery_percent)

        store.update_trigger_state(automation_id, battery_percent)
        return TriggerResult(triggered=False, reason=f"Battery at {battery_percent}%")

    elif condition == "discharged_down_to":
        if threshold is None:
            return TriggerResult(triggered=False, reason="No threshold set")

        if battery_percent <= threshold:
            if last_value is not None and last_value > threshold:
                store.update_trigger_state(automation_id, battery_percent)
                return TriggerResult(
                    triggered=True,
                    reason=f"Battery discharged to {battery_percent}% (threshold: {threshold}%)"
                )
            elif last_value is None:
                store.update_trigger_state(automation_id, battery_percent)

        store.update_trigger_state(automation_id, battery_percent)
        return TriggerResult(triggered=False, reason=f"Battery at {battery_percent}%")

    elif condition == "discharged_to_reserve":
        if backup_reserve is None:
            return TriggerResult(triggered=False, reason="Backup reserve unavailable")

        if battery_percent <= backup_reserve + 1:
            if last_value is not None and last_value > backup_reserve + 1:
                store.update_trigger_state(automation_id, battery_percent)
                return TriggerResult(
                    triggered=True,
                    reason=f"Battery at reserve ({battery_percent}%, reserve: {backup_reserve}%)"
                )
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
    automation_id: int
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
            if last_value is not None and last_value < threshold_kw:
                store.update_trigger_state(automation_id, current_value)
                return TriggerResult(
                    triggered=True,
                    reason=f"{source} rose to {current_value:.2f}kW (threshold: {threshold_kw}kW)"
                )
            elif last_value is None:
                store.update_trigger_state(automation_id, current_value)

    elif transition == "drops_below":
        if current_value <= threshold_kw:
            if last_value is not None and last_value > threshold_kw:
                store.update_trigger_state(automation_id, current_value)
                return TriggerResult(
                    triggered=True,
                    reason=f"{source} dropped to {current_value:.2f}kW (threshold: {threshold_kw}kW)"
                )
            elif last_value is None:
                store.update_trigger_state(automation_id, current_value)

    store.update_trigger_state(automation_id, current_value)
    return TriggerResult(triggered=False, reason=f"{source} at {current_value:.2f}kW")


def _evaluate_price_trigger(
    trigger: Dict[str, Any],
    current_state: Dict[str, Any],
    last_value: Optional[float],
    store: "AutomationStore",
    automation_id: int
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
            if last_value is not None and last_value < threshold:
                store.update_trigger_state(automation_id, current_price)
                return TriggerResult(
                    triggered=True,
                    reason=f"{price_type} price rose to ${current_price:.4f}/kWh"
                )
            elif last_value is None:
                store.update_trigger_state(automation_id, current_price)

    elif transition == "drops_below":
        if current_price <= threshold:
            if last_value is not None and last_value > threshold:
                store.update_trigger_state(automation_id, current_price)
                return TriggerResult(
                    triggered=True,
                    reason=f"{price_type} price dropped to ${current_price:.4f}/kWh"
                )
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
