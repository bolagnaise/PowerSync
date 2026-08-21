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


def test_equal_price_window_starts_in_its_earliest_executable_slots(
    optimizer_module,
):
    """A 10:00 free window must not drift to its equally free 13:00 tail."""
    optimizer = _optimizer(optimizer_module)
    scenario = _kwargs(cheap_slots=tuple(range(8)))
    scenario["import_prices"] = [0.0] * 8
    scenario["export_prices"] = [0.0] * 8

    result = optimizer.optimize(
        **scenario,
        ev_plan=_ev_plan(optimizer_module),
    )

    ev_kw = [ev_kw for _battery_kw, ev_kw in _flows(result)]
    assert ev_kw[:2] == pytest.approx([7.0, 7.0], abs=0.05)
    assert max(ev_kw[2:]) < 0.05


def test_solar_only_ev_cannot_be_moved_to_a_cheaper_grid_slot(optimizer_module):
    optimizer = _optimizer(optimizer_module)
    kwargs = _kwargs(cheap_slots=(0, 1, 3, 4, 5, 6, 7))
    kwargs["solar_forecast"] = [0.0, 0.0, 7.5, 0.0, 0.0, 0.0, 0.0, 0.0]
    plan = _ev_plan(
        optimizer_module,
        energy_needed_kwh=7.0,
        allow_grid=(False,) * 8,
        allow_solar=(True,) * 8,
        allow_battery=(False,) * 8,
        window_source=("solar_surplus",) * 8,
    )

    result = optimizer.optimize(**kwargs, ev_plan=plan)

    ev_kw = [action.ev_charge_w / 1000.0 for action in result.schedule.actions]
    assert sum(ev_kw) == pytest.approx(7.0, abs=0.05)
    assert ev_kw[2] == pytest.approx(7.0, abs=0.05)
    assert max(result.ev_source_by_vehicle_w["car"]["grid"]) < 1.0
    assert sum(result.ev_source_by_vehicle_w["car"]["solar"]) / 1000.0 == pytest.approx(
        7.0, abs=0.05
    )


def test_consume_battery_stop_at_floor_blocks_grid_fallback_at_floor(
    optimizer_module,
):
    optimizer = _optimizer(optimizer_module)
    kwargs = _kwargs()
    kwargs["current_soc"] = 0.30
    kwargs["allow_grid_charge"] = False
    plan = _ev_plan(
        optimizer_module,
        energy_needed_kwh=7.0,
        allow_grid=(True,) * 8,
        allow_battery=(True,) * 8,
        battery_floor_soc=(0.30,) * 8,
        stop_at_battery_floor=(True,) * 8,
    )

    result = optimizer.optimize(**kwargs, ev_plan=plan)

    assert sum(action.ev_charge_w for action in result.schedule.actions) < 1.0


def test_consume_battery_floor_with_grid_fallback_preserves_the_floor(
    optimizer_module,
):
    optimizer = _optimizer(optimizer_module)
    kwargs = _kwargs()
    kwargs["current_soc"] = 0.30
    plan = _ev_plan(
        optimizer_module,
        energy_needed_kwh=7.0,
        allow_grid=(True,) * 8,
        allow_battery=(True,) * 8,
        battery_floor_soc=(0.30,) * 8,
        stop_at_battery_floor=(False,) * 8,
    )

    result = optimizer.optimize(**kwargs, ev_plan=plan)

    assert sum(action.ev_charge_w for action in result.schedule.actions) / 1000.0 == pytest.approx(
        7.0, abs=0.05
    )
    assert max(result.ev_source_by_vehicle_w["car"]["battery"]) < 1.0
    assert min(
        action.soc
        for action in result.schedule.actions
        if action.ev_charge_w > 1.0
    ) >= 0.30 - 1e-6


def test_consume_battery_grid_fallback_remains_feasible_below_the_floor(
    optimizer_module,
):
    kwargs = _kwargs()
    kwargs["current_soc"] = 0.20
    kwargs["allow_grid_charge"] = False
    result = _optimizer(optimizer_module).optimize(
        **kwargs,
        ev_plan=_ev_plan(
            optimizer_module,
            energy_needed_kwh=7.0,
            allow_grid=(True,) * 8,
            allow_battery=(True,) * 8,
            battery_floor_soc=(0.30,) * 8,
            stop_at_battery_floor=(False,) * 8,
        ),
    )

    assert result.feasible
    assert sum(result.ev_source_by_vehicle_w["car"]["grid"]) / 1000.0 == pytest.approx(
        7.0, abs=0.05
    )
    assert max(result.ev_source_by_vehicle_w["car"]["battery"]) < 1.0


def test_limit_grid_import_uses_battery_then_grid_after_its_floor(
    optimizer_module,
):
    plan = _ev_plan(
        optimizer_module,
        energy_needed_kwh=7.0,
        allow_grid=(True,) * 8,
        allow_battery=(True,) * 8,
        battery_floor_soc=(0.30,) * 8,
        stop_at_battery_floor=(False,) * 8,
        limit_grid_import=(True,) * 8,
    )
    above_kwargs = _kwargs()
    above_kwargs["current_soc"] = 0.50
    above = _optimizer(optimizer_module).optimize(**above_kwargs, ev_plan=plan)
    below_kwargs = _kwargs()
    below_kwargs["current_soc"] = 0.20
    below = _optimizer(optimizer_module).optimize(**below_kwargs, ev_plan=plan)

    assert sum(above.ev_source_by_vehicle_w["car"]["grid"]) < 1.0
    assert sum(above.ev_source_by_vehicle_w["car"]["battery"]) / 1000.0 == pytest.approx(
        7.0, abs=0.05
    )
    assert sum(below.ev_source_by_vehicle_w["car"]["grid"]) / 1000.0 == pytest.approx(
        7.0, abs=0.05
    )


