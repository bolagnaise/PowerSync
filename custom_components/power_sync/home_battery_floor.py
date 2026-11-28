"""One home-battery floor rule for every EV charging mode.

Mirrors the optimizer's EV contract (``battery_optimizer``):
- a session that is not running may not start below the start threshold,
  unless the caller has a solar exception;
- a running session continues;
- a running session stops at the floor only when "stop at floor" is set.

An unknown battery level never counts as 0%. It blocks a new start only when
that mode's start threshold is above 0, and it never stops a running session:
only a real reading at or below the floor does.
"""

from __future__ import annotations

import math
from typing import Any


def valid_home_battery_soc(value: Any) -> float | None:
    """Return a 0-100 battery percentage, or None when the reading is unusable."""
    if value is None or isinstance(value, bool):
        return None
    try:
        soc = float(value)
    except (TypeError, ValueError):
        return None
    return soc if math.isfinite(soc) and 0 <= soc <= 100 else None


def home_battery_start_allowed(
    soc: Any, min_to_start: Any, *, solar_exception: bool = False,
) -> tuple[bool, str | None]:
    """Return whether a new EV session may start, and why not when it may not."""
    try:
        threshold = max(0.0, float(min_to_start or 0))
    except (TypeError, ValueError):
        threshold = 0.0
    if threshold <= 0 or solar_exception:
        return True, None
    reading = valid_home_battery_soc(soc)
    if reading is None:
        return False, f"Home battery level unknown; waiting for a reading to check the {threshold:g}% minimum"
    if reading < threshold:
        return False, f"Home battery {reading:.0f}% < {threshold:g}% minimum to start"
    return True, None


def home_battery_floor_reached(soc: Any, floor: Any, stop_at_floor: bool) -> bool:
    """Whether a real reading says a running session must stop at its floor."""
    if not stop_at_floor:
        return False
    try:
        level = float(floor or 0)
    except (TypeError, ValueError):
        return False
    reading = valid_home_battery_soc(soc)
    return level > 0 and reading is not None and reading <= level
