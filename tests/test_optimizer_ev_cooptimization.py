"""End-to-end LP tests for EV charging co-optimized with the home battery.

Scenario throughout: a tight site import limit that the battery alone would
happily saturate. Before the EV was modeled, the LP planned battery charge
against the whole limit and the car silently ate into it at runtime.
"""

from __future__ import annotations

import importlib
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
COMPONENT_ROOT = ROOT / "custom_components" / "power_sync"
_SENTINEL = object()
_STUBS = (
    "homeassistant",
    "homeassistant.util",
    "homeassistant.util.dt",
    "power_sync",
    "power_sync.optimization",
    "power_sync.optimization.battery_optimizer",
    "power_sync.optimization.ev_load_plan",
    "power_sync.optimization.schedule_reader",
)


@pytest.fixture()
def optimizer_module():
    saved = {name: sys.modules.get(name, _SENTINEL) for name in _STUBS}
    for name in _STUBS:
        sys.modules.pop(name, None)
    ha = types.ModuleType("homeassistant")
    util = types.ModuleType("homeassistant.util")
    dt = types.ModuleType("homeassistant.util.dt")
    dt.now = lambda: datetime(2026, 8, 19, tzinfo=timezone.utc)
    dt.utcnow = dt.now
    dt.UTC = timezone.utc
    util.dt = dt
    ha.util = util
    sys.modules.update(
        {"homeassistant": ha, "homeassistant.util": util, "homeassistant.util.dt": dt}
    )
    package = types.ModuleType("power_sync")
    package.__path__ = [str(COMPONENT_ROOT)]
    optimization = types.ModuleType("power_sync.optimization")
    optimization.__path__ = [str(COMPONENT_ROOT / "optimization")]
    sys.modules["power_sync"] = package
    sys.modules["power_sync.optimization"] = optimization
    module = importlib.import_module("power_sync.optimization.battery_optimizer")
    try:
        yield module
    finally:
        for name, value in saved.items():
            if value is _SENTINEL:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value


# 16.1 kW site import limit, 14.7 kW battery charge capability: the battery
# alone can nearly saturate the connection.
def _optimizer(module, *, max_grid_import_w=16_100):
    return module.BatteryOptimizer(
        capacity_wh=40_000,
        max_charge_w=14_700,
        max_discharge_w=10_000,
        max_grid_import_w=max_grid_import_w,
        backup_reserve=0.05,
        interval_minutes=60,
        horizon_hours=8,
    )


def _ev_plan(module, **overrides):
    kwargs = {
        "vehicle_id": "car",
        # Plugged in for the whole horizon, 7 kW charger.
        "max_power_kw": (7.0,) * 8,
        "energy_needed_kwh": 14.0,
        "charge_efficiency": 1.0,
        "min_power_kw": 1.4,
    }
    kwargs.update(overrides)
    return module.EVChargePlan(**kwargs)


def _kwargs(n=8, *, cheap_slots=(0, 1, 2, 3)):
    start = datetime(2026, 8, 19, 22, 0, tzinfo=timezone.utc)
    # Cheap overnight window then expensive daytime.
    import_prices = [0.10 if idx in cheap_slots else 0.45 for idx in range(n)]
    return {
        "import_prices": import_prices,
        "export_prices": [0.05] * n,
        "solar_forecast": [0.0] * n,
        "load_forecast": [0.5] * n,
        "current_soc": 0.10,
        "schedule_timestamps": [start + timedelta(hours=i) for i in range(n)],
        "allow_grid_charge": True,
    }


def _flows(result, dt_hours=1.0):
    """Return per-slot (battery_charge_kw, ev_charge_kw) from a solved plan."""
    return [
        (action.battery_charge_w / 1000.0, action.ev_charge_w / 1000.0)
        for action in result.schedule.actions
    ]


def test_battery_and_ev_together_respect_the_site_import_limit(optimizer_module):
    optimizer = _optimizer(optimizer_module)

    result = optimizer.optimize(
        **_kwargs(),
        ev_plan=_ev_plan(optimizer_module),
    )

    assert result.feasible
    for battery_kw, ev_kw in _flows(result):
        # 0.5 kW house load shares the same 16.1 kW connection.
        assert battery_kw + ev_kw + 0.5 <= 16.1 + 1e-6


def test_ev_energy_is_delivered_within_its_window(optimizer_module):
    optimizer = _optimizer(optimizer_module)

    result = optimizer.optimize(
        **_kwargs(),
        ev_plan=_ev_plan(optimizer_module),
    )

    delivered_kwh = sum(ev_kw for _battery_kw, ev_kw in _flows(result))
    assert delivered_kwh == pytest.approx(14.0, abs=0.05)


def test_ev_charging_lands_in_the_cheapest_slots(optimizer_module):
    optimizer = _optimizer(optimizer_module)

    result = optimizer.optimize(
        **_kwargs(cheap_slots=(4, 5, 6, 7)),
        ev_plan=_ev_plan(optimizer_module),
    )

    flows = _flows(result)
    cheap_ev_kwh = sum(ev_kw for _b, ev_kw in flows[4:])
    expensive_ev_kwh = sum(ev_kw for _b, ev_kw in flows[:4])
    assert cheap_ev_kwh > expensive_ev_kwh


