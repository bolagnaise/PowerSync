"""Measured earnings, quota persistence and supply-charge display contracts."""
from __future__ import annotations

import importlib.util
import sys
import asyncio
import re
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
from types import SimpleNamespace

import pytest

from test_energy_accumulator_midnight_persistence import _Clock, _new_accumulator
from test_energy_accumulator_midnight_persistence import _Store
from test_zerohero_settlement import zerohero

ROOT = Path(__file__).resolve().parents[1]


def _module(name="daily_cost_test"):
    spec = importlib.util.spec_from_file_location(
        name, ROOT / "custom_components/power_sync/daily_cost.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _time(hour=18, minute=0, day=25):
    return datetime(2026, 10, day, hour, minute, tzinfo=ZoneInfo("Australia/Sydney"))


def _config(**overrides):
    values = dict(plan="zerohero_custom", start="18:00", end="21:00",
                  export_cap_kwh=15.0, super_export_rate=0.15,
                  credit_amount=1, import_limit_kw=0.03)
    values.update(overrides)
    return zerohero.ZeroHeroConfig(**values)


def test_reported_15kwh_earns_225_and_supply_is_counted_once():
    clock = _Clock(_time())
    acc = _new_accumulator(clock)
    acc._daily_cost_tracker = _module().DailyCostTracker(_config(), 1.20)
    acc.update(0, -5, 5, 2, 0.30, 0.05)
    for _ in range(36):
        clock.current += timedelta(minutes=5)
        acc.update(0, -5, 5, 2, 0.30, 0.05)
    summary = acc.as_dict()
    assert summary["export_earnings_today"] == pytest.approx(2.25)
    assert summary["net_cost_today"] == pytest.approx(-2.05)
    assert summary["zerohero_credit_status"] == "unverified"
    assert summary["daily_supply_charge"] == 1.20
    assert summary["avg_cost_per_kwh_today"] == pytest.approx(-2.05 / 6, abs=0.0001)
    assert acc.as_dict()["net_cost_today"] == summary["net_cost_today"]
    assert summary["mtd_supply_charge"] == pytest.approx(25 * 1.20)


def test_forecast_uses_configured_total_rate_then_base_and_resets_local_day():
    m = _module()
    prices = m.forecast_export_value_prices(
        _config(), [_time(), _time(19), _time(20), _time(21), _time(day=26)],
        [6000] * 5, [0.05] * 5, 60, {"2026-10-25": 15, "2026-10-26": 15},
    )
    assert prices == pytest.approx([0.15, 0.15, 0.10, 0.05, 0.15])


def test_exact_window_boundaries_split_measured_energy():
    tracker = _module().DailyCostTracker(_config())
    tracker.observe(None, _time(17, 59), 6, 0.05, False)
    tracker.observe(_time(17, 59), _time(18, 1), 6, 0.05, True)
    assert tracker.state["bonus"] == pytest.approx(0.01)
    tracker.observe(_time(20, 59), _time(21, 1), 6, 0.05, True)
    assert tracker.state["bonus"] == pytest.approx(0.02)


def test_partial_quota_and_same_day_restart_do_not_regrant_bonus():
    m = _module()
    tracker = m.DailyCostTracker(_config())
    tracker.observe(None, _time(), 6, 0.05, False)
    tracker.observe(_time(), _time(20, 30), 6, 0.05, True)
    assert tracker.state["used"] == pytest.approx(15)
    restored = m.DailyCostTracker(_config(), 1.2, tracker.state)
    restored.observe(None, _time(20, 30), 6, 0.05, False)
    restored.observe(_time(20, 30), _time(20, 35), 6, 0.05, True)
    assert restored.state["bonus"] == pytest.approx(1.5)
    restored.observe(None, _time(day=26), 6, 0.05, False)
    restored.observe(_time(day=26), _time(18, 5, day=26), 6, 0.05, True)
    assert restored.state["bonus"] == pytest.approx(0.05)
    assert not restored.state["partial"]


@pytest.mark.parametrize("kind", ["late_start", "gap", "missing_price"])
def test_unknown_quota_history_withholds_earnings(kind):
    tracker = _module().DailyCostTracker(_config())
    if kind == "late_start":
        tracker.observe(None, _time(19), 6, 0.05, False)
    else:
        tracker.observe(None, _time(), 6, 0.05, False)
        tracker.observe(_time(), _time(18, 5), 6,
                        None if kind == "missing_price" else 0.05, kind != "gap")
    tracker.observe(_time(19), _time(19, 5), 6, 0.05, True)
    assert tracker.state["bonus"] == 0
    assert tracker.state["partial"]
    summary = tracker.summary({"export_earnings_today": 0.25, "mtd_export_earnings": 0.25})
    assert summary["export_earnings_today"] is None
    assert summary["net_cost_today"] is None


def test_configured_10c_plan_and_higher_base_are_respected():
    m = _module()
    config = zerohero.zerohero_config_from_settings({"globird_plan": "zerohero_jul_2026"})
    prices = m.forecast_export_value_prices(
        config, [_time(), _time(19)], [5000, 5000], [0.05, 0.20], 60,
        {"2026-10-25": 15},
    )
    assert prices == pytest.approx([0.10, 0.20])


def test_base_only_provider_does_not_receive_globird_bonus():
    tracker = _module().DailyCostTracker(None, 1.2)
    tracker.observe(None, _time(19), 5, 0.05, False)
    summary = tracker.summary({"import_cost_today": 3, "export_earnings_today": 0.75})
    assert summary["export_earnings_today"] == 0.75
    assert summary["net_cost_today"] == pytest.approx(3.45)


def test_accumulator_persists_bonus_separately_from_base_cost():
    clock = _Clock(_time())
    acc = _new_accumulator(clock, _Store())
    m = _module()
    acc._daily_cost_tracker = m.DailyCostTracker(_config(), 1.2)
    acc.update(0, -6, 6, 0, 0.30, 0.05)
    clock.current += timedelta(minutes=5)
    acc.update(0, -6, 6, 0, 0.30, 0.05)
    payload = acc._data_to_save()
    assert payload["export_earnings_today"] == pytest.approx(0.025)
    assert payload["daily_cost_state"]["bonus"] == pytest.approx(0.05)
    restored = _new_accumulator(clock, _Store(payload))
    asyncio.run(restored.async_restore())
    restored._daily_cost_tracker = m.DailyCostTracker(_config(), 1.2, restored._daily_cost_state)
    restored.update(0, -6, 6, 0, 0.30, 0.05)
    assert restored.as_dict()["export_earnings_today"] == pytest.approx(0.075)
    clock.current += timedelta(minutes=5)
    restored.update(0, -6, 6, 0, 0.30, 0.05)
    assert restored.as_dict()["export_earnings_today"] == pytest.approx(0.15)


def test_dst_elapsed_time_and_month_rollover_do_not_inflate_costs():
    m = _module()
    zone = ZoneInfo("Australia/Sydney")
    first = datetime(2026, 4, 5, 2, 30, tzinfo=zone, fold=0)
    last = datetime(2026, 4, 5, 2, 30, tzinfo=zone, fold=1)
    config = _config(start="00:00", end="04:00")
    assert sum(h for _, active, h in m._parts(first, last, config) if active) == 1
    tracker = m.DailyCostTracker(None, 1.2)
    tracker.observe(None, datetime(2026, 10, 31, 23, 59, tzinfo=zone), 0, 0.05, False)
    tracker.observe(None, datetime(2026, 11, 1, 0, 1, tzinfo=zone), 0, 0.05, False)
    summary = tracker.summary({"import_cost_today": 0, "export_earnings_today": 0})
    assert summary["mtd_supply_charge"] == 1.2
    assert summary["supply_charge_days"] == 1


def test_existing_frontend_window_consumes_quota_weighted_value_series():
    source = (ROOT / "custom_components/power_sync/frontend/power-sync-strategy.js").read_text()
    method = re.search(r"  _energyValueForSegments\(segments, action, model\) \{(.*?)\n  \}\n\n  _actionRangesFromApi", source, re.S)
    script = "const calc = function(segments, action, model) {" + method.group(1) + "};\n" + r"""
    const start = '2026-10-25T18:00:00+11:00';
    const end = '2026-10-25T21:00:00+11:00';
    const model = {intervalMinutes: 60, detailedSchedule: true, points: [0,1,2].map(i => ({
      timestamp: new Date(Date.parse(start) + i*3600000).toISOString(),
      exportKw: 5, exportPrice: 5, exportValuePrice: 15
    }))};
    const result = calc([{timestamp:start, end_time:end}], 'export', model);
    if (result.energyKwh !== 15 || Math.abs(result.value - 2.25) > 1e-9) throw Error(JSON.stringify(result));
    """
    subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)


