"""Tests for provider-neutral closed-cycle battery efficiency learning."""

from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

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
    assert result.accepted_cycle is True
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
        for minutes, soc, power in observations:
            observed_at = morning + timedelta(minutes=minutes)
            result = learner.observe(
                timestamp=observed_at, soc=soc, battery_power_kw=power, **common
            )
        assert result.accepted_cycle

    assert learner.evidence(observed_at)[:2] == (6, 6)
    assert learner.candidate_round_trip_efficiency(observed_at) == pytest.approx(0.92)
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


def test_store_round_trip_excludes_open_cycle_and_toggle_disables_application():
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

    assert restored.state == "idle"
    assert resolved.battery_efficiency_source == "disabled"
    assert resolved.charge_efficiency == pytest.approx(0.92)
    assert restored.to_dict()["version"] == 1
    assert BatteryEfficiencyLearner.from_dict({"version": 999}).history == []