def test_strict_solar_can_start_below_minimum_only_after_battery_reserve(
    optimizer_module,
):
    kwargs = _kwargs()
    kwargs["current_soc"] = 0.20
    kwargs["solar_forecast"] = [8.0] + [0.0] * 7
    policy = dict(
        energy_needed_kwh=2.0,
        max_power_kw=(2.0,) + (0.0,) * 7,
        allow_grid=(False,) * 8,
        allow_solar=(True,) * 8,
        allow_battery=(False,) * 8,
        min_start_soc=(0.30,) * 8,
        solar_battery_reserve_kw=(5.0,) * 8,
    )
    blocked = _optimizer(optimizer_module).optimize(
        **kwargs,
        ev_plan=_ev_plan(optimizer_module, **policy),
    )
    allowed = _optimizer(optimizer_module).optimize(
        **kwargs,
        ev_plan=_ev_plan(
            optimizer_module,
            **policy,
            allow_min_start_solar_exception=(True,) * 8,
        ),
    )

    assert sum(action.ev_charge_w for action in blocked.schedule.actions) < 1.0
    assert sum(action.ev_charge_w for action in allowed.schedule.actions) / 1000.0 == pytest.approx(
        2.0, abs=0.05
    )


def test_low_level_solar_start_exception_cannot_be_supplied_by_grid(
    optimizer_module,
):
    kwargs = _kwargs()
    kwargs["current_soc"] = 0.20
    kwargs["allow_grid_charge"] = False
    result = _optimizer(optimizer_module).optimize(
        **kwargs,
        ev_plan=_ev_plan(
            optimizer_module,
            energy_needed_kwh=7.0,
            allow_grid=(True,) * 8,
            allow_solar=(True,) * 8,
            min_start_soc=(0.30,) * 8,
            allow_min_start_solar_exception=(True,) * 8,
        ),
    )

    assert sum(action.ev_charge_w for action in result.schedule.actions) < 1.0


def test_preserve_home_battery_prevents_discharge_while_ev_is_on(
    optimizer_module,
):
    optimizer = _optimizer(optimizer_module)
    kwargs = _kwargs()
    kwargs["current_soc"] = 0.90
    plan = _ev_plan(
        optimizer_module,
        energy_needed_kwh=7.0,
        allow_grid=(True,) * 8,
        allow_battery=(False,) * 8,
        preserve_home_battery=(True,) * 8,
    )

    result = optimizer.optimize(**kwargs, ev_plan=plan)

    for action in result.schedule.actions:
        if action.ev_charge_w > 1.0:
            assert action.battery_discharge_w < 1.0


def test_minimum_home_battery_soc_blocks_only_a_new_ev_session(
    optimizer_module,
):
    kwargs = _kwargs()
    kwargs["current_soc"] = 0.20
    kwargs["allow_grid_charge"] = False
    plan = _ev_plan(
        optimizer_module,
        energy_needed_kwh=7.0,
        min_start_soc=(0.30,) * 8,
    )

    blocked = _optimizer(optimizer_module).optimize(**kwargs, ev_plan=plan)
    continuing = _optimizer(optimizer_module).optimize(
        **kwargs,
        ev_plan=_ev_plan(
            optimizer_module,
            energy_needed_kwh=7.0,
            min_start_soc=(0.30,) * 8,
            initially_charging=True,
        ),
    )

    assert sum(action.ev_charge_w for action in blocked.schedule.actions) < 1.0
    continuing_kwh = (
        sum(action.ev_charge_w for action in continuing.schedule.actions)
        / 1000.0
    )
    assert continuing_kwh == pytest.approx(7.0, abs=0.05)


def test_ev_charger_minimum_power_is_not_relaxed_by_the_lp(optimizer_module):
    result = _optimizer(optimizer_module).optimize(
        **_kwargs(),
        ev_plan=_ev_plan(
            optimizer_module,
            energy_needed_kwh=0.5,
            min_power_kw=1.4,
        ),
    )

    active_power_kw = [
        action.ev_charge_w / 1000.0
        for action in result.schedule.actions
        if action.ev_charge_w > 1.0
    ]
    assert active_power_kw
    assert min(active_power_kw) >= 1.4 - 0.01


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

    assert 'if effective_source == "internal"' in source
    assert "self._build_ev_charge_plan(self._price_timestamps(n_ev))" in source
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


# --------------------------------------------------------------------------
# #371: the coordinator's hand-off of EV demand into the LP.
#
# _regenerate_plan() stamps ChargingPlan.target_time from an HA-local *naive*
# clock. _build_ev_charge_plan() parsed it back naive and compared it against
# an aware dt_util.now(), raising TypeError into its own broad guard -- so
# every Smart Schedule vehicle with a departure time silently got no LP
# co-optimization at all, and its planner-chosen windows were handed to the
# solve as fixed load instead.
#
# Source-extracted (AGENTS.md pattern) so the published method body runs.
# --------------------------------------------------------------------------

_PERTH = timezone(timedelta(hours=8))
_NOW = datetime(2026, 8, 20, 6, 25, tzinfo=_PERTH)


def _extract_build_ev_charge_plan():
    import ast

    source = (COMPONENT_ROOT / "optimization" / "coordinator.py").read_text()
    for node in ast.walk(ast.parse(source)):
        if (
            isinstance(node, ast.FunctionDef)
            and node.name == "_build_ev_charge_plan"
        ):
            return ast.get_source_segment(source, node)
    raise AssertionError("_build_ev_charge_plan not found in coordinator.py")


