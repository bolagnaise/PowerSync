"""Regression coverage for capped custom-tariff import allowances."""

from __future__ import annotations

import ast
from datetime import datetime, timedelta, timezone
import importlib.util
from pathlib import Path
import sys
import textwrap
import types
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
COMPONENT_ROOT = ROOT / "custom_components" / "power_sync"
TEST_PACKAGE = "powersync_tariff_quota_testpkg"
package = types.ModuleType(TEST_PACKAGE)
package.__path__ = [str(COMPONENT_ROOT)]
sys.modules[TEST_PACKAGE] = package

for module_name in ("quota", "tariff_quota", "tariff_templates"):
    qualified = f"{TEST_PACKAGE}.{module_name}"
    spec = importlib.util.spec_from_file_location(
        qualified,
        COMPONENT_ROOT / f"{module_name}.py",
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[qualified] = module
    assert spec and spec.loader
    spec.loader.exec_module(module)

quota = sys.modules[f"{TEST_PACKAGE}.quota"]
tariff_quota = sys.modules[f"{TEST_PACKAGE}.tariff_quota"]
templates = sys.modules[f"{TEST_PACKAGE}.tariff_templates"]


def _ergon_tariff() -> dict:
    return templates.TARIFF_TEMPLATES["ergon_solar_sharer_12f_2026"]


def test_ergon_12f_template_has_official_rates_and_allowance() -> None:
    tariff = _ergon_tariff()
    rates = tariff["energy_charges"]["All Year"]
    allowance = tariff["import_quota"]

    assert tariff["currency"] == "AUD"
    assert tariff["daily_supply_charge"] == pytest.approx(1.77853)
    assert rates == {
        "SUPER_OFF_PEAK": pytest.approx(0.08101),
        "SHOULDER": pytest.approx(0.08101),
        "PEAK": pytest.approx(0.48902),
        "OFF_PEAK": pytest.approx(0.26412),
    }
    assert allowance["windows"] == [["11:00", "14:00"]]
    assert allowance["daily_cap_kwh"] == 24
    assert allowance["base_price_c_per_kwh"] == pytest.approx(8.101)
    assert allowance["bonus_price_c_per_kwh"] == pytest.approx(8.101)
    assert allowance["stop_grid_charging_at_quota"] is True


def test_custom_tariff_rule_rejects_malformed_time_and_caps_bonus() -> None:
    tariff = {
        "name": "Test",
        "import_quota": {
            "daily_cap_kwh": 24,
            "base_price_c_per_kwh": 8,
            "bonus_price_c_per_kwh": 9,
            "windows": [["11:00", "14:00"]],
        },
    }
    rule = tariff_quota.custom_tariff_import_quota_rule(
        tariff,
        default_timezone="Australia/Brisbane",
    )

    assert rule is not None
    assert rule.bonus_price_c_per_kwh == 8
    tariff["import_quota"]["windows"] = [["25:00", "14:00"]]
    assert (
        tariff_quota.custom_tariff_import_quota_rule(
            tariff,
            default_timezone="Australia/Brisbane",
        )
        is None
    )


def test_custom_tariff_hash_changes_when_allowance_changes() -> None:
    tariff = _ergon_tariff()
    rule = tariff_quota.custom_tariff_import_quota_rule(
        tariff,
        default_timezone="UTC",
    )
    assert rule is not None
    original = tariff_quota.custom_tariff_quota_hash(tariff, rule)
    changed = {**tariff, "import_quota": {**tariff["import_quota"], "daily_cap_kwh": 25}}
    changed_rule = tariff_quota.custom_tariff_import_quota_rule(
        changed,
        default_timezone="UTC",
    )
    assert changed_rule is not None

    assert tariff_quota.custom_tariff_quota_hash(changed, changed_rule) != original


def test_custom_tariff_ledger_settles_only_window_import_up_to_cap() -> None:
    tariff = _ergon_tariff()
    rule = tariff_quota.custom_tariff_import_quota_rule(
        tariff,
        default_timezone="UTC",
    )
    assert rule is not None
    ledger = quota.QuotaLedger((rule,))
    aest = timezone(timedelta(hours=10))
    before_window = datetime(2026, 7, 14, 10, 55, tzinfo=aest)

    ledger.observe_cumulative("import", 100, before_window)
    settled = ledger.observe_cumulative(
        "import",
        130,
        before_window + timedelta(hours=3, minutes=10),
    )

    assert ledger.state.confidence == "authoritative"
    assert settled == pytest.approx(24)
    assert ledger.remaining_kwh(
        tariff_quota.CUSTOM_TARIFF_IMPORT_RULE_ID
    ) == 0


def test_optimizer_wires_custom_quota_groups_limits_and_persistence() -> None:
    source = (COMPONENT_ROOT / "optimization" / "coordinator.py").read_text()

    assert "def _apply_custom_tariff_quota_optimizer_inputs(" in source
    assert "def _apply_custom_tariff_quota_grid_charge_limit(" in source
    assert "self._capture_provider_quota_measurements_before_plan()" in source
    assert "import_group_ids=self._last_import_bonus_group_ids" in source
    assert '"provider": "custom_tariff"' in source
    assert "custom_tariff_quota_contract(tariff, rules, ledger)" in source
    assert "self._custom_tariff_export_window_slots(n)" in source

    api_source = (COMPONENT_ROOT / "__init__.py").read_text()
    assert "raw_import_quota = data.get(\"import_quota\")" in api_source
    assert "Import allowance requires a valid HH:MM window" in api_source
    assert "raw_export_quota = data.get(\"export_quota\")" in api_source
    assert "Export allowance requires a valid HH:MM window" in api_source


def test_exhausted_allowance_restores_post_cap_display_price() -> None:
    coordinator_path = COMPONENT_ROOT / "optimization" / "coordinator.py"
    source = coordinator_path.read_text()
    module = ast.parse(source)
    method = next(
        child
        for node in module.body
        if isinstance(node, ast.ClassDef)
        and node.name == "OptimizationCoordinator"
        for child in node.body
        if isinstance(child, ast.FunctionDef)
        and child.name == "_apply_custom_tariff_quota_optimizer_inputs"
    )
    namespace = {
        "CUSTOM_TARIFF_IMPORT_RULE_ID": (
            tariff_quota.CUSTOM_TARIFF_IMPORT_RULE_ID
        ),
        "CUSTOM_TARIFF_EXPORT_RULE_ID": (
            tariff_quota.CUSTOM_TARIFF_EXPORT_RULE_ID
        ),
        "custom_tariff_quota_bonus_series": (
            tariff_quota.custom_tariff_quota_bonus_series
        ),
        "_LOGGER": SimpleNamespace(info=lambda *args, **kwargs: None),
        "dt_util": SimpleNamespace(
            now=lambda: datetime(
                2026,
                7,
                31,
                11,
                0,
                tzinfo=timezone(timedelta(hours=10)),
            )
        ),
        "tariff_datetime": quota.tariff_datetime,
    }
    segment = ast.get_source_segment(source, method)
    assert segment is not None
    exec(textwrap.dedent(segment), namespace)

    tariff = _ergon_tariff()
    rule = tariff_quota.custom_tariff_import_quota_rule(
        tariff,
        default_timezone="Australia/Brisbane",
    )
    assert rule is not None
    state = quota.QuotaLedgerState(
        tariff_day="2026-07-31",
        timezone_token="Australia/Brisbane",
        confidence="authoritative",
        settled_kwh={
            tariff_quota.CUSTOM_TARIFF_IMPORT_RULE_ID: rule.daily_cap_kwh,
        },
    )
    ledger = quota.QuotaLedger((rule,), state)
    timestamps = [
        datetime(
            2026,
            7,
            31,
            11,
            0,
            tzinfo=timezone(timedelta(hours=10)),
        ),
        datetime(
            2026,
            8,
            1,
            11,
            0,
            tzinfo=timezone(timedelta(hours=10)),
        ),
    ]
    coordinator = SimpleNamespace(
        _last_display_import_prices=[0.08101, 0.08101],
        _last_grid_charge_cap_import_prices=[0.08101, 0.08101],
        _ensure_custom_tariff_quota_ledger=lambda now=None: (
            tariff,
            tariff_quota.CustomTariffQuotaRules(import_rule=rule),
            ledger,
            "hash",
        ),
        _price_timestamps=lambda count: timestamps[:count],
    )

    namespace["_apply_custom_tariff_quota_optimizer_inputs"](
        coordinator,
        [0.08101, 0.08101],
        [0.0, 0.0],
    )

    assert coordinator._last_display_import_prices == pytest.approx(
        [0.08101, 0.0]
    )
    assert coordinator._last_grid_charge_cap_import_prices == pytest.approx(
        [0.08101, 0.0]
    )
    assert coordinator._last_zerocharge_bonus_prices == pytest.approx(
        [0.0, 0.08101]
    )
    assert coordinator._last_import_bonus_group_ids == [None, "2026-08-01"]
    assert coordinator._last_import_bonus_caps_by_group == {
        "2026-08-01": pytest.approx(24.0),
    }
    # An import-only tariff stages no export bonus at all.
    assert coordinator._last_export_bonus_group_ids is None
    assert coordinator._last_export_bonus_caps_by_group is None
    assert coordinator._last_zerohero_bonus_prices == [0.0, 0.0]
    assert coordinator._last_zerohero_bonus_cap_kwh == 0.0


# --- premium export allowance ------------------------------------------------

AEST = timezone(timedelta(hours=10))


def _coordinator_method(name: str, namespace: dict | None = None):
    """Compile one OptimizationCoordinator method without importing HA."""
    source = (COMPONENT_ROOT / "optimization" / "coordinator.py").read_text()
    module = ast.parse(source)
    method = next(
        child
        for node in module.body
        if isinstance(node, ast.ClassDef) and node.name == "OptimizationCoordinator"
        for child in node.body
        if isinstance(child, ast.FunctionDef) and child.name == name
    )
    scope = {
        "CUSTOM_TARIFF_IMPORT_RULE_ID": tariff_quota.CUSTOM_TARIFF_IMPORT_RULE_ID,
        "CUSTOM_TARIFF_EXPORT_RULE_ID": tariff_quota.CUSTOM_TARIFF_EXPORT_RULE_ID,
        "custom_tariff_quota_bonus_series": tariff_quota.custom_tariff_quota_bonus_series,
        "_LOGGER": SimpleNamespace(info=lambda *args, **kwargs: None),
        "dt_util": SimpleNamespace(now=lambda: datetime(2026, 7, 31, 17, 0, tzinfo=AEST)),
        "tariff_datetime": quota.tariff_datetime,
        "math": __import__("math"),
        "Any": object,
        "datetime": datetime,
        **(namespace or {}),
    }
    exec(textwrap.dedent(ast.get_source_segment(source, method)), scope)
    return scope[name]


def _export_tariff(**overrides) -> dict:
    """Ergon import allowance plus a 12c premium on the first 10 kWh 17-21."""
    tariff = dict(_ergon_tariff())
    tariff["export_quota"] = {
        "enabled": True,
        "name": "Evening premium",
        "timezone_token": "Australia/Brisbane",
        "windows": [["17:00", "21:00"]],
        "daily_cap_kwh": 10.0,
        "base_price_c_per_kwh": 4.0,
        "bonus_price_c_per_kwh": 8.0,
        **overrides,
    }
    return tariff


def _legacy_import_only_hash(tariff: dict, rule) -> str:
    """The hash an import-only tariff had before export allowances existed."""
    import hashlib
    import json

    payload = {
        "name": tariff.get("name"),
        "utility": tariff.get("utility"),
        "rule": {
            "timezone_token": rule.timezone_token,
            "windows": rule.windows,
            "daily_cap_kwh": rule.daily_cap_kwh,
            "base_price_c_per_kwh": rule.base_price_c_per_kwh,
            "bonus_price_c_per_kwh": rule.bonus_price_c_per_kwh,
        },
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def test_export_quota_rule_validates_shape_and_shares_the_import_clock() -> None:
    tariff = _export_tariff()
    rules = tariff_quota.custom_tariff_quota_rules(tariff, default_timezone="UTC")
    assert rules is not None
    export_rule = rules.export_rule
    assert export_rule is not None
    assert export_rule.rule_id == tariff_quota.CUSTOM_TARIFF_EXPORT_RULE_ID
    assert export_rule.direction == "export"
    assert export_rule.windows == (("17:00", "21:00"),)
    assert export_rule.effective_price_c_per_kwh(True) == pytest.approx(12.0)
    assert export_rule.effective_price_c_per_kwh(False) == pytest.approx(4.0)
    assert [rule.rule_id for rule in rules.rules] == [
        tariff_quota.CUSTOM_TARIFF_IMPORT_RULE_ID,
        tariff_quota.CUSTOM_TARIFF_EXPORT_RULE_ID,
    ]

    for bad in (
        {"bonus_price_c_per_kwh": None},  # a premium is required
        {"bonus_price_c_per_kwh": 0},
        {"daily_cap_kwh": 0},
        {"base_price_c_per_kwh": -1},
        {"windows": [["17:00", "25:00"]]},
        {"enabled": False},
    ):
        broken = _export_tariff(**bad)
        if bad.get("bonus_price_c_per_kwh", 1) is None:
            broken["export_quota"].pop("bonus_price_c_per_kwh")
        assert (
            tariff_quota.custom_tariff_export_quota_rule(broken, default_timezone="UTC")
            is None
        ), bad

    # One ledger keeps one tariff day: a different export clock is rejected.
    other_clock = tariff_quota.custom_tariff_quota_rules(
        _export_tariff(timezone_token="Australia/Sydney"),
        default_timezone="UTC",
    )
    assert other_clock is not None
    assert other_clock.import_rule is not None and other_clock.export_rule is None

    export_only = {k: v for k, v in _export_tariff().items() if k != "import_quota"}
    rules = tariff_quota.custom_tariff_quota_rules(export_only, default_timezone="UTC")
    assert rules is not None and rules.import_rule is None
    assert rules.timezone_token == "Australia/Brisbane"


def test_import_only_tariff_keeps_its_hash_ledger_and_restore() -> None:
    tariff = _ergon_tariff()
    rules = tariff_quota.custom_tariff_quota_rules(tariff, default_timezone="UTC")
    assert rules is not None and rules.export_rule is None
    assert len(rules.rules) == 1

    content_hash = tariff_quota.custom_tariff_quota_hash(
        tariff, rules.import_rule, rules.export_rule
    )
    assert content_hash == _legacy_import_only_hash(tariff, rules.import_rule)
    assert content_hash == tariff_quota.custom_tariff_quota_hash(tariff, rules.import_rule)
    with_export = _export_tariff()
    export_rules = tariff_quota.custom_tariff_quota_rules(with_export, default_timezone="UTC")
    assert tariff_quota.custom_tariff_quota_hash(
        with_export, export_rules.import_rule, export_rules.export_rule
    ) != content_hash, "adding an export allowance starts a new settlement contract"

    # A state saved by an import-only ledger restores unchanged.
    ledger = quota.QuotaLedger(rules.rules)
    start = datetime(2026, 7, 14, 10, 55, tzinfo=AEST)
    ledger.observe_cumulative("import", 100, start)
    ledger.observe_cumulative("import", 110, start + timedelta(hours=2))
    saved = ledger.state.to_dict()
    restored = quota.QuotaLedger(rules.rules, quota.QuotaLedgerState.from_dict(saved))
    assert restored.state.to_dict() == saved
    assert restored.remaining_kwh(tariff_quota.CUSTOM_TARIFF_IMPORT_RULE_ID) == pytest.approx(
        ledger.remaining_kwh(tariff_quota.CUSTOM_TARIFF_IMPORT_RULE_ID)
    )
    assert saved["last_sample_at"]["export"] is None

    contract = tariff_quota.custom_tariff_quota_contract(tariff, rules, ledger)
    assert set(contract["quotas"]) == {"import"}
    assert set(contract["prices"]) == {"import"}


def test_shared_ledger_settles_export_window_up_to_cap() -> None:
    rules = tariff_quota.custom_tariff_quota_rules(_export_tariff(), default_timezone="UTC")
    ledger = quota.QuotaLedger(rules.rules)
    midnight = datetime(2026, 7, 14, 0, 2, tzinfo=AEST)
    ledger.observe_cumulative("import", 0, midnight)
    assert ledger.state.confidence == "unknown", "both directions need a baseline"
    ledger.observe_cumulative("export", 0, midnight)
    assert ledger.state.confidence == "authoritative"

    # 1 kWh exported before the window earns nothing.
    assert ledger.observe_cumulative(
        "export", 1, datetime(2026, 7, 14, 16, 30, tzinfo=AEST)
    ) == 0
    # 6 kWh exported 16:30-17:30 puts half inside the window.
    settled = ledger.observe_cumulative("export", 7, datetime(2026, 7, 14, 17, 30, tzinfo=AEST))
    assert settled == pytest.approx(3.0)
    # A further 12 kWh in the window only fills the remaining 7 kWh.
    settled = ledger.observe_cumulative("export", 19, datetime(2026, 7, 14, 19, 30, tzinfo=AEST))
    assert settled == pytest.approx(7.0)
    assert ledger.remaining_kwh(tariff_quota.CUSTOM_TARIFF_EXPORT_RULE_ID) == 0
    assert ledger.remaining_kwh(tariff_quota.CUSTOM_TARIFF_IMPORT_RULE_ID) == pytest.approx(24.0)

    contract = tariff_quota.custom_tariff_quota_contract(_export_tariff(), rules, ledger)
    assert contract["quotas"]["export"]["settled_kwh"] == pytest.approx(10.0)
    assert contract["quotas"]["export"]["remaining_kwh"] == 0
    assert contract["quotas"]["export"]["bonus_c_per_kwh"] == pytest.approx(8.0)
    assert contract["prices"]["export"] == {
        "base_c_per_kwh": 4.0,
        "effective_c_per_kwh": 4.0,
    }, "a spent allowance falls back to the window rate"

    # The next tariff day starts a fresh allowance for both directions.
    ledger.advance_to(datetime(2026, 7, 15, 0, 1, tzinfo=AEST))
    assert ledger.remaining_kwh(tariff_quota.CUSTOM_TARIFF_EXPORT_RULE_ID) == pytest.approx(10.0)


@pytest.mark.parametrize("with_export", [False, True])
def test_coordinator_settles_export_from_daily_total_or_power(with_export: bool) -> None:
    tariff = _export_tariff() if with_export else _ergon_tariff()
    rules = tariff_quota.custom_tariff_quota_rules(tariff, default_timezone="UTC")
    ledger = quota.QuotaLedger(rules.rules)
    summary = {"grid_import_today_kwh": 1.0, "grid_export_today_kwh": 2.0}
    energy = {"energy_summary": summary}
    coordinator = SimpleNamespace(
        _ensure_custom_tariff_quota_ledger=lambda now=None: (tariff, rules, ledger, "hash"),
        _get_energy_data=lambda: energy,
        _energy_summary_total_kwh=_coordinator_method("_energy_summary_total_kwh"),
        _schedule_cost_save=lambda: None,
    )
    settle = _coordinator_method("_settle_custom_tariff_quota_measurements")

    settle(coordinator, datetime(2026, 7, 14, 0, 2, tzinfo=AEST), 0.0, 0.0)
    summary.update(grid_import_today_kwh=1.5, grid_export_today_kwh=3.0)
    settle(coordinator, datetime(2026, 7, 14, 17, 0, tzinfo=AEST), 0.0, 0.0)
    summary["grid_export_today_kwh"] = 7.0
    delta = settle(coordinator, datetime(2026, 7, 14, 18, 0, tzinfo=AEST), 0.0, 4.0)

    if not with_export:
        assert delta == {"import": 0.0, "export": 0.0}
        assert ledger.state.last_sample_at["export"] is None, "import-only ledgers never sample export"
        assert ledger.state.source_kind["export"] is None
        assert ledger.state.confidence == "authoritative"
        return

    assert delta["export"] == pytest.approx(4.0)
    assert ledger.state.source_kind["export"] == "total_increasing"
    assert ledger.state.confidence == "authoritative"

    # Without a daily total the export allowance integrates grid power.
    energy.pop("energy_summary")
    settle(coordinator, datetime(2026, 7, 14, 18, 5, tzinfo=AEST), 0.0, 3.0)
    delta = settle(coordinator, datetime(2026, 7, 14, 18, 10, tzinfo=AEST), 0.0, 3.0)
    assert delta["export"] == pytest.approx(3.0 * 5 / 60)
    assert ledger.state.source_kind["export"] == "power_integrated"
    # Import's last sample is over the continuity limit old, so the switch to
    # integrated power fails closed and withholds the bonuses.
    assert ledger.state.confidence == "unknown"
    assert ledger.state.reason == "power telemetry gap"


def test_optimizer_inputs_price_the_premium_export_allowance() -> None:
    tariff = _export_tariff()
    rules = tariff_quota.custom_tariff_quota_rules(tariff, default_timezone="UTC")
    state = quota.QuotaLedgerState(
        tariff_day="2026-07-31",
        timezone_token="Australia/Brisbane",
        confidence="authoritative",
        settled_kwh={
            tariff_quota.CUSTOM_TARIFF_IMPORT_RULE_ID: 24.0,
            tariff_quota.CUSTOM_TARIFF_EXPORT_RULE_ID: 7.5,
        },
    )
    ledger = quota.QuotaLedger(rules.rules, state)
    timestamps = [
        datetime(2026, 7, 31, 16, 0, tzinfo=AEST),  # before the window
        datetime(2026, 7, 31, 17, 0, tzinfo=AEST),  # today, 2.5 kWh left
        datetime(2026, 7, 31, 21, 0, tzinfo=AEST),  # window end is exclusive
        datetime(2026, 8, 1, 18, 0, tzinfo=AEST),  # tomorrow, full 10 kWh
    ]
    coordinator = SimpleNamespace(
        _last_display_import_prices=[0.26412] * 4,
        _last_display_export_prices=[0.04] * 4,
        _last_grid_charge_cap_import_prices=[0.26412] * 4,
        _ensure_custom_tariff_quota_ledger=lambda now=None: (tariff, rules, ledger, "hash"),
        _price_timestamps=lambda count: timestamps[:count],
    )

    _coordinator_method("_apply_custom_tariff_quota_optimizer_inputs")(
        coordinator,
        [0.26412] * 4,
        [0.04] * 4,
    )

    assert coordinator._last_zerohero_bonus_prices == pytest.approx([0.0, 0.08, 0.0, 0.08])
    assert coordinator._last_export_bonus_group_ids == [None, "2026-07-31", None, "2026-08-01"]
    assert coordinator._last_export_bonus_caps_by_group == {
        "2026-07-31": pytest.approx(2.5),
        "2026-08-01": pytest.approx(10.0),
    }
    assert coordinator._last_zerohero_bonus_cap_kwh == pytest.approx(12.5)
    assert coordinator._last_display_export_prices == pytest.approx([0.04, 0.12, 0.04, 0.12])
    # The spent import allowance still prices only tomorrow's window.
    assert coordinator._last_import_bonus_group_ids == [None, None, None, None]
    assert coordinator._last_zerocharge_bonus_prices == [0.0] * 4

    # Unknown settlement confidence withholds every bonus.
    ledger.state.confidence = "unknown"
    _coordinator_method("_apply_custom_tariff_quota_optimizer_inputs")(
        coordinator,
        [0.26412] * 4,
        [0.04] * 4,
    )
    assert coordinator._last_zerohero_bonus_prices == [0.0] * 4
    assert coordinator._last_export_bonus_group_ids is None


def test_export_earnings_pay_the_premium_only_on_settled_energy() -> None:
    rule = tariff_quota.custom_tariff_export_quota_rule(_export_tariff(), default_timezone="UTC")
    inside = datetime(2026, 7, 31, 18, 0, tzinfo=AEST)
    outside = datetime(2026, 7, 31, 22, 0, tzinfo=AEST)

    # 0.5 kWh exported, 0.2 kWh still within the cap: 0.5 x 4c + 0.2 x 8c.
    assert tariff_quota.custom_tariff_export_earnings(
        rule, observed_at=inside, export_kwh=0.5, eligible_kwh=0.2, tariff_export_price=0.12,
    ) == pytest.approx(0.5 * 0.04 + 0.2 * 0.08)
    # Outside the window the tariff's own rate applies.
    assert tariff_quota.custom_tariff_export_earnings(
        rule, observed_at=outside, export_kwh=0.5, eligible_kwh=0.0, tariff_export_price=0.02,
    ) == pytest.approx(0.01)

    source = (COMPONENT_ROOT / "optimization" / "coordinator.py").read_text()
    assert "actual_export_earnings = custom_tariff_export_earnings(" in source
