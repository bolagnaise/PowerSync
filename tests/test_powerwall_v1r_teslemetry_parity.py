"""Regression coverage for PowerSync parity with aiopowerwall v0.3.0."""

from __future__ import annotations

import ast
import asyncio
import base64
import functools
import importlib
import json
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

energy_device = importlib.import_module(f"{_LOCAL_PKG}.energy_device")
authorization_types_pb2 = energy_device.authorization_types_pb2
common_api_pb2 = energy_device.common_api_pb2
device_pb2 = energy_device.device_pb2
transport_pb2 = energy_device.transport_pb2

transport_mod = importlib.import_module(f"{_LOCAL_PKG}.transport")
client_mod = importlib.import_module(f"{_LOCAL_PKG}.client")
host_mod = importlib.import_module(f"{_PKG}.powerwall_host")
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
@async_test
async def test_common_api_schema_uses_published_field_numbers_and_networking_status_drops_wifi_config():
    common_fields = common_api_pb2.CommonMessages.DESCRIPTOR.fields_by_name
    assert common_fields["get_system_info_request"].number == 2
    assert common_fields["get_system_info_response"].number == 3
    assert common_fields["get_networking_status_request"].number == 22
    assert common_fields["get_networking_status_response"].number == 23
    assert common_fields["check_internet_request"].number == 30
    assert common_fields["check_internet_response"].number == 31

    transport = _transport_without_key()

    async def post_v1r(envelope_bytes: bytes, din: str):
        request = transport_pb2.MessageEnvelope()
        request.ParseFromString(envelope_bytes)
        reply = transport_pb2.MessageEnvelope()
        response = reply.common.get_networking_status_response
        response.wifi_config.ssid = "MyHomeNetwork"
        response.wifi_config.password.value = "hunter2-super-secret"
        response.wifi.enabled = True
        response.wifi.active_route = True
        response.wifi.ipv4_config.dhcp_enabled = True
        response.wifi.connectivity_status.connected_physical = True
        response.wifi.connectivity_status.connected_internet = True
        return transport_mod.TEDAPIResponse(True, reply.SerializeToString())

    transport.post_v1r = post_v1r

    networking = await transport.get_networking_status("DIN--1")

    assert set(networking) == {"wifi"}, "must carry only the allowlisted interface keys"
    assert "MyHomeNetwork" not in repr(networking), "SSID from wifi_config must not leak"
    assert (
        "hunter2-super-secret" not in repr(networking)
    ), "password from wifi_config must not leak"


@async_test
async def test_common_api_diagnostics_are_read_only_and_credential_free():
    transport = _transport_without_key()
    observed_requests: list[str] = []

    async def post_v1r(envelope_bytes: bytes, din: str):
        assert din == "DIN--1"
        request = transport_pb2.MessageEnvelope()
        request.ParseFromString(envelope_bytes)
        assert request.delivery_channel == transport_pb2.DELIVERY_CHANNEL_HERMES_COMMAND
        assert request.sender.authorized_client == 1
        assert request.recipient.din == "DIN--1"
        request_name = request.common.WhichOneof("message")
        observed_requests.append(request_name)

        reply = transport_pb2.MessageEnvelope()
        if request_name == "get_system_info_request":
            response = reply.common.get_system_info_response
            response.device_id.part_number = "1234567-00-A"
            response.device_id.serial_number = "TG123456789"
            if hasattr(response, "firmware_version"):
                response.din = "DIN--1"
                response.firmware_version.version = "24.44.0"
                response.firmware_version.githash = b"\xaa\xbb"
            else:
                response.din.value = "DIN--1"
                response.firmare_version.version = "24.44.0"
                response.firmare_version.githash = b"\xaa\xbb"
            response.device_type = device_pb2.DEVICE_TYPE_SITECONTROLLER
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


def _modern_system_info_wire(*, din="DIN--1", extra_fields=True, device_type=4):
    """Independent wire fixture for the format observed on firmware 26.26.4."""
    from importlib import import_module
    wire = import_module(f"{_LOCAL_PKG}.fleet_api_bms")
    response = (
        wire._field_bytes(1, wire._field_string(1, "1234567-00-A") + wire._field_string(2, "TG123456789"))
        + wire._field_string(2, din)
        + wire._field_bytes(3, wire._field_string(1, "26.26.4") + wire._field_bytes(2, b"\xaa\xbb"))
        + wire._field_varint(6, device_type)
    )
    if extra_fields:
        # Update/compliance/signature fields need not be decoded for identity.
        response += wire._field_bytes(5, b"\x0a\x00")
        response += wire._field_bytes(7, b"\x0a\x00")
        response += wire._field_bytes(8, b"opaque-signature")
    return wire._field_bytes(4, wire._field_bytes(3, response))


@pytest.mark.parametrize("extra_fields", [False, True])
@async_test
async def test_get_system_info_decodes_modern_gateway_wire(extra_fields):
    transport = _transport_without_key()
    transport.post_v1r = AsyncMock(return_value=transport_mod.TEDAPIResponse(
        True, _modern_system_info_wire(extra_fields=extra_fields),
    ))

    result = await transport.get_system_info("DIN--1")

    assert result == {
        "part_number": "1234567-00-A", "serial_number": "TG123456789",
        "din": "DIN--1", "firmware_version": "26.26.4",
        "firmware_githash": "aabb", "device_type": "SITECONTROLLER",
    }
    transport.post_v1r.assert_awaited_once()
    request = transport_pb2.MessageEnvelope()
    request.ParseFromString(transport.post_v1r.await_args.args[0])
    assert request.common.WhichOneof("message") == "get_system_info_request"


@pytest.mark.parametrize("din,device_type", [("", 4), ("other-gateway", 4), ("DIN--1\n", 4), ("DIN--1", 0)])
@async_test
async def test_modern_system_info_requires_target_identity(din, device_type):
    transport = _transport_without_key()
    transport.post_v1r = AsyncMock(return_value=transport_mod.TEDAPIResponse(
        True, _modern_system_info_wire(din=din, device_type=device_type),
    ))
    assert await transport.get_system_info("DIN--1") is None


