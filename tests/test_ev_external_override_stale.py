"""Solar Surplus external-manual override must not outlive its evidence."""

from __future__ import annotations

import ast
import math
import time
from pathlib import Path
from typing import Any, Dict

import pytest

from conftest import load_source_node

ACTIONS = Path(__file__).parents[1] / "custom_components/power_sync/automations/actions.py"
WINDOW = 30 * 60


def _stale_fn():
    ns = {
        "math": math, "time": time, "Dict": Dict, "Any": Any,
        "_ACTIVE_EV_POWER_EPSILON_KW": 0.05,
        "EXTERNAL_MANUAL_OVERRIDE_STALE_SECONDS": WINDOW,
    }
    node = load_source_node(ACTIONS, "_external_manual_override_stale")
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(ACTIONS), "exec"), ns)
    return ns["_external_manual_override_stale"]


def test_sleeping_car_without_draw_releases_after_window():
    stale = _stale_fn()
    state = {"external_manual_override_since": 1000.0}
    assert stale(state, power_available=False, power_kw=0.0, now=1000.0 + WINDOW - 1) is False
    assert stale(state, power_available=False, power_kw=0.0, now=1000.0 + WINDOW) is True


def test_measured_draw_keeps_override_however_old():
    stale = _stale_fn()
    state = {"external_manual_override_since": 0.0}
    assert stale(state, power_available=True, power_kw=7.2, now=10 * WINDOW) is False


@pytest.mark.parametrize("since", [None, "bad", float("nan")])
def test_restored_state_without_timestamp_starts_window_instead_of_never(since):
    stale = _stale_fn()
    state = {"external_manual_override_since": since} if since is not None else {}
    assert stale(state, power_available=False, power_kw=0.0, now=5000.0) is False
    assert state["external_manual_override_since"] == 5000.0
    assert stale(state, power_available=False, power_kw=0.0, now=5000.0 + WINDOW) is True


def test_every_override_set_site_stamps_its_start():
    source = ACTIONS.read_text()
    sets = source.count('state["external_manual_override"] = True')
    assert sets >= 2
    assert source.count('state["external_manual_override_since"] = time.time()') == sets


def test_surplus_loop_consults_staleness_before_suspending():
    body = ast.unparse(load_source_node(ACTIONS, "_dynamic_ev_update_surplus"))
    check = body.index("_external_manual_override_stale(")
    suspend = body.index("External manual charging active; Solar Surplus rate control suspended")
    assert check < suspend
