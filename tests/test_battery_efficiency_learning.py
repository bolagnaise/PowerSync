"""Tests for provider-neutral closed-cycle battery efficiency learning."""

from __future__ import annotations

import importlib.util
import json
import logging
import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest


MODULE_PATH = (
    Path(__file__).resolve().parent.parent
    / "custom_components"
    / "power_sync"
    / "optimization"
    / "battery_efficiency.py"
)
SPEC = importlib.util.spec_from_file_location(
    "battery_efficiency_learning_test",
    MODULE_PATH,
)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)

BatteryEfficiencyLearner = MODULE.BatteryEfficiencyLearner
DEFAULT_RTE = MODULE.DEFAULT_ROUND_TRIP_EFFICIENCY


def _record_cycle(
    learner: BatteryEfficiencyLearner,
    start: datetime,
    *,
    discharge_kw: float = 1.6,
) -> datetime:
    """Record a 20%-SOC closed loop with 2 kWh in and known AC output."""
    previous_cycles = len(learner.history)
    common = {
        "capacity_kwh": 10.0,
        "topology_fingerprint": "test|integrated_ac_power|ac_system|10000",
    }
    learner.observe(
        timestamp=start,
        soc=0.50,
        battery_power_kw=0.0,
        **common,
    )
    for index in range(1, 13):
        learner.observe(
            timestamp=start + timedelta(minutes=5 * index),
            soc=0.50 + 0.20 * index / 12,
            battery_power_kw=-2.0,
            **common,
        )
    learner.observe(
        timestamp=start + timedelta(minutes=65),
        soc=0.70,
        battery_power_kw=0.0,
        **common,
    )
    for index in range(1, 13):
        learner.observe(
            timestamp=start + timedelta(minutes=65 + 5 * index),
            soc=0.70 - 0.20 * index / 12,
            battery_power_kw=discharge_kw,
            **common,
        )
    end = start + timedelta(minutes=130)
    result = learner.observe(
        timestamp=end,
        soc=0.50,
        battery_power_kw=0.0,
        **common,
    )
    # A continuously moving battery now closes at the actual SOC return,
    # before the subsequent quiet sample. Exactly one new loop is accepted.
    assert len(learner.history) == previous_cycles + 1
    return end


def test_cold_start_preserves_legacy_physical_and_economic_values():
    learner = BatteryEfficiencyLearner()

    resolved = learner.resolved_parameters(
        now=datetime(2026, 7, 1, tzinfo=timezone.utc)
    )

    assert resolved.battery_efficiency_source == "cold_start"
    assert resolved.charge_efficiency == pytest.approx(0.92)
    assert resolved.physical_round_trip_efficiency == pytest.approx(DEFAULT_RTE)
    assert resolved.economic_round_trip_efficiency == pytest.approx(DEFAULT_RTE)


@pytest.mark.parametrize("peak_soc", [0.98, 1.0])
def test_daily_cycle_returns_to_reserve_after_full_charge(peak_soc):
    learner = BatteryEfficiencyLearner()
    start = datetime(2026, 7, 1, 7, tzinfo=timezone.utc)
    reserve_soc = 0.20
    span = peak_soc - reserve_soc
    common = {"capacity_kwh": 46.8, "topology_fingerprint": "ac-battery"}

    for day in range(6):
        morning = start + timedelta(days=day)
        observations = [(0, reserve_soc, 0.0)]
        observations.extend(
            (5 * i, reserve_soc + span * i / 60, -8.0)
            for i in range(1, 61)
        )
        observations.extend((5 * i, peak_soc, 0.0) for i in range(61, 121))
        observations.extend(
            (600 + 5 * i, peak_soc - span * i / 60, 7.36)
            for i in range(1, 61)
        )
        observations.append((905, reserve_soc, 0.0))
        accepted_cycles = 0
        for minutes, soc, power in observations:
            observed_at = morning + timedelta(minutes=minutes)
            result = learner.observe(
                timestamp=observed_at, soc=soc, battery_power_kw=power, **common
            )
            accepted_cycles += result.accepted_cycle
        assert accepted_cycles == 1

    assert learner.evidence(observed_at)[:2] == (6, 6)
    # The return closes on the SOC crossing, before the final ramp to idle.
    assert learner.candidate_round_trip_efficiency(observed_at) == pytest.approx(
        0.92 * 59.5 / 60
    )
    resolved = learner.resolved_parameters(now=observed_at)
    assert resolved.battery_efficiency_source == "learned"
    assert resolved.physical_round_trip_efficiency > DEFAULT_RTE
    assert resolved.economic_round_trip_efficiency == pytest.approx(DEFAULT_RTE)