@async_test
async def test_legacy_system_info_with_unknown_tag_six_keeps_wrapped_din():
    from importlib import import_module
    wire = import_module(f"{_LOCAL_PKG}.fleet_api_bms")
    reply = transport_pb2.MessageEnvelope()
    response = reply.common.get_system_info_response
    response.din.value = "DIN--1"
    response.firmare_version.version = "24.44.0"
    response.device_type = device_pb2.DEVICE_TYPE_SITECONTROLLER
    # An unknown future field must not make legacy wrapped DIN look modern.
    inner = response.SerializeToString() + wire._field_varint(6, 4)
    raw = wire._field_bytes(4, wire._field_bytes(3, inner))
    transport = _transport_without_key()
    transport.post_v1r = AsyncMock(return_value=transport_mod.TEDAPIResponse(True, raw))
    result = await transport.get_system_info("DIN--1")
    assert result["din"] == "DIN--1"
    assert result["firmware_version"] == "24.44.0"
    assert result["device_type"] == "SITECONTROLLER"


@async_test
async def test_missing_common_api_response_is_reported_in_diagnostics():
    client = client_mod.PowerwallLocalClient.__new__(client_mod.PowerwallLocalClient)
    client._din = "DIN--1"
    client._transport = SimpleNamespace(
        get_system_info=AsyncMock(return_value=None),
        get_networking_status=AsyncMock(return_value={}),
        check_internet=AsyncMock(return_value={}),
    )

    result = await client.get_v1r_diagnostics()

    assert result["system_info"] is None
    assert "system_info" in result["errors"]
    assert "networking" not in result["errors"]
    assert "internet" not in result["errors"]


@async_test
async def test_get_system_info_supports_legacy_tesla_protocol_shape():
    transport = _transport_without_key()
    transport._read_common = AsyncMock(
        return_value=SimpleNamespace(
            device_id=SimpleNamespace(
                part_number="1234567-00-A",
                serial_number="TG123456789",
            ),
            din=SimpleNamespace(value="DIN--1"),
            firmare_version=SimpleNamespace(
                version="24.44.0",
                githash=b"\xaa\xbb",
            ),
            device_type=device_pb2.DEVICE_TYPE_SITECONTROLLER,
        )
    )

    assert await transport.get_system_info("DIN--1") == {
        "part_number": "1234567-00-A",
        "serial_number": "TG123456789",
        "din": "DIN--1",
        "firmware_version": "24.44.0",
        "firmware_githash": "aabb",
        "device_type": "SITECONTROLLER",
    }


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
    transport.get_backup_events = AsyncMock(return_value={"manual_backup": {"active": True}, "backup_events": []})
    transport.cancel_manual_backup = AsyncMock(return_value=True)
    captured: dict[str, object] = {}
    monkeypatch.setattr(transport_mod.time, "time", lambda: 1_700_000_000)

    async def post_v1r(envelope_bytes: bytes, din: str, **kwargs):
        kwargs["before_send"]()
        request = transport_pb2.MessageEnvelope()
        request.ParseFromString(envelope_bytes)
        captured["request"] = request
        reply = transport_pb2.MessageEnvelope()
        reply.teg.schedule_manual_backup_event_response.SetInParent()
        return transport_mod.TEDAPIResponse(True, reply.SerializeToString())

    transport.post_v1r = post_v1r

    assert await transport.schedule_manual_backup("DIN--1", 3600) is True
    transport.get_backup_events.assert_awaited_once_with("DIN--1")
    transport.cancel_manual_backup.assert_awaited_once_with("DIN--1")
    request = captured["request"]
    assert isinstance(request, transport_pb2.MessageEnvelope)
    info = request.teg.schedule_manual_backup_event_request.scheduling_info
    assert info.start_time.seconds == 1_700_000_000
    assert info.duration_seconds == 3600
    assert info.priority == (1 << 64) - 1


@async_test
async def test_local_backup_events_are_decoded(monkeypatch):
    transport = _transport_without_key()
    monkeypatch.setattr(transport_mod.time, "time", lambda: 1_700_000_100)

    reply = transport_pb2.MessageEnvelope()
    manual = reply.teg.get_backup_events_response.manual_backup_event.scheduling_info
    manual.start_time.seconds = 1_700_000_000
    manual.duration_seconds = 600
    manual.priority = 99
    scheduled = reply.teg.get_backup_events_response.backup_events.add()
    scheduled.id = "event-1"
    scheduled.name = "Storm"
    # `sheduling_info` (sic) — misspelled upstream in BackupEvent.
    scheduled.sheduling_info.start_time.seconds = 1_700_001_000
    scheduled.sheduling_info.duration_seconds = 300
    scheduled.sheduling_info.priority = 10
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
    reply = transport_pb2.MessageEnvelope()
    response = reply.authorization.list_authorized_clients_response
    response.enable_line_switch_off = True
    record = response.clients.add()
    record.public_key = b"public-key"
    record.state = authorization_types_pb2.AUTHORIZED_STATE_VERIFIED
    record.type = authorization_types_pb2.AUTHORIZED_CLIENT_TYPE_CUSTOMER_MOBILE_APP
    record.description = "PowerSync Local Client"
    record.key_type = authorization_types_pb2.AUTHORIZED_KEY_TYPE_RSA
    record.roles.append(authorization_types_pb2.AUTHORIZATION_ROLE_CUSTOMER)
    record.verification = authorization_types_pb2.AUTHORIZED_VERIFICATION_TYPE_PRESENCE_PROOF
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
async def test_v1r_diagnostics_keep_successful_paths_when_one_endpoint_fails():
    client = _bare_client()
    client._transport = SimpleNamespace(
        get_system_info=AsyncMock(side_effect=RuntimeError("unsupported field")),
        get_networking_status=AsyncMock(
            return_value={"wifi": {"active_route": True}}
        ),
        check_internet=AsyncMock(
            return_value={
                "wifi": {"connectivity": {"internet": True}}
            }
        ),
    )

    diagnostics = await client.get_v1r_diagnostics()

    assert diagnostics["system_info"] is None
    assert diagnostics["networking"] == {"wifi": {"active_route": True}}
    assert diagnostics["internet"] == {
        "wifi": {"connectivity": {"internet": True}}
    }
    assert diagnostics["errors"] == {"system_info": "unsupported field"}


