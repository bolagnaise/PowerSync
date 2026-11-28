"""An away car counts as home charging demand only from an expected return.

Scenario that prompted this (2026-11-28): a car ~165 km away with 59% and
~332 km of range, not coming home tonight, still had its full Smart Schedule
plan in the battery plan. Range alone does not catch that case, so "away with
no expected return" must itself remove the demand.
"""
from __future__ import annotations

import asyncio
import importlib
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
COMPONENT_ROOT = ROOT / "custom_components" / "power_sync"
_SENTINEL = object()
_NAMES = (
    "power_sync",
    "power_sync.optimization",
    "power_sync.optimization.ev_return",
    "power_sync.optimization.ev_return_inputs",
    "power_sync.automations",
    "power_sync.automations.ev_charging_planner",
)

HOME = (-27.242063, 152.996002)
AWAY = (-28.626951, 153.58634)  # ~165 km straight line
NOW = datetime(2026, 11, 28, 18, 0, tzinfo=timezone(timedelta(hours=10)))


@pytest.fixture()
def mods():
    saved = {name: sys.modules.get(name, _SENTINEL) for name in _NAMES}
    for name in _NAMES:
        sys.modules.pop(name, None)
    package = types.ModuleType("power_sync")
    package.__path__ = [str(COMPONENT_ROOT)]
    optimization = types.ModuleType("power_sync.optimization")
    optimization.__path__ = [str(COMPONENT_ROOT / "optimization")]
    sys.modules["power_sync"] = package
    sys.modules["power_sync.optimization"] = optimization
    ev_return = importlib.import_module("power_sync.optimization.ev_return")
    inputs = importlib.import_module("power_sync.optimization.ev_return_inputs")
    try:
        yield ev_return, inputs
    finally:
        for name, value in saved.items():
            if value is _SENTINEL:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value


def _telemetry(m, **kwargs):
    base = dict(latitude=AWAY[0], longitude=AWAY[1], range_km=332.0, soc=59.0)
    base.update(kwargs)
    return m.EVReturnTelemetry(**base)


def _resolve(m, telemetry, **kwargs):
    return m.resolve_ev_return(
        now=NOW, away=kwargs.pop("away", True), telemetry=telemetry,
        home_lat=HOME[0], home_lon=HOME[1], **kwargs)


def test_reported_case_away_with_enough_range_and_no_return_adds_no_demand(mods):
    m, _ = mods
    decision = _resolve(m, _telemetry(m))
    assert not m.range_shortfall(decision.distance_km, 332.0)  # range would not fire
    assert decision.away and not decision.include
    assert decision.source == "no_return_evidence"
    assert decision.distance_km == pytest.approx(165, abs=2)


def test_present_car_keeps_existing_behaviour(mods):
    m, _ = mods
    assert _resolve(m, _telemetry(m), away=False) is m.PRESENT


def test_range_shortfall_drops_demand_even_with_learned_routine(mods):
    m, _ = mods
    decision = _resolve(m, _telemetry(m, range_km=150.0), learned_available=True)
    assert not decision.include and decision.source == "range_shortfall"


def test_learned_routine_still_decides_when_range_is_fine(mods):
    m, _ = mods
    decision = _resolve(m, _telemetry(m), learned_available=True)
    assert decision.include and decision.use_learned and decision.arrival is None


def test_navigation_home_sets_arrival_and_estimates_soc(mods):
    m, _ = mods
    eta = NOW + timedelta(hours=2, minutes=30)
    decision = _resolve(m, _telemetry(m, navigating_home=True, nav_eta=eta, nav_distance_km=200.0))
    assert decision.include and decision.source == "navigation"
    assert decision.arrival.arrival == eta
    # 200 km at 332 km per 59% uses ~35.5%.
    assert decision.arrival.soc == pytest.approx(59 - 200 * 59 / 332)


def test_owner_not_home_beats_navigation(mods):
    m, _ = mods
    override = m.EVReturnOverride(m.MODE_NOT_HOME, NOW + timedelta(hours=16), NOW)
    decision = _resolve(m, _telemetry(m, navigating_home=True, nav_eta=NOW + timedelta(hours=1)),
                        override=override)
    assert not decision.include and decision.source == "owner_not_home"


def test_owner_home_at_plans_from_that_time_then_lapses(mods):
    m, _ = mods
    at = NOW + timedelta(hours=3)
    override = m.EVReturnOverride(m.MODE_HOME_AT, at, NOW)
    decision = _resolve(m, _telemetry(m), override=override)
    assert decision.include and decision.arrival.arrival == at
    assert decision.arrival.soc < 59
    late = m.resolve_ev_return(
        now=at + timedelta(minutes=61), away=True, telemetry=_telemetry(m),
        home_lat=HOME[0], home_lon=HOME[1], override=override)
    assert not late.include and late.source == "owner_home_at_overdue"