@pytest.fixture()
def build_ev_charge_plan():
    """The real _build_ev_charge_plan, plus the log lines it swallows."""
    names = _STUBS + (
        "power_sync.automations",
        "power_sync.automations.ev_charging_planner",
        "power_sync.const",
        "power_sync.solar_surplus_config",
        "power_sync.optimization._ev_plan_probe",
    )
    saved = {name: sys.modules.get(name, _SENTINEL) for name in names}
    for name in names:
        sys.modules.pop(name, None)

    ha = types.ModuleType("homeassistant")
    util = types.ModuleType("homeassistant.util")
    dt = types.ModuleType("homeassistant.util.dt")
    dt.now = lambda: _NOW
    dt.utcnow = lambda: _NOW.astimezone(timezone.utc)
    dt.UTC = timezone.utc
    dt.DEFAULT_TIME_ZONE = _PERTH
    dt.parse_datetime = lambda value: datetime.fromisoformat(value)
    util.dt = dt
    ha.util = util
    sys.modules.update(
        {"homeassistant": ha, "homeassistant.util": util, "homeassistant.util.dt": dt}
    )

    package = types.ModuleType("power_sync")
    package.__path__ = [str(COMPONENT_ROOT)]
    optimization = types.ModuleType("power_sync.optimization")
    optimization.__path__ = [str(COMPONENT_ROOT / "optimization")]
    automations = types.ModuleType("power_sync.automations")
    planner = types.ModuleType("power_sync.automations.ev_charging_planner")
    holder: dict = {
        "executor": None,
        "solar_config": {
            "allow_parallel_charging": False,
            "max_battery_charge_rate_kw": 5.0,
        },
    }
    planner.get_auto_schedule_executor = lambda: holder["executor"]
    planner.is_smart_schedule_grid_price_allowed = (
        lambda *, source, price_cents, max_grid_price_cents, priority: (
            not str(source).startswith("grid")
            or str(getattr(priority, "value", priority) or "").lower()
            == "time_critical"
            or max_grid_price_cents is None
            or price_cents is None
            or float(price_cents) <= float(max_grid_price_cents)
        )
    )
    const = types.ModuleType("power_sync.const")
    const.DOMAIN = "power_sync"
    solar_config = types.ModuleType("power_sync.solar_surplus_config")
    solar_config.get_stored_solar_surplus_config = (
        lambda _entry: holder["solar_config"]
    )
    sys.modules.update(
        {
            "power_sync": package,
            "power_sync.optimization": optimization,
            "power_sync.automations": automations,
            "power_sync.automations.ev_charging_planner": planner,
            "power_sync.const": const,
            "power_sync.solar_surplus_config": solar_config,
        }
    )

    warnings: list[str] = []
    probe = types.ModuleType("power_sync.optimization._ev_plan_probe")
    probe.__package__ = "power_sync.optimization"
    probe.dt_util = dt
    probe.Any = object
    probe._LOGGER = types.SimpleNamespace(
        debug=lambda *args, **kwargs: None,
        info=lambda *args, **kwargs: None,
        warning=lambda message, *args: warnings.append(str(message) % args),
        error=lambda *args, **kwargs: None,
    )
    sys.modules["power_sync.optimization._ev_plan_probe"] = probe
    body = _extract_build_ev_charge_plan().splitlines()
    wrapped = "def _outer():\n" + "\n".join(f"    {line}" for line in body)
    wrapped += "\n    return _build_ev_charge_plan\n"
    exec(compile(wrapped, "<extracted>", "exec"), probe.__dict__)
    method = probe.__dict__["_outer"]()

    try:
        yield method, holder, warnings
    finally:
        for name, value in saved.items():
            if value is _SENTINEL:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value


_ENTRY = types.SimpleNamespace(entry_id="entry-1")


def _ev_executor(target_time):
    plan = types.SimpleNamespace(
        target_time=target_time,
        energy_needed_kwh=13.75,
    )
    settings = types.SimpleNamespace(
        enabled=True,
        max_charge_amps=46,
        voltage=240,
        phases=1,
        min_charge_amps=6,
    )
    return types.SimpleNamespace(
        config_entry=_ENTRY,
        _state={"veh-1": types.SimpleNamespace(current_plan=plan)},
        _settings={"veh-1": settings},
    )


def _solve_timestamps(count=48):
    return [_NOW + timedelta(minutes=30 * index) for index in range(count)]


@pytest.mark.parametrize(
    "target_time",
    [
        None,
        "2026-08-20T15:00:00",           # HA-local naive: what is really stored
        "2026-08-20T15:00:00+08:00",     # aware control
    ],
    ids=["no-deadline", "naive-deadline", "aware-deadline"],
)
def test_a_stored_departure_time_still_reaches_the_solver(
    build_ev_charge_plan, target_time
):
    method, holder, warnings = build_ev_charge_plan
    holder["executor"] = _ev_executor(target_time)

    plans = method(
        types.SimpleNamespace(config_entry=_ENTRY), _solve_timestamps()
    )

    assert plans is not None, warnings
    # One managed vehicle -> a one-entry per-vehicle list.
    assert len(plans) == 1
    plan = plans[0]
    assert plan.energy_needed_kwh == pytest.approx(13.75)
    assert max(plan.max_power_kw) == pytest.approx(11.04, abs=0.01)
    assert warnings == []