@async_test
async def test_dcq_preserves_signature_rejection_for_repair_handling():
    client = _bare_client()
    client._transport = SimpleNamespace(
        post_v1r=AsyncMock(
            side_effect=client_mod.PowerwallSignatureError("inactive key")
        )
    )

    with pytest.raises(client_mod.PowerwallSignatureError, match="inactive key"):
        await client._fetch_dcq_local()


@async_test
async def test_snapshot_preserves_signature_rejection_for_coordinator():
    client = _bare_client()
    client._transport = SimpleNamespace(
        post_v1r=AsyncMock(
            side_effect=client_mod.PowerwallSignatureError("inactive key")
        ),
        read_config=AsyncMock(return_value={}),
    )

    with pytest.raises(client_mod.PowerwallSignatureError, match="inactive key"):
        await client.get_snapshot()


def _dcq_snapshot_reply(payload=None):
    wire = importlib.import_module(f"{_LOCAL_PKG}.fleet_api_bms")
    if payload is None:
        payload = {"control": {
            "systemStatus": {"nominalFullPackEnergyWh": 13500,
                             "nominalEnergyRemainingWh": 9000},
            "islanding": {"contactorClosed": True, "gridOK": True, "microGridOK": True},
            "meterAggregates": [{"location": key, "realPowerW": value}
                                for key, value in (("site", 800), ("load", 1000),
                                                   ("solar", 200), ("battery", 0))],
        }}
    envelope = wire._field_bytes(16, wire._field_bytes(2,
        wire._field_string(2, json.dumps(payload))))
    return transport_mod.TEDAPIResponse(True, envelope)


@async_test
@pytest.mark.parametrize("empty_reply", [
    transport_mod.TEDAPIResponse(True, None),
    transport_mod.TEDAPIResponse(True, b""),
    transport_mod.TEDAPIResponse(True, b"\x82\x01\x00"),
    _dcq_snapshot_reply({}),
])
async def test_snapshot_recovers_one_empty_dcq_with_new_telemetry_and_config(empty_reply):
    client = _bare_client()
    first_config = {"site_info": {"min_site_meter_power_ac": -10}}
    current_config = {"site_info": {"min_site_meter_power_ac": -2}}
    client._transport = SimpleNamespace(
        post_v1r=AsyncMock(side_effect=[empty_reply, _dcq_snapshot_reply()]),
        read_config=AsyncMock(side_effect=[first_config, current_config]),
    )

    snapshot = await client.get_snapshot()

    assert snapshot.soc is not None and snapshot.grid_status == "SystemGridConnected"
    assert (snapshot.load_w, snapshot.solar_w, snapshot.grid_w) == (1000, 200, 800)
    assert snapshot.raw["config"] == current_config
    assert client._transport.post_v1r.await_count == 2
    assert client._transport.read_config.await_count == 2


@async_test
async def test_snapshot_success_does_not_add_a_retry():
    client = _bare_client()
    client._transport = SimpleNamespace(post_v1r=AsyncMock(return_value=_dcq_snapshot_reply()),
        read_config=AsyncMock(return_value={}))

    assert (await client.get_snapshot()).load_w == 1000
    client._transport.post_v1r.assert_awaited_once()
    client._transport.read_config.assert_awaited_once()


@async_test
async def test_snapshot_empty_dcq_remains_bounded_and_never_uses_a_third_reply():
    client = _bare_client()
    client._transport = SimpleNamespace(
        post_v1r=AsyncMock(side_effect=[transport_mod.TEDAPIResponse(True, None),
            transport_mod.TEDAPIResponse(True, None), _dcq_snapshot_reply()]),
        read_config=AsyncMock(return_value={}))

    with pytest.raises(client_mod.PowerwallUnreachableError, match="no DeviceControllerQuery data"):
        await client.get_snapshot()
    assert client._transport.post_v1r.await_count == 2
    assert client._transport.read_config.await_count == 2


@async_test
@pytest.mark.parametrize("source", ["query", "config"])
async def test_snapshot_does_not_retry_a_rejected_pairing_key(source):
    client = _bare_client()
    rejected = client_mod.PowerwallSignatureError("inactive key")
    client._transport = SimpleNamespace(
        post_v1r=AsyncMock(side_effect=rejected if source == "query" else None,
            return_value=transport_mod.TEDAPIResponse(True, None)),
        read_config=AsyncMock(side_effect=rejected if source == "config" else None,
            return_value={}))

    with pytest.raises(client_mod.PowerwallSignatureError, match="inactive key"):
        await client.get_snapshot()
    client._transport.post_v1r.assert_awaited_once()
    client._transport.read_config.assert_awaited_once()


@async_test
async def test_snapshot_retry_does_not_reuse_config_when_the_new_read_fails():
    client = _bare_client()
    client._transport = SimpleNamespace(
        post_v1r=AsyncMock(side_effect=[transport_mod.TEDAPIResponse(True, None), _dcq_snapshot_reply()]),
        read_config=AsyncMock(side_effect=[{"site_info": {"min_site_meter_power_ac": -10}}, None]))

    snapshot = await client.get_snapshot()
    assert snapshot.raw["config"] is None


