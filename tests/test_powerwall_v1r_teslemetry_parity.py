"""Regression coverage for PowerSync parity with aiopowerwall v0.3.0."""

from __future__ import annotations

import ast
import asyncio
import base64
import functools
import importlib
import sys
import textwrap
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa


ROOT = (
    Path(__file__).resolve().parent.parent
    / "custom_components"
    / "power_sync"
)
LOCAL_ROOT = ROOT / "powerwall_local"

# Load the local modules under an isolated namespace so importing this focused
# test never executes the integration's 34k-line __init__.py or requires HA.
_PKG = "ps_v1r_parity"
pkg = types.ModuleType(_PKG)
pkg.__path__ = [str(ROOT)]
sys.modules.setdefault(_PKG, pkg)

_LOCAL_PKG = f"{_PKG}.powerwall_local"
local_pkg = types.ModuleType(_LOCAL_PKG)
local_pkg.__path__ = [str(LOCAL_ROOT)]
sys.modules.setdefault(_LOCAL_PKG, local_pkg)

transport_mod = importlib.import_module(f"{_LOCAL_PKG}.transport")
client_mod = importlib.import_module(f"{_LOCAL_PKG}.client")
host_mod = importlib.import_module(f"{_PKG}.powerwall_host")
combined_pb2 = importlib.import_module(f"{_LOCAL_PKG}.tedapi_combined_pb2")
tesla_pb2 = importlib.import_module(f"{_LOCAL_PKG}.tesla_local_pb2")


def async_test(function):
    """Run one async unit test without requiring pytest-asyncio."""
    @functools.wraps(function)
    def wrapper(*args, **kwargs):
        return asyncio.run(function(*args, **kwargs))

    return wrapper


def _transport_without_key():
    return transport_mod.TEDAPIv1rTransport.__new__(
        transport_mod.TEDAPIv1rTransport
    )


def _private_key_pem() -> bytes:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


def test_scheme_bearing_gateway_host_is_normalized_before_local_transport_use():
    key_pem = _private_key_pem()

    transport = transport_mod.TEDAPIv1rTransport(
        "http://192.168.1.108/",
        key_pem,
        din="DIN--1",
    )
    client = client_mod.PowerwallLocalClient(
        "https://192.168.1.108/",
        version=client_mod.PowerwallVersion.PW3,
        private_key_pem=key_pem,
        din="DIN--1",
    )

    assert transport._host == "192.168.1.108"
    assert client.host == "192.168.1.108"
    assert client._transport._host == "192.168.1.108"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("192.168.1.108", "192.168.1.108"),
        (" http://192.168.1.108/ ", "192.168.1.108"),
        ("https://powerwall.local/", "powerwall.local"),
        ("powerwall.local:8443", "powerwall.local:8443"),
        ("https://[fe80::1]/", "[fe80::1]"),
        ("fe80::1", "[fe80::1]"),
        ("", ""),
    ],
)
def test_gateway_host_normalization(value, expected):
    assert host_mod.normalize_powerwall_gateway_host(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "ftp://192.168.1.108",
        "http://user:password@192.168.1.108/",
        "https://192.168.1.108/status",
        "192.168.1.108/status",
        "https://192.168.1.108/?token=value",
        "https://192.168.1.108/#status",
        "http://powerwall.local:not-a-port",
        "http://powerwall.local:70000",
        "powerwall.local:0",
        "http://",
        "power wall",
        "https://power wall/",
        "powerwall.local\ninvalid",
        123,
    ],
)
def test_gateway_host_normalization_rejects_ambiguous_authorities(value):
    with pytest.raises(ValueError):
        host_mod.normalize_powerwall_gateway_host(value)


