"""Stored EV mode settings shared by UI reads, automation writes and executors."""

from __future__ import annotations

import math
import re
from typing import Any


MODE_DEFAULTS = {
    "scheduled_charging": {
        "enabled": False, "start_time": "00:00", "end_time": "06:00",
        "max_price_cents": 30, "preserve_home_battery": False, "no_grid_import": False,
    },
    "price_level_charging": {
        "enabled": False, "recovery_soc": 40, "recovery_price_cents": 30,
        "opportunity_price_cents": 10, "preserve_home_battery": False,
        "no_grid_import": False, "home_battery_minimum": 20,
    },
}


def finite_price_cents(value: Any, field: str, *, legacy: bool = False) -> float:
    """Accept finite minor currency units per kWh, including negative tariffs."""
    if isinstance(value, str) and legacy:
        try:
            value = float(value)
        except ValueError:
            pass
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            if math.isfinite(value):
                return float(value)
        except OverflowError:
            pass
    raise ValueError(f"{field} must be a finite number in minor currency units/kWh")


def _mode_value(field: str, value: Any, *, legacy: bool) -> Any:
    if field.endswith("_price_cents"):
        return finite_price_cents(value, field, legacy=legacy)
    if field in ("recovery_soc", "home_battery_minimum"):
        if legacy and isinstance(value, str):
            try:
                value = float(value)
            except ValueError:
                pass
        if isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value <= 100:
            return value
        raise ValueError(f"{field} must be a percentage from 0 to 100")
    if field in ("start_time", "end_time"):
        if isinstance(value, str) and re.fullmatch(r"(?:[01]?\d|2[0-3]):[0-5]\d", value):
            return value.zfill(5)
        raise ValueError(f"{field} must be a local time in HH:MM format")
    if isinstance(value, bool):
        return value
    if legacy and value in (0, 1, "true", "false"):
        return value in (1, "true")
    raise ValueError(f"{field} must be a boolean")


def charging_mode_settings(mode: str, stored=None, updates=None) -> dict:
    """Apply a partial patch; retain old finite values and close corrupt price gates."""
    defaults = MODE_DEFAULTS[mode]
    if updates is not None and not isinstance(updates, dict):
        raise ValueError("EV charging settings must be an object")
    updates = updates or {}
    stored = stored if isinstance(stored, dict) else {}
    settings = {**defaults, **stored}
    invalid_protection = False
    for key in (defaults.keys() & stored.keys()) - updates.keys():
        try:
            settings[key] = _mode_value(key, stored[key], legacy=True)
        except ValueError:
            if key.endswith("_price_cents"):
                settings[key] = 0.0
            else:
                # A corrupt time, battery threshold or protection must be repaired
                # before the persisted mode can take ownership again.
                settings[key] = defaults[key]
                invalid_protection = True
    for key in defaults.keys() & updates.keys():
        settings[key] = _mode_value(key, updates[key], legacy=False)
    if invalid_protection:
        if updates.get("enabled") is True:
            raise ValueError("Repair invalid EV charging settings before enabling this mode")
        settings["enabled"] = False
    if updates.get("no_grid_import") is True and not updates.get("preserve_home_battery"):
        settings["preserve_home_battery"] = False
    if settings["preserve_home_battery"]:
        settings["no_grid_import"] = False
    return settings


class SmartScheduleSettingsConflict(ValueError):
    """A guarded setting changed after the editor read it."""

    def __init__(self, field: str):
        self.field = field
        super().__init__(f"{field} changed while editing. Reload the vehicle settings.")


_SMART_DAY_FIELDS = {
    "departure_times": "time",
    "departure_priorities": "priority",
    "departure_max_grid_price_cents": "max_grid_price_cents",
    "departure_min_battery_to_start": "min_battery_to_start",
    "departure_consume_battery_level": "consume_battery_level",
    "departure_stop_at_battery_floor": "stop_at_battery_floor",
    "departure_limit_grid_import": "limit_grid_import",
    "departure_preserve_home_battery": "preserve_home_battery",
}


def _valid_guarded_value(key: str, value: Any) -> bool:
    if key == "max_grid_price_cents":
        finite_price_cents(value, key)
        return True
    if key == "priority":
        return value in ("solar_only", "solar_preferred", "cost_optimized", "time_critical")
    if key == "time":
        return isinstance(value, str) and re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value) is not None
    if key in ("target_soc", "consume_battery_level", "min_battery_to_start"):
        minimum = 1 if key == "target_soc" else 0
        return type(value) in (int, float) and minimum <= value <= 100 and value == int(value)
    if key in ("enabled", "preserve_home_battery", "limit_grid_import", "stop_at_battery_floor"):
        return type(value) is bool
    return False


def _valid_guarded_field(key: str, value: Any) -> bool:
    if key in _SMART_DAY_FIELDS:
        return isinstance(value, dict) and all(
            day in ("0", "1", "2", "3", "4", "5", "6") and _valid_guarded_value(_SMART_DAY_FIELDS[key], item)
            for day, item in value.items()
        )
    return key != "time" and _valid_guarded_value(key, value)


def check_smart_settings_precondition(current: dict, updates: dict) -> None:
    """Check the proposed JSON patch against the latest editable vehicle snapshot."""
    expected = updates["expected_settings"]
    if not isinstance(expected, dict) or not expected:
        raise ValueError("expected_settings must be a nonempty object of edited settings")
    for key, before in expected.items():
        if key not in updates or key not in current:
            raise ValueError(f"expected_settings must name an edited setting: {key}")
        if not _valid_guarded_field(key, before) or not _valid_guarded_field(key, updates[key]):
            raise ValueError(f"Invalid expected_settings or update for {key}")
    for key, before in expected.items():
        latest = current[key]
        if not _valid_guarded_field(key, latest) or (latest != before and latest != updates[key]):
            raise SmartScheduleSettingsConflict(key)
