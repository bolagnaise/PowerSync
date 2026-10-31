"""A forecast is not an accepted home-battery charging commitment."""
from types import SimpleNamespace as NS

import pytest

from test_native_rate_admission import admission, NOW, VIN
from test_tesla_native_rate_ha import adapter, live_adapter


def unissued_plan(a, *, enabled, monitoring):
    a.entry.options["monitoring_mode"] = monitoring
    optimizer = a.runtime.get("optimization_coordinator")
    if optimizer is None:
        optimizer = a.runtime["optimization_coordinator"] = NS()
    optimizer._get_current_action = lambda: NS(action="charge", power_w=10000)
    optimizer._enabled = enabled
    optimizer._monitoring_mode_active = lambda: monitoring


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled,monitoring", [(False, True), (False, False), (True, False)])
async def test_unissued_plan_cannot_deny_manual_ev_capacity(admission, enabled, monitoring):
    a = admission
    unissued_plan(a, enabled=enabled, monitoring=monitoring)
    context = a.m._EV_SUBMISSION_CONTEXT.get()
    context["params"]["owner_mode"] = "boost"
    # Grid6k includes the actual 1k battery draw and 2.3k current EV draw.
    # Raising the EV from10A to20A projects8.3k, below the known15k limit.
    assert await a.m.prepare_ev_submission(a.hass, a.state.entity_id, operation="rate", amps=20)
    evidence = a.m._site_evidence(a.hass, a.entry, a.runtime, VIN, context["params"])
    assert evidence["battery_pending_w"] == 0
    assert evidence["borrowable_battery_w"] == 0
    assert evidence["battery_generation"] is None
    assert a.budget.managed[VIN]["reserved_w"] == 4600
    assert a.budget.unobserved_w("site") == 2300


@pytest.mark.asyncio
async def test_forecast_fix_keeps_automated_monitoring_gate(admission):
    a = admission
    unissued_plan(a, enabled=True, monitoring=True)
    a.m._EV_SUBMISSION_CONTEXT.get()["params"]["owner_mode"] = "scheduled"
    with pytest.raises(a.m.SiteImportAdmissionDenied):
        await a.m.prepare_ev_submission(a.hass, a.state.entity_id, operation="rate", amps=20)
    assert not a.budget.managed


@pytest.mark.asyncio
@pytest.mark.parametrize("generation", [None, 7])
async def test_real_accepted_battery_commitment_remains_reserved(admission, generation):
    a = admission
    unissued_plan(a, enabled=False, monitoring=True)
    a.runtime["powerwall_battery_dispatch"] = NS(
        charge_generation=generation, _last_watts=-10000, prospective_charge_w=10000)
    evidence = a.m._site_evidence(a.hass, a.entry, a.runtime, VIN, {})
    assert evidence["battery_pending_w"] == 9000
    assert evidence["battery_generation"] == generation
    # A possible 10k submitted target cannot be freed by changing its forecast.
    with pytest.raises(a.m.SiteImportAdmissionDenied):
        await a.m.prepare_ev_submission(a.hass, a.state.entity_id, operation="rate", amps=20)
    assert not a.budget.managed


@pytest.mark.asyncio
async def test_actual_native_draw_is_not_silently_lent_without_capability(admission):
    a = admission
    unissued_plan(a, enabled=True, monitoring=False)
    a.snapshot.grid_w = 15000
    a.snapshot.battery_w = -10000
    with pytest.raises(a.m.SiteImportAdmissionDenied):
        await a.m.prepare_ev_submission(a.hass, a.state.entity_id, operation="rate", amps=20)
    assert not a.budget.managed


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled,monitoring", [(False, True), (False, False), (True, False)])
async def test_native_observation_reserves_measured_battery_not_unissued_plan(live_adapter, enabled, monitoring):
    a = live_adapter
    unissued_plan(a, enabled=enabled, monitoring=monitoring)
    observed = await a.ports.read(VIN)
    assert observed is not None
    assert observed.non_ev_net_w == 2000
    assert observed.planned_battery_w == 5000
    assert observed.borrowable_battery_w == 0
    assert a.m.decide_rate(observed, 32, NOW)[0] == 32
    # Read-only accounting does not authorize an automatic write when disabled.
    if not enabled or monitoring:
        assert a.ports.permitted(VIN, None, False) is False


@pytest.mark.asyncio
@pytest.mark.parametrize("committed,expected", [(0, 5000), (6000, 6000), (12000, 12000)])
async def test_native_observation_counts_actual_and_accepted_target_once(live_adapter, committed, expected):
    a = live_adapter
    unissued_plan(a, enabled=True, monitoring=False)
    a.runtime["powerwall_battery_dispatch"] = NS(
        charge_generation=None, prospective_charge_w=committed, _last_watts=-committed)
    observed = await a.ports.read(VIN)
    assert observed is not None
    assert observed.planned_battery_w == expected
    assert observed.non_ev_net_w == 2000
    assert observed.borrowable_battery_w == 0


@pytest.mark.asyncio
async def test_two_ev_unobserved_reservations_are_counted_once_after_plan_removed(admission):
    a = admission
    unissued_plan(a, enabled=False, monitoring=True)
    a.m._EV_SUBMISSION_CONTEXT.get()["params"]["owner_mode"] = "boost"
    assert await a.m.prepare_ev_submission(a.hass, a.state.entity_id, operation="rate", amps=20)
    second = "5YJ3E1EA7KF000002"
    evidence = a.m._site_evidence(a.hass, a.entry, a.runtime, VIN, {})
    evidence["actual_ev_w"] = 0
    record = {"entry_id": a.entry.entry_id, "site": "site",
              "identity": "tesla_fleet:provider:second:amps", "reserved_w": 4600,
              "target_amps": 20, "target_w": 4600, "operation": "rate"}
    assert await a.budget.admit_managed(second, record, evidence, lambda: True) is not None
    # Meter6k already contains VIN1's physical2.3k. Reserve only its remaining
    # 2.3k plus VIN2's unobserved4.6k: prospective total12.9k, not15.2k.
    assert a.budget.unobserved_w("site") == 6900
    assert evidence["grid_w"] + a.budget.unobserved_w("site") == 12900
    third = {"entry_id": a.entry.entry_id, "site": "site",
             "identity": "tesla_fleet:provider:third:amps", "reserved_w": 2300}
    assert await a.budget.admit_managed("5YJ3E1EA7KF000003", third, evidence, lambda: True) is None
    a.budget.observe(second, watts=4600, at=NOW, meter_at=NOW)
    evidence["grid_w"] += 4600
    assert evidence["grid_w"] + a.budget.unobserved_w("site") == 12900
    assert await a.budget.admit_managed("5YJ3E1EA7KF000003", third, evidence, lambda: True) is None