@pytest.mark.parametrize(
    ("saved_host", "expected_host"),
    [
        ("http://192.168.1.108/", "192.168.1.108"),
        ("ftp://192.168.1.108/", None),
        (123, None),
    ],
)
def test_existing_gateway_host_is_migrated_on_client_build(
    saved_host, expected_host
):
    source = (LOCAL_ROOT / "views.py").read_text()
    tree = ast.parse(source)
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_build_client"
    )
    function_source = ast.get_source_segment(source, function)
    assert function_source is not None

    class _ConfigEntries:
        @staticmethod
        def async_update_entry(entry, *, data):
            entry.data = data

    entry = SimpleNamespace(data={"powerwall_local_ip": saved_host})
    hass = SimpleNamespace(config_entries=_ConfigEntries())
    namespace = {
        "Any": object,
        "ConfigEntry": object,
        "HomeAssistant": object,
        "PowerwallLocalClient": object,
        "CONF_POWERWALL_LOCAL_IP": "powerwall_local_ip",
        "CONF_POWERWALL_LOCAL_VERSION": "powerwall_local_version",
        "CONF_POWERWALL_LOCAL_PRIVATE_KEY": "powerwall_local_private_key_pem",
        "CONF_POWERWALL_LOCAL_DIN": "powerwall_local_din",
        "normalize_powerwall_gateway_host": (
            host_mod.normalize_powerwall_gateway_host
        ),
        "_LOGGER": SimpleNamespace(
            info=lambda *args, **kwargs: None,
            warning=lambda *args, **kwargs: None,
        ),
    }
    exec(function_source, namespace)

    assert asyncio.run(namespace["_build_client"](hass, entry)) is None
    if expected_host is None:
        assert "powerwall_local_ip" not in entry.data
    else:
        assert entry.data["powerwall_local_ip"] == expected_host
def test_common_api_schema_uses_published_field_numbers_without_wifi_config():
    common_fields = combined_pb2.CommonMessages.DESCRIPTOR.fields_by_name
    assert common_fields["get_system_info_request"].number == 2
    assert common_fields["get_system_info_response"].number == 3
    assert common_fields["get_networking_status_request"].number == 22
    assert common_fields["get_networking_status_response"].number == 23
    assert common_fields["check_internet_request"].number == 30
    assert common_fields["check_internet_response"].number == 31

    networking_fields = (
        combined_pb2.CommonAPIGetNetworkingStatusResponse.DESCRIPTOR.fields_by_name
    )
    assert set(networking_fields) == {"wifi", "eth", "gsm"}
    assert 1 not in {
        field.number
        for field in networking_fields.values()
    }, "upstream WifiConfig field must remain structurally unavailable"


@async_test
async def test_common_api_diagnostics_are_read_only_and_credential_free():
    transport = _transport_without_key()
    observed_requests: list[str] = []

    async def post_v1r(envelope_bytes: bytes, din: str):
        assert din == "DIN--1"
        request = combined_pb2.MessageEnvelope()
        request.ParseFromString(envelope_bytes)
        assert request.deliveryChannel == combined_pb2.DELIVERY_CHANNEL_HERMES_COMMAND
        assert request.sender.authorizedClient == 1
        assert request.recipient.din == "DIN--1"
        request_name = request.common.WhichOneof("message")
        observed_requests.append(request_name)

        reply = combined_pb2.MessageEnvelope()
        if request_name == "get_system_info_request":
            response = reply.common.get_system_info_response
            response.device_id.part_number = "1234567-00-A"
            response.device_id.serial_number = "TG123456789"
            response.din.value = "DIN--1"
            response.firmare_version.version = "24.44.0"
            response.firmare_version.githash = b"\xaa\xbb"
            response.device_type = combined_pb2.DEVICE_TYPE_SITECONTROLLER
        elif request_name == "get_networking_status_request":
            wifi = reply.common.get_networking_status_response.wifi
            wifi.enabled = True
            wifi.active_route = True
            wifi.ipv4_config.dhcp_enabled = True
            wifi.ipv4_config.address = 0xC0A80132
            wifi.ipv4_config.subnet_mask = 0xFFFFFF00
            wifi.ipv4_config.gateway = 0xC0A80101
            wifi.connectivity_status.connected_physical = True
            wifi.connectivity_status.connected_internet = True
            wifi.connectivity_status.connected_tesla = True
            wifi.connectivity_status.rssi.value = -57
            wifi.connectivity_status.rssi.signal_strength_percent.value = 78
            wifi.connectivity_status.snr.value = 31
        elif request_name == "check_internet_request":
            eth = reply.common.check_internet_response.eth
            eth.enabled = True
            eth.connectivity_status.connected_physical = True
            eth.connectivity_status.connected_internet = True
        else:
            pytest.fail(f"unexpected Common API request: {request_name}")
        return transport_mod.TEDAPIResponse(True, reply.SerializeToString())

    transport.post_v1r = post_v1r

    system = await transport.get_system_info("DIN--1")
    networking = await transport.get_networking_status("DIN--1")
    internet = await transport.check_internet("DIN--1")

    assert observed_requests == [
        "get_system_info_request",
        "get_networking_status_request",
        "check_internet_request",
    ]
    assert system == {
        "part_number": "1234567-00-A",
        "serial_number": "TG123456789",
        "din": "DIN--1",
        "firmware_version": "24.44.0",
        "firmware_githash": "aabb",
        "device_type": "SITECONTROLLER",
    }
    assert networking == {
        "wifi": {
            "enabled": True,
            "active_route": True,
            "ipv4": {
                "dhcp_enabled": True,
                "address": "192.168.1.50",
                "subnet_mask": "255.255.255.0",
                "gateway": "192.168.1.1",
                "dns": [],
            },
            "connectivity": {
                "physical": True,
                "internet": True,
                "tesla": True,
                "rssi_dbm": -57,
                "signal_strength_percent": 78,
                "snr_db": 31,
            },
        }
    }
    assert internet["eth"]["connectivity"]["internet"] is True
    assert "ssid" not in repr(networking).lower()
    assert "password" not in repr(networking).lower()


