from datetime import datetime, timezone
import importlib.util
from pathlib import Path
import sys
import types

import pytest

ROOT = Path(__file__).parents[1]
PACKAGE = "flow_power_contract_test"
package = types.ModuleType(PACKAGE)
package.__path__ = [str(ROOT / "custom_components" / "power_sync")]
sys.modules[PACKAGE] = package


def _load(name: str):
    spec = importlib.util.spec_from_file_location(
        f"{PACKAGE}.{name}",
        ROOT / "custom_components" / "power_sync" / f"{name}.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_load("const")
quota = _load("quota")
flow_power = _load("flow_power")
QuotaLedger = quota.QuotaLedger
QuotaLedgerState = quota.QuotaLedgerState
flow_power_plan_catalog = flow_power.flow_power_plan_catalog
flow_power_price_series = flow_power.flow_power_price_series
flow_power_provider_contract = flow_power.flow_power_provider_contract
flow_power_quota_rules = flow_power.flow_power_quota_rules
resolve_flow_power_plan = flow_power.resolve_flow_power_plan
validate_flow_power_plan_selection = flow_power.validate_flow_power_plan_selection


def _snapshot(plan=None, *, region=None, legacy_rate=0.45, legacy_end=None):
    raw = None if plan is None else {
        "schema_version": 1,
        "plan_id": plan,
        "region": region,
        "effective_from": "2026-09-01",
        "overrides": {},
    }
    return resolve_flow_power_plan(
        raw,
        timezone_token="Australia/Sydney",
        legacy_export_rate_dollars=legacy_rate,
        legacy_happy_hour_end=legacy_end,
    )


def _at(day, hour, minute=0):
    return datetime(2026, 8 if day == 31 else 9, day if day == 31 else day,
                    hour, minute, tzinfo=timezone.utc)


def test_missing_plan_stays_legacy_with_original_window_fallback():
    snapshot = _snapshot()
    assert snapshot.plan_id == "legacy_unclassified"
    assert snapshot.legacy_happy_hour_end == "19:30"
    series = flow_power_price_series(
        snapshot,
        [datetime(2026, 9, 2, 8, 0, tzinfo=timezone.utc)],
        [0.31],
    )
    assert series.settlement_export == (0.45,)
    assert series.export_bonus == (0.0,)


def test_explicit_happy_hour_switches_at_local_effective_date():
    snapshot = _snapshot("happy_hour_2026", region="NSW", legacy_rate=0.45,
                         legacy_end="19:30")
    timestamps = [
        datetime(2026, 8, 31, 9, 0, tzinfo=timezone.utc),  # 19:00 AEST
        datetime(2026, 9, 1, 9, 0, tzinfo=timezone.utc),   # 19:00 AEST
    ]
    ledger = QuotaLedger(flow_power_quota_rules(snapshot), QuotaLedgerState(
        tariff_day="2026-09-01", timezone_token="Australia/Sydney",
        confidence="authoritative",
    ))
    series = flow_power_price_series(snapshot, timestamps, [0.2, 0.2], ledger=ledger)
    assert series.active_plan_ids == ("legacy_unclassified", "happy_hour_2026")
    assert series.settlement_export == pytest.approx((0.45, 0.10))
    assert series.export_bonus == pytest.approx((0.0, 0.25))


@pytest.mark.parametrize(
    ("plan", "region", "base", "bonus"),
    [
        ("happy_hour_2026", "NSW", 0.10, 0.25),
        ("happy_hour_2026", "VIC", 0.10, 0.20),
        ("four_free_2026", "NSW", 0.05, 0.15),
        ("four_free_2026", "VIC", 0.02, 0.15),
    ],
)
def test_export_plan_base_and_bonus(plan, region, base, bonus):
    snapshot = _snapshot(plan, region=region)
    ledger = QuotaLedger(flow_power_quota_rules(snapshot), QuotaLedgerState(
        tariff_day="2026-09-02", timezone_token="Australia/Sydney",
        confidence="authoritative",
    ))
    at = datetime(2026, 9, 2, 9, 0, tzinfo=timezone.utc)
    series = flow_power_price_series(snapshot, [at], [0.3], ledger=ledger)
    assert series.settlement_export == pytest.approx((base,))
    assert series.export_bonus == pytest.approx((bonus,))


def test_unknown_confidence_preserves_base_and_disables_bonus():
    snapshot = _snapshot("happy_hour_2026", region="NSW")
    ledger = QuotaLedger(flow_power_quota_rules(snapshot), QuotaLedgerState(
        tariff_day="2026-09-02", timezone_token="Australia/Sydney",
        confidence="unknown",
    ))
    series = flow_power_price_series(
        snapshot, [datetime(2026, 9, 2, 9, 0, tzinfo=timezone.utc)], [0.3], ledger=ledger
    )
    assert series.settlement_export == pytest.approx((0.10,))
    assert series.export_bonus == (0.0,)


def test_four_free_has_independent_hourly_import_buckets():
    snapshot = _snapshot("four_free_2026", region="SEQ")
    rules = flow_power_quota_rules(snapshot)
    assert [(rule.rule_id, rule.daily_cap_kwh) for rule in rules if rule.direction == "import"] == [
        ("flow_4free_import_11", 8.0),
        ("flow_4free_import_12", 8.0),
        ("flow_4free_import_13", 8.0),
        ("flow_4free_import_14", 8.0),
    ]
    ledger = QuotaLedger(rules, QuotaLedgerState(
        tariff_day="2026-09-02", timezone_token="Australia/Sydney",
        confidence="authoritative",
    ))
    timestamps = [datetime(2026, 9, 2, hour - 10, 30, tzinfo=timezone.utc)
                  for hour in range(11, 15)]
    series = flow_power_price_series(snapshot, timestamps, [0.31] * 4, ledger=ledger)
    assert series.marginal_import == (0.0, 0.0, 0.0, 0.0)
    assert len(set(series.import_group_ids)) == 4
    assert set(series.import_group_caps_kwh.values()) == {8.0}


def test_flow_home_is_two_cents_all_day_without_quota():
    snapshot = _snapshot("flow_home_2026", region="NSW")
    timestamps = [datetime(2026, 9, 2, hour, tzinfo=timezone.utc) for hour in (0, 8, 16)]
    series = flow_power_price_series(snapshot, timestamps, [0.2] * 3)
    assert series.settlement_export == pytest.approx((0.02, 0.02, 0.02))
    assert series.export_bonus == (0.0, 0.0, 0.0)
    assert flow_power_quota_rules(snapshot) == ()


def test_catalog_and_region_validation_do_not_infer_seq_from_qld():
    four_free = next(item for item in flow_power_plan_catalog()
                     if item["plan_id"] == "four_free_2026")
    assert [region["value"] for region in four_free["regions"]] == ["NSW", "SA", "SEQ", "VIC"]
    with pytest.raises(ValueError, match="not available"):
        validate_flow_power_plan_selection({"plan_id": "four_free_2026", "region": "QLD"})


def test_contract_read_does_not_mutate_ledger():
    snapshot = _snapshot("happy_hour_2026", region="NSW")
    ledger = QuotaLedger(flow_power_quota_rules(snapshot), QuotaLedgerState(
        tariff_day="2026-09-02", timezone_token="Australia/Sydney",
        confidence="authoritative", settled_kwh={"flow_happy_hour_export": 4.0},
    ))
    before = ledger.state.to_dict()
    contract = flow_power_provider_contract(
        snapshot, at=datetime(2026, 9, 2, 9, tzinfo=timezone.utc),
        import_price=0.3, ledger=ledger,
        planned_kwh={"flow_happy_hour_export": 3.5},
    )
    assert contract["prices"]["marginal"]["export"] == pytest.approx(0.35)
    assert contract["quotas"][0]["remaining_kwh"] == 11.0
    assert contract["quotas"][0]["planned_kwh"] == 3.5
    assert ledger.state.to_dict() == before


def test_plan_hash_changes_with_contract_or_timezone():
    sydney = _snapshot("happy_hour_2026", region="NSW")
    brisbane = resolve_flow_power_plan(
        sydney.selection.to_dict(), timezone_token="Australia/Brisbane",
        legacy_export_rate_dollars=0.45, legacy_happy_hour_end="19:30",
    )
    vic = _snapshot("happy_hour_2026", region="VIC")
    assert len({sydney.plan_hash, brisbane.plan_hash, vic.plan_hash}) == 3
