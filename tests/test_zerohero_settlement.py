"""Regression tests for GloBird ZeroHero settlement rules."""

from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent
ZEROHERO_PATH = ROOT / "custom_components" / "power_sync" / "zerohero.py"


def _load_zerohero_module():
    spec = importlib.util.spec_from_file_location("powersync_zerohero_test", ZEROHERO_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


zerohero = _load_zerohero_module()


def _ts(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 5, 3, hour, minute, tzinfo=timezone.utc)


def test_current_plan_topup_applies_only_to_first_15kwh_in_window():
    config = zerohero.zerohero_config_from_settings(
        {"globird_plan": "zerohero_current"}
    )

    result = zerohero.settle_zerohero_series(
        config,
        [_ts(18, 0), _ts(19, 0), _ts(20, 0), _ts(21, 0)],
        [0.0, 0.0, 0.0, 0.0],
        [6.0, 6.0, 6.0, 6.0],
        [0.05, 0.05, 0.05, 0.05],
    )

    assert result.total_export_kwh == pytest.approx(24.0)
    assert result.bonus_export_kwh == pytest.approx(15.0)
    assert result.base_export_earnings == pytest.approx(1.20)
    assert result.bonus_export_earnings == pytest.approx(1.50)
    assert result.export_earnings == pytest.approx(2.70)


def test_jul_2026_plan_uses_10c_super_export_and_zerocharge_window():
    config = zerohero.zerohero_config_from_settings(
        {"globird_plan": "zerohero_jul_2026"}
    )

    assert config.start == "18:00"
    assert config.end == "21:00"
    assert config.export_cap_kwh == pytest.approx(15.0)
    assert config.super_export_rate == pytest.approx(0.10)
    assert config.import_allowance_kwh == pytest.approx(0.09)
    assert config.zerocharge_start == "12:00"
    assert config.zerocharge_end == "15:00"
    assert config.zerocharge_import_cap_kwh == pytest.approx(50.0)
    assert zerohero.zerocharge_is_in_window(_ts(12, 0), config)
    assert not zerohero.zerocharge_is_in_window(_ts(15, 0), config)


def test_legacy_plan_topup_applies_only_to_first_10kwh_before_8pm():
    config = zerohero.zerohero_config_from_settings(
        {"globird_plan": "zerohero_legacy"}
    )

    result = zerohero.settle_zerohero_series(
        config,
        [_ts(18, 0), _ts(19, 0), _ts(20, 0)],
        [0.0, 0.0, 0.0],
        [6.0, 6.0, 6.0],
        [0.04, 0.04, 0.04],
    )

    assert result.total_export_kwh == pytest.approx(18.0)
    assert result.bonus_export_kwh == pytest.approx(10.0)
    assert result.base_export_earnings == pytest.approx(0.72)
    assert result.bonus_export_earnings == pytest.approx(1.10)


def test_import_above_hourly_allowance_loses_credit():
    config = zerohero.zerohero_config_from_settings(
        {"globird_plan": "zerohero_current"}
    )

    result = zerohero.settle_zerohero_series(
        config,
        [_ts(18, 0), _ts(19, 0), _ts(20, 0), _ts(21, 0)],
        [0.02, 0.02, 0.06, 0.0],
        [1.0, 1.0, 1.0, 0.0],
        [0.05, 0.05, 0.05, 0.05],
        include_credit=True,
    )

    assert config.import_allowance_kwh == pytest.approx(0.09)
    assert result.import_window_kwh == pytest.approx(0.10)
    assert result.credit_status == "lost"
    assert result.credit_value == 0.0


def test_credit_is_included_when_window_stays_under_threshold():
    config = zerohero.zerohero_config_from_settings(
        {"globird_plan": "zerohero_current"}
    )

    result = zerohero.settle_zerohero_series(
        config,
        [_ts(18, 0), _ts(19, 0), _ts(20, 0), _ts(21, 0)],
        [0.01, 0.01, 0.01, 0.0],
        [1.0, 1.0, 1.0, 0.0],
        [0.05, 0.05, 0.05, 0.05],
        include_credit=True,
    )

    assert result.import_window_kwh == pytest.approx(0.03)
    assert result.credit_status == "earned"
    assert result.credit_value == pytest.approx(1.0)


def test_zerocharge_import_credit_applies_only_to_capped_window_imports():
    config = zerohero.zerohero_config_from_settings(
        {"globird_plan": "zerohero_jul_2026"}
    )

    used, credit = zerohero.settle_zerocharge_imports(
        config,
        [_ts(11, 55), _ts(12, 0), _ts(13, 0), _ts(15, 0)],
        [10.0, 30.0, 30.0, 10.0],
        [0.40, 0.50, 0.60, 0.70],
    )

    assert used == pytest.approx(60.0)
    assert credit == pytest.approx(15.0 + 12.0)


def test_existing_custom_zerohero_does_not_enable_zerocharge_by_default():
    config = zerohero.zerohero_config_from_settings(
        {
            "globird_plan": "zerohero_custom",
            "globird_zerohero_start": "18:00",
            "globird_zerohero_end": "21:00",
            "globird_zerohero_export_cap_kwh": 15,
            "globird_zerohero_super_export_rate": 10,
            "globird_zerohero_credit_amount": 1,
            "globird_zerohero_import_limit_kw": 0.03,
        }
    )

    assert not config.zerocharge_enabled