@async_test
async def test_local_island_mode_uses_app_wire_values_and_identity():
    transport = _transport_without_key()
    captured: dict[str, object] = {}

    async def post_v1r(envelope_bytes: bytes, din: str):
        captured["din"] = din
        request = tesla_pb2.MessageEnvelope()
        request.ParseFromString(envelope_bytes)
        captured["request"] = request

        reply = tesla_pb2.MessageEnvelope()
        reply.teg.setIslandModeResponse.result = 0
        return transport_mod.TEDAPIResponse(True, reply.SerializeToString())

    transport.post_v1r = post_v1r

    assert await transport.set_island_mode("DIN--1", off_grid=True) is True
    request = captured["request"]
    assert isinstance(request, tesla_pb2.MessageEnvelope)
    assert request.deliveryChannel == 2
    assert request.sender.authorizedClient == 1
    assert request.recipient.din == "DIN--1"
    assert request.teg.setIslandModeRequest.mode == 6
    assert request.teg.setIslandModeRequest.force is True


@async_test
async def test_schedule_manual_backup_cancels_prior_and_sets_full_priority(monkeypatch):
    transport = _transport_without_key()
    transport.cancel_manual_backup = AsyncMock(return_value=True)
    captured: dict[str, object] = {}
    monkeypatch.setattr(transport_mod.time, "time", lambda: 1_700_000_000)

    async def post_v1r(envelope_bytes: bytes, din: str):
        request = combined_pb2.MessageEnvelope()
        request.ParseFromString(envelope_bytes)
        captured["request"] = request
        reply = combined_pb2.MessageEnvelope()
        reply.teg.schedule_manual_backup_event_response.SetInParent()
        return transport_mod.TEDAPIResponse(True, reply.SerializeToString())

    transport.post_v1r = post_v1r

    assert await transport.schedule_manual_backup("DIN--1", 3600) is True
    transport.cancel_manual_backup.assert_awaited_once_with("DIN--1")
    request = captured["request"]
    assert isinstance(request, combined_pb2.MessageEnvelope)
    info = request.teg.schedule_manual_backup_event_request.scheduling_info
    assert info.start_time.seconds == 1_700_000_000
    assert info.duration_seconds == 3600
    assert info.priority == (1 << 64) - 1


@async_test
async def test_local_backup_events_are_decoded(monkeypatch):
    transport = _transport_without_key()
    monkeypatch.setattr(transport_mod.time, "time", lambda: 1_700_000_100)

    reply = combined_pb2.MessageEnvelope()
    manual = reply.teg.get_backup_events_response.manual_backup_event.scheduling_info
    manual.start_time.seconds = 1_700_000_000
    manual.duration_seconds = 600
    manual.priority = 99
    scheduled = reply.teg.get_backup_events_response.backup_events.add()
    scheduled.id = "event-1"
    scheduled.name = "Storm"
    scheduled.scheduling_info.start_time.seconds = 1_700_001_000
    scheduled.scheduling_info.duration_seconds = 300
    scheduled.scheduling_info.priority = 10
    transport.post_v1r = AsyncMock(
        return_value=transport_mod.TEDAPIResponse(True, reply.SerializeToString())
    )

    payload = await transport.get_backup_events("DIN--1")

    assert payload == {
        "manual_backup": {
            "start_time": 1_700_000_000,
            "duration_seconds": 600,
            "end_time": 1_700_000_600,
            "active": True,
            "priority": 99,
        },
        "backup_events": [
            {
                "id": "event-1",
                "name": "Storm",
                "start_time": 1_700_001_000,
                "duration_seconds": 300,
                "priority": 10,
            }
        ],
    }


