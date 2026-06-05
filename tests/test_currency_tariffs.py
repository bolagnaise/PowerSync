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


def test_dashboard_chart_applies_price_multiplier_at_render_boundaries():
    dashboard = COMPONENT_ROOT / "frontend" / "power-sync-strategy.js"
    source = dashboard.read_text()

    assert "data: series.data.map(([t, v]) => [t, v * configuredYMultiplier])" not in source
    assert "const yMultiplier = Number.isFinite(configuredYMultiplier)" in source
    assert "const scaled = v * yMultiplier" in source
    assert "const label = this._formatValue(tick, unit, compactUnit)" in source
    assert "_formatValue(point[1] * yMultiplier" in source


def test_dashboard_history_chart_projects_series_to_now():
    dashboard = COMPONENT_ROOT / "frontend" / "power-sync-strategy.js"
    source = dashboard.read_text()

    assert "const data = this._projectHistoryToNow(rawData, stateObj, now, start, end)" in source
    assert "projected.push([now, value])" in source
    assert "const marker = mode === 'tou'" in source


def test_dashboard_history_chart_requests_full_update_history():
    dashboard = COMPONENT_ROOT / "frontend" / "power-sync-strategy.js"
    source = dashboard.read_text()

    assert "significant_changes_only: '0'" in source
    assert "Date.parse(p.last_updated || p.last_changed)" in source


def test_dashboard_history_chart_renders_state_history_as_steps_by_default():
    dashboard = COMPONENT_ROOT / "frontend" / "power-sync-strategy.js"
    source = dashboard.read_text()

    assert "const step = config.stepLine !== undefined ? config.stepLine : mode === 'history'" in source
    assert "Date.parse(stateObj?.last_updated || stateObj?.last_changed || '')" in source


def test_dashboard_history_chart_filters_impossible_home_load_values():
    dashboard = COMPONENT_ROOT / "frontend" / "power-sync-strategy.js"
    source = dashboard.read_text()

    assert "const filtered = this._filterSeriesData(data, s)" in source
    assert "const minValue = Number(seriesConfig?.minValue)" in source
    assert "name: 'Home', color: '#9C27B0', minValue: 0" in source


def test_dashboard_prefers_backend_matched_ev_label():
    dashboard = COMPONENT_ROOT / "frontend" / "power-sync-strategy.js"
    source = dashboard.read_text()

    assert "attributes?.vehicle_name" in source
    assert "config.ev_label = matchedVehicleName" in source