@async_test
async def test_snapshot_retry_rechecks_local_access_before_the_second_read():
    client = _bare_client()
    async def disabled_during_read(*args):
        client._local_access_enabled = False
        return transport_mod.TEDAPIResponse(True, None)
    client._transport = SimpleNamespace(post_v1r=AsyncMock(side_effect=disabled_during_read),
        read_config=AsyncMock(return_value={}))

    with pytest.raises(client_mod.PowerwallUnreachableError, match="local access disabled"):
        await client.get_snapshot()
    client._transport.post_v1r.assert_awaited_once()


@async_test
async def test_snapshot_does_not_retry_an_unreachable_gateway():
    client = _bare_client()
    client._transport = SimpleNamespace(
        post_v1r=AsyncMock(side_effect=client_mod.PowerwallUnreachableError("timed out")),
        read_config=AsyncMock(return_value={}))

    with pytest.raises(client_mod.PowerwallUnreachableError, match="timed out"):
        await client.get_snapshot()
    client._transport.post_v1r.assert_awaited_once()


@async_test
async def test_snapshot_cancellation_during_retry_never_starts_another_read(monkeypatch):
    client = _bare_client()
    client._transport = SimpleNamespace(post_v1r=AsyncMock(
        return_value=transport_mod.TEDAPIResponse(True, None)), read_config=AsyncMock(return_value={}))
    monkeypatch.setattr(client_mod.asyncio, "sleep", AsyncMock(side_effect=asyncio.CancelledError))

    with pytest.raises(asyncio.CancelledError):
        await client.get_snapshot()
    client._transport.post_v1r.assert_awaited_once()


@async_test
@pytest.mark.parametrize("source", ["query", "config"])
async def test_snapshot_cancelled_child_read_is_not_retried(source):
    client = _bare_client()
    client._transport = SimpleNamespace(
        post_v1r=AsyncMock(side_effect=asyncio.CancelledError if source == "query" else None,
            return_value=transport_mod.TEDAPIResponse(True, None)),
        read_config=AsyncMock(side_effect=asyncio.CancelledError if source == "config" else None,
            return_value={}))

    with pytest.raises(asyncio.CancelledError):
        await client.get_snapshot()
    client._transport.post_v1r.assert_awaited_once()


@pytest.mark.parametrize(
    ("grid_status", "off_grid", "expected"),
    [
        ("Active", False, True),
        ("Active", True, False),
        ("SystemGridConnected", False, True),
        ("SystemGridConnected", True, False),
        ("Inactive", True, True),
        ("Inactive", False, False),
        ("Islanded", True, True),
        ("Islanded", False, False),
        ("Off-Grid", True, True),
        ("Off-Grid", False, False),
        ("SystemIslandedActive", True, True),
        ("SystemIslandedActive", False, False),
        ("SystemIslandedReady", False, False),
        ("SystemIslandedReady", True, False),
        ("SystemTransitionToGrid", False, False),
        ("SystemTransitionToGrid", True, False),
        ("SystemTransitionToIsland", True, False),
        ("SystemTransitionToIsland", False, False),
        ("SystemMicroGridFaulted", True, False),
        ("SystemMicroGridFaulted", False, False),
        ("SystemWaitForUser", True, False),
        ("SystemWaitForUser", False, False),
        (None, False, False),
        (None, True, False),
        ("unexpected-status", False, False),
        ("unexpected-status", True, False),
    ],
)
@async_test
async def test_wait_for_grid_state_confirms_only_matching_terminal_state(
    grid_status,
    off_grid,
    expected,
):
    client = _bare_client()
    client.get_snapshot = AsyncMock(
        return_value=SimpleNamespace(grid_status=grid_status)
    )

    assert await client._wait_for_grid_state(off_grid=off_grid, timeout=0) is expected


@async_test
async def test_wait_for_grid_state_keeps_polling_after_unknown(monkeypatch):
    client = _bare_client()
    client.get_snapshot = AsyncMock(
        side_effect=[
            SimpleNamespace(grid_status="SystemIslandedReady"),
            SimpleNamespace(grid_status="SystemGridConnected"),
        ]
    )
    sleep = AsyncMock(return_value=None)
    monkeypatch.setattr(client_mod.asyncio, "sleep", sleep)

    assert await client._wait_for_grid_state(off_grid=False, timeout=1) is True
    assert client.get_snapshot.await_count == 2
    sleep.assert_awaited_once_with(1.0)


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
@pytest.mark.parametrize("enabled, info, expected", [
    (True, {"on_grid_solar_curtailment_enabled": True}, True),
    (False, {}, True),
    (False, {"on_grid_solar_curtailment_enabled": False}, True),
    (True, {}, False),
    (False, {"on_grid_solar_curtailment_enabled": True}, False),
    (True, {"on_grid_solar_curtailment_enabled": 1}, False),
    (False, None, False),
])
async def test_on_grid_solar_curtailment_requires_structured_native_readback(enabled, info, expected):
    client = _bare_client()
    client._transport = SimpleNamespace(
        write_config=AsyncMock(return_value=True),
        read_config=AsyncMock(return_value={"site_info": info}),
    )
    assert await client.set_on_grid_solar_curtailment(enabled) is expected
    client._transport.write_config.assert_awaited_once_with(
        "DIN--1", {"site_info.on_grid_solar_curtailment_enabled": enabled},
    )


@async_test
async def test_on_grid_solar_curtailment_rejects_bad_input_and_failed_write():
    client = _bare_client()
    client._transport = SimpleNamespace(write_config=AsyncMock(return_value=False), read_config=AsyncMock())
    with pytest.raises(ValueError, match="boolean"):
        await client.set_on_grid_solar_curtailment("false")
    client._transport.write_config.assert_not_awaited()
    assert await client.set_on_grid_solar_curtailment(True) is False
    client._transport.read_config.assert_not_awaited()
    client._transport = None
    assert await client.set_on_grid_solar_curtailment(True) is False