def test_a_naive_deadline_bounds_the_window_like_an_aware_one(
    build_ev_charge_plan,
):
    method, holder, _ = build_ev_charge_plan
    coordinator = types.SimpleNamespace(config_entry=_ENTRY)
    timestamps = _solve_timestamps()

    holder["executor"] = _ev_executor("2026-08-20T15:00:00")
    naive = method(coordinator, timestamps)[0]
    holder["executor"] = _ev_executor("2026-08-20T15:00:00+08:00")
    aware = method(coordinator, timestamps)[0]

    assert naive.max_power_kw == aware.max_power_kw
    # 06:25 -> 15:00 on 30-minute slots: the car is unavailable after that.
    assert naive.max_power_kw[-1] == 0.0


def test_a_deadline_already_past_is_still_skipped(build_ev_charge_plan):
    method, holder, _ = build_ev_charge_plan
    holder["executor"] = _ev_executor("2026-08-20T05:00:00")

    assert (
        method(types.SimpleNamespace(config_entry=_ENTRY), _solve_timestamps())
        == []
    )


def test_optimizer_plan_uses_smart_schedule_windows_and_policy(
    build_ev_charge_plan,
):
    method, holder, warnings = build_ev_charge_plan
    windows = [
        types.SimpleNamespace(
            start_time="2026-08-20T08:00:00+08:00",
            end_time="2026-08-20T09:00:00+08:00",
            source="grid_offpeak",
            estimated_power_kw=7.0,
        ),
        types.SimpleNamespace(
            start_time="2026-08-20T12:00:00+08:00",
            end_time="2026-08-20T13:00:00+08:00",
            source="solar_surplus",
            estimated_power_kw=5.0,
        ),
    ]
    charging_plan = types.SimpleNamespace(
        target_time="2026-08-20T15:00:00+08:00",
        energy_needed_kwh=13.75,
        windows=windows,
    )
    settings = types.SimpleNamespace(
        enabled=True,
        max_charge_amps=46,
        voltage=240,
        phases=1,
        min_charge_amps=6,
        get_effective_consume_battery_level=lambda _weekday: 30,
        get_effective_stop_at_battery_floor=lambda _weekday: True,
        get_effective_preserve_home_battery=lambda _weekday: False,
        get_effective_min_battery_to_start=lambda _weekday: 20,
        get_effective_limit_grid_import=lambda _weekday: True,
    )
    holder["executor"] = types.SimpleNamespace(
        config_entry=_ENTRY,
        _state={
            "veh-1": types.SimpleNamespace(
                current_plan=charging_plan,
                is_charging=False,
            )
        },
        _settings={"veh-1": settings},
    )

    coordinator = types.SimpleNamespace(config_entry=_ENTRY)
    plan = method(coordinator, _solve_timestamps())[0]

    assert warnings == []
    assert plan.charge_efficiency == 1.0
    assert plan.energy_needed_kwh == pytest.approx(13.75)
    grid_slots = [
        index
        for index, source in enumerate(plan.window_source)
        if source == "grid_offpeak"
    ]
    solar_slots = [
        index
        for index, source in enumerate(plan.window_source)
        if source == "solar_surplus"
    ]
    assert grid_slots and solar_slots
    assert all(
        plan.max_power_kw[index] == pytest.approx(11.04)
        for index in grid_slots
    )
    assert all(
        plan.max_power_kw[index] == pytest.approx(5.0)
        for index in solar_slots
    )
    assert all(plan.allow_grid[index] for index in grid_slots)
    assert all(plan.allow_battery[index] for index in grid_slots)
    assert all(plan.battery_floor_soc[index] == pytest.approx(0.30) for index in grid_slots)
    assert all(plan.min_start_soc[index] == pytest.approx(0.20) for index in grid_slots)
    assert all(not plan.allow_grid[index] for index in solar_slots)
    assert all(not plan.allow_battery[index] for index in solar_slots)
    assert all(plan.min_start_soc[index] == pytest.approx(0.20) for index in solar_slots)
    assert all(
        not plan.allow_min_start_solar_exception[index]
        for index in solar_slots
    )
    assert all(plan.limit_grid_import[index] for index in grid_slots + solar_slots)
    policy_segments = coordinator._last_ev_optimizer_policy["veh-1"]["segments"]
    assert {segment["window_source"] for segment in policy_segments} >= {
        "grid_offpeak",
        "solar_surplus",
    }
    assert all(
        plan.max_power_kw[index] == 0.0
        for index, source in enumerate(plan.window_source)
        if not source
    )


def test_optimizer_rejects_a_stale_grid_window_above_current_price_cap(
    build_ev_charge_plan,
):
    method, holder, warnings = build_ev_charge_plan
    charging_plan = types.SimpleNamespace(
        target_time="2026-08-20T15:00:00+08:00",
        energy_needed_kwh=7.0,
        priority="cost_optimized",
        max_grid_price_cents=40.0,
        windows=[
            types.SimpleNamespace(
                start_time="2026-08-20T06:00:00+08:00",
                end_time="2026-08-20T10:00:00+08:00",
                source="grid_offpeak",
                estimated_power_kw=7.0,
                price_cents_kwh=30.0,
            )
        ],
    )
    settings = types.SimpleNamespace(
        enabled=True,
        max_charge_amps=32,
        voltage=240,
        phases=1,
        min_charge_amps=6,
        get_effective_max_grid_price=lambda _weekday: 25.0,
        get_effective_priority=lambda _weekday: "cost_optimized",
    )
    holder["executor"] = types.SimpleNamespace(
        config_entry=_ENTRY,
        _state={
            "veh-1": types.SimpleNamespace(
                current_plan=charging_plan,
                is_charging=False,
            )
        },
        _settings={"veh-1": settings},
    )

    coordinator = types.SimpleNamespace(config_entry=_ENTRY)
    plans = method(coordinator, _solve_timestamps())

    assert plans == []
    assert warnings == []
    assert any(
        segment["constraint_reason"] == "price_blocked"
        for segment in coordinator._last_ev_optimizer_policy["veh-1"][
            "segments"
        ]
    )


