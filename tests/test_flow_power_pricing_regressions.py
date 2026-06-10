"""Regression tests for Flow Power pricing inputs."""

from __future__ import annotations

import ast
import asyncio
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


class _FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def text(self):
        return json.dumps(self._payload)

    async def json(self, content_type=None):
        return self._payload


class _FakeSession:
    closed = False

    def __init__(self, payloads):
        self.payloads = payloads
        self.calls = []

    def post(self, url, json=None, headers=None, timeout=None):
        endpoint = url.rsplit("/", 1)[-1]
        self.calls.append((endpoint, json, headers))
        response = self.payloads[endpoint]
        if isinstance(response, tuple):
            payload, status = response
        else:
            payload, status = response, 200
        return _FakeResponse(payload, status=status)


def _flow_power_api_module():
    saved_power_sync = sys.modules.get("power_sync")
    saved_ha = sys.modules.get("homeassistant")
    saved_ha_util = sys.modules.get("homeassistant.util")
    saved_ha_dt = sys.modules.get("homeassistant.util.dt")
    fake_power_sync = types.ModuleType("power_sync")
    fake_power_sync.__path__ = [str(COMPONENT_ROOT)]
    fake_ha = types.ModuleType("homeassistant")
    fake_ha_util = types.ModuleType("homeassistant.util")
    fake_ha_dt = types.ModuleType("homeassistant.util.dt")
    fake_ha_dt.UTC = timezone.utc
    fake_ha_dt.utcnow = lambda: datetime(2026, 6, 8, 0, 0, tzinfo=timezone.utc)
    fake_ha_util.dt = fake_ha_dt
    sys.modules["power_sync"] = fake_power_sync
    sys.modules["homeassistant"] = fake_ha
    sys.modules["homeassistant.util"] = fake_ha_util
    sys.modules["homeassistant.util.dt"] = fake_ha_dt
    try:
        sys.modules.pop("power_sync.flow_power_api", None)
        return importlib.import_module("power_sync.flow_power_api")
    finally:
        if saved_power_sync is None:
            sys.modules.pop("power_sync", None)
        else:
            sys.modules["power_sync"] = saved_power_sync
        for name, saved in (
            ("homeassistant", saved_ha),
            ("homeassistant.util", saved_ha_util),
            ("homeassistant.util.dt", saved_ha_dt),
        ):
            if saved is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = saved


def test_flow_power_api_client_posts_key_and_normalizes_sites_summary_and_prices():
    api = _flow_power_api_module()
    session = _FakeSession(
        {
            "GetResidentialSites": {
                "sites": [{"nmi": "4407000000", "networkTariff": "BLNREX2,BLNRSS2"}]
            },
            "GetResidentialSiteSummary": {
                "LWAP": 16.3,
                "TWAP": 18.7,
                "LWAPImp": 23.6,
                "TWAPImp": 18.7,
                "PEATarget": 0.0,
                "PEATargetImport": 0.0,
                "GST": 1.1,
            },
            "dispatch5mins": {
                "data": [{"timestamp": "2026-06-08T10:00:00+10:00", "price": 123.4}]
            },
            "predispatch30mins": {
                "result": [{"periodDateTime": "2026/06/08 10:30:00", "RRP": 98.0}]
            },
        }
    )
    client = api.FlowPowerAPIClient("secret-key", session)

    async def run():
        sites = await client.get_residential_sites()
        summary = await client.get_residential_site_summary("4407000000")
        dispatch = await client.dispatch5mins("nsw")
        forecast = await client.predispatch30mins("nsw")
        return sites, summary, dispatch, forecast

    sites, summary, dispatch, forecast = asyncio.run(run())

    assert sites == [
        {
            "nmi": "4407000000",
            "networkTariff": "BLNREX2,BLNRSS2",
            "raw": {"nmi": "4407000000", "networkTariff": "BLNREX2,BLNRSS2"},
        }
    ]
    assert summary["source"] == "api"
    assert summary["bpea"] == 0.0
    assert summary["gst_multiplier"] == 1.1
    assert dispatch[0]["perKwh"] == 12.34
    assert forecast[0]["perKwh"] == 9.8
    assert all(call[2]["x-api-key"] == "secret-key" for call in session.calls)