@async_test
@pytest.mark.parametrize("deny_at", ["config_read", "write_session"])
async def test_native_config_guard_runs_after_reads_and_session_acquisition(deny_at):
    transport = transport_mod.TEDAPIv1rTransport("192.168.1.10", _private_key_pem(), din="DIN--1")
    allowed = True
    sessions = 0
    writes = []

    class Response:
        status = 200
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def read(self):
            nonlocal allowed
            if deny_at == "config_read": allowed = False
            env = transport_pb2.MessageEnvelope()
            env.filestore.read_file_response.file.blob = json.dumps({"site_info": {}}).encode()
            env.filestore.read_file_response.hash = b"hash"
            reply = energy_device.signed_message_pb2.RoutableMessage()
            reply.protobuf_message_as_bytes = env.SerializeToString()
            return reply.SerializeToString()

    class Session:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        def post(self, url, *, data, headers):
            outer = energy_device.signed_message_pb2.RoutableMessage()
            outer.ParseFromString(data)
            env = transport_pb2.MessageEnvelope()
            env.ParseFromString(outer.protobuf_message_as_bytes)
            if env.filestore.HasField("update_file_request"): writes.append(data)
            return Response()

    async def acquire():
        nonlocal sessions, allowed
        sessions += 1
        if sessions == 2 and deny_at == "write_session": allowed = False
        return Session()
    def guard():
        if not allowed: raise RuntimeError("authority_changed")
    transport._session = acquire
    with pytest.raises(RuntimeError, match="authority_changed"):
        await transport.write_config("DIN--1", {"site_info.on_grid_solar_curtailment_enabled": True}, before_write=guard)
    assert writes == [], "Neither a delayed config read nor session acquisition may bypass the final authority check"


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
    assert "dispatch_powerwall_setting(" in helper
    assert '"site_info.disallow_charge_from_grid_with_solar_installed": not enabled' in helper
    assert 'label=f"{reason} grid charging"' in helper
    assert "config = await transport.read_config(din)" in (LOCAL_ROOT / "dispatch.py").read_text()
    assert 'refresh_powerwall_local_after_settings_write(' in source


def test_max_backup_service_stages_cleanup_before_native_or_cloud_actuation():
    source = _function_source("handle_schedule_max_backup")
    assert "await local_client.schedule_max_backup(duration_minutes * 60, end_ts=end_ts)" in source
    assert "_cloud_tariff_control_requested(call)" in source
    assert source.index("persisted = await _persist_max_backup_schedule(") < source.index("await local_client.schedule_max_backup(")
    assert '"apply_pending": True' in source and '"site_id": site_id' in source
    assert "if not local_event and saved_reserve is None:" in source
    assert "cleanup remains owned" in source
    assert "await handle_set_backup_reserve(make_service_call(" in source


@pytest.mark.parametrize("route,outcome,reserve,error", [
    ("local", "ack", None, None),
    ("local", "false", None, "not acknowledged"),
    ("local", "exception", 2, "reply lost"),
    ("tariff", "exception", 2, None),
    ("cloud", "unused", 2, None),
    ("cloud", "unused", None, "trustworthy"),
])
def test_max_backup_keeps_route_specific_cleanup_and_refuses_unrestorable_fallback(route, outcome, reserve, error):
    """Unknown native replies keep cleanup; cloud never invents a baseline."""
    from inspect import signature
    from conftest import load_power_sync_function
    from test_service_calls import CurrentServiceCall

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
    service_calls, persisted = [], []
    local_client = SimpleNamespace(schedule_max_backup=AsyncMock(return_value=outcome == "ack"))
    if outcome == "exception":
        local_client.schedule_max_backup.side_effect = RuntimeError("reply lost")
    entry_data = {
        "powerwall_site_control": SimpleNamespace(site_id="site"),
        "powerwall_local": {
            "client": None if route == "cloud" else local_client,
            "coordinator": SimpleNamespace(data=SimpleNamespace(backup_reserve_percent=reserve)),
        },
    }
    hass = SimpleNamespace(data={"power_sync": {"entry-1": entry_data}})
    async def persist(payload, *, is_current=None):
        assert is_current is None or is_current()
        persisted.append(dict(payload) if payload is not None else None)
        return True
    async def set_backup_reserve(request):
        service_calls.append(request.data)
        return {"success": True}
    namespace = {
        "hass": hass,
        "entry": SimpleNamespace(entry_id="entry-1"),
        "DOMAIN": "power_sync",
        "ServiceCall": object,
        "HomeAssistantError": RuntimeError,
        "SERVICE_SET_BACKUP_RESERVE": "set_backup_reserve",
        "make_service_call": load_power_sync_function("make_service_call", {"signature": signature,
            "ServiceCall": CurrentServiceCall}, path=ROOT / "service_calls.py"),
        "handle_set_backup_reserve": set_backup_reserve,
        "_cloud_tariff_control_requested": lambda _call: route == "tariff",
        "_max_backup_site_current": lambda data: data.get("max_backup_site_id") == "site",
        "_LOGGER": SimpleNamespace(error=lambda *_a, **_k: None, info=lambda *_a, **_k: None),
        "_get_tesla_coordinator_for_service": lambda _name: SimpleNamespace(_site_info_cache={}),
        "_persist_max_backup_schedule": persist,
        "_max_backup_restore": lambda *_a, **_k: None,
    }
    try:
        exec(source, namespace)
        request = SimpleNamespace(data={"duration_minutes": 30}, context=None)
        if error:
            with pytest.raises(RuntimeError, match=error):
                asyncio.run(namespace["handle_schedule_max_backup"](request))
        else:
            asyncio.run(namespace["handle_schedule_max_backup"](request))
    finally:
        for name, module in previous.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module

    if route == "local":
        local_client.schedule_max_backup.assert_awaited_once_with(1800, end_ts=persisted[0]["end_ts"])
        assert service_calls == []
        assert persisted[0]["local_event"] is True and persisted[0]["apply_pending"] is True
        assert persisted[0]["site_id"] == "site" and persisted[0]["saved_reserve"] == reserve
        assert entry_data["max_backup_end_ts"] == persisted[0]["end_ts"]
        assert persisted[-1]["apply_pending"] is bool(error)
    else:
        local_client.schedule_max_backup.assert_not_awaited()
        if reserve is None:
            assert not service_calls and not persisted and "max_backup_end_ts" not in entry_data
        else:
            assert service_calls == [{"percent": 100, "source": "user"}]
            assert persisted[0]["local_event"] is False and persisted[-1]["apply_pending"] is False


