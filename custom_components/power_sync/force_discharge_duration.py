"""Validation for manual charge/discharge windows, independent of hardware control."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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
        raise ValueError("Charge/discharge duration must be a whole number from 1 to 1440 minutes (24 hours)")
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
        raise ValueError(f"{label} cannot confirm support for this duration; check the native duration control's range and step")


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


def resolve_manual_force_duration(data: Any, options: dict, time_zone: str, *, now: datetime | None = None) -> int:
    """Resolve explicit minutes or the next real HA-local HH:mm occurrence.

    UTC round-tripping excludes nonexistent spring-forward times; checking both
    folds picks the next occurrence during a repeated autumn hour. Native force
    lifecycles accept whole minutes, so finish-at requests round up by <60 s.
    """
    if "until" not in data:
        return selected_discharge_duration(data.get("duration", 30), options)
    if "duration" in data:
        raise ValueError("Choose duration or until, not both")
    value = data["until"]
    if not isinstance(value, str) or not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", value):
        raise ValueError("Until must be a clock time in HH:mm format")
    try:
        zone = ZoneInfo(time_zone)
    except (ZoneInfoNotFoundError, TypeError, ValueError) as err:
        raise ValueError("Home Assistant timezone is unavailable") from err
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    hour, minute = map(int, value.split(":"))
    local_date = now.astimezone(zone).date()
    candidates = []
    for offset in range(3):
        day = local_date + timedelta(days=offset)
        for fold in (0, 1):
            local = datetime(day.year, day.month, day.day, hour, minute, tzinfo=zone, fold=fold)
            utc = local.astimezone(timezone.utc)
            back = utc.astimezone(zone)
            if back.replace(tzinfo=None) == local.replace(tzinfo=None) and utc > now:
                candidates.append(utc)
    if not candidates:
        raise ValueError("Until has no upcoming valid occurrence")
    return validate_discharge_duration(math.ceil((min(candidates) - now).total_seconds() / 60))


def validate_native_charge_duration(hass: Any, entry_data: dict, minutes: int) -> None:
    """Reject unsupported manual charge windows before ownership or writes."""
    if entry_data.get("foxess_coordinator") is not None and minutes > 1092:
        raise ValueError("FoxESS charge supports at most 1092 minutes (16-bit timeout)")
    solax = getattr(entry_data.get("solax_coordinator"), "_controller", None)
    if solax is not None:
        profile = getattr(solax, "_control_profile", None)
        if profile == "remote_control":
            _number_accepts_duration(hass, solax, "remotecontrol_autorepeat_duration", minutes * 60, "SolaX")
        elif profile == "force_time":
            if minutes >= 1440:
                raise ValueError("SolaX native charge schedules support less than 24 hours")
        elif profile != "manual":
            raise ValueError("SolaX charge duration support is not ready; wait for native control discovery")
    neovolt = getattr(entry_data.get("neovolt_coordinator"), "_controller", None)
    if neovolt is not None:
        for controller in getattr(neovolt, "_controllers", [neovolt]):
            _number_accepts_duration(hass, controller, "dispatch_duration", minutes, "Neovolt")