def test_learning_requires_cycles_days_and_equivalent_cycle_evidence():
    learner = BatteryEfficiencyLearner()
    last = None
    for index in range(10):
        start = datetime(
            2026,
            7,
            1 + index // 3,
            tzinfo=timezone.utc,
        ) + timedelta(hours=3 * (index % 3))
        last = _record_cycle(learner, start)

    assert last is not None
    cycles, days, equivalent_cycles = learner.evidence(last)
    resolved = learner.resolved_parameters(now=last)

    assert cycles == 10
    assert days == 4
    assert equivalent_cycles >= 1.5
    assert resolved.battery_efficiency_confidence > 0
    assert resolved.battery_efficiency_source == "learned"
    assert resolved.physical_round_trip_efficiency < DEFAULT_RTE
    assert resolved.economic_round_trip_efficiency == pytest.approx(
        resolved.physical_round_trip_efficiency
    )


def test_higher_learned_physical_efficiency_never_relaxes_economic_guard():
    learner = BatteryEfficiencyLearner()
    last = None
    for index in range(9):
        start = datetime(
            2026,
            7,
            1 + index // 3,
            tzinfo=timezone.utc,
        ) + timedelta(hours=3 * (index % 3))
        last = _record_cycle(learner, start, discharge_kw=1.9)

    assert last is not None
    resolved = learner.resolved_parameters(now=last)

    assert resolved.physical_round_trip_efficiency > DEFAULT_RTE
    assert resolved.economic_round_trip_efficiency == pytest.approx(DEFAULT_RTE)


def test_gap_direction_mismatch_and_topology_change_reject_open_cycle():
    learner = BatteryEfficiencyLearner()
    start = datetime(2026, 7, 1, tzinfo=timezone.utc)
    common = {
        "capacity_kwh": 10.0,
        "topology_fingerprint": "topology-a",
    }
    learner.observe(
        timestamp=start,
        soc=0.50,
        battery_power_kw=0.0,
        **common,
    )
    mismatch = learner.observe(
        timestamp=start + timedelta(minutes=5),
        soc=0.52,
        battery_power_kw=2.0,
        **common,
    )
    assert mismatch.rejection_reason == "power_soc_direction_mismatch"
    assert learner.state == "idle"

    learner.observe(
        timestamp=start + timedelta(minutes=10),
        soc=0.50,
        battery_power_kw=0.0,
        **common,
    )
    gap = learner.observe(
        timestamp=start + timedelta(minutes=30),
        soc=0.50,
        battery_power_kw=0.0,
        **common,
    )
    assert gap.rejection_reason == "telemetry_gap"

    changed = learner.observe(
        timestamp=start + timedelta(minutes=35),
        soc=0.50,
        battery_power_kw=0.0,
        capacity_kwh=10.0,
        topology_fingerprint="topology-b",
    )
    assert changed.rejection_reason == "topology_changed"
    assert learner.history == []


def test_store_round_trip_preserves_open_cycle_and_toggle_disables_application():
    learner = BatteryEfficiencyLearner()
    start = datetime(2026, 7, 1, tzinfo=timezone.utc)
    learner.observe(
        timestamp=start,
        soc=0.50,
        battery_power_kw=0.0,
        capacity_kwh=10.0,
        topology_fingerprint="topology-a",
    )

    restored = BatteryEfficiencyLearner.from_dict(learner.to_dict())
    resolved = restored.resolved_parameters(
        application_enabled=False,
        now=start,
    )

    assert restored.state == "anchored"
    assert resolved.battery_efficiency_source == "disabled"
    assert resolved.charge_efficiency == pytest.approx(0.92)
    assert restored.to_dict()["version"] == 1
    assert BatteryEfficiencyLearner.from_dict({"version": 999}).history == []


@pytest.mark.parametrize("checkpoint", [None, {"last_soc": 4}, {"charge_kwh": -1},
    {"topology_fingerprint": "other-site"}, {"last_timestamp": "invalid"}])
def test_legacy_or_invalid_checkpoint_keeps_completed_history(checkpoint):
    learner = BatteryEfficiencyLearner()
    start = datetime(2026, 7, 1, tzinfo=timezone.utc)
    _record_cycle(learner, start)
    stored = learner.to_dict()
    if checkpoint is None:
        stored.pop("active_cycle")
    else:
        stored["active_cycle"].update(checkpoint)

    restored = BatteryEfficiencyLearner.from_dict(stored)

    assert restored.state == "idle"
    assert restored.to_dict()["history"] == stored["history"]