def test_battery_plan_shrinks_when_the_car_needs_the_same_headroom(
    optimizer_module,
):
    # The regression this whole change exists for. The car is plugged in only
    # across the two cheap slots and needs every one of its 7 kW there, so the
    # battery genuinely cannot have the whole connection any more.
    scenario = _kwargs(cheap_slots=(0, 1))
    without_ev = _optimizer(optimizer_module).optimize(**scenario)
    with_ev = _optimizer(optimizer_module).optimize(
        **scenario,
        ev_plan=_ev_plan(
            optimizer_module,
            max_power_kw=(7.0, 7.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
            energy_needed_kwh=14.0,
        ),
    )

    peak_without = max(battery_kw for battery_kw, _ev in _flows(without_ev))
    peak_with = max(battery_kw for battery_kw, _ev in _flows(with_ev))
    # 16.1 kW limit less 0.5 kW house less the car's 7 kW leaves 8.6 kW.
    assert peak_without == pytest.approx(14.7, abs=0.1)
    assert peak_with == pytest.approx(8.6, abs=0.1)


def test_no_ev_plan_leaves_the_model_unchanged(optimizer_module):
    baseline = _optimizer(optimizer_module).optimize(**_kwargs())
    with_empty_plan = _optimizer(optimizer_module).optimize(
        **_kwargs(),
        ev_plan=_ev_plan(optimizer_module, energy_needed_kwh=0.0),
    )

    assert _flows(baseline) == _flows(with_empty_plan)
    assert all(action.ev_charge_w == 0 for action in baseline.schedule.actions)


def test_impossible_ev_demand_stays_feasible_and_charges_what_it_can(
    optimizer_module,
):
    # A hard delivery constraint would make the whole solve infeasible and drop
    # every user to the greedy fallback over one unreachable car target.
    optimizer = _optimizer(optimizer_module)

    result = optimizer.optimize(
        **_kwargs(),
        ev_plan=_ev_plan(
            optimizer_module,
            max_power_kw=(7.0, 7.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
            energy_needed_kwh=90.0,
        ),
    )

    assert result.feasible
    delivered_kwh = sum(ev_kw for _b, ev_kw in _flows(result))
    # Fills the two available slots at full rate rather than giving up.
    assert delivered_kwh == pytest.approx(14.0, abs=0.05)


def test_ev_only_charges_inside_its_plugged_in_window(optimizer_module):
    optimizer = _optimizer(optimizer_module)

    result = optimizer.optimize(
        **_kwargs(),
        ev_plan=_ev_plan(
            optimizer_module,
            max_power_kw=(0.0, 0.0, 0.0, 0.0, 7.0, 7.0, 7.0, 7.0),
            energy_needed_kwh=14.0,
        ),
    )

    flows = _flows(result)
    assert all(ev_kw == pytest.approx(0.0) for _b, ev_kw in flows[:4])
    assert sum(ev_kw for _b, ev_kw in flows[4:]) == pytest.approx(14.0, abs=0.05)


def test_greedy_fallback_accounts_for_the_car_as_known_load(optimizer_module):
    # The greedy heuristic cannot co-optimize, but it must not plan battery
    # charge against import headroom the car is going to occupy. It takes the
    # car as known load, which is visible in the charge ceiling it works to.
    optimizer = _optimizer(optimizer_module)

    without_ev_kw = optimizer._charge_limit_kw(0.5, 0.0, True)
    with_ev_kw = optimizer._charge_limit_kw(0.5 + 7.0, 0.0, True)

    assert without_ev_kw == pytest.approx(14.7)
    assert with_ev_kw == pytest.approx(8.6, abs=0.01)


def test_greedy_fallback_reports_the_ev_draw_it_assumed(optimizer_module):
    optimizer = _optimizer(optimizer_module)
    scenario = _kwargs()

    greedy = optimizer._solve_greedy(
        8,
        scenario["import_prices"],
        scenario["export_prices"],
        [0.0] * 8,
        [0.5] * 8,
        0.10,
        "cost",
        ev_plan=_ev_plan(optimizer_module),
    )

    ev_kwh = sum(
        action.ev_charge_w / 1000.0 for action in greedy.schedule.actions
    )
    assert ev_kwh == pytest.approx(14.0, abs=0.05)


def test_overlay_and_decision_variable_are_mutually_exclusive():
    # Regression: the coordinator has always folded planned EV load into the
    # load forecast. Adding the ev_charge decision variable without disabling
    # that overlay counts the car twice and understates import headroom.
    source = (
        Path(__file__).resolve().parents[1]
        / "custom_components" / "power_sync" / "optimization" / "coordinator.py"
    ).read_text()

    assert "self._pending_ev_charge_plan = self._build_ev_charge_plan(" in source
    assert "effective_ev_load_w = zeros" in source
    # The solve must consume the plan decided at overlay time, not rebuild it
    # after the overlay has already been applied to load.
    assert 'ev_charge_plan = getattr(self, "_pending_ev_charge_plan", None)' in source


def test_double_counting_would_halve_the_battery_plan(optimizer_module):
    # Guards the arithmetic the exclusivity protects: if the car were counted
    # in load *and* as a decision variable, the battery would be squeezed by
    # twice the car's draw.
    scenario = _kwargs(cheap_slots=(0, 1))
    plan = _ev_plan(
        optimizer_module,
        max_power_kw=(7.0, 7.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
        energy_needed_kwh=14.0,
    )
    correct = _optimizer(optimizer_module).optimize(**scenario, ev_plan=plan)

    doubled = dict(scenario)
    doubled["load_forecast"] = [0.5 + 7.0, 0.5 + 7.0] + [0.5] * 6
    wrong = _optimizer(optimizer_module).optimize(**doubled, ev_plan=plan)

    peak_correct = max(b for b, _ev in _flows(correct))
    peak_wrong = max(b for b, _ev in _flows(wrong))
    assert peak_correct == pytest.approx(8.6, abs=0.1)
    assert peak_wrong < peak_correct - 5.0
