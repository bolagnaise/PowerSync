"""Focused tests for planning-only external energy resources."""

from datetime import datetime, timedelta, timezone
import importlib.util
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "custom_components" / "power_sync" / "optimization" / "external_energy_resource.py"
SPEC = importlib.util.spec_from_file_location("powersync_external_energy_test", MODULE_PATH)
assert SPEC and SPEC.loader
module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = module
SPEC.loader.exec_module(module)

ExternalEnergyLedgerState = module.ExternalEnergyLedgerState
ExternalEnergyResourceConfig = module.ExternalEnergyResourceConfig
ResolvedExternalEnergySession = module.ResolvedExternalEnergySession
allocate_external_energy = module.allocate_external_energy
expand_external_energy_sessions = module.expand_external_energy_sessions
reduce_external_energy_ledger = module.reduce_external_energy_ledger


UTC = timezone.utc


def _session(
    *,
    resource_id: str = "vehicle",
    session_id: str = "vehicle:session",
    remaining_kwh: float = 10.0,
    available: tuple[bool, ...] = (True, True, True),
    max_kw: tuple[float, ...] | None = None,
    start: datetime | None = None,
) -> ResolvedExternalEnergySession:
    start = start or datetime(2026, 8, 30, 22, 0, tzinfo=UTC)
    slots = tuple(start + timedelta(minutes=5 * index) for index in range(len(available)))
    max_kw = max_kw or (3.6,) * len(available)
    return ResolvedExternalEnergySession(
        resource_id=resource_id,
        session_id=session_id,
        planning_mode="import_offset_only",
        sink_mode="import_offset_only",
        remaining_ac_kwh=remaining_kwh,
        available_slots=available,
        max_discharge_kw=max_kw,
        session_start_utc=start,
        session_end_utc=start + timedelta(minutes=5 * len(available)),
        slot_starts_utc=slots,
    )


def test_recurring_cross_midnight_sessions_are_keyed_by_utc_start() -> None:
    config = ExternalEnergyResourceConfig(
        resource_id="vehicle",
        usable_energy_wh=10_000,
        max_power_w=3_600,
        start_local="22:00",
        end_local="06:00",
        timezone="Australia/Brisbane",
    )
    start = datetime(2026, 8, 30, 11, 0, tzinfo=UTC)
    end = datetime(2026, 9, 1, 23, 0, tzinfo=UTC)
    sessions = expand_external_energy_sessions(
        config, start, end, slot_duration=timedelta(minutes=5)
    )

    assert len(sessions) == 3
    assert sessions[0].session_start_utc == datetime(2026, 8, 30, 12, 0, tzinfo=UTC)
    assert sessions[0].session_end_utc == datetime(2026, 8, 30, 20, 0, tzinfo=UTC)
    assert sessions[0].session_id.endswith("2026-08-30T12:00:00+00:00")
    assert sessions[0].remaining_energy_wh == pytest.approx(10_000)
    assert sum(sessions[0].available_slots) == 96


def test_allocation_prefers_highest_avoided_import_then_chronological() -> None:
    session = _session(remaining_kwh=0.1, available=(True, True, True))
    result = allocate_external_energy(
        (session,),
        eligible_native_home_import_kw=(1.0, 1.0, 1.0),
        avoided_import_price=(10.0, 30.0, 30.0),
    )

    plan = result.plans[0]
    # At 1 kW for five minutes a slot is 0.08333 kWh.  The equal-priced
    # slots are consumed chronologically before any lower-priced slot.
    assert plan.planned_discharge_w[0] == pytest.approx(0.0)
    assert plan.planned_discharge_w[1] == pytest.approx(1_000.0)
    assert plan.planned_discharge_w[2] == pytest.approx(200.0)
    assert plan.planned_energy_kwh == pytest.approx(0.1)


def test_allocation_is_capped_per_slot_and_per_session() -> None:
    session = _session(remaining_kwh=10.0, available=(True,) * 40)
    result = allocate_external_energy(
        (session,),
        eligible_native_home_import_kw=(10.0,) * 40,
        avoided_import_price=(50.0,) * 40,
    )

    plan = result.plans[0]
    assert max(plan.planned_discharge_w) <= 3_600.0 + 1e-9
    assert plan.planned_energy_kwh == pytest.approx(10.0)
    assert plan.remaining_after_plan_kwh == pytest.approx(0.0)
    assert sum(result.external_power_kw) > 0


def test_overlapping_resources_have_independent_budgets_but_share_home_import() -> None:
    first = _session(resource_id="first", session_id="first:one", remaining_kwh=0.2)
    second = _session(resource_id="second", session_id="second:one", remaining_kwh=0.2)
    result = allocate_external_energy(
        (first, second),
        eligible_native_home_import_kw=(1.0, 1.0, 1.0),
        avoided_import_price=(20.0, 20.0, 20.0),
    )

    assert sum(result.external_power_kw) <= 3.0 + 1e-9
    assert sum(plan.planned_energy_kwh for plan in result.plans) == pytest.approx(0.25)
    assert all(plan.planned_energy_kwh <= 0.2 + 1e-9 for plan in result.plans)