def test_midnight_keeps_full_month_interval_but_only_new_day_energy():
    clock = _Clock(_time(23, 59))
    acc = _new_accumulator(clock)
    acc.update(0, -6, 6, 0, 0.30, 0.05)
    clock.current = _time(0, 1, day=26)
    acc.update(0, -6, 6, 0, 0.30, 0.05)
    assert acc.grid_export_kwh == pytest.approx(0.1)
    assert acc.mtd_grid_export_kwh == pytest.approx(0.2)


def test_continuous_overnight_window_keeps_new_day_quota_known():
    tracker = _module().DailyCostTracker(_config(start="22:00", end="02:00"))
    tracker.observe(None, _time(0, 0), 6, 0.05, False)
    # Seed a complete observation sequence through the next midnight.
    cursor = _time(0, 0)
    while cursor < _time(0, 1, day=26):
        end = cursor + timedelta(minutes=1)
        tracker.observe(cursor, end, 6, 0.05, True)
        cursor = end
    assert tracker.state["used"] == pytest.approx(0.1)
    assert tracker.state["bonus"] == pytest.approx(0.01)
    assert not tracker.state["partial"]


def test_restart_during_bonus_window_cannot_assume_no_export_while_down():
    m = _module()
    tracker = m.DailyCostTracker(_config())
    tracker.observe(None, _time(), 6, 0.05, False)
    tracker.observe(_time(), _time(18, 5), 6, 0.05, True)
    restored = m.DailyCostTracker(_config(), state=tracker.state)
    restored.observe(None, _time(19), 6, 0.05, False)
    assert restored.state["partial"]
    assert restored.state["bonus"] == pytest.approx(0.05)
    assert restored.state["used"] == 15