@pytest.mark.parametrize("decision", ["away", "unplugged"])
def test_unavailable_vehicle_keeps_future_demand_but_zeros_current_slot(
    build_ev_charge_plan,
    decision,
):
    method, holder, warnings = build_ev_charge_plan
    charging_plan = types.SimpleNamespace(
        target_time="2026-08-20T15:00:00+08:00",
        energy_needed_kwh=7.0,
        windows=[
            types.SimpleNamespace(
                start_time="2026-08-20T06:00:00+08:00",
                end_time="2026-08-20T10:00:00+08:00",
                source="grid_offpeak",
                estimated_power_kw=7.0,
            )
        ],
    )
    settings = types.SimpleNamespace(
        enabled=True,
        max_charge_amps=32,
        voltage=240,
        phases=1,
        min_charge_amps=6,
    )
    holder["executor"] = types.SimpleNamespace(
        config_entry=_ENTRY,
        _state={
            "veh-1": types.SimpleNamespace(
                current_plan=charging_plan,
                is_charging=False,
                last_decision=decision,
            )
        },
        _settings={"veh-1": settings},
    )

    plan = method(
        types.SimpleNamespace(config_entry=_ENTRY), _solve_timestamps()
    )[0]

    assert warnings == []
    assert plan.max_power_kw[0] == 0.0
    assert plan.max_power_kw[1] > 0.0
    assert plan.window_source[0] == "unavailable_now"


def test_reconciliation_keeps_the_planned_ev_draw(optimizer_module):
    """The published schedule is the reconciled one, so it must keep the car.

    ``reconcile_result_with_schedule`` restamps every action so the emitted
    plan matches physical dispatch, and the coordinator publishes *that*
    schedule. Rebuilding ``ScheduleAction`` there without ``ev_charge_w``
    zeroed the draw the LP had just solved. Since the load overlay is
    deliberately blanked whenever the LP co-optimizes the car, the plan was
    then left with no EV load from either source.
    """
    optimizer = _optimizer(optimizer_module)
    kwargs = _kwargs()

    result = optimizer.optimize(**kwargs, ev_plan=_ev_plan(optimizer_module))
    solved_ev_kwh = sum(ev_kw for _battery_kw, ev_kw in _flows(result))
    assert solved_ev_kwh > 1.0, "LP did not plan any EV charging to begin with"

    reconciled = optimizer.reconcile_result_with_schedule(
        result,
        result.schedule,
        import_prices=kwargs["import_prices"],
        export_prices=kwargs["export_prices"],
        solar=kwargs["solar_forecast"],
        load=kwargs["load_forecast"],
        initial_soc=kwargs["current_soc"],
    )

    reconciled_ev_kwh = sum(
        (action.ev_charge_w or 0.0) / 1000.0
        for action in reconciled.schedule.actions
    )
    assert reconciled_ev_kwh == pytest.approx(solved_ev_kwh, abs=0.05)
    reconciled_source_kwh = sum(
        sum(series)
        for series in reconciled.ev_source_by_vehicle_w["car"].values()
    ) / 1000.0
    assert reconciled_source_kwh == pytest.approx(reconciled_ev_kwh, abs=0.05)


def test_the_infeasible_hold_still_plans_for_the_car(optimizer_module):
    """The self-consumption fallback must not lose the EV the way the LP path did.

    The coordinator blanks its EV load overlay whenever it passes an
    ``ev_plan``, so a fallback that ignores the plan emits a schedule with no
    EV demand at all — the same blind spot, reached by a different route. The
    hold has no ``ev_charge`` variable, so it takes the greedy path's
    conservative assumption instead: the car's as-soon-as-possible draw.
    """
    optimizer = _optimizer(optimizer_module)
    kwargs = _kwargs()
    plan = _ev_plan(optimizer_module)

    hold = optimizer._solve_self_consumption_hold(
        len(kwargs["import_prices"]),
        kwargs["import_prices"],
        kwargs["export_prices"],
        kwargs["solar_forecast"],
        kwargs["load_forecast"],
        kwargs["current_soc"],
        "cost",
        schedule_timestamps=kwargs["schedule_timestamps"],
        ev_plan=plan,
    )

    planned_ev_kwh = sum(
        (action.ev_charge_w or 0.0) / 1000.0
        for action in hold.schedule.actions
    )
    assert planned_ev_kwh == pytest.approx(plan.energy_needed_kwh, abs=0.05)
    assert sum(hold.ev_charge_by_vehicle_w["car"]) / 1000.0 == pytest.approx(
        plan.energy_needed_kwh, abs=0.05
    )
    source_kwh = sum(
        sum(series)
        for series in hold.ev_source_by_vehicle_w["car"].values()
    ) / 1000.0
    assert source_kwh == pytest.approx(plan.energy_needed_kwh, abs=0.05)


