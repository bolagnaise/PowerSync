"""Tests for configurable export-curtailment price thresholds."""

from __future__ import annotations

import sys
import types
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parent.parent / "custom_components" / "power_sync"
_ps = types.ModuleType("power_sync")
_ps.__path__ = [str(ROOT)]
sys.modules["power_sync"] = _ps

from power_sync.curtailment_config import (  # noqa: E402
    CURTAILMENT_HYSTERESIS_CENTS,
    DEFAULT_CURTAILMENT_EXPORT_THRESHOLD_CENTS,
    export_earnings_are_uneconomic,
    get_curtailment_price_thresholds,
)


def _entry(*, options=None, data=None):
    return SimpleNamespace(options=options or {}, data=data or {})


def test_curtailment_threshold_defaults_preserve_one_cent_behavior():
    enter, exit_ = get_curtailment_price_thresholds(_entry())

    assert enter == DEFAULT_CURTAILMENT_EXPORT_THRESHOLD_CENTS == 1.0
    assert exit_ == enter + CURTAILMENT_HYSTERESIS_CENTS == 1.2


def test_zero_threshold_enters_only_below_zero_and_exits_at_deadband():
    entry = _entry(options={"curtailment_export_threshold_cents": 0})

    assert export_earnings_are_uneconomic(0.1, False, entry) is False
    assert export_earnings_are_uneconomic(-0.1, False, entry) is True
    assert export_earnings_are_uneconomic(0.1, True, entry) is True
    assert export_earnings_are_uneconomic(0.2, True, entry) is False


def test_options_override_legacy_data_and_invalid_values_fall_back():
    enter, exit_ = get_curtailment_price_thresholds(
        _entry(
            options={"curtailment_export_threshold_cents": "2.5"},
            data={"curtailment_export_threshold_cents": 0.5},
        )
    )
    assert (enter, exit_) == (2.5, 2.7)

    assert get_curtailment_price_thresholds(
        _entry(options={"curtailment_export_threshold_cents": "not-a-number"})
    ) == (1.0, 1.2)

