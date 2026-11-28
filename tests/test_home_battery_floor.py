"""One home-battery floor rule for every EV charging mode."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parent.parent / "custom_components" / "power_sync" / "home_battery_floor.py"
_spec = importlib.util.spec_from_file_location("home_battery_floor_under_test", _PATH)
floor = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(floor)


@pytest.mark.parametrize(("soc", "minimum", "solar", "allowed"), [
    (50, 20, False, True),
    (20, 20, False, True),
    (19.9, 20, False, False),
    (None, 20, False, False),       # unknown blocks a new start...
    (None, 0, False, True),         # ...only when the threshold is above 0
    ("unavailable", 20, False, False),
    (150, 20, False, False),        # out-of-range readings are unknown
    (5, 20, True, True),            # solar exception
    (5, None, False, True),
])
def test_start_threshold(soc, minimum, solar, allowed):
    ok, reason = floor.home_battery_start_allowed(soc, minimum, solar_exception=solar)
    assert ok is allowed
    assert (reason is None) is allowed


@pytest.mark.parametrize(("soc", "level", "stop", "reached"), [
    (10, 10, True, True),
    (9, 10, True, True),
    (11, 10, True, False),
    (5, 10, False, False),          # "stop at floor" off: keep going
    (None, 10, True, False),        # an unknown level never stops a session
    (0, 0, True, False),            # no floor set
])
def test_running_floor(soc, level, stop, reached):
    assert floor.home_battery_floor_reached(soc, level, stop) is reached