@async_test
async def test_timed_battery_dispatch_and_resume_use_distinct_typed_requests():
    transport = _transport_without_key()
    requests = []

    async def post(payload, din):
        request = transport_pb2.MessageEnvelope.FromString(payload)
        requests.append(request)
        assert din == "gateway"
        assert request.recipient.din == din
        assert request.delivery_channel == transport_pb2.DELIVERY_CHANNEL_HERMES_COMMAND
        assert request.sender.authorized_client == 1
        reply = transport_pb2.MessageEnvelope()
        reply.teg.dispatch_battery_power_response.SetInParent()
        return transport_mod.TEDAPIResponse(True, reply.SerializeToString())

    transport.post_v1r = post
    assert await transport.dispatch_battery_power("gateway", -500, 30)
    command = requests[-1].teg.dispatch_battery_power_request
    assert command.HasField("real_power")
    assert not command.HasField("resume_device_mode")
    assert command.real_power.power_watts == -500
    assert command.real_power.duration_seconds == 30
    assert await transport.resume_battery_operation("gateway")
    command = requests[-1].teg.dispatch_battery_power_request
    assert command.HasField("resume_device_mode")
    assert not command.HasField("real_power")


def test_duration_only_and_explicit_zero_dispatch_have_identical_wire_value():
    omitted=transport_pb2.MessageEnvelope()
    omitted.teg.dispatch_battery_power_request.real_power.duration_seconds=30
    explicit=transport_pb2.MessageEnvelope()
    explicit.CopyFrom(omitted)
    explicit.teg.dispatch_battery_power_request.real_power.power_watts=0
    assert omitted.SerializeToString()==explicit.SerializeToString()
    assert omitted.teg.dispatch_battery_power_request.real_power.power_watts==0


@pytest.mark.asyncio
@pytest.mark.parametrize('journal_fails',[False,True])
async def test_actual_signed_acceptance_deadline_is_durable_before_http_submission(journal_fails):
    transport=transport_mod.TEDAPIv1rTransport('192.168.1.10',_private_key_pem(),din='DIN--1')
    deadlines=[];posted=[]
    class Session:
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        def post(self,url,*,data,headers):
            signed=energy_device.signed_message_pb2.RoutableMessage.FromString(data)
            assert deadlines==[signed.signature_data.rsa_data.expires_at]
            posted.append(data)
            raise asyncio.TimeoutError
    async def acquire():return Session()
    async def persist(until):
        await asyncio.sleep(0)
        if journal_fails:raise OSError('journal unavailable')
        deadlines.append(until)
    transport._session=acquire
    if journal_fails:
        with pytest.raises(OSError):await transport.post_v1r(b'payload','DIN--1',before_submit=persist)
        assert not posted
    else:
        with pytest.raises(transport_mod.PowerwallUnreachableError):
            await transport.post_v1r(b'payload','DIN--1',before_submit=persist)
        assert posted and deadlines


@async_test
async def test_native_max_resume_preserves_fence_until_actual_transport_submission():
    transport=_transport_without_key();submitted=[];allowed=True
    async def post(payload,din,*,before_send):
        nonlocal allowed
        allowed=False  # Simulate session/auth acquisition after caller check.
        before_send()
        submitted.append(payload)
    def fence():
        if not allowed:raise RuntimeError('request superseded')
    transport.post_v1r=post
    with pytest.raises(RuntimeError,match='superseded'):
        await transport.resume_battery_operation('gateway',before_write=fence)
    assert submitted==[]


@async_test
async def test_native_max_resume_carries_durable_expiry_callback_through_client_and_transport():
    client=_bare_client();transport=_transport_without_key();events=[]
    client._transport=transport
    async def post(payload,din,*,before_send,before_submit):
        request=transport_pb2.MessageEnvelope.FromString(payload)
        assert request.teg.dispatch_battery_power_request.HasField('resume_device_mode')
        await before_submit(12345);before_send();events.append('post')
        reply=transport_pb2.MessageEnvelope();reply.teg.dispatch_battery_power_response.SetInParent()
        return transport_mod.TEDAPIResponse(True,reply.SerializeToString())
    async def durable(until):events.append(('persist',until))
    def fence():events.append('fence')
    transport.post_v1r=post
    assert await client.resume_battery_operation(before_write=fence,before_submit=durable)
    assert events==[('persist',12345),'fence','post']


@async_test
async def test_config_restore_rejects_external_owner_in_exact_read_modify_write_snapshot():
    transport=_transport_without_key();writes=[]
    async def post(payload,din,**kwargs):
        request=transport_pb2.MessageEnvelope.FromString(payload)
        assert request.filestore.HasField('read_file_request')
        reply=transport_pb2.MessageEnvelope()
        reply.filestore.read_file_response.file.blob=json.dumps({
            'site_info':{'backup_reserve_percent':30}}).encode()
        reply.filestore.read_file_response.hash=b'hash'
        writes.append('read')
        return transport_mod.TEDAPIResponse(True,reply.SerializeToString())
    transport.post_v1r=post
    assert not await transport.write_config('gateway',{'site_info.backup_reserve_percent':5},
        validate_config=lambda config:config['site_info']['backup_reserve_percent']==100)
    assert writes==['read']