def test_owner_home_at_outranks_range_shortfall(mods):
    m, _ = mods
    override = m.EVReturnOverride(m.MODE_HOME_AT, NOW + timedelta(hours=3), NOW)
    decision = _resolve(m, _telemetry(m, range_km=100.0), override=override)
    assert decision.include and decision.source == "owner_home_at"


def test_plan_selection(mods):
    m, _ = mods
    live, learned, ret = object(), object(), object()
    away_none = _resolve(m, _telemetry(m))
    assert m.select_home_demand_plan(away_none, current_plan=live, historical_plan=learned,
                                     return_plan=None) == (None, False)
    nav = _resolve(m, _telemetry(m, navigating_home=True, nav_eta=NOW + timedelta(hours=1)))
    assert m.select_home_demand_plan(nav, current_plan=live, historical_plan=learned,
                                     return_plan=ret) == (ret, True)
    routine = _resolve(m, _telemetry(m), learned_available=True)
    assert m.select_home_demand_plan(routine, current_plan=live, historical_plan=learned,
                                     return_plan=None) == (learned, True)
    assert m.select_home_demand_plan(None, current_plan=live, historical_plan=None,
                                     return_plan=None) == (live, False)


def test_override_round_trip_and_validation(mods):
    m, inputs = mods
    override = inputs.build_override(m.MODE_NOT_HOME, NOW + timedelta(hours=16), NOW)
    assert m.EVReturnOverride.from_dict(override.to_dict()) == override
    assert inputs.build_override(m.MODE_AUTO, None, NOW) is None
    with pytest.raises(ValueError):
        inputs.build_override(m.MODE_HOME_AT, None, NOW)
    with pytest.raises(ValueError):
        inputs.build_override(m.MODE_NOT_HOME, NOW - timedelta(minutes=1), NOW)
    assert m.EVReturnOverride.from_dict({"mode": "bogus"}) is None

    store = SimpleNamespace(_data={}, saved=0)

    async def save():
        store.saved += 1
    store.async_save = save
    asyncio.run(inputs.async_save_overrides(store, {"veh-1": override}))
    assert inputs.load_overrides(store) == {"veh-1": override}


def _state(value, **attrs):
    return SimpleNamespace(state=value, attributes=attrs)


def test_telemetry_reads_tesla_entities(mods):
    _, inputs = mods
    eta = (NOW + timedelta(hours=2)).isoformat()
    states = {
        "device_tracker.w3rt1e_location_2": _state("not_home", latitude=AWAY[0], longitude=AWAY[1]),
        "device_tracker.w3rt1e_route": _state("home"),
        "sensor.w3rt1e_battery_range": _state("206.3", unit_of_measurement="mi"),
        "sensor.w3rt1e_estimate_battery_range": _state("1", unit_of_measurement="km"),
        "sensor.w3rt1e_ideal_battery_range_2": _state("2", unit_of_measurement="km"),
        "sensor.w3rt1e_time_to_arrival": _state(eta),
        "sensor.w3rt1e_distance_to_arrival": _state("190", unit_of_measurement="km"),
        "sensor.w3rt1e_battery_level": _state("59"),
    }
    telemetry = inputs.telemetry_from_states(states, home_lat=HOME[0], home_lon=HOME[1], soc=None)
    assert telemetry.latitude == AWAY[0]
    assert telemetry.range_km == pytest.approx(206.3 * 1.609344)
    assert telemetry.navigating_home and telemetry.nav_eta == datetime.fromisoformat(eta)
    assert telemetry.nav_distance_km == 190 and telemetry.soc == 59


def test_telemetry_ignores_routes_elsewhere_and_unknown_eta(mods):
    _, inputs = mods
    states = {
        "device_tracker.w3rt1e_route": _state("not_home", latitude=-28.0, longitude=153.4),
        "sensor.w3rt1e_time_to_arrival": _state((NOW + timedelta(hours=1)).isoformat()),
    }
    telemetry = inputs.telemetry_from_states(states, home_lat=HOME[0], home_lon=HOME[1], soc=40)
    assert not telemetry.navigating_home and telemetry.nav_eta is None
    unknown = {"device_tracker.w3rt1e_route": _state("home"),
               "sensor.w3rt1e_time_to_arrival": _state("unavailable")}
    assert not inputs.telemetry_from_states(unknown, home_lat=HOME[0], home_lon=HOME[1],
                                            soc=40).navigating_home


_METHODS = ("_refresh_ev_return_decisions", "_historical_ev_forecast_for",
            "_ev_return_decision_for", "_ev_return_suppressed_vins",
            "_learned_ev_forecast_for")


