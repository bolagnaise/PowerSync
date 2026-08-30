"""Focused tests for the global battery-export price policy."""

from __future__ import annotations

import importlib
import sys
import types
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
COMPONENT_ROOT = ROOT / "custom_components" / "power_sync"


@pytest.fixture()
def export_policy_module():
    """Import the pure module without loading Home Assistant integration code."""
    names = (
        "power_sync",
        "power_sync.const",
        "power_sync.optimization",
        "power_sync.optimization.export_policy",
    )
    saved = {name: sys.modules.get(name) for name in names}
    package = types.ModuleType("power_sync")
    package.__path__ = [str(COMPONENT_ROOT)]
    optimization = types.ModuleType("power_sync.optimization")
    optimization.__path__ = [str(COMPONENT_ROOT / "optimization")]
    sys.modules["power_sync"] = package
    for name in ("power_sync.const", "power_sync.optimization.export_policy"):
        sys.modules.pop(name, None)
    sys.modules["power_sync.optimization"] = optimization

    module = importlib.import_module("power_sync.optimization.export_policy")
    try:
        yield module
    finally:
        for name, previous in saved.items():
            if previous is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous


def test_normalization_is_idempotent_and_keeps_dollars_per_kwh(export_policy_module):
    normalize = export_policy_module.normalize_min_export_price

    assert normalize("0.075") == pytest.approx(0.075)
    assert normalize(normalize("0.075")) == pytest.approx(0.075)
    assert normalize(0.075) == pytest.approx(0.075)
    assert normalize(-0.1) == 0.0


@pytest.mark.parametrize(
    "value",
    [None, "not-a-price", True, float("nan"), float("inf")],
)
def test_invalid_floor_values_safely_disable_policy(export_policy_module, value):
    assert export_policy_module.get_min_export_price({
        export_policy_module.CONF_OPTIMIZATION_MIN_EXPORT_PRICE: value,
    }) == 0.0


def test_entry_options_take_precedence_over_entry_data(export_policy_module):
    entry = types.SimpleNamespace(
        options={export_policy_module.CONF_OPTIMIZATION_MIN_EXPORT_PRICE: "0.12"},
        data={export_policy_module.CONF_OPTIMIZATION_MIN_EXPORT_PRICE: "0.03"},
    )

    assert export_policy_module.get_min_export_price(entry) == pytest.approx(0.12)


@pytest.mark.parametrize(
    ("price", "floor", "expected"),
    [
        (0.099999, 0.10, False),
        (0.10, 0.10, True),
        (0.100001, 0.10, True),
        (0.0, 0.0, False),
        (-0.01, 0.0, False),
        (0.000001, 0.0, True),
    ],
)
def test_predicate_requires_positive_real_price_and_honors_boundary(
    export_policy_module,
    price,
    floor,
    expected,
):
    assert export_policy_module.export_price_allows_battery_export(
        price,
        floor,
    ) is expected


@pytest.mark.parametrize(
    "price",
    [None, "unknown", True, float("nan"), float("inf")],
)
def test_invalid_real_prices_fail_closed_with_or_without_floor(
    export_policy_module,
    price,
):
    predicate = export_policy_module.export_price_allows_battery_export

    assert predicate(price, 0.0) is False
    assert predicate(price, 0.10) is False


def test_slot_mask_uses_supplied_real_prices_without_provider_overlays(
    export_policy_module,
):
    prices = [0.099999, 0.10, 0.100001, 0.0, None, float("nan"), 0.50]

    assert export_policy_module.battery_export_allowed_slots(prices, "0.10") == [
        False,
        True,
        True,
        False,
        False,
        False,
        True,
    ]


def test_default_mask_preserves_positive_price_behavior(export_policy_module):
    assert export_policy_module.battery_export_allowed_slots(
        [0.0, 0.05, -0.1, 1.0]
    ) == [False, True, False, True]


def test_none_prices_return_empty_mask(export_policy_module):
    assert export_policy_module.battery_export_allowed_slots(None, 0.10) == []