@async_test
@pytest.mark.parametrize('export', ['absent', 'battery_ok', 'pv_only'])
async def test_actual_filestore_rmw_preserves_cloud_export_field_and_checks_exact_blob(export):
    transport = _transport_without_key()
    original = {'default_real_mode': 'self_consumption', 'unrelated': {'keep': True},
                'site_info': {'backup_reserve_percent': 5}}
    if export != 'absent':
        original['site_info']['customer_preferred_export_rule'] = export
    written = []
    async def post(payload, din, **kwargs):
        request = transport_pb2.MessageEnvelope.FromString(payload)
        reply = transport_pb2.MessageEnvelope()
        if request.filestore.HasField('read_file_request'):
            reply.filestore.read_file_response.file.blob = json.dumps(original).encode()
            reply.filestore.read_file_response.hash = b'fresh-original-hash'
        else:
            update = request.filestore.update_file_request
            assert update.hash == b'fresh-original-hash'
            written.append(json.loads(update.file.blob))
            reply.filestore.update_file_response.SetInParent()
        return transport_mod.TEDAPIResponse(True, reply.SerializeToString())
    transport.post_v1r = post
    result = await transport.write_config('gateway', {'default_real_mode': 'autonomous'},
        validate_config=lambda config: config['site_info'].get('customer_preferred_export_rule')
            in (None, 'battery_ok'))
    assert result is (export != 'pv_only')
    if export == 'pv_only':
        assert not written
    else:
        assert written == [dict(original, default_real_mode='autonomous')]
        assert written[0]['site_info'] == original['site_info']


@pytest.mark.parametrize("watts,seconds", [(float('nan'), 30), (float('inf'), 30), (True, 30), (500, 0), (500, -1), (500, 86401), (500, 1.5), (500, True)])
@async_test
async def test_invalid_battery_dispatch_never_reaches_gateway(watts, seconds):
    transport = _transport_without_key()
    transport.post_v1r = AsyncMock()
    with pytest.raises(ValueError):
        await transport.dispatch_battery_power("gateway", watts, seconds)
    transport.post_v1r.assert_not_called()


@pytest.mark.parametrize("payload,ok", [(b'', True), (b'bad protobuf', True), (b'\xaa\x01\x00', True), (b'', False)])
@async_test
async def test_battery_dispatch_rejects_missing_wrong_or_malformed_ack(payload, ok):
    transport = _transport_without_key()
    transport.post_v1r = AsyncMock(return_value=transport_mod.TEDAPIResponse(ok, payload))
    assert not await transport.dispatch_battery_power("gateway", 500, 30)


@async_test
async def test_disabled_local_client_never_dispatches_or_resumes():
    client = client_mod.PowerwallLocalClient.__new__(client_mod.PowerwallLocalClient)
    client._local_access_enabled = False
    client._host = 'gateway'
    client._transport = SimpleNamespace(dispatch_battery_power=AsyncMock(), resume_battery_operation=AsyncMock())
    with pytest.raises(Exception, match="LAN access is disabled"):
        await client.dispatch_battery_power(500, 30)
    with pytest.raises(Exception, match="LAN access is disabled"):
        await client.resume_battery_operation()
    client._transport.dispatch_battery_power.assert_not_called()
    client._transport.resume_battery_operation.assert_not_called()


@async_test
async def test_missing_dispatch_reply_logs_sent_power_and_reply_fields(caplog):
    transport = _transport_without_key()
    reply = transport_pb2.MessageEnvelope()
    reply.teg.get_config_response.SetInParent()
    transport.post_v1r = AsyncMock(return_value=transport_mod.TEDAPIResponse(True, reply.SerializeToString()))
    assert not await transport.dispatch_battery_power('gateway', 10800, 60)
    assert 'watts=10800.0 seconds=60' in caplog.text
    assert 'get_config_response' in caplog.text
    assert 'unexpected reply' in caplog.text


@pytest.mark.parametrize('code,message,expected', [
    (11,'power 10570.500000 exceeds discharge limit 10000',10000),
    (7,'power 10570.500000 exceeds discharge limit 10000',None),
    (11,'power 1 exceeds discharge limit 10000',None),
    (11,'power 10570.500000 exceeds discharge limit 12000',None),
    (11,'power 10570.500000 exceeds discharge limit 0',None),
    (11,'unknown error',None),
])
@async_test
async def test_only_typed_matching_lower_dispatch_limit_is_learned(code,message,expected):
    transport=_transport_without_key()
    reply=transport_pb2.MessageEnvelope()
    reply.common.error_response.status.code=code
    reply.common.error_response.status.message=message
    transport.post_v1r=AsyncMock(return_value=transport_mod.TEDAPIResponse(True,reply.SerializeToString()))
    assert not await transport.dispatch_battery_power('gateway',10570.5,12)
    assert getattr(transport,'dispatch_discharge_limit_w',None)==expected


@pytest.mark.parametrize('watts,code,message', [
    (-12050, 11, 'requested charge power exceeds gateway allowance'),
    (500, 12, 'battery dispatch is not implemented'),
])
@async_test
async def test_dispatch_gateway_error_is_reported_without_accepting_or_retrying(watts, code, message, caplog):
    transport = _transport_without_key()
    reply = transport_pb2.MessageEnvelope()
    reply.common.error_response.status.code = code
    reply.common.error_response.status.message = message
    transport.post_v1r = AsyncMock(return_value=transport_mod.TEDAPIResponse(True, reply.SerializeToString()))
    assert not await transport.dispatch_battery_power('gateway', watts, 30)
    transport.post_v1r.assert_awaited_once()
    assert f'code={code}' in caplog.text
    assert message in caplog.text
    assert getattr(transport, 'dispatch_discharge_limit_w', None) is None


