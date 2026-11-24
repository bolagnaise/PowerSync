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
    assert "custom_tariff_quota_contract(tariff, rule, ledger)" in source

    api_source = (COMPONENT_ROOT / "__init__.py").read_text()
    assert "raw_import_quota = data.get(\"import_quota\")" in api_source
    assert "Import allowance requires a valid HH:MM window" in api_source


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
            rule,
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


def _window_rule():
    rule = tariff_quota.custom_tariff_import_quota_rule(
        {
            "name": "Test",
            "import_quota": {
                "daily_cap_kwh": 24,
                "base_price_c_per_kwh": 8,
                "bonus_price_c_per_kwh": 8,
                "windows": [["11:00", "14:00"]],
            },
        },
        default_timezone="Australia/Brisbane",
    )
    assert rule is not None
    return rule


def test_import_cost_uses_tariff_rate_outside_allowance_window() -> None:
    rule = _window_rule()
    brisbane = timezone(timedelta(hours=10))

    # Outside the window the tariff's own slot rate applies, not the
    # allowance window's base rate.
    evening = tariff_quota.custom_tariff_import_cost(
        rule,
        observed_at=datetime(2026, 7, 31, 18, 0, tzinfo=brisbane),
        import_kwh=2.0,
        eligible_kwh=0.0,
        tariff_import_price=0.35,
    )
    assert evening == pytest.approx(0.70)

    # Inside the window every kWh costs the window rate and the
    # ledger-accepted kWh are discounted.
    midday = tariff_quota.custom_tariff_import_cost(
        rule,
        observed_at=datetime(2026, 7, 31, 12, 0, tzinfo=brisbane),
        import_kwh=2.0,
        eligible_kwh=1.5,
        tariff_import_price=0.35,
    )
    assert midday == pytest.approx(2.0 * 0.08 - 1.5 * 0.08)

    # Eligible energy still pending from the window keeps its discount
    # when it is settled after the window closes.
    after_window = tariff_quota.custom_tariff_import_cost(
        rule,
        observed_at=datetime(2026, 7, 31, 14, 5, tzinfo=brisbane),
        import_kwh=1.0,
        eligible_kwh=0.5,
        tariff_import_price=0.35,
    )
    assert after_window == pytest.approx(0.35 - 0.5 * 0.08)

    # Negative measurements never produce credits.
    assert tariff_quota.custom_tariff_import_cost(
        rule,
        observed_at=datetime(2026, 7, 31, 18, 0, tzinfo=brisbane),
        import_kwh=-1.0,
        eligible_kwh=-1.0,
        tariff_import_price=0.35,
    ) == 0


def test_cost_tracking_prices_custom_import_from_settlement_series() -> None:
    source = (COMPONENT_ROOT / "optimization" / "coordinator.py").read_text()
    module = ast.parse(source)
    method = next(
        child
        for node in module.body
        if isinstance(node, ast.ClassDef)
        and node.name == "OptimizationCoordinator"
        for child in node.body
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
        and child.name == "_track_actual_cost"
    )
    segment = ast.get_source_segment(source, method)
    assert segment is not None
    custom_branch = segment[segment.index("custom_runtime = self._ensure_custom"):]
    custom_branch = custom_branch[: custom_branch.index("zerohero_config =")]

    # The display series is discounted inside the window, so the custom
    # branch must read the undiscounted settlement series, and must not
    # price all import at the allowance window's base rate.
    assert "custom_tariff_import_cost(" in custom_branch
    assert '"_last_settlement_import_prices", None)' in custom_branch
    assert "or disp_import" in custom_branch
    assert "rule.base_price_c_per_kwh" not in custom_branch
