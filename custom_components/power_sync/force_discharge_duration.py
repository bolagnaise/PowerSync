"""Validation for manual discharge windows, independent of hardware control."""
from __future__ import annotations

import math
import re
from typing import Any

# Native daily schedules cannot represent a multi-day discharge window.
MAX_FORCE_DISCHARGE_MINUTES = 1440
CUSTOM_DISCHARGE_DURATION = "Custom"
CUSTOM_DISCHARGE_DURATION_KEY = "force_discharge_custom_duration"


def validate_discharge_duration(value: Any) -> int:
    """Reject invalid requests instead of silently starting a 30-minute window."""
    try:
        minutes = float(value)
    except (TypeError, ValueError, OverflowError):
        minutes = math.nan
    if (isinstance(value, bool) or not math.isfinite(minutes)
            or not minutes.is_integer() or not 1 <= minutes <= MAX_FORCE_DISCHARGE_MINUTES):
        raise ValueError("Force Discharge duration must be a whole number from 1 to 1440 minutes (24 hours)")
    return int(minutes)


def selected_discharge_duration(value: Any, options: dict) -> int:
    """Resolve the explicit custom selection without substituting a default."""
    if value == CUSTOM_DISCHARGE_DURATION:
        value = options.get(CUSTOM_DISCHARGE_DURATION_KEY, 30)
    return validate_discharge_duration(value)


def _number_accepts_duration(hass: Any, controller: Any, key: str, value: int, label: str) -> None:
    entity_id = getattr(controller, "_entity_map", {}).get(key)
    state = hass.states.get(entity_id) if entity_id else None
    try:
        low = float(state.attributes["min"])
        high = float(state.attributes["max"])
        step = float(state.attributes["step"])
        valid = (all(math.isfinite(x) for x in (low, high, step)) and step > 0
                 and low <= value <= high
                 and math.isclose((value - low) / step, round((value - low) / step), abs_tol=1e-6))
    except (AttributeError, KeyError, TypeError, ValueError, OverflowError):
        valid = False
    if not valid:
        raise ValueError(f"{label} cannot confirm support for this discharge duration; check the native duration control's range and step")


def validate_native_discharge_duration(hass: Any, entry_data: dict, minutes: int) -> None:
    """Preflight new manual values before changing ownership, timers or power."""
    if entry_data.get("foxess_coordinator") is not None and minutes > 1092:
        raise ValueError("FoxESS discharge supports at most 1092 minutes (16-bit timeout)")
    solax = getattr(entry_data.get("solax_coordinator"), "_controller", None)
    if solax is not None:
        profile = getattr(solax, "_control_profile", None)
        if profile == "force_time":
            entity_id = getattr(solax, "_entity_map", {}).get("export_duration")
            state = hass.states.get(entity_id) if entity_id else None
            options = state.attributes.get("options", []) if state else []
            supported = []
            for option in options:
                match = re.fullmatch(r"(\d+)(?:\s*(?:min|mins|minutes?))?", str(option).strip(), re.IGNORECASE)
                if match:
                    supported.append(int(match.group(1)))
            if minutes not in supported:
                raise ValueError(f"SolaX native export duration does not support {minutes} minutes; choose an exact native option")
        elif profile == "remote_control":
            _number_accepts_duration(hass, solax, "remotecontrol_autorepeat_duration", minutes * 60, "SolaX")
        elif profile != "manual":
            raise ValueError("SolaX discharge duration support is not ready; wait for native control discovery")
    neovolt = getattr(entry_data.get("neovolt_coordinator"), "_controller", None)
    if neovolt is not None:
        for controller in getattr(neovolt, "_controllers", [neovolt]):
            _number_accepts_duration(hass, controller, "dispatch_duration", minutes, "Neovolt")