def test_flow_power_api_client_decodes_nested_json_string_payloads():
    api = _flow_power_api_module()
    session = _FakeSession(
        {
            "GetResidentialSites": json.dumps(
                {"sites": [{"nmi": "4407000000", "networkTariff": "BLNREX2"}]}
            ),
            "dispatch5mins": json.dumps(
                {"data": [{"timestamp": "2026-06-08T10:00:00+10:00", "price": 123.4}]}
            ),
            "predispatch30mins": json.dumps(
                {"result": [{"periodDateTime": "2026/06/08 10:30:00", "RRP": 98.0}]}
            ),
        }
    )
    client = api.FlowPowerAPIClient("secret-key", session)

    async def run():
        sites = await client.get_residential_sites()
        dispatch = await client.dispatch5mins("nsw")
        forecast = await client.predispatch30mins("nsw")
        return sites, dispatch, forecast

    sites, dispatch, forecast = asyncio.run(run())

    assert sites[0]["nmi"] == "4407000000"
    assert sites[0]["networkTariff"] == "BLNREX2"
    assert dispatch[0]["perKwh"] == 12.34
    assert forecast[0]["perKwh"] == 9.8


def test_flow_power_api_client_normalizes_kwatch_key_value_price_records():
    api = _flow_power_api_module()
    session = _FakeSession(
        {
            "dispatch5mins": [
                {"Key": "2026-06-08T10:05:00+10:00", "Value": 145.6},
                {"Key": "2026-06-08T10:00:00+10:00", "Value": 123.4},
            ],
            "predispatch5mins": [
                {"key": "2026-06-08T10:10:00+10:00", "value": 156.7},
            ],
        }
    )
    client = api.FlowPowerAPIClient("secret-key", session)

    async def run():
        dispatch = await client.dispatch5mins("nsw", period=60)
        forecast = await client.predispatch5mins("nsw", period=60)
        return dispatch, forecast

    dispatch, forecast = asyncio.run(run())

    assert [entry["nemTime"] for entry in dispatch] == [
        "2026-06-08T10:00:00+10:00",
        "2026-06-08T10:05:00+10:00",
    ]
    assert [round(entry["perKwh"], 2) for entry in dispatch] == [12.34, 14.56]
    assert forecast[0]["nemTime"] == "2026-06-08T10:10:00+10:00"
    assert round(forecast[0]["perKwh"], 2) == 15.67


def test_flow_power_api_client_reports_allowlist_403_separately():
    api = _flow_power_api_module()
    session = _FakeSession(
        {
            "dispatch5mins": ("Host not allowlisted for this API key", 403),
        }
    )
    client = api.FlowPowerAPIClient("secret-key", session)

    async def run():
        try:
            await client.dispatch5mins("nsw")
        except api.FlowPowerAPIError as err:
            return str(err)
        return None

    assert asyncio.run(run()) == "host_not_allowlisted"