def test_legacy_mtd_bonus_history_is_explicitly_partial(monkeypatch):
    m = _module("daily_cost_fixture.daily_cost")
    monkeypatch.setitem(sys.modules, "daily_cost_fixture.zerohero", zerohero)
    entry = SimpleNamespace(data={"electricity_provider": "globird", "globird_plan": "zerohero_current"}, options={})
    hass = SimpleNamespace(data={}, config_entries=SimpleNamespace(async_get_entry=lambda _: entry))
    tracker = m.configure_tracker(hass, "entry", _time(17), None, {}, 15.0, "2026-10")
    tracker.observe(None, _time(17), 0, 0.05, False)
    assert tracker.state["mtd_partial"]
    assert not tracker.state["partial"]


def test_configured_supply_uses_calendar_month_and_finite_rates():
    m = _module()
    entry = SimpleNamespace(data={"daily_supply_charge": 1.25, "monthly_supply_charge": 31}, options={})
    assert m.configured_supply_charge(entry, _time()) == 1.25
    entry.options = {"daily_supply_charge": 0}
    assert m.configured_supply_charge(entry, _time()) == 1.0
    entry.options = {"daily_supply_charge": float("nan"), "monthly_supply_charge": float("inf")}
    assert m.configured_supply_charge(entry, _time()) == 0


def test_normal_month_rollover_does_not_become_legacy_unknown(monkeypatch):
    m = _module("daily_cost_fixture.daily_cost")
    monkeypatch.setitem(sys.modules, "daily_cost_fixture.zerohero", zerohero)
    entry = SimpleNamespace(data={"electricity_provider": "globird", "globird_plan": "zerohero_current"}, options={})
    hass = SimpleNamespace(data={}, config_entries=SimpleNamespace(async_get_entry=lambda _: entry))
    zone = ZoneInfo("Australia/Sydney")
    old = datetime(2026, 10, 31, 23, 59, tzinfo=zone)
    now = datetime(2026, 11, 1, 0, 1, tzinfo=zone)
    tracker = m.DailyCostTracker(_config())
    tracker.observe(None, datetime(2026, 10, 31, 17, 0, tzinfo=zone), 0, 0.05, False)
    tracker.observe(old, old, 0, 0.05, False)
    configured = m.configure_tracker(hass, "entry", now, tracker, {}, 15.0, "2026-10")
    configured.observe(old, now, 6, 0.05, True)
    assert configured.state["month"] == "2026-11"
    assert not configured.state["mtd_partial"]