def _continuous_profile(start, *, lower=False, endpoint=1.0):
    """54 kWh loop with no quiet anchor/return and known 80% AC energy ratio."""
    first_power, second_power = (9.6, -12.0) if lower else (-12.0, 9.6)
    peak = 0.0 if lower else endpoint
    rows = [(start, 0.5, first_power)]
    # Last outward sample ramps to zero for a single instant at the endpoint.
    weights = [1.0] * 29 + [0.5]
    total = sum(weights)
    elapsed = 0
    running = 0.0
    for i, weight in enumerate(weights):
        elapsed += 5
        running += weight
        soc = 0.5 + (peak - 0.5) * running / total
        rows.append((start + timedelta(minutes=elapsed), soc, 0.0 if i == 29 else first_power))
    weights = [0.5] + [1.0] * 29
    running = 0.0
    for i, weight in enumerate(weights):
        elapsed += 5
        running += weight
        soc = peak + (0.5 - peak) * running / total
        if i == 29:
            soc = 0.5
        rows.append((start + timedelta(minutes=elapsed), soc, second_power))
    return rows


def _feed_profile(learner, rows):
    accepted = []
    for at, soc, power in rows:
        result = learner.observe(timestamp=at, soc=soc, battery_power_kw=power,
            capacity_kwh=54.0, topology_fingerprint="synthetic-54kwh-ac")
        if result.accepted_cycle:
            accepted.append(learner.history[-1])
    return accepted


@pytest.mark.parametrize("lower", [False, True])
def test_continuous_full_depth_loop_learns_without_quiet_anchor_or_return(lower):
    start = datetime(2026, 7, 1, tzinfo=timezone.utc)
    learner = BatteryEfficiencyLearner()
    accepted = _feed_profile(learner, _continuous_profile(start, lower=lower))
    assert len(accepted) == 1
    assert accepted[0].round_trip_efficiency == pytest.approx(0.8)
    assert accepted[0].charge_kwh == pytest.approx(29.5)
    assert accepted[0].discharge_kwh == pytest.approx(23.6)
    assert "soc_boundary" not in learner.rejections


@pytest.mark.parametrize("lower", [False, True])
def test_short_restarts_preserve_full_depth_cycle(lower):
    rows = _continuous_profile(datetime(2026, 7, 1, tzinfo=timezone.utc), lower=lower)
    uninterrupted = _feed_profile(BatteryEfficiencyLearner(), rows)
    learner = BatteryEfficiencyLearner()
    assert _feed_profile(learner, rows[:15]) == []
    learner = BatteryEfficiencyLearner.from_dict(json.loads(json.dumps(learner.to_dict())))
    assert _feed_profile(learner, rows[15:31]) == []
    learner = BatteryEfficiencyLearner.from_dict(json.loads(json.dumps(learner.to_dict())))
    # Startup can report the last acquisition again before publishing a new one.
    assert _feed_profile(learner, rows[30:31]) == []
    assert learner.history == []
    assert _feed_profile(learner, rows[31:]) == uninterrupted


@pytest.mark.parametrize("invalid", ["long_gap", "stale", "changed_topology"])
def test_restart_keeps_telemetry_and_topology_guards(invalid):
    rows = _continuous_profile(datetime(2026, 7, 1, tzinfo=timezone.utc))
    learner = BatteryEfficiencyLearner()
    _feed_profile(learner, rows[:31])
    restored = BatteryEfficiencyLearner.from_dict(learner.to_dict())
    assert restored.state == "excursion"
    at, soc, power = rows[31]
    result = restored.observe(
        timestamp=at + (timedelta(hours=1) if invalid == "long_gap" else timedelta()),
        soc=soc,
        battery_power_kw=power,
        capacity_kwh=54.0,
        topology_fingerprint=(
            "changed-battery" if invalid == "changed_topology" else "synthetic-54kwh-ac"
        ),
        valid=invalid != "stale",
        skip_reason="stale_telemetry" if invalid == "stale" else None,
    )
    assert result.rejection_reason == {
        "long_gap": "telemetry_gap",
        "stale": "stale_telemetry",
        "changed_topology": "topology_changed",
    }[invalid]
    assert restored.history == []


