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