def _credit_window(tracker, *, import_kw=0.0, start=None, gap=False):
    cursor = start or _time()
    tracker.observe(None, cursor, 0, 0.05, False, import_kw=import_kw)
    while cursor < _time(21):
        end = cursor + timedelta(minutes=5)
        tracker.observe(cursor, end, 0, 0.05, not (gap and cursor.hour == 19),
                        import_kw=import_kw)
        cursor = end
    return tracker


def _cost_base():
    return dict(import_cost_today=0.21, export_earnings_today=1.69,
                avg_cost_per_kwh_today=0, load_today_kwh=10,
                mtd_import_cost=0.21, mtd_export_earnings=1.69,
                avg_cost_per_kwh_mtd=0, mtd_load_kwh=10)


def test_requested_039_becomes_minus_061_but_never_verified_locally():
    tracker = _credit_window(_module().DailyCostTracker(_config(), 1.87))
    summary = tracker.summary(_cost_base())
    assert summary["net_cost_today"] == pytest.approx(-0.61)
    assert summary["zerohero_credit_today"] == 1
    assert summary["zerohero_credit_status"] == "unverified"
    assert summary["avg_cost_per_kwh_today"] == pytest.approx(-0.061)
    assert summary["avg_cost_per_kwh_mtd"] == pytest.approx((0.21 - 1.69 + 25 * 1.87 - 1) / 10)
    assert tracker.summary(_cost_base()) == summary  # no repeated deduction


@pytest.mark.parametrize("variant", ["imports", "gap", "late", "missing", "restart"])
def test_credit_withheld_for_imports_or_unobserved_window(variant):
    tracker = _module().DailyCostTracker(_config())
    if variant == "restart":
        tracker.observe(None, _time(), 0, 0.05, False, import_kw=0)
        tracker.observe(_time(), _time(18, 55), 0, 0.05, True, import_kw=0)
        tracker = _module().DailyCostTracker(_config(), state=tracker.state)
    tracker = _credit_window(tracker, import_kw=(0.1 if variant == "imports" else
                            None if variant == "missing" else 0),
                            start=_time(19) if variant in ("late", "restart") else None,
                            gap=variant == "gap")
    summary = tracker.summary(_cost_base())
    assert summary["zerohero_credit_today"] == 0
    assert summary["zerohero_credit_status"] == ("missed" if variant == "imports" else "unknown")


def test_no_credit_before_end_and_plan_allowance_is_respected():
    tracker = _module().DailyCostTracker(_config())
    tracker.observe(None, _time(), 0, 0.05, False, import_kw=0)
    tracker.observe(_time(), _time(20, 59), 0, 0.05, True, import_kw=0)
    assert tracker.summary(_cost_base())["zerohero_credit_today"] == 0
    assert tracker.summary(_cost_base())["zerohero_credit_status"] == "pending"
    tracker = _credit_window(_module().DailyCostTracker(_config()), import_kw=0.02)
    assert tracker.summary(_cost_base())["zerohero_credit_status"] == "unverified"


@pytest.mark.parametrize("api_amount", [-1.0, -0.75, 0.0, 0.25])
def test_next_day_api_replaces_estimate_persists_and_does_not_double_count(api_amount):
    m = _module()
    tracker = _credit_window(m.DailyCostTracker(_config()))
    tracker.observe(None, _time(9, day=26), 0, 0.05, False, import_kw=0)
    assert tracker.summary(_cost_base())["mtd_zerohero_credit"] == 1
    rows = [{"date": "2026-10-25", "amount": 0.2, "chargeCategory": "USAGE", "complete": True}]
    if api_amount:
        rows.append({"date": "2026-10-25", "amount": api_amount,
                     "chargeCategory": "ZEROHERO CREDIT", "complete": True})
    for _ in range(2):
        tracker.reconcile_credit({"daily": rows}, _time(9, day=26))
    restored = m.DailyCostTracker(_config(), state=tracker.state)
    summary = restored.summary(_cost_base())
    assert summary["zerohero_credit_today"] == 0
    assert summary["mtd_zerohero_credit"] == -api_amount
    assert summary["zerohero_credit_days"]["2026-10-25"]["status"] == (
        "confirmed" if api_amount else "not_awarded")