@pytest.mark.parametrize("fallback", ["hold", "greedy"])
def test_fallback_source_reporting_never_uses_a_policy_blocked_source(
    optimizer_module,
    fallback,
):
    """Fallback diagnostics retain the policy allocation, even with surplus."""
    optimizer = _optimizer(optimizer_module)
    kwargs = _kwargs()
    kwargs["solar_forecast"] = [8.0] * 8
    plan = _ev_plan(
        optimizer_module,
        allow_grid=(True,) * 8,
        allow_solar=(False,) * 8,
        allow_battery=(False,) * 8,
    )
    method = (
        optimizer._solve_self_consumption_hold
        if fallback == "hold"
        else optimizer._solve_greedy
    )

    result = method(
        len(kwargs["import_prices"]),
        kwargs["import_prices"],
        kwargs["export_prices"],
        kwargs["solar_forecast"],
        kwargs["load_forecast"],
        kwargs["current_soc"],
        "cost",
        schedule_timestamps=kwargs["schedule_timestamps"],
        ev_plan=plan,
    )

    result = optimizer.reconcile_result_with_schedule(
        result,
        result.schedule,
        import_prices=kwargs["import_prices"],
        export_prices=kwargs["export_prices"],
        solar=kwargs["solar_forecast"],
        load=kwargs["load_forecast"],
        initial_soc=kwargs["current_soc"],
    )

    assert sum(result.ev_charge_by_vehicle_w["car"]) / 1000.0 == pytest.approx(
        plan.energy_needed_kwh,
        abs=0.05,
    )
    assert max(result.ev_source_by_vehicle_w["car"]["solar"]) < 1.0
    assert max(result.ev_source_by_vehicle_w["car"]["battery"]) < 1.0
    assert sum(result.ev_source_by_vehicle_w["car"]["grid"]) / 1000.0 == pytest.approx(
        plan.energy_needed_kwh,
        abs=0.05,
    )


def test_fallback_honours_minimum_start_soc_for_new_but_not_active_session(
    optimizer_module,
):
    optimizer = _optimizer(optimizer_module)
    kwargs = _kwargs()
    kwargs["current_soc"] = 0.20

    def _hold(initially_charging):
        return optimizer._solve_self_consumption_hold(
            len(kwargs["import_prices"]),
            kwargs["import_prices"],
            kwargs["export_prices"],
            kwargs["solar_forecast"],
            kwargs["load_forecast"],
            kwargs["current_soc"],
            "cost",
            schedule_timestamps=kwargs["schedule_timestamps"],
            ev_plan=_ev_plan(
                optimizer_module,
                min_start_soc=(0.30,) * 8,
                initially_charging=initially_charging,
            ),
        )

    blocked = _hold(False)
    continuing = _hold(True)

    assert sum(action.ev_charge_w for action in blocked.schedule.actions) < 1.0
    assert sum(action.ev_charge_w for action in continuing.schedule.actions) / 1000.0 == pytest.approx(
        14.0, abs=0.05
    )


def test_the_infeasible_hold_is_unchanged_without_a_car(optimizer_module):
    """No EV plan must leave the fallback's dispatch exactly as it was."""
    optimizer = _optimizer(optimizer_module)
    kwargs = _kwargs()

    hold = optimizer._solve_self_consumption_hold(
        len(kwargs["import_prices"]),
        kwargs["import_prices"],
        kwargs["export_prices"],
        kwargs["solar_forecast"],
        kwargs["load_forecast"],
        kwargs["current_soc"],
        "cost",
        schedule_timestamps=kwargs["schedule_timestamps"],
    )

    assert all(
        (action.ev_charge_w or 0.0) == 0.0 for action in hold.schedule.actions
    )


def _staged_plans(module, n):
    """Two cars: one leaving early, one with no deadline at all.

    Modeled on a real two-Tesla site — a car due at 06:00 and a second car
    with no departure time, on a tariff whose free window opens at 10:00.
    """
    from power_sync.optimization.ev_load_plan import combine_ev_charge_plans

    leaves_early = module.EVChargePlan(
        vehicle_id="leaves_at_slot_10",
        max_power_kw=tuple(7.36 if i <= 10 else 0.0 for i in range(n)),
        energy_needed_kwh=32.9,
        charge_efficiency=0.9,
    )
    stays_all_day = module.EVChargePlan(
        vehicle_id="no_deadline",
        max_power_kw=(7.36,) * n,
        energy_needed_kwh=7.6,
        charge_efficiency=0.9,
    )
    return combine_ev_charge_plans([leaves_early, stays_all_day], n)


def _staged_kwargs(n=24):
    """Overnight at 31c to slot 10, a free window at slots 15-18, 51c otherwise."""
    start = datetime(2026, 8, 20, 19, 0, tzinfo=timezone.utc)
    prices = [
        0.31 if i <= 10 else (0.0 if 15 <= i <= 18 else 0.51)
        for i in range(n)
    ]
    return {
        "import_prices": prices,
        "export_prices": [0.05] * n,
        "solar_forecast": [0.0] * n,
        "load_forecast": [0.5] * n,
        "current_soc": 0.50,
        "schedule_timestamps": [start + timedelta(hours=i) for i in range(n)],
        "allow_grid_charge": True,
    }


def _staged_optimizer(module, n):
    return module.BatteryOptimizer(
        capacity_wh=40_000,
        max_charge_w=14_700,
        max_discharge_w=10_000,
        max_grid_import_w=16_100,
        backup_reserve=0.05,
        interval_minutes=60,
        horizon_hours=n,
    )


def test_combining_vehicles_keeps_each_ones_deadline(optimizer_module):
    """Summing two cars into one energy figure must not erase the earlier one.

    The aggregate is one block against one import limit, but the earlier car's
    deadline is a real constraint on *when* part of that block has to land.
    Without staging, the solver satisfied the whole 40.5 kWh total by parking
    most of it in the free window — which the 06:00 car cannot reach — and the
    plan showed it charging in a window it would already have left.
    """
    n = 24
    optimizer = _staged_optimizer(optimizer_module, n)

    result = optimizer.optimize(
        **_staged_kwargs(n),
        ev_plan=_staged_plans(optimizer_module, n),
    )

    assert result.feasible
    ev_kw = [action.ev_charge_w / 1000.0 for action in result.schedule.actions]
    # Grid-side kWh, so compare against the early car's need grossed up for
    # charge efficiency: 32.9 / 0.9.
    before_deadline_kwh = sum(ev_kw[:11])
    assert before_deadline_kwh >= 32.9 / 0.9 - 0.05

    # The car with no deadline should still take the free window rather than
    # buying its energy at 31c overnight alongside the other one.
    free_window_kwh = sum(ev_kw[15:19])
    assert free_window_kwh >= 7.6 / 0.9 - 0.05


