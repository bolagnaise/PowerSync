"""Currency tests for tariff payloads and static dashboard formatting."""

from __future__ import annotations

import importlib
import sys
import types
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
COMPONENT_ROOT = ROOT / "custom_components" / "power_sync"


def _install_tariff_stubs() -> None:
    ha_root = types.ModuleType("homeassistant")
    ha_util = types.ModuleType("homeassistant.util")
    ha_dt = types.ModuleType("homeassistant.util.dt")
    ha_dt.now = lambda *args, **kwargs: datetime(2026, 5, 3, 12, 0, tzinfo=timezone.utc)
    ha_util.dt = ha_dt
    ha_root.util = ha_util
    sys.modules["homeassistant"] = ha_root
    sys.modules["homeassistant.util"] = ha_util
    sys.modules["homeassistant.util.dt"] = ha_dt

    ps_module = types.ModuleType("power_sync")
    ps_module.__path__ = [str(COMPONENT_ROOT)]
    sys.modules["power_sync"] = ps_module


def _tariff_converter():
    _install_tariff_stubs()
    sys.modules.pop("power_sync.tariff_converter", None)
    return importlib.import_module("power_sync.tariff_converter")


def test_generated_tariff_payload_uses_provider_currency():
    converter = _tariff_converter()
    prices = {"PERIOD_00_00": 0.25}

    octopus = converter._build_tariff_structure(
        prices,
        prices,
        electricity_provider="octopus",
    )
    epex = converter._build_tariff_structure(
        prices,
        prices,
        electricity_provider="epex",
    )

    assert octopus["currency"] == "GBP"
    assert epex["currency"] == "EUR"


def test_generated_tariff_payload_allows_explicit_currency_override():
    converter = _tariff_converter()
    prices = {"PERIOD_00_00": 0.25}

    tariff = converter._build_tariff_structure(
        prices,
        prices,
        electricity_provider="other",
        currency="NZD",
    )

    assert tariff["currency"] == "NZD"


def test_custom_tariff_converter_defaults_missing_currency():
    init_source = (COMPONENT_ROOT / "__init__.py").read_text()

    assert "def convert_custom_tariff_to_schedule(" in init_source
    assert 'custom_tariff.get("currency")' in init_source
    assert "DEFAULT_CURRENCY" in init_source


def test_dashboard_has_no_legacy_dollar_or_cent_symbol_formatters():
    dashboard = COMPONENT_ROOT / "frontend" / "power-sync-strategy.js"
    source = dashboard.read_text()

    assert "'$'" not in source
    assert "¢" not in source
    assert "minor_price_unit" in source
    assert "_priceMeta" in source
    tou_section = source.split("function _touSchedule", 1)[1].split(
        "function _lpForecastSummary",
        1,
    )[0]
    assert "yMultiplier: 100" in tou_section