def test_repeated_normal_full_depth_cycles_advance_confidence_and_survive_store():
    learner = BatteryEfficiencyLearner()
    for day in range(10):
        start = datetime(2026, 7, 1 + day, tzinfo=timezone.utc)
        # Like a process restart, an overnight telemetry gap must not bridge
        # energy between separate daily loops. Accepted history survives.
        learner = BatteryEfficiencyLearner.from_dict(learner.to_dict())
        _feed_profile(learner, _continuous_profile(start, lower=bool(day % 2)))
    now = start + timedelta(hours=6)
    resolved = learner.resolved_parameters(now=now)
    assert len(learner.history) == 10
    assert resolved.battery_efficiency_source == "learned"
    assert resolved.battery_efficiency_candidate_rte == pytest.approx(.8)
    assert resolved.battery_efficiency_confidence > 0
    assert resolved.physical_round_trip_efficiency < DEFAULT_RTE
    assert resolved.economic_round_trip_efficiency <= DEFAULT_RTE


def test_return_crossing_interpolates_energy_instead_of_counting_overshoot():
    start = datetime(2026, 7, 1, tzinfo=timezone.utc)
    rows = _continuous_profile(start)
    # Cross the anchor halfway through the final interval. The tail belongs
    # to neither the completed cycle nor a fabricated second observation.
    at, _, power = rows[-1]
    previous_soc = rows[-2][1]
    rows[-1] = at, .5 - (previous_soc - .5), power
    learner = BatteryEfficiencyLearner()
    accepted = _feed_profile(learner, rows)
    assert len(accepted) == 1
    assert accepted[0].discharge_kwh == pytest.approx(23.2)
    assert accepted[0].timestamp == (at - timedelta(minutes=2.5)).isoformat()


def test_near_return_without_crossing_is_not_a_completed_cycle():
    rows = _continuous_profile(datetime(2026, 7, 1, tzinfo=timezone.utc))
    at, _, power = rows[-1]
    rows[-1] = at, .5001, power
    learner = BatteryEfficiencyLearner()
    assert _feed_profile(learner, rows) == []
    assert learner.history == []


def test_telemetry_gap_during_endpoint_visit_cannot_be_bridged():
    rows = _continuous_profile(datetime(2026, 7, 1, tzinfo=timezone.utc))
    rows = rows[:30] + [(at + timedelta(hours=1), soc, p) for at, soc, p in rows[30:]]
    learner = BatteryEfficiencyLearner()
    assert _feed_profile(learner, rows) == []
    assert learner.rejections["telemetry_gap"] >= 1


def _coordinator_methods(*names):
    import ast
    from typing import Any

    path = MODULE_PATH.parent / "coordinator.py"
    tree = ast.parse(path.read_text())
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "OptimizationCoordinator")
    methods = [
        n for n in cls.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in names
    ]
    namespace = {
        "datetime": datetime,
        "dt_util": SimpleNamespace(now=lambda: datetime.now(timezone.utc)),
        "BatteryEfficiencyLearner": BatteryEfficiencyLearner,
        "Any": Any,
        "math": math,
        "_LOGGER": logging.getLogger(__name__),
        "CUSTOM_BATTERY_SYSTEM": "custom",
        "CUSTOM_BATTERY_LEVEL_ENTITY": "level",
        "CUSTOM_BATTERY_POWER_ENTITY": "power",
        "CUSTOM_GRID_POWER_ENTITY": "grid",
        "CUSTOM_SOLAR_POWER_ENTITY": "solar",
        "CUSTOM_LOAD_POWER_ENTITY": "load",
    }
    exec(compile(ast.Module(body=methods, type_ignores=[]), str(path), "exec"), namespace)
    return tuple(namespace[name] for name in names)


def test_coordinator_uses_acquisition_clock_and_paired_soc():
    now = datetime(2026, 7, 1, tzinfo=timezone.utc)
    captured = []
    learner = SimpleNamespace(observe=lambda **kw: (captured.append(kw) or MODULE.ObservationResult(False)))
    coordinator = SimpleNamespace(_get_energy_data=lambda: {"battery_level": 1.0, "battery_power": -12.0,
        "last_update": now - timedelta(seconds=30)}, _battery_efficiency_learner=learner,
        _battery_efficiency_topology_fingerprint=lambda _: "test-ac")
    (observe,) = _coordinator_methods("_observe_battery_efficiency")
    observe(coordinator, timestamp=now, capacity_wh=54000)
    assert captured[-1]["soc"] == pytest.approx(.01)
    assert captured[-1]["timestamp"] == now - timedelta(seconds=30)
    assert captured[-1]["valid"] is True
    coordinator._get_energy_data = lambda: {"battery_level": 50, "battery_power": -12,
        "last_update": now - timedelta(minutes=5)}
    observe(coordinator, timestamp=now, capacity_wh=54000)
    assert captured[-1]["valid"] is False

    coordinator._get_energy_data = lambda: {
        "battery_level": 110, "battery_power": -12, "last_update": now,
    }
    observe(coordinator, timestamp=now, capacity_wh=54000)
    assert captured[-1]["soc"] is None