def test_supply_only_and_same_day_api_cannot_verify_credit():
    tracker = _credit_window(_module().DailyCostTracker(_config()))
    rows = [{"date": "2026-10-25", "amount": -1, "complete": True,
             "chargeCategory": "ZEROHERO CREDIT"}]
    tracker.reconcile_credit({"daily": rows}, _time(22))
    assert tracker.summary(_cost_base())["zerohero_credit_status"] == "unverified"
    rows[0]["complete"] = False
    tracker.reconcile_credit({"daily": rows, "available_daily": rows}, _time(9, day=26))
    assert tracker.summary(_cost_base())["zerohero_credit_status"] == "unverified"


def test_multi_service_account_never_confirms_another_sites_credit(monkeypatch):
    m = _module("daily_cost_fixture.daily_cost")
    monkeypatch.setitem(sys.modules, "daily_cost_fixture.zerohero", zerohero)
    entry = SimpleNamespace(data={"electricity_provider": "globird", "globird_plan": "zerohero_current"}, options={})
    detail = {"cost_summary": {"daily": [{"date": "2026-10-25", "amount": -1,
              "chargeCategory": "ZEROHERO CREDIT", "complete": True}]}}
    portal = SimpleNamespace(data={"service_data": {"one": detail, "two": detail}})
    hass = SimpleNamespace(data={"power_sync": {"entry": {"globird_coordinator": portal}}},
                           config_entries=SimpleNamespace(async_get_entry=lambda _: entry))
    tracker = m.configure_tracker(hass, "entry", _time(9, day=26), None, {})
    assert "2026-10-25" not in tracker.state["credits"]
    del portal.data["service_data"]["two"]
    tracker = m.configure_tracker(hass, "entry", _time(9, day=26), tracker, {})
    assert tracker.state["credits"]["2026-10-25"]["status"] == "confirmed"


def test_overnight_credit_is_owned_by_window_start_day():
    tracker = _module().DailyCostTracker(_config(start="22:00", end="02:00"))
    tracker.observe(None, _time(22), 0, 0.05, False, import_kw=0)
    tracker.observe(_time(22), _time(2, day=26), 0, 0.05, True, import_kw=0)
    summary = tracker.summary(_cost_base())
    assert summary["zerohero_credit_today"] == 0
    assert summary["zerohero_credit_days"]["2026-10-25"] == {"amount": 1, "status": "unverified"}


def test_month_rollover_drops_prior_month_credit_from_mtd():
    tracker = _credit_window(_module().DailyCostTracker(_config()))
    november = _time().replace(month=11, day=1, hour=9)
    tracker.observe(None, november, 0, 0.05, False, import_kw=0)
    assert tracker.summary(_cost_base())["mtd_zerohero_credit"] == 0


def test_credit_survives_accumulator_store_restore_without_regranting():
    clock = _Clock(_time())
    acc = _new_accumulator(clock, _Store())
    m = _module()
    acc._daily_cost_tracker = m.DailyCostTracker(_config())
    acc.update(0, -5, 5, 2, 0.30, 0.05)
    for _ in range(36):
        clock.current += timedelta(minutes=5)
        acc.update(0, -5, 5, 2, 0.30, 0.05)
    payload = acc._data_to_save()
    assert payload["daily_cost_state"]["credits"]["2026-10-25"]["amount"] == 1
    restored = _new_accumulator(clock, _Store(payload))
    asyncio.run(restored.async_restore())
    restored._daily_cost_tracker = m.DailyCostTracker(_config(), state=restored._daily_cost_state)
    clock.current += timedelta(minutes=5)
    restored.update(0, -5, 5, 2, 0.30, 0.05)
    assert restored.as_dict()["zerohero_credit_today"] == 1
    assert restored.as_dict()["zerohero_credit_status"] == "unverified"


def test_plan_change_mid_window_cannot_claim_credit():
    m = _module()
    tracker = m.DailyCostTracker(_config())
    tracker.observe(_time(), _time(19), 0, 0.05, True, import_kw=0)
    tracker = m.DailyCostTracker(_config(credit_amount=2), state=tracker.state)
    tracker.observe(_time(19), _time(21), 0, 0.05, True, import_kw=0)
    assert tracker.summary(_cost_base())["zerohero_credit_status"] == "unknown"
    assert tracker.summary(_cost_base())["zerohero_credit_today"] == 0