def test_ev_demand_is_never_covered_by_external_resource() -> None:
    session = _session(remaining_kwh=1.0, available=(True,))
    result = allocate_external_energy(
        (session,),
        eligible_native_home_import_kw=(3.0,),
        planned_ev_charge_kw=(3.0,),
        avoided_import_price=(20.0,),
    )

    assert result.external_power_kw == (0.0,)
    assert result.plans[0].planned_energy_kwh == 0.0


def test_import_offset_does_not_change_battery_actions_or_grid_export() -> None:
    session = _session(remaining_kwh=1.0, available=(True, True))
    result = allocate_external_energy(
        (session,),
        eligible_native_home_import_kw=(2.0, 2.0),
        avoided_import_price=(20.0, 20.0),
        grid_import_without_resource_kw=(2.0, 2.0),
        grid_export_without_resource_kw=(0.0, 1.5),
    )

    assert result.grid_import_with_resource_kw[0] < result.grid_import_without_resource_kw[0]
    assert result.grid_export_with_resource_kw == (0.0, 1.5)
    # The result has no battery action at all: callers retain the base
    # battery charge/discharge decisions unchanged.
    assert not hasattr(result, "battery_charge_kw")


def test_ledger_uses_measured_energy_once_and_never_replenishes_from_lower_late_data() -> None:
    start = datetime(2026, 8, 30, 22, 0, tzinfo=UTC)
    session = _session(remaining_kwh=0.3, available=(True, True, True), start=start)
    planned = (1_000.0, 1_000.0, 1_000.0)

    first = reduce_external_energy_ledger(
        session,
        None,
        now=start + timedelta(minutes=5),
        planned_discharge_w=planned,
    )
    assert first.remaining_ac_kwh(session) == pytest.approx(0.2166666667)

    second = reduce_external_energy_ledger(
        session,
        first,
        now=start + timedelta(minutes=10),
        # Delayed measured telemetry reports less for slot zero.  It cannot
        # undo the conservative fallback; slot one is measured authoritatively.
        measured_energy_wh=(50.0, 50.0),
        planned_discharge_w=planned,
    )
    assert second.entries[0].consumed_energy_wh == pytest.approx(133.3333333)

    third = reduce_external_energy_ledger(
        session,
        second,
        now=start + timedelta(minutes=15),
    )
    # Slot one remains the measured 50 Wh value, rather than reverting to the
    # previous 83.33 Wh plan. Re-running is idempotent.
    assert third.entries[0].consumed_energy_wh == pytest.approx(216.6666667)
    again = reduce_external_energy_ledger(session, third, now=start + timedelta(minutes=15))
    assert again == third


def test_shifted_rolling_horizon_settles_dropped_previous_slot() -> None:
    start = datetime(2026, 8, 30, 22, 0, tzinfo=UTC)
    first_session = _session(
        remaining_kwh=0.3,
        available=(True, True),
        start=start,
    )
    first = reduce_external_energy_ledger(
        first_session,
        None,
        now=start,
        planned_discharge_w=(1_000.0, 1_000.0),
    )
    shifted = _session(
        remaining_kwh=0.3,
        available=(True, True),
        start=start + timedelta(minutes=5),
    )
    # Preserve the same recurring-session identity while its aligned horizon
    # advances by one slot.
    shifted = module.replace(
        shifted,
        session_id=first_session.session_id,
        session_start_utc=first_session.session_start_utc,
    )

    second = reduce_external_energy_ledger(
        shifted,
        first,
        now=start + timedelta(minutes=5),
    )

    assert second.remaining_ac_kwh(shifted) == pytest.approx(0.2166666667)


def test_corrupt_active_ledger_fails_closed() -> None:
    session = _session(remaining_kwh=10.0, available=(True,))
    corrupt = ExternalEnergyLedgerState.from_dict(
        {"schema_version": 999, "entries": {}}
    )
    reduced = reduce_external_energy_ledger(
        session,
        corrupt,
        now=session.session_start_utc + timedelta(minutes=5),
        planned_discharge_w=(3_600.0,),
    )

    assert reduced.corrupt is True
    assert reduced.remaining_ac_kwh(session) == 0.0
    assert reduced.entries[0].corrupt is True


def test_invalid_session_fails_closed_without_any_allocation() -> None:
    invalid = _session(remaining_kwh=1.0, available=(True,), max_kw=(float("nan"),))
    result = allocate_external_energy(
        (invalid,), eligible_native_home_import_kw=(5.0,), avoided_import_price=(10.0,)
    )
    assert result.external_power_kw == (0.0,)
    assert result.plans[0].reason == "invalid_session"
