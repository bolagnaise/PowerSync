"""Regression tests for Flow Power pricing inputs."""

from __future__ import annotations

import ast
import importlib
import importlib.util
import json
import sys
import types
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parent.parent
COMPONENT_ROOT = ROOT / "custom_components" / "power_sync"


def _method_source(file_path: Path, class_name: str, method_name: str) -> str:
    module = ast.parse(file_path.read_text())
    for node in module.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == method_name:
                    return ast.unparse(item)
    raise AssertionError(f"{class_name}.{method_name} not found")


def test_flow_power_sensor_uses_shared_pricing_context():
    source = _method_source(
        COMPONENT_ROOT / "sensor.py",
        "FlowPowerPriceSensor",
        "_calculate_pea_auto",
    )

    assert "_get_pricing_context" in source
    assert "calculate_flow_power_pea" in source


def test_flow_power_tariff_generation_uses_portal_aware_context():
    source = (COMPONENT_ROOT / "__init__.py").read_text()

    assert "resolve_flow_power_pricing_context" in source
    assert "bpea=pricing.bpea" in source
    assert "gst_multiplier=pricing.gst_multiplier" in source


def test_flow_power_pricing_context_uses_raw_twap_with_portal_account_values():
    saved_power_sync = sys.modules.get("power_sync")
    saved_const = sys.modules.get("power_sync.const")
    saved_helper = sys.modules.get("power_sync.flow_power_pricing")
    try:
        package = types.ModuleType("power_sync")
        package.__path__ = [str(COMPONENT_ROOT)]
        sys.modules["power_sync"] = package
        sys.modules.pop("power_sync.flow_power_pricing", None)
        helper = importlib.import_module("power_sync.flow_power_pricing")
    finally:
        if saved_power_sync is None:
            sys.modules.pop("power_sync", None)
        else:
            sys.modules["power_sync"] = saved_power_sync
        if saved_helper is None:
            sys.modules.pop("power_sync.flow_power_pricing", None)
        else:
            sys.modules["power_sync.flow_power_pricing"] = saved_helper
        if saved_const is None:
            sys.modules.pop("power_sync.const", None)
        else:
            sys.modules["power_sync.const"] = saved_const

    context = helper.resolve_flow_power_pricing_context(
        options={},
        data={},
        domain_data={
            "flow_power_twap_tracker": SimpleNamespace(twap=8.25),
            "flow_power_portal_data": {
                "twap": 21.0,
                "twap_import": 20.5,
                "bpea": 2.3,
                "bpea_import": 2.1,
                "gst_multiplier": 1.2,
            },
        },
    )

    assert context.twap == 8.25
    assert context.twap_source == "dynamic"
    assert context.bpea == 2.1
    assert context.bpea_source == "portal"
    assert context.gst_multiplier == 1.2
    assert round(helper.calculate_flow_power_pea(
        20.0,
        context,
        tariff_rate=12.0,
        avg_daily_tariff=5.0,
    ), 2) == 19.0


def test_flow_power_pricing_context_does_not_use_portal_twap_for_pea():
    saved_power_sync = sys.modules.get("power_sync")
    saved_const = sys.modules.get("power_sync.const")
    saved_helper = sys.modules.get("power_sync.flow_power_pricing")
    try:
        package = types.ModuleType("power_sync")
        package.__path__ = [str(COMPONENT_ROOT)]
        sys.modules["power_sync"] = package
        sys.modules.pop("power_sync.flow_power_pricing", None)
        helper = importlib.import_module("power_sync.flow_power_pricing")
    finally:
        if saved_power_sync is None:
            sys.modules.pop("power_sync", None)
        else:
            sys.modules["power_sync"] = saved_power_sync
        if saved_helper is None:
            sys.modules.pop("power_sync.flow_power_pricing", None)
        else:
            sys.modules["power_sync.flow_power_pricing"] = saved_helper
        if saved_const is None:
            sys.modules.pop("power_sync.const", None)
        else:
            sys.modules["power_sync.const"] = saved_const

    context = helper.resolve_flow_power_pricing_context(
        options={},
        data={},
        domain_data={
            "flow_power_twap_tracker": SimpleNamespace(twap=11.49),
            "flow_power_portal_data": {
                "twap": 21.02,
                "twap_import": 21.02,
                "bpea_import": 1.7,
                "gst_multiplier": 1.1,
            },
        },
    )

    assert context.twap == 11.49
    assert context.twap_source == "dynamic"
    assert round(helper.calculate_flow_power_pea(
        11.02,
        context,
        tariff_rate=5.85,
        avg_daily_tariff=10.48,
    ), 2) == -6.85


