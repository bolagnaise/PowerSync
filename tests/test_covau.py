from datetime import datetime, timedelta, timezone
import asyncio
import ast
import importlib.util
import json
from pathlib import Path
import sys
import types

import pytest

ROOT = Path(__file__).resolve().parents[1]
TEST_PACKAGE = "powersync_covau_testpkg"
package = types.ModuleType(TEST_PACKAGE)
package.__path__ = [str(ROOT / "custom_components" / "power_sync")]
sys.modules[TEST_PACKAGE] = package

for module_name in ("quota", "covau"):
    qualified = f"{TEST_PACKAGE}.{module_name}"
    spec = importlib.util.spec_from_file_location(
        qualified, ROOT / "custom_components" / "power_sync" / f"{module_name}.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[qualified] = module
    assert spec and spec.loader
    spec.loader.exec_module(module)

covau = sys.modules[f"{TEST_PACKAGE}.covau"]
quota = sys.modules[f"{TEST_PACKAGE}.quota"]
COVAU_EXPORT_RULE_ID = covau.COVAU_EXPORT_RULE_ID
COVAU_IMPORT_RULE_ID = covau.COVAU_IMPORT_RULE_ID
CovaUQuotaRuntime = covau.CovaUQuotaRuntime
SUPPORTED_SOLARMAX_PLANS = covau.SUPPORTED_SOLARMAX_PLANS
covau_plan_candidates = covau.covau_plan_candidates
covau_price_series = covau.covau_price_series
covau_quota_rules = covau.covau_quota_rules
import_price_c_per_kwh = covau.import_price_c_per_kwh
normalize_covau_plan = covau.normalize_covau_plan
QuotaLedger = quota.QuotaLedger
QuotaLedgerState = quota.QuotaLedgerState


PLAN_RATES = {
    "COV1117610MRE2@EME": ("1.3818", "0.5581", "0.2547"),
    "COV1117611MRE2@EME": ("1.5272", "0.421", "0.3503"),
    "COV1117612MRE2@EME": ("2.0909", "0.4121", "0.2498"),
    "COV1117614MRE2@EME": ("1.3818", "0.4505", "0.2446"),
    "COV1117616MRE2@EME": ("1.5636", "0.5344", "0.3197"),
}


def _raw(plan_id: str) -> dict:
    supply, peak, offpeak = PLAN_RATES[plan_id]
    metadata = SUPPORTED_SOLARMAX_PLANS[plan_id]
    return {
        "data": {
            "planId": plan_id,
            "displayName": metadata["display_name"],
            "effectiveFrom": "2026-07-01T00:00:00.000Z",
            "lastUpdated": "2026-06-30T14:06:51.584Z",
            "electricityContract": {
                "timeZone": "AEST",
                "tariffPeriod": [{
                    "dailySupplyCharge": supply,
                    "timeOfUseRates": [
                        {"displayName": "Peak", "rates": [{"unitPrice": peak}], "timeOfUse": [{"startTime": "15:00", "endTime": "20:59"}]},
                        {"displayName": "Off-Peak", "rates": [{"unitPrice": offpeak}], "timeOfUse": [
                            {"startTime": "06:00", "endTime": "10:59"},
                            {"startTime": "14:00", "endTime": "14:59"},
                            {"startTime": "21:00", "endTime": "23:59"},
                        ]},
                        {"displayName": "EV Off-Peak", "rates": [{"unitPrice": "0.15"}], "timeOfUse": [{"startTime": "00:00", "endTime": "05:59"}]},
                        {"displayName": "Free Usage", "rates": [{"volume": 50, "unitPrice": "0.00"}, {"unitPrice": offpeak}], "timeOfUse": [{"startTime": "11:00", "endTime": "13:59"}]},
                    ],
                }],
                "solarFeedInTariff": [{"timeVaryingTariffs": [
                    {"rates": [{"volume": 30, "unitPrice": "0.15"}], "timeVariations": [{"startTime": "18:00", "endTime": "20:59"}]},
                    {"rates": [{"unitPrice": "0.05"}], "timeVariations": [{"startTime": "18:00", "endTime": "20:59"}]},
                    {"rates": [{"unitPrice": "0.05"}], "timeVariations": [{"startTime": "21:00", "endTime": "17:59"}]},
                ]}],
            },
        }
    }


@pytest.mark.parametrize("plan_id", PLAN_RATES)
def test_all_supported_solarmax_fixtures_normalize_gst_and_quotas(plan_id: str) -> None:
    snapshot = normalize_covau_plan(_raw(plan_id), plan_id)
    supply, peak, offpeak = (float(value) for value in PLAN_RATES[plan_id])
    assert snapshot.supply_c_per_day == pytest.approx(supply * 110)
    assert snapshot.import_periods[0].c_per_kwh == pytest.approx(peak * 110)
    assert any(period.c_per_kwh == pytest.approx(offpeak * 110) for period in snapshot.import_periods)
    assert snapshot.export_base_c_per_kwh == 5
    assert snapshot.premium_export_total_c_per_kwh == 15
    assert snapshot.free_import_cap_kwh == 50
    assert snapshot.premium_export_cap_kwh == 30
    assert len(snapshot.content_hash) == 64


def test_sa_fixed_aest_boundaries_ignore_adelaide_dst() -> None:
    snapshot = normalize_covau_plan(_raw("COV1117616MRE2@EME"), "COV1117616MRE2@EME")
    # 01:00 UTC is 11:00 fixed AEST, even while Adelaide observes UTC+10:30.
    at_start = datetime(2026, 1, 15, 1, 0, tzinfo=timezone.utc)
    before_start = at_start - timedelta(minutes=1)
    assert import_price_c_per_kwh(snapshot, before_start) == pytest.approx(35.167)
    assert import_price_c_per_kwh(snapshot, at_start) == pytest.approx(35.167)
    rules = covau_quota_rules(snapshot)
    assert not rules[0].contains(before_start)
    assert rules[0].contains(at_start)


def test_unknown_confidence_disables_bonuses_without_changing_base_prices() -> None:
    snapshot = normalize_covau_plan(_raw("COV1117616MRE2@EME"), "COV1117616MRE2@EME")
    ledger = QuotaLedger(covau_quota_rules(snapshot), QuotaLedgerState(confidence="unknown"))
    ts = [datetime(2026, 7, 14, 11, 0, tzinfo=timezone(timedelta(hours=10)))]
    imports, exports, import_bonus, export_bonus, import_cap, export_cap = covau_price_series(snapshot, ts, ledger)
    assert imports == [pytest.approx(0.35167)]
    assert exports == [0.05]
    assert import_bonus == [0]
    assert export_bonus == [0]
    assert import_cap == 0
    assert export_cap == 0


def test_postcode_filters_but_keeps_distributor_confirmation_candidates() -> None:
    assert {item["distributor"] for item in covau_plan_candidates("5000")} == {"SA Power Networks"}
    assert len(covau_plan_candidates("2150")) == 3
    assert covau_plan_candidates("3000") == []
    assert len(covau_plan_candidates(None)) == 5
    assert {rule.rule_id for rule in covau_quota_rules(normalize_covau_plan(_raw("COV1117616MRE2@EME"), "COV1117616MRE2@EME"))} == {COVAU_IMPORT_RULE_ID, COVAU_EXPORT_RULE_ID}


def test_standalone_runtime_uses_current_read_time_for_unchanged_new_day_totals() -> None:
    snapshot = normalize_covau_plan(
        _raw("COV1117616MRE2@EME"),
        "COV1117616MRE2@EME",
    )

    class State:
        def __init__(self, value):
            self.state = value
            self.attributes = {"unit_of_measurement": "kWh"}

    class States:
        values = {
            "sensor.import_energy": State("100"),
            "sensor.export_energy": State("20"),
        }

        def get(self, entity_id):
            return self.values.get(entity_id)

    hass = types.SimpleNamespace(states=States())
    entry = types.SimpleNamespace(entry_id="entry")
    runtime = CovaUQuotaRuntime(
        hass,
        entry,
        snapshot,
        grid_power_kw_getter=lambda: None,
        import_energy_entity="sensor.import_energy",
        export_energy_entity="sensor.export_energy",
    )
    midday = datetime(2026, 7, 14, 12, 0, tzinfo=timezone(timedelta(hours=10)))
    asyncio.run(runtime.async_sample(now=midday))
    assert runtime.ledger.state.confidence == "unknown"

    # Neither total changes, but polling the entities after fixed-AEST
    # midnight establishes an authoritative baseline for the new tariff day.
    reset = datetime(2026, 7, 15, 0, 1, tzinfo=timezone(timedelta(hours=10)))
    asyncio.run(runtime.async_sample(now=reset))
    assert runtime.ledger.state.tariff_day == "2026-07-15"
    assert runtime.ledger.state.confidence == "authoritative"


def test_covau_startup_contract_and_translations_are_wired() -> None:
    integration_source = (
        ROOT / "custom_components" / "power_sync" / "__init__.py"
    ).read_text(encoding="utf-8")
    assert 'expected_title = "PowerSync CovaU SolarMax"' in integration_source
    assert '"covau",\n    )' in integration_source
    assert "_sync_covau_withdrawn_plan_issue" in integration_source

    strings = json.loads(
        (ROOT / "custom_components" / "power_sync" / "strings.json").read_text()
    )
    english = json.loads(
        (
            ROOT
            / "custom_components"
            / "power_sync"
            / "translations"
            / "en.json"
        ).read_text()
    )
    for payload in (strings, english):
        assert payload["config"]["step"]["covau_postcode"]
        assert payload["config"]["step"]["covau_plan"]
        assert payload["config"]["step"]["covau_manual_tariff"]
        assert payload["options"]["step"]["covau_options"]
        assert (
            payload["options"]["step"]["covau_options"]["data"]
            ["covau_refresh_public_plan"]
        )
        assert payload["issues"]["covau_plan_withdrawn"]

    config_flow_source = (
        ROOT / "custom_components" / "power_sync" / "config_flow.py"
    ).read_text(encoding="utf-8")
    assert 'refresh_key = "covau_refresh_public_plan"' in config_flow_source
    assert "not keeping_cached_manual" in config_flow_source


def test_covau_mobile_provider_contract_is_read_only_at_the_correct_endpoint() -> None:
    source = (
        ROOT / "custom_components" / "power_sync" / "__init__.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(source)
    methods: dict[tuple[str, str], str] = {}
    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        for child in node.body:
            if isinstance(child, ast.AsyncFunctionDef):
                methods[(node.name, child.name)] = (
                    ast.get_source_segment(source, child) or ""
                )

    marker = "CovaU plan snapshots and settlement meters are read-only here"
    assert marker in methods[("ProviderConfigView", "post")]
    assert marker not in methods[("ProviderConfigView", "get")]
    assert marker not in methods[("PowerwallTypeView", "get")]
    assert marker not in methods[("BatteryHealthView", "post")]
