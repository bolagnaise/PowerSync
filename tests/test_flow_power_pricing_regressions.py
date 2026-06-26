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
    assert session.calls[0][1] == {}
    assert all(call[2]["x-api-key"] == "secret-key" for call in session.calls)


def test_flow_power_kwatch_account_summary_warning_waits_for_portal_fallback():
    source = (COMPONENT_ROOT / "__init__.py").read_text()

    assert "KWatch account summary failed, trying portal fallback" not in source
    assert "KWatch account summary unavailable (%s); using portal fallback" in source
    assert (
        "KWatch account summary failed and portal fallback did not load account data: %s"
        in source
    )


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


def test_flow_power_api_client_decodes_kwatch_key_value_price_strings():
    api = _flow_power_api_module()
    session = _FakeSession(
        {
            "dispatch5mins": json.dumps(
                [
                    {"Key": "2026-06-12T20:00:00+10:00", "Value": 65.7},
                    {"Key": "2026-06-12T20:05:00+10:00", "Value": "72.5"},
                ]
            ),
        }
    )
    client = api.FlowPowerAPIClient("secret-key", session)

    async def run():
        return await client.dispatch5mins("qld", period=60)

    dispatch = asyncio.run(run())

    assert [entry["nemTime"] for entry in dispatch] == [
        "2026-06-12T20:00:00+10:00",
        "2026-06-12T20:05:00+10:00",
    ]
    assert [entry["perKwh"] for entry in dispatch] == [6.57, 7.25]


def test_flow_power_api_client_normalizes_uppercase_kwatch_fields():
    api = _flow_power_api_module()
    session = _FakeSession(
        {
            "dispatch5mins": {
                "PriceData": [
                    {"SETTLEMENT_DATE": "2026/06/08 10:00:00", "RRP": 123.4}
                ]
            },
            "predispatch30mins": {
                "RESULT": [
                    {"FORECAST_DATETIME": "2026/06/08 10:30:00", "PRICE": 98.0}
                ]
            },
        }
    )
    client = api.FlowPowerAPIClient("secret-key", session)

    async def run():
        dispatch = await client.dispatch5mins("nsw")
        forecast = await client.predispatch30mins("nsw")
        return dispatch, forecast

    dispatch, forecast = asyncio.run(run())

    assert dispatch[0]["nemTime"] == "2026-06-08T10:00:00+00:00"
    assert dispatch[0]["perKwh"] == 12.34
    assert forecast[0]["nemTime"] == "2026-06-08T10:30:00+00:00"
    assert forecast[0]["perKwh"] == 9.8


def test_flow_power_api_client_does_not_collapse_untimestamped_forecasts():
    api = _flow_power_api_module()
    client = api.FlowPowerAPIClient("secret-key", None)

    forecast = client._normalize_price_records(
        {
            "data": [
                {"RRP": 100.0},
                {"RRP": 200.0},
                {"RRP": 300.0},
            ]
        },
        duration=30,
    )

    assert [entry["nemTime"] for entry in forecast] == [
        "2026-06-08T00:00:00+00:00",
        "2026-06-08T00:30:00+00:00",
        "2026-06-08T01:00:00+00:00",
    ]
    assert [entry["perKwh"] for entry in forecast] == [10.0, 20.0, 30.0]


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
        dispatch = await client.dispatch5mins("nsw", period=60)
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
    assert session.calls[1][1]["period"] == 60


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
    assert "predispatch30mins(self.api_region, period=1)" in source
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


def test_flow_power_pricing_context_uses_account_twap_with_portal_account_values():
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

    assert context.twap == 20.5
    assert context.twap_source == "portal"
    assert context.bpea == 2.1
    assert context.bpea_source == "portal"
    assert context.gst_multiplier == 1.2
    assert round(helper.calculate_flow_power_pea(
        20.0,
        context,
        tariff_rate=12.0,
        avg_daily_tariff=5.0,
    ), 2) == 4.3


def test_flow_power_pricing_context_uses_portal_twap_for_pea():
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

    assert context.twap == 21.02
    assert context.twap_source == "portal"
    assert round(helper.calculate_flow_power_pea(
        11.02,
        context,
        tariff_rate=5.85,
        avg_daily_tariff=10.48,
    ), 2) == -17.33


def test_flow_power_pricing_context_uses_override_before_portal_twap():
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
        options={"fp_twap_override": 12.34},
        data={},
        domain_data={
            "flow_power_twap_tracker": SimpleNamespace(twap=8.25),
            "flow_power_portal_data": {
                "twap_import": 20.5,
                "bpea_import": 2.1,
                "gst_multiplier": 1.1,
            },
        },
    )

    assert context.twap == 12.34
    assert context.twap_source == "override"


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