def test_a_single_vehicle_still_gets_exactly_one_stage(optimizer_module):
    """One car must produce the pre-staging model unchanged."""
    plan = _ev_plan(optimizer_module)

    assert plan.staged_requirements == ((7, 14.0),)


def test_combined_stages_are_cumulative(optimizer_module):
    """Each stage carries everything owed by then, not just that car's share."""
    combined = _staged_plans(optimizer_module, 24)

    assert combined.staged_requirements == ((10, 32.9), (23, 40.5))


def _conservation_errors(result, kwargs, n):
    """Per-slot power-balance error of the published plan, in kW.

    grid_import - grid_export must equal (house load + EV draw) - solar
    + battery_charge - battery_discharge. The LP enforces exactly this
    balance, so any residual is introduced by post-solve rewriting.
    """
    errors = []
    actions = result.schedule.actions
    grid_import = [w / 1000.0 for w in (result.grid_import_w or [0] * n)]
    grid_export = [w / 1000.0 for w in (result.grid_export_w or [0] * n)]
    for t in range(n):
        action = actions[t]
        lhs = grid_import[t] - grid_export[t]
        rhs = (
            kwargs["load_forecast"][t]
            + action.ev_charge_w / 1000.0
            - kwargs["solar_forecast"][t]
            + action.battery_charge_w / 1000.0
            - action.battery_discharge_w / 1000.0
        )
        errors.append(lhs - rhs)
    return errors


def test_published_flows_conserve_energy_with_the_car(optimizer_module):
    """The reconciled plan must balance with the EV it says it planned.

    Post-solve passes recomputed grid flows, SOC and costs from
    (solar, load) alone while the car's draw sat on the very actions they
    were rewriting — so the published plan described a house with no car:
    grid import missing the EV, and a battery serving the car clipped back
    to house-only discharge with a flat SOC.
    """
    optimizer = _optimizer(optimizer_module)
    kwargs = _kwargs()

    result = optimizer.optimize(**kwargs, ev_plan=_ev_plan(optimizer_module))
    reconciled = optimizer.reconcile_result_with_schedule(
        result,
        result.schedule,
        import_prices=kwargs["import_prices"],
        export_prices=kwargs["export_prices"],
        solar=kwargs["solar_forecast"],
        load=kwargs["load_forecast"],
        initial_soc=kwargs["current_soc"],
    )

    ev_kwh = sum(
        a.ev_charge_w / 1000.0 for a in reconciled.schedule.actions
    )
    assert ev_kwh > 1.0, "no EV draw survived reconciliation"
    errors = _conservation_errors(reconciled, kwargs, 8)
    worst = max(abs(e) for e in errors)
    assert worst < 0.05, f"power balance broken by {worst:.2f} kW: {errors}"


def test_battery_serving_the_car_keeps_its_discharge_and_soc(optimizer_module):
    """A battery discharging into the car must not be clipped to house load.

    Expensive grid all horizon and a nearly full battery: the LP serves the
    car from the pack. In self-consumption the inverter physically does
    exactly that — the car is home load. Reconciliation capped natural
    discharge at (house - solar), erasing the car's share and restamping a
    flat SOC over a battery that will really be draining hard.
    """
    optimizer = _optimizer(optimizer_module)
    kwargs = _kwargs(cheap_slots=())          # every slot expensive
    plan = _ev_plan(optimizer_module, energy_needed_kwh=10.0)

    result = optimizer.optimize(**kwargs, ev_plan=plan)
    # Re-solve context: battery starts high so discharge is the cheap source.
    kwargs["current_soc"] = 0.90
    result = optimizer.optimize(**kwargs, ev_plan=plan)
    reconciled = optimizer.reconcile_result_with_schedule(
        result,
        result.schedule,
        import_prices=kwargs["import_prices"],
        export_prices=kwargs["export_prices"],
        solar=kwargs["solar_forecast"],
        load=kwargs["load_forecast"],
        initial_soc=kwargs["current_soc"],
    )

    actions = reconciled.schedule.actions
    ev_kwh = sum(a.ev_charge_w / 1000.0 for a in actions)
    assert ev_kwh > 1.0
    supplied_kwh = sum(
        a.battery_discharge_w / 1000.0 for a in actions
    ) + sum((w or 0.0) / 1000.0 for w in (reconciled.grid_import_w or []))
    house_kwh = sum(kwargs["load_forecast"])
    # Whoever supplies it, the car's energy has to come from somewhere.
    assert supplied_kwh >= house_kwh + ev_kwh - 0.1
    # And the SOC trajectory must reflect the drain, not a house-only hold.
    socs = [a.soc for a in actions if a.soc is not None]
    discharged_kwh = sum(a.battery_discharge_w / 1000.0 for a in actions)
    if discharged_kwh > 1.0:
        expected_drop = discharged_kwh / 0.95 / 40.0
        assert socs[0] - socs[-1] >= expected_drop * 0.8


def _fallback_conservation(result, kwargs, n):
    """Conservation check for fallback paths, from the result's own flows."""
    errors = []
    actions = result.schedule.actions
    for t in range(n):
        action = actions[t]
        lhs = (result.grid_import_w[t] - result.grid_export_w[t]) / 1000.0
        rhs = (
            kwargs["load_forecast"][t]
            + action.ev_charge_w / 1000.0
            - kwargs["solar_forecast"][t]
            + action.battery_charge_w / 1000.0
            - action.battery_discharge_w / 1000.0
        )
        errors.append(lhs - rhs)
    return errors


