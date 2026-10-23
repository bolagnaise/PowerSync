"""Tesla temporary force-discharge tariff rates and config-flow boundaries."""

from __future__ import annotations

import ast
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parent.parent / "custom_components" / "power_sync"
BUY_KEY = "tesla_force_discharge_buy_price"
SELL_KEY = "tesla_force_discharge_sell_price"


def _function(path: Path, name: str):
    tree = ast.parse(path.read_text())
    return next(
        node for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name
    )


def _price_helpers():
    path = ROOT / "tesla_force_tariff.py"
    validate = _function(path, "validate_force_discharge_prices")
    configured = _function(path, "configured_force_discharge_prices")
    configured.body = [
        node for node in configured.body if not isinstance(node, ast.ImportFrom)
    ]
    module = ast.fix_missing_locations(ast.Module(body=[validate, configured], type_ignores=[]))
    namespace = {
        "math": math,
        "DEFAULT_TESLA_FORCE_DISCHARGE_BUY_PRICE": 0.55,
        "DEFAULT_TESLA_FORCE_DISCHARGE_SELL_PRICE": 25.0,
        "CONF_TESLA_FORCE_DISCHARGE_BUY_PRICE": BUY_KEY,
        "CONF_TESLA_FORCE_DISCHARGE_SELL_PRICE": SELL_KEY,
    }
    exec(compile(module, str(path), "exec"), namespace)
    return namespace["validate_force_discharge_prices"], namespace["configured_force_discharge_prices"]


def test_tesla_force_discharge_prices_default_and_invalid_existing_settings():
    validate, configured = _price_helpers()
    assert configured({}) == (0.55, 25.0)
    assert configured({BUY_KEY: 0.7, SELL_KEY: 30}) == (0.7, 30.0)
    assert configured({BUY_KEY: float("nan"), SELL_KEY: 25}) == (0.55, 25.0)
    for buy, sell in ((99, 99), (25, 0.55), (-1, 25), (0.55, float("inf"))):
        with pytest.raises(ValueError):
            validate(buy, sell)


def test_tesla_discharge_tariff_uses_configured_rates_only_in_force_window():
    _, configured = _price_helpers()
    path = ROOT / "__init__.py"
    function = _function(path, "_create_discharge_tariff")
    module = ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[]))
    entry = SimpleNamespace(data={BUY_KEY: 0.7, SELL_KEY: 30})
    namespace = {
        "datetime": datetime,
        "timedelta": timedelta,
        "entry": entry,
        "hass": object(),
        "dt_util": SimpleNamespace(now=lambda: datetime(2026, 9, 28, 18, 5, tzinfo=timezone.utc)),
        "_LOGGER": SimpleNamespace(info=lambda *args, **kwargs: None),
        "currency_for_entry": lambda *_: "AUD",
        "configured_force_discharge_prices": configured,
    }
    exec(compile(module, str(path), "exec"), namespace)
    tariff, expiry = namespace["_create_discharge_tariff"](30)
    buy = tariff["energy_charges"]["Summer"]["rates"]
    sell = tariff["sell_tariff"]["energy_charges"]["Summer"]["rates"]
    assert (buy["18:00"], sell["18:00"]) == (0.7, 30.0)
    assert (buy["18:30"], sell["18:30"]) == (0.7, 30.0)
    assert (buy["19:00"], sell["19:00"]) == (0.30, 0.08)
    assert expiry.hour == 19 and expiry.minute == 0


@pytest.mark.parametrize("minute", [0, 5, 29, 30, 59])
def test_full_day_discharge_does_not_wrap_to_one_period(minute):
    _, configured = _price_helpers()
    path = ROOT / "__init__.py"
    function = _function(path, "_create_discharge_tariff")
    now = datetime(2026, 10, 24, 23, minute, tzinfo=timezone.utc)
    namespace = {
        "datetime": datetime, "timedelta": timedelta,
        "entry": SimpleNamespace(data={}), "hass": object(),
        "dt_util": SimpleNamespace(now=lambda: now),
        "_LOGGER": SimpleNamespace(info=lambda *args, **kwargs: None),
        "currency_for_entry": lambda *_: "AUD",
        "configured_force_discharge_prices": configured,
    }
    exec(compile(ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[])), str(path), "exec"), namespace)
    tariff, expiry = namespace["_create_discharge_tariff"](1440)
    rates = tariff["sell_tariff"]["energy_charges"]["Summer"]["rates"]
    assert len(rates) == 48
    assert set(rates.values()) == {25.0}
    assert now + timedelta(minutes=1440) <= expiry <= now + timedelta(minutes=1470)


def test_tesla_config_flow_exposes_and_persists_both_rates():
    source = (ROOT / "config_flow.py").read_text()
    setup = ast.get_source_segment(source, _function(ROOT / "config_flow.py", "async_step_tesla_provider"))
    options_methods = [
        node for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "async_step_tesla_connection"
    ]
    options = ast.get_source_segment(source, options_methods[0])
    site = ast.get_source_segment(source, _function(ROOT / "config_flow.py", "async_step_site_selection"))
    for key in ("CONF_TESLA_FORCE_DISCHARGE_BUY_PRICE", "CONF_TESLA_FORCE_DISCHARGE_SELL_PRICE"):
        assert key in setup and key in options and key in site
        assert f"new_data[{key}]" in options
        assert f"self._site_data[{key}]" in site