@async_test
async def test_authorized_clients_are_read_and_normalized_locally():
    transport = _transport_without_key()
    reply = combined_pb2.MessageEnvelope()
    response = reply.authorization.list_authorized_clients_response
    response.enable_line_switch_off = True
    record = response.clients.add()
    record.public_key = b"public-key"
    record.state = combined_pb2.AUTHORIZED_STATE_VERIFIED
    record.type = combined_pb2.AUTHORIZED_CLIENT_TYPE_CUSTOMER_MOBILE_APP
    record.description = "PowerSync Local Client"
    record.key_type = combined_pb2.AUTHORIZED_KEY_TYPE_RSA
    record.roles.append(combined_pb2.AUTHORIZATION_ROLE_CUSTOMER)
    record.verification = combined_pb2.AUTHORIZED_VERIFICATION_TYPE_PRESENCE_PROOF
    record.added_time.seconds = 1234
    transport.post_v1r = AsyncMock(
        return_value=transport_mod.TEDAPIResponse(True, reply.SerializeToString())
    )

    payload = await transport.list_authorized_clients("DIN--1")

    assert payload is not None
    assert payload["enable_line_switch_off"] is True
    assert payload["clients"] == [
        {
            "public_key": base64.b64encode(b"public-key").decode("ascii"),
            "state": "VERIFIED",
            "type": "CUSTOMER_MOBILE_APP",
            "description": "PowerSync Local Client",
            "key_type": "RSA",
            "roles": ["CUSTOMER"],
            "verification": "PRESENCE_PROOF",
            "added_time": 1234,
            "identifier": None,
            "authorized_by_public_key": None,
        }
    ]


def _bare_client() -> object:
    client = client_mod.PowerwallLocalClient.__new__(client_mod.PowerwallLocalClient)
    client._host = "192.168.1.50"
    client._din = "DIN--1"
    client._local_access_enabled = True
    client._fleet_api_base = "https://fleet.example"
    client._fleet_api_token = "token"
    client._energy_site_id = 123
    return client


@async_test
async def test_client_grid_import_export_writes_both_settings_atomically():
    client = _bare_client()
    client._transport = SimpleNamespace(write_config=AsyncMock(return_value=True))

    result = await client.set_grid_import_export(
        customer_preferred_export_rule="battery_ok",
        disallow_charge_from_grid_with_solar_installed=False,
    )

    assert result is True
    client._transport.write_config.assert_awaited_once_with(
        "DIN--1",
        {
            "site_info.customer_preferred_export_rule": "battery_ok",
            "site_info.disallow_charge_from_grid_with_solar_installed": False,
        },
    )


@async_test
async def test_verify_pairing_prefers_local_authorized_client_read():
    client = _bare_client()
    public_key = b"public-key"
    client._transport = SimpleNamespace(_public_key_der=public_key)
    client.list_authorized_clients = AsyncMock(
        return_value={
            "clients": [
                {
                    "public_key": base64.b64encode(public_key).decode("ascii"),
                    "state": "VERIFIED",
                }
            ]
        }
    )
    client._verify_pairing_cloud = AsyncMock(return_value=1)

    assert await client.verify_pairing() == 3
    client._verify_pairing_cloud.assert_not_awaited()


@pytest.mark.parametrize("confirmed", [True, False])
@async_test
async def test_go_off_grid_uses_local_confirmation_or_cloud_fallback(confirmed):
    client = _bare_client()
    client._transport = SimpleNamespace(
        set_island_mode=AsyncMock(return_value=True),
    )
    client.verify_pairing = AsyncMock(return_value=3)
    client.trigger_islanding = AsyncMock(return_value=True)
    client._wait_for_grid_state = AsyncMock(return_value=confirmed)
    client._send_signed_device_command = AsyncMock(return_value=True)

    assert await client.go_off_grid() is True

    client._transport.set_island_mode.assert_awaited_once_with(
        "DIN--1", off_grid=True, force=True, mode_override=6
    )
    client.trigger_islanding.assert_awaited_once()
    if confirmed:
        client._send_signed_device_command.assert_not_awaited()
    else:
        client._send_signed_device_command.assert_awaited_once_with(
            off_grid=True, mode_override=6
        )