def test_the_greedy_fallback_balances_and_does_not_double_count(
    optimizer_module,
):
    """Greedy folds the car into its placement load AND stamps ev_charge_w.

    Once downstream physics re-adds the stamped draw, greedy must hand those
    passes house-only load — otherwise the car is counted twice and the
    fallback's grid flows inflate by the whole EV draw.
    """
    optimizer = _optimizer(optimizer_module)
    kwargs = _kwargs()

    result = optimizer._solve_greedy(
        8,
        kwargs["import_prices"],
        kwargs["export_prices"],
        kwargs["solar_forecast"],
        kwargs["load_forecast"],
        kwargs["current_soc"],
        "cost",
        schedule_timestamps=kwargs["schedule_timestamps"],
        allow_grid_charge=True,
        ev_plan=_ev_plan(optimizer_module),
    )

    assert sum(a.ev_charge_w for a in result.schedule.actions) > 0
    errors = _fallback_conservation(result, kwargs, 8)
    worst = max(abs(e) for e in errors)
    assert worst < 0.05, f"greedy balance broken by {worst:.2f} kW: {errors}"


def test_the_hold_fallback_balances_with_the_car(optimizer_module):
    """Same invariant for the infeasible-solve hold."""
    optimizer = _optimizer(optimizer_module)
    kwargs = _kwargs()

    hold = optimizer._solve_self_consumption_hold(
        8,
        kwargs["import_prices"],
        kwargs["export_prices"],
        kwargs["solar_forecast"],
        kwargs["load_forecast"],
        kwargs["current_soc"],
        "cost",
        schedule_timestamps=kwargs["schedule_timestamps"],
        ev_plan=_ev_plan(optimizer_module),
    )

    assert sum(a.ev_charge_w for a in hold.schedule.actions) > 0
    errors = _fallback_conservation(hold, kwargs, 8)
    worst = max(abs(e) for e in errors)
    assert worst < 0.05, f"hold balance broken by {worst:.2f} kW: {errors}"


def test_per_vehicle_plans_cannot_stack_one_cars_slot_to_double_rate(
    optimizer_module,
):
    """The dashboard showed 14.7 kW EV charging — two chargers' capability
    stacked into slots where only one car would really draw.

    An aggregate block summed both cars' 7.36 kW envelopes, so the solver
    could place 14.72 kW overnight: TESSY charging toward its deadline plus
    W3's energy pushed through a second charger the runtime would never
    start (its own plan holds it for the free window). Per-vehicle variables
    cap each car at its own charger and pin each car's energy to its own
    series.
    """
    n = 24
    optimizer = _staged_optimizer(optimizer_module, n)
    kwargs = _staged_kwargs(n)

    tessy = optimizer_module.EVChargePlan(
        vehicle_id="TESSY",
        max_power_kw=tuple(7.36 if i <= 10 else 0.0 for i in range(n)),
        energy_needed_kwh=32.9,
        charge_efficiency=0.9,
    )
    w3 = optimizer_module.EVChargePlan(
        vehicle_id="W3RT1E",
        max_power_kw=(7.36,) * n,
        energy_needed_kwh=7.6,
        charge_efficiency=0.9,
    )

    result = optimizer.optimize(**kwargs, ev_plan=[tessy, w3])
    assert result.feasible

    by_vehicle = result.ev_charge_by_vehicle_w
    assert set(by_vehicle) == {"TESSY", "W3RT1E"}

    # Each car is capped at its own charger, every slot.
    for vehicle_id, series in by_vehicle.items():
        assert max(series) <= 7.36 * 1000 + 1, f"{vehicle_id} over its charger"

    # TESSY's own charger delivers TESSY's own energy before its deadline.
    tessy_kwh = sum(by_vehicle["TESSY"][:11]) / 1000.0 * 0.9
    assert tessy_kwh == pytest.approx(32.9, abs=0.1)
    assert max(by_vehicle["TESSY"][11:], default=0.0) == 0.0

    # W3 takes the free window instead of riding TESSY's overnight slots at
    # 31c — so overnight never exceeds one charger. This is the 14 kW fix.
    total = [
        (by_vehicle["TESSY"][i] + by_vehicle["W3RT1E"][i]) / 1000.0
        for i in range(n)
    ]
    assert max(total[:11]) <= 7.36 + 0.01, f"overnight stacked: {total[:11]}"
    w3_free_kwh = sum(by_vehicle["W3RT1E"][15:19]) / 1000.0 * 0.9
    assert w3_free_kwh == pytest.approx(7.6, abs=0.1)

    # The stamped total still matches the per-vehicle sum.
    stamped = [a.ev_charge_w for a in result.schedule.actions]
    for i in range(n):
        assert stamped[i] == pytest.approx(
            by_vehicle["TESSY"][i] + by_vehicle["W3RT1E"][i], abs=1.0
        )


def test_schedule_api_serializes_ev_load_before_flow_derivations(
    optimizer_module,
):
    schedule = optimizer_module.OptimizationSchedule(
        actions=[
            optimizer_module.ScheduleAction(
                timestamp=datetime(2026, 8, 21, 1, 30, tzinfo=timezone.utc),
                action="self_consumption",
                power_w=375.0,
                battery_discharge_w=375.0,
                ev_charge_w=7360.0,
            )
        ],
        predicted_cost=0.0,
        predicted_savings=0.0,
    )

    assert schedule.to_api_response()["ev_charging_w"] == [7360.0]