def test_flow_power_api_client_covers_documented_kwatch_endpoints():
    api = _flow_power_api_module()
    session = _FakeSession(
        {
            "GetResidentialSite": {"nmi": "4407000000", "networkTariff": "BLNREX2"},
            "dispatch30mins": [
                {"Key": "2026-06-08T10:00:00+10:00", "Value": 120.0},
            ],
            "dispatch30minsDateRange": [
                {"Key": "2026-06-08T10:30:00+10:00", "Value": 130.0},
            ],
            "PreDispatchDemand30mins": [
                {"Key": "2026-06-08T11:00:00+10:00", "Value": 8100.0},
            ],
            "DispatchDemand30mins": [
                {"Key": "2026-06-08T11:30:00+10:00", "Value": 8200.0},
            ],
            "QuarterCeilingPrice": [
                {"Key": "2026-06-08T12:00:00+10:00", "Value": 14500.0},
            ],
        }
    )
    client = api.FlowPowerAPIClient("secret-key", session)
    start = datetime(2026, 6, 8, 0, 0, tzinfo=timezone.utc)
    end = datetime(2026, 6, 9, 0, 0, tzinfo=timezone.utc)

    async def run():
        site = await client.get_residential_site("4407000000")
        dispatch = await client.dispatch30mins("nsw", period=7)
        ranged = await client.dispatch30mins_date_range("nsw", start, end)
        pre_demand = await client.predispatch_demand30mins("nsw", period=2)
        demand = await client.dispatch_demand30mins("nsw", period=30)
        quarter = await client.quarter_ceiling_price("nsw", 2, start, end)
        return site, dispatch, ranged, pre_demand, demand, quarter

    site, dispatch, ranged, pre_demand, demand, quarter = asyncio.run(run())

    assert site == {"nmi": "4407000000", "networkTariff": "BLNREX2"}
    assert dispatch[0]["perKwh"] == 12.0
    assert ranged[0]["perKwh"] == 13.0
    assert pre_demand[0]["value"] == 8100.0
    assert pre_demand[0]["unit"] == "MW"
    assert demand[0]["value"] == 8200.0
    assert demand[0]["unit"] == "MW"
    assert quarter[0]["perKwh"] == 1450.0
    assert [(call[0], call[1]) for call in session.calls] == [
        ("GetResidentialSite", {"nmi": "4407000000"}),
        ("dispatch30mins", {"regName": "nsw", "period": 7}),
        (
            "dispatch30minsDateRange",
            {
                "regName": "nsw",
                "startDate": "2026-06-08T00:00:00+00:00",
                "endDate": "2026-06-09T00:00:00+00:00",
            },
        ),
        ("PreDispatchDemand30mins", {"regName": "nsw", "period": 2}),
        ("DispatchDemand30mins", {"regName": "nsw", "period": 30}),
        (
            "QuarterCeilingPrice",
            {
                "regName": "nsw",
                "quarter": 2,
                "startDate": "2026-06-08T00:00:00+00:00",
                "endDate": "2026-06-09T00:00:00+00:00",
            },
        ),
    ]


def test_flow_power_price_endpoints_can_work_when_site_lookup_fails():
    api = _flow_power_api_module()
    session = _FakeSession(
        {
            "GetResidentialSites": ({"error": "NMI not linked"}, 500),
            "dispatch5mins": {
                "data": [{"timestamp": "2026-06-08T10:00:00+10:00", "price": 123.4}]
            },
            "predispatch30mins": {
                "result": [{"periodDateTime": "2026/06/08 10:30:00", "RRP": 98.0}]
            },
        }
    )
    client = api.FlowPowerAPIClient("secret-key", session)

    async def run():
        try:
            await client.get_residential_sites()
        except api.FlowPowerAPIError as err:
            site_error = str(err)
        else:
            site_error = None
        dispatch = await client.dispatch5mins("nsw", period=1)
        forecast = await client.predispatch30mins("nsw", period=1)
        return site_error, dispatch, forecast

    site_error, dispatch, forecast = asyncio.run(run())

    assert site_error == "api_status_500"
    assert dispatch[0]["perKwh"] == 12.34
    assert forecast[0]["perKwh"] == 9.8
    assert [call[0] for call in session.calls] == [
        "GetResidentialSites",
        "dispatch5mins",
        "predispatch30mins",
    ]


def test_kwatch_prices_to_amber_format_has_current_and_forecast_shape():
    api = _flow_power_api_module()
    entries = api.kwatch_prices_to_amber_format(
        [{"nemTime": "2026-06-08T10:00:00+10:00", "perKwh": 12.34, "duration": 5}],
        interval_type="CurrentInterval",
        default_duration=5,
    )

    assert entries == [
        {
            "nemTime": "2026-06-08T10:05:00+10:00",
            "perKwh": 12.34,
            "channelType": "general",
            "type": "CurrentInterval",
            "duration": 5,
            "wholesaleKWHPrice": 12.34,
        },
        {
            "nemTime": "2026-06-08T10:05:00+10:00",
            "perKwh": -12.34,
            "channelType": "feedIn",
            "type": "CurrentInterval",
            "duration": 5,
            "wholesaleKWHPrice": 12.34,
        },
    ]


def test_flow_power_kwatch_coordinator_publishes_amber_compatible_data():
    source = _method_source(
        COMPONENT_ROOT / "coordinator.py",
        "FlowPowerKWatchPriceCoordinator",
        "_async_update_data",
    )

    assert "dispatch5mins" in source
    assert "predispatch30mins" in source
    assert "predispatch5mins" in source
    assert "'current': current_prices" in source
    assert "'forecast': forecast" in source
    assert "'source': 'flow_power_kwatch'" in source


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