def test_flow_power_twap_sample_is_recorded_before_battery_route_returns():
    source = (COMPONENT_ROOT / "__init__.py").read_text()
    sample_call = "_record_flow_power_twap_sample(electricity_provider, general_price)"
    route_marker = "# Route to appropriate battery system for tariff sync"

    assert source.index(sample_call) < source.index(route_marker)
    assert source.count("record_price(") == 1


def test_power_sync_requires_aemo_to_tariff_with_endeavour_n73():
    manifest = json.loads((COMPONENT_ROOT / "manifest.json").read_text())

    assert "aemo-to-tariff>=0.7.15" in manifest["requirements"]


def test_network_tariff_lookup_uses_dispatch_interval_end(monkeypatch):
    captured_times = []

    fake_aemo_to_tariff = types.ModuleType("aemo_to_tariff")

    def spot_to_tariff(**kwargs):
        captured_times.append(kwargs["interval_time"])
        return 12.34

    fake_aemo_to_tariff.spot_to_tariff = spot_to_tariff
    monkeypatch.setitem(sys.modules, "aemo_to_tariff", fake_aemo_to_tariff)

    spec = importlib.util.spec_from_file_location(
        "power_sync_tariff_utils_test",
        COMPONENT_ROOT / "tariff_utils.py",
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)

    tz = timezone.utc

    assert module.get_network_tariff_rate(
        datetime(2026, 5, 27, 10, 0, 5, 123456, tzinfo=tz),
        "essential",
        "BLNRSS2",
    ) == 12.34
    assert captured_times[-1] == datetime(2026, 5, 27, 10, 5, tzinfo=tz)

    module.get_network_tariff_rate(
        datetime(2026, 5, 27, 10, 0, 0, tzinfo=tz),
        "essential",
        "BLNRSS2",
    )
    assert captured_times[-1] == datetime(2026, 5, 27, 10, 5, tzinfo=tz)

    module.get_network_tariff_rate(
        datetime(2026, 5, 27, 9, 59, 59, 999999, tzinfo=tz),
        "essential",
        "BLNRSS2",
    )
    assert captured_times[-1] == datetime(2026, 5, 27, 10, 0, tzinfo=tz)


def test_flow_power_tariff_refresh_dispatches_sensor_update_signal():
    source = (COMPONENT_ROOT / "__init__.py").read_text()
    tariff_refresh = source[
        source.index("async def _refresh_fp_tariff_rate"):
        source.index("async def _refresh_fp_avg_daily_tariff")
    ]
    avg_refresh = source[
        source.index("async def _refresh_fp_avg_daily_tariff"):
        source.index("fp_tariff_cancel = async_track_utc_time_change")
    ]

    signal = 'f"power_sync_tariff_updated_{entry.entry_id}"'

    assert '["fp_tariff_rate"] = rate' in tariff_refresh
    assert signal in tariff_refresh
    assert tariff_refresh.index('["fp_tariff_rate"] = rate') < tariff_refresh.index(signal)

    assert '["fp_avg_daily_tariff"] = avg' in avg_refresh
    assert signal in avg_refresh
    assert avg_refresh.index('["fp_avg_daily_tariff"] = avg') < avg_refresh.index(signal)