@async_test
async def test_dispatch_gateway_error_message_is_bounded(caplog):
    transport = _transport_without_key()
    reply = transport_pb2.MessageEnvelope()
    reply.common.error_response.status.code = 12
    reply.common.error_response.status.message = 'x' * 350 + 'OMITTED_DETAIL'
    transport.post_v1r = AsyncMock(return_value=transport_mod.TEDAPIResponse(True, reply.SerializeToString()))
    assert not await transport.dispatch_battery_power('gateway', -500, 30)
    assert 'x' * 350 in caplog.text
    assert 'OMITTED_DETAIL' not in caplog.text


@pytest.mark.parametrize('watts,code,message,expected', [
    (-12000, 11, 'power -12000.000000 exceeds charge limit -10000', 10000),
    (-12000, 7, 'power -12000.000000 exceeds charge limit -10000', None),
    (-12000, 11, 'power -12001.000000 exceeds charge limit -10000', None),
    (-12000, 11, 'power -12000.000000 exceeds charge limit 10000', None),
    (-12000, 11, 'power -12000.000000 exceeds charge limit -13000', None),
    (-12000, 11, 'power -12000.000000 exceeds charge limit 0', None),
    (-12000, 11, 'power -12000.000000 exceeds discharge limit -10000', None),
    (12000, 11, 'power 12000.000000 exceeds charge limit -10000', None),
])
@async_test
async def test_only_matching_signed_lower_charge_ceiling_is_learned(watts, code, message, expected):
    transport = _transport_without_key()
    reply = transport_pb2.MessageEnvelope()
    reply.common.error_response.status.code = code
    reply.common.error_response.status.message = message
    transport.post_v1r = AsyncMock(return_value=transport_mod.TEDAPIResponse(True, reply.SerializeToString()))
    assert not await transport.dispatch_battery_power('gateway', watts, 30)
    assert getattr(transport, 'dispatch_charge_limit_w', None) == expected
    assert getattr(transport, 'dispatch_discharge_limit_w', None) is None


@async_test
async def test_gateway_charge_ceiling_never_increases_from_later_rejections():
    transport = _transport_without_key()
    transport.dispatch_charge_limit_w = 9000
    reply = transport_pb2.MessageEnvelope()
    reply.common.error_response.status.code = 11
    reply.common.error_response.status.message = 'power -12000.000000 exceeds charge limit -10000'
    transport.post_v1r = AsyncMock(return_value=transport_mod.TEDAPIResponse(True, reply.SerializeToString()))
    assert not await transport.dispatch_battery_power('gateway', -12000, 30)
    assert transport.dispatch_charge_limit_w == 9000


@async_test
async def test_snapshot_retries_nonempty_dcq_missing_soc_before_control():
    client = _bare_client()
    partial = {"control": {"systemStatus": {}, "meterAggregates": []}}
    client._transport = SimpleNamespace(
        post_v1r=AsyncMock(side_effect=[_dcq_snapshot_reply(partial), _dcq_snapshot_reply()]),
        read_config=AsyncMock(return_value={}))
    snap = await client.get_snapshot()
    assert snap.soc is not None
    assert snap.grid_status == "SystemGridConnected"
    assert client._transport.post_v1r.await_count == 2


@async_test
async def test_partial_snapshot_retry_is_bounded_and_never_fills_from_previous_read():
    client = _bare_client()
    first = {"control": {"systemStatus": {"nominalFullPackEnergyWh": 13500}, "meterAggregates": []}}
    second = {"control": {"systemStatus": {"nominalEnergyRemainingWh": 9000}, "meterAggregates": []}}
    client._transport = SimpleNamespace(
        post_v1r=AsyncMock(side_effect=[_dcq_snapshot_reply(first), _dcq_snapshot_reply(second), _dcq_snapshot_reply()]),
        read_config=AsyncMock(side_effect=[{"old": True}, {"latest": True}]))
    snap = await client.get_snapshot()
    assert snap.soc is None
    assert snap.raw['config'] == {"latest": True}
    assert client._transport.post_v1r.await_count == 2


@async_test
async def test_snapshot_retry_timestamp_belongs_to_returned_attempt(monkeypatch):
    client = _bare_client()
    clock = SimpleNamespace(at=1000.)
    async def fetch(*_):
        clock.at += 6
        return _dcq_snapshot_reply({"control": {}}) if clock.at == 1006 else _dcq_snapshot_reply()
    client._transport = SimpleNamespace(post_v1r=AsyncMock(side_effect=fetch),read_config=AsyncMock(return_value={}))
    monkeypatch.setattr(client_mod, 'time', SimpleNamespace(monotonic=lambda: clock.at))
    snap = await client.get_snapshot()
    assert snap.observed_monotonic == 1006
    assert clock.at == 1012


@async_test
async def test_control_snapshot_refresh_does_not_repeat_native_config_read():
    client = _bare_client()
    cfg = {"site_info": {"max_site_meter_power_ac": 14.9}}
    client._transport = SimpleNamespace(post_v1r=AsyncMock(return_value=_dcq_snapshot_reply()),
                                       read_config=AsyncMock(side_effect=AssertionError('unexpected config read')))
    snap = await client.get_control_snapshot(cfg)
    assert snap.soc is not None
    assert snap.raw['config'] == cfg
    client._transport.read_config.assert_not_awaited()
    client._transport.post_v1r.assert_awaited_once()


@async_test
async def test_zero_soc_is_not_retried_as_missing():
    client = _bare_client()
    payload = {"control": {"systemStatus": {"nominalFullPackEnergyWh": 13500, "nominalEnergyRemainingWh": 0},
        "islanding": {"contactorClosed": True, "gridOK": True, "microGridOK": True},
        "meterAggregates": [{"location": key, "realPowerW": value} for key, value in
                            (("site", 800), ("load", 1000), ("solar", 200), ("battery", 0))]}}
    client._transport = SimpleNamespace(post_v1r=AsyncMock(return_value=_dcq_snapshot_reply(payload)),
                                       read_config=AsyncMock(return_value={}))
    snap = await client.get_snapshot()
    assert snap.soc == 0
    client._transport.post_v1r.assert_awaited_once()
