from datetime import datetime, timedelta, timezone
import asyncio
import ast
import importlib.util
import json
from pathlib import Path
import sys
import types
from zoneinfo import ZoneInfo

import pytest
import voluptuous as vol

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
COVAU_PARSER_VERSION = covau.COVAU_PARSER_VERSION
CovaUPlanSnapshot = covau.CovaUPlanSnapshot
CovaUQuotaRuntime = covau.CovaUQuotaRuntime
SUPPORTED_SOLARMAX_PLANS = covau.SUPPORTED_SOLARMAX_PLANS
covau_plan_candidates = covau.covau_plan_candidates
covau_price_series = covau.covau_price_series
covau_provider_contract = covau.covau_provider_contract
covau_quota_rules = covau.covau_quota_rules
covau_tariff_schedule = covau.covau_tariff_schedule
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
    supply, peak, offpeak = PLAN_RATES[plan_id.replace("MRE3@", "MRE2@")]
    metadata = SUPPORTED_SOLARMAX_PLANS[covau.RETIRED_SOLARMAX_PLAN_IDS.get(plan_id, plan_id)]
    return {
        "data": {
            "planId": plan_id,
            "displayName": metadata["display_name"],
            "effectiveFrom": (
                "2026-10-01T00:00:00.000Z" if "MRE3@" in plan_id else "2026-07-01T00:00:00.000Z"
            ),
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


@pytest.mark.parametrize("plan_id", [*PLAN_RATES, *(plan_id.replace("MRE2@", "MRE3@") for plan_id in PLAN_RATES)])
def test_all_supported_solarmax_fixtures_normalize_gst_and_quotas(plan_id: str) -> None:
    snapshot = normalize_covau_plan(_raw(plan_id), plan_id)
    supply, peak, offpeak = (float(value) for value in PLAN_RATES[plan_id.replace("MRE3@", "MRE2@")])
    assert snapshot.plan_id == plan_id
    assert snapshot.supply_c_per_day == pytest.approx(supply * 110)
    assert snapshot.import_periods[0].c_per_kwh == pytest.approx(peak * 110)
    assert any(period.c_per_kwh == pytest.approx(offpeak * 110) for period in snapshot.import_periods)
    assert snapshot.export_base_c_per_kwh == 5
    assert snapshot.premium_export_total_c_per_kwh == 15
    assert snapshot.free_import_cap_kwh == 50
    assert snapshot.premium_export_cap_kwh == 30
    assert len(snapshot.content_hash) == 64


@pytest.mark.parametrize(
    "utc_start",
    [
        (2026, 1, 15, 0, 30),
        (2026, 7, 15, 1, 30),
    ],
)
def test_sa_boundaries_follow_adelaide_local_time(
    utc_start: tuple[int, int, int, int, int],
) -> None:
    snapshot = normalize_covau_plan(
        _raw("COV1117616MRE2@EME"),
        "COV1117616MRE2@EME",
        timezone_token="Australia/Adelaide",
    )
    at_start = datetime(*utc_start, tzinfo=timezone.utc)
    before_start = at_start - timedelta(minutes=1)
    assert import_price_c_per_kwh(snapshot, before_start) == pytest.approx(35.167)
    assert import_price_c_per_kwh(snapshot, at_start) == pytest.approx(35.167)
    rules = covau_quota_rules(snapshot)
    assert not rules[0].contains(before_start)
    assert rules[0].contains(at_start)
    assert snapshot.timezone_token == "Australia/Adelaide"
    assert rules[0].timezone_token == "Australia/Adelaide"


@pytest.mark.parametrize(
    ("plan_id", "timezone_name", "utc_start"),
    [
        ("COV1117610MRE2@EME", "Australia/Sydney", (2026, 1, 15, 0, 0)),
        ("COV1117614MRE2@EME", "Australia/Brisbane", (2026, 1, 15, 1, 0)),
    ],
)
def test_solarmax_windows_follow_each_home_assistant_timezone(
    plan_id: str,
    timezone_name: str,
    utc_start: tuple[int, int, int, int, int],
) -> None:
    snapshot = normalize_covau_plan(
        _raw(plan_id),
        plan_id,
        timezone_token=timezone_name,
    )
    at_start = datetime(*utc_start, tzinfo=timezone.utc)
    rule = covau_quota_rules(snapshot)[0]
    assert not rule.contains(at_start - timedelta(minutes=1))
    assert rule.contains(at_start)


def test_saved_fixed_aest_snapshot_migrates_and_invalidates_quota_state() -> None:
    original = normalize_covau_plan(
        _raw("COV1117616MRE2@EME"),
        "COV1117616MRE2@EME",
        timezone_token="Australia/Adelaide",
    ).to_dict()
    original["parser_version"] = 1
    original["timezone_token"] = "AEST"
    original["content_hash"] = "legacy-content-hash"

    migrated = CovaUPlanSnapshot.from_dict(
        original,
        timezone_token="Australia/Adelaide",
    )

    assert migrated.parser_version == COVAU_PARSER_VERSION
    assert migrated.timezone_token == "Australia/Adelaide"
    assert migrated.content_hash != "legacy-content-hash"


def test_corrected_local_cdr_timezone_is_accepted() -> None:
    raw = _raw("COV1117616MRE2@EME")
    raw["data"]["electricityContract"]["timeZone"] = "LOCAL"

    snapshot = normalize_covau_plan(
        raw,
        "COV1117616MRE2@EME",
        timezone_token="Australia/Adelaide",
    )

    assert snapshot.timezone_token == "Australia/Adelaide"


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


def test_provider_contract_current_period_tracks_effective_price_at_window_boundary() -> None:
    snapshot = normalize_covau_plan(_raw("COV1117614MRE2@EME"), "COV1117614MRE2@EME")
    ledger = QuotaLedger(
        covau_quota_rules(snapshot),
        QuotaLedgerState(
            tariff_day="2026-07-16",
            confidence="authoritative",
        ),
    )
    aest = timezone(timedelta(hours=10))

    free_import = covau_provider_contract(
        snapshot,
        ledger,
        now=datetime(2026, 7, 16, 13, 59, tzinfo=aest),
    )
    after_free_import = covau_provider_contract(
        snapshot,
        ledger,
        now=datetime(2026, 7, 16, 14, 0, tzinfo=aest),
    )
    premium_export = covau_provider_contract(
        snapshot,
        ledger,
        now=datetime(2026, 7, 16, 18, 0, tzinfo=aest),
    )

    assert free_import["prices"]["import"]["period"] == COVAU_IMPORT_RULE_ID
    assert after_free_import["prices"]["import"]["period"] == "covau_base_import"
    assert premium_export["prices"]["export"]["period"] == COVAU_EXPORT_RULE_ID

    unknown_ledger = QuotaLedger(
        covau_quota_rules(snapshot),
        QuotaLedgerState(
            tariff_day="2026-07-16",
            confidence="unknown",
        ),
    )
    conservative = covau_provider_contract(
        snapshot,
        unknown_ledger,
        now=datetime(2026, 7, 16, 13, 0, tzinfo=aest),
    )
    assert conservative["prices"]["import"]["period"] == "covau_base_import"


def test_provider_contract_exposes_quota_aware_dashboard_tariff_schedule() -> None:
    snapshot = normalize_covau_plan(
        _raw("COV1117614MRE2@EME"),
        "COV1117614MRE2@EME",
        timezone_token="Australia/Brisbane",
    )
    ledger = QuotaLedger(
        covau_quota_rules(snapshot),
        QuotaLedgerState(
            tariff_day="2026-07-16",
            confidence="authoritative",
        ),
    )
    contract = covau_provider_contract(
        snapshot,
        ledger,
        now=datetime(2026, 7, 16, 18, 0, tzinfo=ZoneInfo("Australia/Brisbane")),
    )

    schedule = contract["tariff_schedule"]
    assert schedule["currency"] == "AUD"
    assert schedule["utility"] == "CovaU"
    assert schedule["plan_name"] == snapshot.display_name
    assert schedule["price_source"] == "covau_aer_cdr"
    assert len(schedule["buy_prices"]) == 48
    assert len(schedule["sell_prices"]) == 48
    assert schedule["buy_prices"]["PERIOD_11_00"] == 0.0
    assert schedule["sell_prices"]["PERIOD_17_30"] == 0.05
    assert schedule["sell_prices"]["PERIOD_18_00"] == 0.15


def test_dashboard_tariff_schedule_explains_unavailable_bonuses() -> None:
    snapshot = normalize_covau_plan(
        _raw("COV1117614MRE2@EME"),
        "COV1117614MRE2@EME",
        timezone_token="Australia/Brisbane",
    )
    ledger = QuotaLedger(
        covau_quota_rules(snapshot),
        QuotaLedgerState(
            tariff_day="2026-07-16",
            confidence="unknown",
        ),
    )

    schedule = covau_tariff_schedule(
        snapshot,
        ledger,
        now=datetime(2026, 7, 16, 18, 0, tzinfo=ZoneInfo("Australia/Brisbane")),
    )

    assert schedule["buy_prices"]["PERIOD_11_00"] > 0.0
    assert schedule["sell_prices"]["PERIOD_18_00"] == 0.05
    assert schedule["unavailable_quota_windows"] == {
        "import": {"start": "11:00", "end": "14:00", "reason": "unverified"},
        "export": {"start": "18:00", "end": "21:00", "reason": "unverified"},
    }

    exhausted = QuotaLedger(
        covau_quota_rules(snapshot),
        QuotaLedgerState(
            tariff_day="2026-07-16",
            confidence="authoritative",
            settled_kwh={
                COVAU_IMPORT_RULE_ID: snapshot.free_import_cap_kwh,
                COVAU_EXPORT_RULE_ID: snapshot.premium_export_cap_kwh,
            },
        ),
    )
    exhausted_schedule = covau_tariff_schedule(
        snapshot,
        exhausted,
        now=datetime(2026, 7, 16, 18, 0, tzinfo=ZoneInfo("Australia/Brisbane")),
    )
    assert exhausted_schedule["buy_prices"]["PERIOD_11_00"] > 0.0
    assert exhausted_schedule["sell_prices"]["PERIOD_18_00"] == 0.05
    assert exhausted_schedule["unavailable_quota_windows"] == {
        "import": {"start": "11:00", "end": "14:00", "reason": "exhausted"},
        "export": {"start": "18:00", "end": "21:00", "reason": "exhausted"},
    }


def test_postcode_filters_but_keeps_distributor_confirmation_candidates() -> None:
    assert covau_plan_candidates("5000") == [
        {"plan_id": "COV1117616MRE3@EME", **SUPPORTED_SOLARMAX_PLANS["COV1117616MRE3@EME"]}
    ]
    assert len(covau_plan_candidates("2150")) == 3
    assert covau_plan_candidates("3000") == []
    assert len(covau_plan_candidates(None)) == 5
    assert all(item["plan_id"].endswith("MRE3@EME") for item in covau_plan_candidates(None))
    assert {rule.rule_id for rule in covau_quota_rules(normalize_covau_plan(_raw("COV1117616MRE2@EME"), "COV1117616MRE2@EME"))} == {COVAU_IMPORT_RULE_ID, COVAU_EXPORT_RULE_ID}


def _covau_flow_methods(*method_names):
    source = ROOT / "custom_components" / "power_sync" / "config_flow.py"
    tree = ast.parse(source.read_text())
    methods = [
        node for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name in method_names
    ]
    prelude = ast.parse("from __future__ import annotations").body
    namespace = {
        name: name.removeprefix("CONF_").lower()
        for node in methods
        for child in ast.walk(node)
        if isinstance(child, ast.Name) and (name := child.id).startswith("CONF_")
    }
    namespace.update(
        vol=vol,
        covau_plan_candidates=covau_plan_candidates,
        SUPPORTED_SOLARMAX_PLANS=SUPPORTED_SOLARMAX_PLANS,
        RETIRED_SOLARMAX_PLAN_IDS=covau.RETIRED_SOLARMAX_PLAN_IDS,
        SelectSelectorMode=types.SimpleNamespace(DROPDOWN="dropdown"),
    )
    for name in (
        "SelectOptionDict", "SelectSelectorConfig", "EntitySelectorConfig",
    ):
        namespace[name] = dict
    for name in ("SelectSelector", "EntitySelector", "BooleanSelector"):
        namespace[name] = lambda *_: lambda value: value
    exec(compile(ast.Module(body=prelude + methods, type_ignores=[]), str(source), "exec"), namespace)
    return namespace


def test_manual_solarmax_rejects_power_sensor_and_keeps_no_meter_fallback():
    methods = _covau_flow_methods("async_step_covau_plan", "_covau_energy_entity_valid")
    power_sensor = types.SimpleNamespace(
        attributes={
            "state_class": "measurement", "device_class": "power",
            "unit_of_measurement": "W",
        },
        state="200",
    )

    async def manual_form():
        return {"step_id": "covau_manual_tariff"}

    flow = types.SimpleNamespace(
        hass=types.SimpleNamespace(states={"sensor.grid_power": power_sensor}),
        async_show_form=lambda **kwargs: kwargs,
        async_step_covau_manual_tariff=manual_form,
        _auto_detect_covau_energy_entity=lambda _direction: "",
    )
    flow._covau_energy_entity_valid = types.MethodType(methods["_covau_energy_entity_valid"], flow)
    plan_step = methods["async_step_covau_plan"]
    invalid = asyncio.run(plan_step(flow, {
        "covau_plan_id": "manual",
        "covau_import_energy_entity": "sensor.grid_power",
        "covau_export_energy_entity": "sensor.grid_power",
    }))
    assert invalid["step_id"] == "covau_plan"
    assert invalid["errors"] == {
        "covau_import_energy_entity": "covau_energy_meter_invalid",
        "covau_export_energy_entity": "covau_energy_meter_invalid",
    }
    assert covau._energy_state_kwh(power_sensor) is None
    assert asyncio.run(plan_step(flow, {"covau_plan_id": "manual"}))["step_id"] == "covau_manual_tariff"


def test_retired_public_plan_can_keep_cached_snapshot_without_substitution():
    plan_id = "COV1117616MRE2@EME"
    raw = _raw(plan_id)
    saved = normalize_covau_plan(raw, plan_id).to_dict()
    current = {
        "covau_plan_id": plan_id,
        "covau_distributor": saved["distributor"],
        "covau_plan_raw": raw,
        "covau_plan_snapshot": saved,
    }
    options_step = _covau_flow_methods("async_step_covau_options")["async_step_covau_options"]
    flow = types.SimpleNamespace(
        _get_option=current.get,
        _save_connection_and_reload=lambda data: data,
        async_show_form=lambda **kwargs: kwargs,
    )
    user_input = {
        "covau_plan_id": plan_id,
        "covau_distributor": saved["distributor"],
    }
    result = asyncio.run(options_step(flow, user_input))
    assert result == {"electricity_provider": "covau", **current, "covau_import_energy_entity": "", "covau_export_energy_entity": ""}
    refresh = asyncio.run(options_step(flow, {**user_input, "covau_refresh_public_plan": True}))
    assert refresh["errors"] == {"covau_plan_id": "covau_unsupported_plan"}


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
    adelaide = ZoneInfo("Australia/Adelaide")
    midday = datetime(2026, 7, 14, 12, 0, tzinfo=adelaide)
    asyncio.run(runtime.async_sample(now=midday))
    assert runtime.ledger.state.confidence == "unknown"

    # Neither total changes, but polling the entities after local
    # midnight establishes an authoritative baseline for the new tariff day.
    reset = datetime(2026, 7, 15, 0, 1, tzinfo=adelaide)
    asyncio.run(runtime.async_sample(now=reset))
    assert runtime.ledger.state.tariff_day == "2026-07-15"
    assert runtime.ledger.state.confidence == "authoritative"


def test_standalone_runtime_recovers_transient_missing_meter_before_quota_windows() -> None:
    snapshot = normalize_covau_plan(
        _raw("COV1117612MRE2@EME"),
        "COV1117612MRE2@EME",
    )

    class State:
        def __init__(self, value):
            self.state = value
            self.attributes = {"unit_of_measurement": "kWh"}

    class States:
        values = {
            "sensor.import_energy": State("100"),
            "sensor.export_energy": None,
        }

        def get(self, entity_id):
            return self.values.get(entity_id)

    states = States()
    hass = types.SimpleNamespace(states=states)
    runtime = CovaUQuotaRuntime(
        hass,
        types.SimpleNamespace(entry_id="entry"),
        snapshot,
        grid_power_kw_getter=lambda: None,
        import_energy_entity="sensor.import_energy",
        export_energy_entity="sensor.export_energy",
    )
    aest = timezone(timedelta(hours=10))

    asyncio.run(
        runtime.async_sample(now=datetime(2026, 7, 18, 8, 34, tzinfo=aest))
    )
    assert runtime.ledger.state.confidence == "unknown"
    assert runtime.ledger.state.reason == "awaiting tariff-day baseline"

    states.values["sensor.export_energy"] = State("1715")
    asyncio.run(
        runtime.async_sample(now=datetime(2026, 7, 18, 10, 49, tzinfo=aest))
    )

    assert runtime.ledger.state.confidence == "authoritative"
    assert runtime.ledger.state.reason is None
    assert runtime.ledger.bucket(COVAU_IMPORT_RULE_ID).effective_price_c_per_kwh == 0


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
    assert "not keeping_cached_snapshot" in config_flow_source


def test_covau_entry_title_wins_over_legacy_aemo_spike_fallback() -> None:
    """An explicit CovaU provider must not be relabelled as legacy GloBird."""
    source = (
        ROOT / "custom_components" / "power_sync" / "__init__.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(source)
    setup = next(
        node
        for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "async_setup_entry"
    )
    title_branch = next(
        node
        for node in setup.body
        if isinstance(node, ast.If)
        and any(
            isinstance(child, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "expected_title"
                for target in child.targets
            )
            for child in ast.walk(node)
        )
    )
    probe = ast.FunctionDef(
        name="resolve_title",
        args=ast.arguments(
            posonlyargs=[],
            args=[
                ast.arg(arg="electricity_provider"),
                ast.arg(arg="has_amber"),
                ast.arg(arg="entry"),
            ],
            kwonlyargs=[],
            kw_defaults=[],
            defaults=[],
        ),
        body=[title_branch, ast.Return(value=ast.Name(id="expected_title", ctx=ast.Load()))],
        decorator_list=[],
    )
    namespace: dict[str, object] = {
        "CONF_AEMO_SPIKE_ENABLED": "aemo_spike_enabled",
    }
    exec(
        compile(
            ast.fix_missing_locations(ast.Module(body=[probe], type_ignores=[])),
            "<title-probe>",
            "exec",
        ),
        namespace,
    )

    entry = types.SimpleNamespace(data={"aemo_spike_enabled": True})
    assert namespace["resolve_title"]("covau", False, entry) == "PowerSync CovaU SolarMax"
    assert namespace["resolve_title"]("amber", False, entry) == "PowerSync Globird"


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