def test_flow_power_price_sensor_listens_for_tariff_updates():
    source = _method_source(
        COMPONENT_ROOT / "sensor.py",
        "FlowPowerPriceSensor",
        "async_added_to_hass",
    )

    assert "async_dispatcher_connect" in source
    assert "SIGNAL_TARIFF_UPDATED.format(self._entry.entry_id)" in source
    assert "_handle_flow_power_tariff_update" in source

    handler = _method_source(
        COMPONENT_ROOT / "sensor.py",
        "FlowPowerPriceSensor",
        "_handle_flow_power_tariff_update",
    )
    assert "async_write_ha_state" in handler


def test_flow_power_tariff_dependent_sensors_listen_for_tariff_updates():
    for class_name in (
        "FlowPowerNetworkTariffSensor",
        "FlowPowerAmberComparisonSensor",
    ):
        source = _method_source(
            COMPONENT_ROOT / "sensor.py",
            class_name,
            "async_added_to_hass",
        )

        assert "async_dispatcher_connect" in source
        assert "SIGNAL_TARIFF_UPDATED.format(self._entry.entry_id)" in source
        assert "_handle_flow_power_tariff_update" in source

        handler = _method_source(
            COMPONENT_ROOT / "sensor.py",
            class_name,
            "_handle_flow_power_tariff_update",
        )
        assert "async_write_ha_state" in handler


def test_network_tariff_dropdown_uses_get_tariffs_api(monkeypatch):
    fake_const = types.ModuleType("power_sync.const")
    fake_const.NETWORK_MODULE_NAME = {"Energex": "energex"}
    fake_power_sync = types.ModuleType("power_sync")
    fake_power_sync.__path__ = [str(COMPONENT_ROOT)]
    fake_aemo_to_tariff = types.ModuleType("aemo_to_tariff")
    fake_energex = types.ModuleType("aemo_to_tariff.energex")
    fake_energex.get_tariffs = lambda: {
        "8400": {"name": "Residential Flat"},
        "3700": {"name": "Residential Demand"},
    }

    monkeypatch.setitem(sys.modules, "power_sync", fake_power_sync)
    monkeypatch.setitem(sys.modules, "power_sync.const", fake_const)
    monkeypatch.setitem(sys.modules, "aemo_to_tariff", fake_aemo_to_tariff)
    monkeypatch.setitem(sys.modules, "aemo_to_tariff.energex", fake_energex)

    spec = importlib.util.spec_from_file_location(
        "power_sync.tariff_utils",
        COMPONENT_ROOT / "tariff_utils.py",
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)

    assert module.get_tariff_codes_for_network("Energex") == {
        "8400": "8400 — Residential Flat",
        "3700": "3700 — Residential Demand",
    }


def test_network_tariff_dropdown_falls_back_to_legacy_tariffs_attr(monkeypatch):
    fake_const = types.ModuleType("power_sync.const")
    fake_const.NETWORK_MODULE_NAME = {"United": "victoria"}
    fake_power_sync = types.ModuleType("power_sync")
    fake_power_sync.__path__ = [str(COMPONENT_ROOT)]
    fake_aemo_to_tariff = types.ModuleType("aemo_to_tariff")
    fake_victoria = types.ModuleType("aemo_to_tariff.victoria")
    fake_victoria.tariffs = {
        "VICR_SINGLE": {"name": "Residential Single Rate"},
    }

    monkeypatch.setitem(sys.modules, "power_sync", fake_power_sync)
    monkeypatch.setitem(sys.modules, "power_sync.const", fake_const)
    monkeypatch.setitem(sys.modules, "aemo_to_tariff", fake_aemo_to_tariff)
    monkeypatch.setitem(sys.modules, "aemo_to_tariff.victoria", fake_victoria)

    spec = importlib.util.spec_from_file_location(
        "power_sync.tariff_utils",
        COMPONENT_ROOT / "tariff_utils.py",
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)

    assert module.get_tariff_codes_for_network("United") == {
        "VICR_SINGLE": "VICR_SINGLE — Residential Single Rate",
    }