def _coordinator_class(mods, executor, locations, telemetry):
    import ast

    ev_return, inputs = mods
    automations = types.ModuleType("power_sync.automations")
    automations.__path__ = []
    planner = types.ModuleType("power_sync.automations.ev_charging_planner")

    class AutoScheduleState:
        def __init__(self, vehicle_id):
            self.vehicle_id = vehicle_id
            self.current_plan = None

    async def get_ev_location(_hass, _entry, vin):
        return locations.get(vin, "unknown")

    planner.AutoScheduleState = AutoScheduleState
    planner.get_auto_schedule_executor = lambda: executor
    planner.get_ev_location = get_ev_location
    sys.modules["power_sync.automations"] = automations
    sys.modules["power_sync.automations.ev_charging_planner"] = planner
    inputs.read_return_telemetry = lambda _hass, vin, soc: telemetry.get(vin, ev_return.EVReturnTelemetry(soc=soc))

    source = (COMPONENT_ROOT / "optimization" / "coordinator.py").read_text()
    segments = [ast.get_source_segment(source, node) for node in ast.walk(ast.parse(source))
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in _METHODS]
    assert len(segments) == len(_METHODS)
    body = "\n\n".join("\n".join("    " + line for line in seg.splitlines()) for seg in segments)
    namespace = {"__name__": "power_sync.optimization._return_probe",
                 "__package__": "power_sync.optimization",
                 "dt_util": SimpleNamespace(now=lambda: NOW),
                 "_LOGGER": SimpleNamespace(debug=lambda *a, **k: None)}
    exec(compile("class Probe:\n" + body + "\n", "<probe>", "exec"), namespace)
    return namespace["Probe"]


def test_coordinator_refresh_builds_decisions_and_clears_answer_on_arrival(mods):
    ev_return, inputs = mods
    entry = SimpleNamespace(entry_id="e1")
    calls = []

    async def regenerate(vehicle_id, cfg, state, *, current_soc, charger_capability, available_from):
        calls.append((vehicle_id, current_soc, available_from))
        state.current_plan = SimpleNamespace(windows=[1])

    async def soc(_vehicle_id):
        return 59.0

    executor = SimpleNamespace(
        config_entry=entry, _settings={"1": SimpleNamespace(enabled=True), "2": SimpleNamespace(enabled=True)},
        _resolve_vehicle_vin=lambda key: {"1": "VIN00000000000001", "2": "VIN00000000000002"}[key],
        _get_vehicle_soc=soc, _regenerate_plan=regenerate)
    eta = NOW + timedelta(hours=2)
    telemetry = {
        "VIN00000000000001": _telemetry(ev_return),
        "VIN00000000000002": _telemetry(ev_return, navigating_home=True, nav_eta=eta, nav_distance_km=100.0),
    }
    locations = {"VIN00000000000001": "not_home", "VIN00000000000002": "not_home",
                 "VIN00000000000003": "home"}
    Probe = _coordinator_class(mods, executor, locations, telemetry)

    store = SimpleNamespace(_data={})

    async def save():
        pass
    store.async_save = save
    stale = ev_return.EVReturnOverride(ev_return.MODE_NOT_HOME, NOW + timedelta(hours=5), NOW)
    asyncio.run(inputs.async_save_overrides(store, {"VIN00000000000003": stale}))

    coordinator = Probe()
    coordinator._entry = entry
    coordinator._ev_integration_enabled = True
    coordinator._use_historical_ev_data = False
    coordinator._tesla_schedule_reader = SimpleNamespace(cache={"VIN00000000000003": None})
    coordinator._canonical_ev_loadpoint_ids = lambda ids: {i: i for i in ids}
    coordinator.hass = SimpleNamespace(
        data={"power_sync": {"e1": {"automation_store": store}}},
        config=SimpleNamespace(latitude=HOME[0], longitude=HOME[1]))

    asyncio.run(coordinator._refresh_ev_return_decisions())

    assert not coordinator._ev_return_decisions["1"].include
    assert coordinator._ev_return_decisions["VIN00000000000001"].source == "no_return_evidence"
    assert coordinator._ev_return_decisions["2"].source == "navigation"
    assert calls == [("2", pytest.approx(59 - 100 * 59 / 332), eta)]
    assert "2" in coordinator._ev_return_plans and "1" not in coordinator._ev_return_plans
    # The car that arrived home has its answer cleared and persisted.
    assert inputs.load_overrides(store) == {}
    assert coordinator._historical_ev_forecast_for("VIN00000000000001") is None
    assert coordinator._historical_ev_forecast_for("2").arrival == eta
    assert coordinator._ev_return_suppressed_vins(
        ["VIN00000000000001", "VIN00000000000002", "VIN00000000000003"]) == frozenset({"VIN00000000000001"})
    assert coordinator._ev_return_status["1"]["counts_as_home_demand"] is False