def _function_source(name: str) -> str:
    source = (ROOT / "__init__.py").read_text()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            segment = ast.get_source_segment(source, node)
            assert segment is not None
            return segment
    raise AssertionError(f"function {name} not found")


def test_grid_charging_service_is_local_first_with_cloud_fallback_and_refresh():
    source = _function_source("handle_set_grid_charging")
    helper = _function_source("_tesla_force_apply_grid_charging")
    assert "await _tesla_force_apply_grid_charging(" in source
    assert "dispatch_powerwall_write(" in helper
    assert '"site_info.disallow_charge_from_grid_with_solar_installed": not enabled' in helper
    assert 'label=f"{reason} grid charging"' in helper
    assert "config = await transport.read_config(din)" in helper
    assert 'refresh_powerwall_local_after_settings_write(' in source


def test_max_backup_service_uses_local_event_with_reserve_fallback():
    source = _function_source("handle_schedule_max_backup")
    assert "await local_client.schedule_max_backup(duration_minutes * 60)" in source
    assert "if local_event:" in source
    assert "if saved_reserve is None:" in source
    assert "refusing reserve fallback" in source
    assert 'DOMAIN, "set_backup_reserve"' in source
    assert '"local_event": local_event' in source


def test_max_backup_refuses_unrestorable_reserve_fallback():
    """A failed local event must not leave a local-only site at 100% reserve."""
    source = textwrap.dedent(_function_source("handle_schedule_max_backup"))

    event_module = types.ModuleType("homeassistant.helpers.event")
    event_module.async_call_later = lambda *_args, **_kwargs: lambda: None
    helpers_module = types.ModuleType("homeassistant.helpers")
    helpers_module.event = event_module
    homeassistant_module = types.ModuleType("homeassistant")
    homeassistant_module.helpers = helpers_module
    replacements = {
        "homeassistant": homeassistant_module,
        "homeassistant.helpers": helpers_module,
        "homeassistant.helpers.event": event_module,
    }
    previous = {name: sys.modules.get(name) for name in replacements}
    sys.modules.update(replacements)

    service_calls: list[tuple] = []
    persisted: list[dict | None] = []

    class Services:
        async def async_call(self, *args, **kwargs):
            service_calls.append((args, kwargs))

    local_client = SimpleNamespace(schedule_max_backup=AsyncMock(return_value=False))
    entry_data = {
        "powerwall_local": {
            "client": local_client,
            "coordinator": SimpleNamespace(
                data=SimpleNamespace(backup_reserve_percent=None)
            ),
        }
    }
    hass = SimpleNamespace(
        data={"power_sync": {"entry-1": entry_data}},
        services=Services(),
    )

    async def persist(payload):
        persisted.append(payload)

    namespace = {
        "hass": hass,
        "entry": SimpleNamespace(entry_id="entry-1"),
        "DOMAIN": "power_sync",
        "ServiceCall": object,
        "_LOGGER": SimpleNamespace(
            error=lambda *_args, **_kwargs: None,
            info=lambda *_args, **_kwargs: None,
            warning=lambda *_args, **_kwargs: None,
        ),
        "_get_tesla_coordinator_for_service": lambda _name: None,
        "_persist_max_backup_schedule": persist,
        "_max_backup_restore": lambda *_args, **_kwargs: None,
    }
    try:
        exec(source, namespace)
        asyncio.run(
            namespace["handle_schedule_max_backup"](
                SimpleNamespace(data={"duration_minutes": 30})
            )
        )
    finally:
        for name, module in previous.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module

    local_client.schedule_max_backup.assert_awaited_once_with(1800)
    assert service_calls == []
    assert persisted == [None]
    assert "max_backup_saved_reserve" not in entry_data
    assert "max_backup_end_ts" not in entry_data
    assert "max_backup_local_event" not in entry_data