def test_custom_entity_reports_learn_a_daily_full_cycle_and_reject_unpaired_data():
    from functools import partial

    read_numeric, read_custom, observe = _coordinator_methods(
        "_read_numeric_state", "_read_custom_energy_data", "_observe_battery_efficiency"
    )
    states = {}
    learner = BatteryEfficiencyLearner()
    coordinator = SimpleNamespace(
        battery_system="custom",
        hass=SimpleNamespace(states=states),
        _last_custom_energy_warning=None,
        _get_custom_entity_id=lambda key: key if key in ("level", "power") else "",
        _power_to_kw=lambda value, unit: value / 1000 if unit == "W" else value,
        _battery_efficiency_learner=learner,
        _battery_efficiency_topology_fingerprint=lambda _: "custom-battery",
        _schedule_battery_efficiency_learning_save=lambda: None,
        battery_efficiency_learning_enabled=True,
    )
    coordinator._read_numeric_state = partial(read_numeric, coordinator)
    coordinator._get_energy_data = partial(read_custom, coordinator)

    rows = _continuous_profile(datetime(2026, 7, 1, tzinfo=timezone.utc))
    at, _, _ = rows[30]
    # Six hours at 100% with fresh, zero-power observations.
    plateau = [(at + timedelta(minutes=5 * index), 1.0, 0.0) for index in range(1, 73)]
    rows = rows[:31] + plateau + [
        (at + timedelta(hours=6), soc, power) for at, soc, power in rows[31:]
    ]
    for at, soc, power in rows:
        states["level"] = SimpleNamespace(
            state=str(soc * 100), attributes={}, last_reported=at
        )
        states["power"] = SimpleNamespace(
            state=str(power * 1000), attributes={"unit_of_measurement": "W"},
            last_reported=at + timedelta(seconds=8),
        )
        observe(coordinator, timestamp=at + timedelta(seconds=15), capacity_wh=54000)

    assert len(learner.history) == 1
    assert learner.history[-1].round_trip_efficiency == pytest.approx(.8)
    assert learner.history[-1].timestamp == at.isoformat()

    report_time = at + timedelta(minutes=1)
    states["power"].last_reported = report_time
    observe(coordinator, timestamp=report_time, capacity_wh=54000)
    assert learner.rejections["stale_telemetry"] == 1

    # An explicitly missing field timestamp must never fall back to a poll time.
    stale_data = coordinator._get_energy_data()
    stale_data["last_update"] = report_time
    stale_data["telemetry_observed_at"]["battery_level"] = None
    coordinator._get_energy_data = lambda: stale_data
    observe(coordinator, timestamp=report_time, capacity_wh=54000)
    assert learner.rejections["stale_telemetry"] == 2


def test_published_diagnostics_keep_current_plan_values_separate_from_new_evidence():
    learner = BatteryEfficiencyLearner()
    for day in range(10):
        start = datetime(2026, 7, 1 + day, tzinfo=timezone.utc)
        _feed_profile(learner, _continuous_profile(start))
    now = start + timedelta(hours=6)
    diagnostics = learner.diagnostics(now=now,
        planning_parameters=MODULE.ResolvedOptimizerParameters.legacy())
    assert diagnostics["state"] == "learned"
    assert diagnostics["candidate_round_trip_efficiency"] == pytest.approx(.8)
    assert diagnostics["physical_round_trip_efficiency"] == pytest.approx(DEFAULT_RTE)
    assert diagnostics["applied_one_way_efficiency"] == pytest.approx(.92)


def test_sign_reversal_uses_interval_energy_direction_and_return_prefix():
    # A final discharging sample does not erase the charging energy delivered
    # during most of the preceding acquisition interval.
    learner = BatteryEfficiencyLearner()
    start = datetime(2026, 7, 1, tzinfo=timezone.utc)
    common = {"capacity_kwh":54,"topology_fingerprint":"synthetic-ac"}
    learner.observe(timestamp=start,soc=.8,battery_power_kw=-12,**common)
    result = learner.observe(timestamp=start+timedelta(minutes=5),soc=.81,battery_power_kw=2,**common)
    assert result.rejection_reason is None
    assert learner.state != "idle"
    charge, discharge = MODULE._integrate_directional_energy(-4,4,1)
    assert charge == pytest.approx(1)
    assert discharge == pytest.approx(1)