def test_flow_power_startup_populates_tariff_schedule_after_ha_started():
    source = (COMPONENT_ROOT / "__init__.py").read_text()

    assert 'if electricity_provider == "flow_power":' in source
    assert "async def _flow_power_startup_tariff_sync" in source
    assert 'handle_sync_rest_api_check(check_name="flow power startup")' in source
    assert "CONF_AUTO_SYNC_ENABLED" in source
    assert "hass.bus.async_listen_once(" in source
    assert "EVENT_HOMEASSISTANT_STARTED" in source


def test_force_session_refreshes_display_tariff_before_upload_skip():
    source = (COMPONENT_ROOT / "__init__.py").read_text()
    sync_start = source.index("async def _handle_sync_tou_internal")
    sync_source = source[
        sync_start:
        source.index("hass.services.async_register(DOMAIN, SERVICE_SYNC_TOU", sync_start)
    ]

    assert "skip_battery_tariff_sync = False" in sync_source
    assert "refreshing display tariff schedule only" in sync_source

    force_guard_pos = sync_source.index("if force_discharge_state.get(\"active\"):")
    start_sync_pos = sync_source.index("_LOGGER.info(\"=== Starting TOU sync ===\")")
    display_store_pos = sync_source.index("[\"tariff_schedule\"] = {")
    upload_skip_pos = sync_source.index("if skip_battery_tariff_sync:")
    token_pos = sync_source.index("current_token, current_provider = token_getter()")

    assert force_guard_pos < start_sync_pos < display_store_pos < upload_skip_pos < token_pos


def test_sigenergy_flow_power_sync_stores_canonical_tariff_schedule():
    source = (COMPONENT_ROOT / "__init__.py").read_text()
    sync_source = source[
        source.index("async def _sync_tariff_to_sigenergy"):
        source.index("async def _sync_tariff_to_foxess")
    ]

    assert '["tariff_schedule"] = {' in sync_source
    assert '"buy_prices": canonical_buy_rates' in sync_source
    assert '"sell_prices": canonical_sell_rates' in sync_source
    assert 'f"power_sync_tariff_updated_{entry.entry_id}"' in sync_source
    assert "Tariff schedule stored for sigenergy dashboard" in sync_source
    assert "current_actual_interval=current_actual_interval" in sync_source


def test_flow_power_display_schedule_pea_ignores_raw_current_interval():
    source = (COMPONENT_ROOT / "__init__.py").read_text()
    helper_source = source[
        source.index("def _apply_provider_tariff_adjustments"):
        source.index("async def _sync_tariff_to_sigenergy")
    ]

    assert "raw 5-minute KWatch dispatch" in helper_source
    assert "current_actual_interval=None" in helper_source


def test_flow_power_main_schedule_pea_ignores_raw_current_interval():
    source = (COMPONENT_ROOT / "__init__.py").read_text()
    sync_start = source.index("async def _handle_sync_tou_internal")
    sync_source = source[
        sync_start:
        source.index("hass.services.async_register(DOMAIN, SERVICE_SYNC_TOU", sync_start)
    ]
    flow_power_pea_source = sync_source[
        sync_source.index("# Apply Flow Power PEA pricing"):
        sync_source.index("elif flow_power_price_source in")
    ]

    assert "raw 5-minute KWatch dispatch" in flow_power_pea_source
    assert "current_actual_interval=None" in flow_power_pea_source


def test_sigenergy_force_session_can_refresh_display_schedule_without_cloud_upload():
    source = (COMPONENT_ROOT / "__init__.py").read_text()
    sigenergy_source = source[
        source.index("async def _sync_tariff_to_sigenergy"):
        source.index("async def _sync_tariff_to_foxess")
    ]
    sync_start = source.index("async def _handle_sync_tou_internal")
    sync_source = source[
        sync_start:
        source.index("hass.services.async_register(DOMAIN, SERVICE_SYNC_TOU", sync_start)
    ]

    assert "upload_to_cloud: bool = True" in sigenergy_source
    assert "if not upload_to_cloud:" in sigenergy_source
    assert "display tariff schedule refreshed" in sigenergy_source
    assert "upload_to_cloud=not skip_battery_tariff_sync" in sync_source

    no_upload_pos = sigenergy_source.index("if not upload_to_cloud:")
    credential_guard_pos = sigenergy_source.index("if not all([station_id, username, pass_enc]):")
    cloud_upload_pos = sigenergy_source.index("client.set_tariff_rate")

    assert no_upload_pos < credential_guard_pos < cloud_upload_pos


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
