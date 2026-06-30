"""Regression tests for FoxESS Cloud API helpers."""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import sys
import types
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent
COMPONENT_ROOT = ROOT / "custom_components" / "power_sync"
_SENTINEL = object()


@pytest.fixture()
def foxess_api_module():
    saved_modules = {
        name: sys.modules.get(name, _SENTINEL)
        for name in ("power_sync", "power_sync.foxess_api", "power_sync.const")
    }

    power_sync = types.ModuleType("power_sync")
    power_sync.__path__ = [str(COMPONENT_ROOT)]
    sys.modules["power_sync"] = power_sync

    try:
        yield importlib.import_module("power_sync.foxess_api")
    finally:
        sys.modules.pop("power_sync.foxess_api", None)
        for name, module in saved_modules.items():
            if module is _SENTINEL:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module


class _FakeResp:
    def __init__(self, status, payload):
        self.status = status
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def json(self):
        return self._payload

    async def text(self):
        return str(self._payload)


class _FakeSession:
    def __init__(self, status, payload):
        self.closed = False
        self.status = status
        self.payload = payload
        self.requests = []

    def request(self, method, url, **kwargs):
        self.requests.append((method, url, kwargs))
        return _FakeResp(self.status, self.payload)


def _run_request(foxess_api_module, status, payload):
    client = foxess_api_module.FoxESSCloudClient("api-key", "INV123")
    client._session = _FakeSession(status, payload)
    return asyncio.run(client._request("POST", "/op/test", {"x": 1}))


def test_request_accepts_code_field_and_unwraps_result(foxess_api_module):
    assert _run_request(foxess_api_module, 200, {"code": 0, "result": {"value": 7}}) == {"value": 7}


def test_request_accepts_errno_200_and_falls_back_to_data(foxess_api_module):
    assert _run_request(foxess_api_module, 200, {"errno": 200, "data": [1, 2]}) == [1, 2]


def test_request_raises_foxess_api_error_with_code(foxess_api_module):
    with pytest.raises(foxess_api_module.FoxESSApiError) as excinfo:
        _run_request(
            foxess_api_module, 200, {"errno": 44096, "msg": "unsupported function code"}
        )
    assert str(excinfo.value.code) == "44096"


def test_request_http_error_carries_status_code(foxess_api_module):
    with pytest.raises(foxess_api_module.FoxESSApiError) as excinfo:
        _run_request(foxess_api_module, 500, {"msg": "boom"})
    assert excinfo.value.code == "http_500"


def test_signature_uses_literal_crlf_separator(foxess_api_module, monkeypatch):
    monkeypatch.setattr(foxess_api_module.time, "time", lambda: 1712345678.901)

    client = foxess_api_module.FoxESSCloudClient("api-key", "INV123")
    headers = client._generate_signature("/op/v0/device/list")

    expected = hashlib.md5(
        b"/op/v0/device/list\\r\\napi-key\\r\\n1712345678901"
    ).hexdigest()
    crlf_signature = hashlib.md5(
        b"/op/v0/device/list\r\napi-key\r\n1712345678901"
    ).hexdigest()
    assert headers["timestamp"] == "1712345678901"
    assert headers["signature"] == expected
    assert headers["signature"] != crlf_signature


def test_real_data_query_uses_v1_sns_shape(foxess_api_module, monkeypatch):
    client = foxess_api_module.FoxESSCloudClient("api-key", "INV123")
    calls = []

    async def fake_post(path, payload, *, write=False):
        calls.append((path, payload, write))
        return {"ok": True}

    monkeypatch.setattr(client, "_post", fake_post)

    result = asyncio.run(client.get_real_data())

    assert result == {"ok": True}
    assert calls == [
        (
            "/op/v1/device/real/query",
            {
                "sns": ["INV123"],
                "variables": [
                    "pvPower",
                    "gridConsumptionPower",
                    "feedinPower",
                    "meterPower",
                    "loadsPower",
                    "batPower",
                    "invBatPower",
                    "batChargePower",
                    "batDischargePower",
                    "SoC",
                    "workMode",
                    "generationPower",
                    "chargePower",
                    "dischargePower",
                    "chargeEnergyToTal",
                    "dischargeEnergyToTal",
                ],
            },
            False,
        )
    ]


def test_scheduler_set_uses_v3_groups_and_extra_param(foxess_api_module, monkeypatch):
    client = foxess_api_module.FoxESSCloudClient("api-key", "INV123")
    calls = []

    async def fake_post(path, payload, *, write=False):
        calls.append((path, payload, write))
        return {"ok": True}

    monkeypatch.setattr(client, "_post", fake_post)

    asyncio.run(
        client.set_scheduler(
            "INV123",
            [
                {
                    "startHour": 1,
                    "startMinute": 30,
                    "endHour": 2,
                    "endMinute": 0,
                    "workMode": "ForceCharge",
                    "minSocOnGrid": 20,
                    "fdSoc": 90,
                    "fdPwr": 5000,
                    "maxSoc": 95,
                }
            ],
        )
    )

    path, payload, write = calls[0]
    assert path == "/op/v3/device/scheduler/enable"
    assert write is True
    assert payload["deviceSN"] == "INV123"
    assert payload["groups"][0]["workMode"] == "ForceCharge"
    assert payload["groups"][0]["extraParam"] == {
        "minSocOnGrid": 20.0,
        "fdSoc": 90.0,
        "fdPwr": 5000.0,
        "maxSoc": 95.0,
    }
    assert "minSocOnGrid" not in payload["groups"][0]
    assert "fdPwr" not in payload["groups"][0]


def test_scheduler_set_preserves_explicit_extra_limits(foxess_api_module, monkeypatch):
    client = foxess_api_module.FoxESSCloudClient("api-key", "INV123")
    calls = []

    async def fake_post(path, payload, *, write=False):
        calls.append((path, payload, write))
        return {"ok": True}

    monkeypatch.setattr(client, "_post", fake_post)

    asyncio.run(
        client.set_scheduler(
            "INV123",
            [
                {
                    "startHour": 0,
                    "startMinute": 0,
                    "endHour": 23,
                    "endMinute": 59,
                    "workMode": "SelfUse",
                    "exportLimit": 12000,
                    "pvLimit": 8000,
                    "reactivePower": 0,
                }
            ],
        )
    )

    extra = calls[0][1]["groups"][0]["extraParam"]
    assert extra["exportLimit"] == 12000.0
    assert extra["pvLimit"] == 8000.0
    assert extra["reactivePower"] == 0.0
    assert "importLimit" not in extra


def test_setting_soc_and_modbus_passthrough_payloads(foxess_api_module, monkeypatch):
    client = foxess_api_module.FoxESSCloudClient("api-key", "INV123")
    post_calls = []
    get_calls = []

    async def fake_post(path, payload, *, write=False):
        post_calls.append((path, payload, write))
        return {"ok": True}

    async def fake_get(path, params):
        get_calls.append((path, params))
        return {"ok": True}

    monkeypatch.setattr(client, "_post", fake_post)
    monkeypatch.setattr(client, "_get", fake_get)

    asyncio.run(client.set_device_setting("WorkMode", "SelfUse"))
    asyncio.run(client.get_battery_soc())
    asyncio.run(client.set_battery_soc(120, -5))
    asyncio.run(client.send_modbus_command("LOGGER1", "AQIDBA==", timeout=12))

    assert post_calls[0] == (
        "/op/v0/device/setting/set",
        {"sn": "INV123", "key": "WorkMode", "value": "SelfUse"},
        True,
    )
    assert get_calls == [("/op/v0/device/battery/soc/get", {"sn": "INV123"})]
    assert post_calls[1] == (
        "/op/v0/device/battery/soc/set",
        {"sn": "INV123", "minSoc": 100, "minSocOnGrid": 0},
        True,
    )
    assert post_calls[2] == (
        "/op/v0/module/modbus/commands",
        {"sn": "LOGGER1", "timeout": 12, "data": "AQIDBA=="},
        True,
    )


def test_set_work_mode_disables_scheduler_and_retries_on_44096(foxess_api_module, monkeypatch):
    client = foxess_api_module.FoxESSCloudClient("api-key", "INV123")
    setting_calls = []
    flag_calls = []

    async def fake_set_setting(key, value, sn=""):
        setting_calls.append((key, value))
        if len(setting_calls) == 1:
            raise foxess_api_module.FoxESSApiError(
                "FoxESS API error 44096: unsupported function code", 44096
            )
        return {"ok": True}

    async def fake_flag(enabled, sn=""):
        flag_calls.append(enabled)
        return {"ok": True}

    monkeypatch.setattr(client, "set_device_setting", fake_set_setting)
    monkeypatch.setattr(client, "set_scheduler_flag", fake_flag)

    asyncio.run(client.set_work_mode("Backup"))

    # First write is rejected, scheduler flag disabled, then the write is retried.
    assert setting_calls == [("WorkMode", "Backup"), ("WorkMode", "Backup")]
    assert flag_calls == [False]


def test_set_work_mode_propagates_non_scheduler_errors(foxess_api_module, monkeypatch):
    client = foxess_api_module.FoxESSCloudClient("api-key", "INV123")

    async def fake_set_setting(key, value, sn=""):
        raise foxess_api_module.FoxESSApiError("FoxESS API error 40256: illegal signature", 40256)

    async def fake_flag(enabled, sn=""):
        raise AssertionError("scheduler flag must not be touched for non-44096 errors")

    monkeypatch.setattr(client, "set_device_setting", fake_set_setting)
    monkeypatch.setattr(client, "set_scheduler_flag", fake_flag)

    with pytest.raises(foxess_api_module.FoxESSApiError):
        asyncio.run(client.set_work_mode("Backup"))


def test_set_device_setting_optional_swallows_unsupported_key(foxess_api_module, monkeypatch):
    client = foxess_api_module.FoxESSCloudClient("api-key", "INV123")

    async def unsupported(key, value, sn=""):
        raise foxess_api_module.FoxESSApiError(
            "FoxESS API error 40257: this device does not currently support", 40257
        )

    monkeypatch.setattr(client, "set_device_setting", unsupported)
    assert asyncio.run(client.set_device_setting_optional("ActivePowerLimit", 5000)) is False

    async def other_error(key, value, sn=""):
        raise foxess_api_module.FoxESSApiError("FoxESS API error 40256", 40256)

    monkeypatch.setattr(client, "set_device_setting", other_error)
    with pytest.raises(foxess_api_module.FoxESSApiError):
        asyncio.run(client.set_device_setting_optional("ActivePowerLimit", 5000))


def test_scheduler_flag_uses_v1_enable_payload(foxess_api_module, monkeypatch):
    client = foxess_api_module.FoxESSCloudClient("api-key", "INV123")
    calls = []

    async def fake_post(path, payload, *, write=False):
        calls.append((path, payload, write))
        return {"ok": True}

    monkeypatch.setattr(client, "_post", fake_post)

    asyncio.run(client.set_scheduler_flag(False))

    assert calls == [
        ("/op/v1/device/scheduler/set/flag", {"deviceSN": "INV123", "enable": 0}, True)
    ]


def test_scheduler_helpers_filter_hidden_defaults_and_extract_serials(foxess_api_module):
    assert foxess_api_module._extract_device_sn({"deviceSN": "AAA"}) == "AAA"
    assert foxess_api_module._extract_device_sn({"sn": "BBB"}) == "BBB"
    assert foxess_api_module._extract_device_sn({"serialNumber": "CCC"}) == "CCC"

    filtered = foxess_api_module.filter_public_scheduler_groups(
        [
            {"isRemainMode": True, "workMode": "SelfUse"},
            {
                "startHour": 0,
                "startMinute": 0,
                "endHour": 23,
                "endMinute": 59,
                "workMode": "SelfUse",
            },
            {
                "startHour": 6,
                "startMinute": 0,
                "endHour": 7,
                "endMinute": 30,
                "workMode": "ForceDischarge",
                "fdPwr": 3000,
            },
        ]
    )

    assert len(filtered) == 1
    assert filtered[0]["workMode"] == "ForceDischarge"
    assert filtered[0]["extraParam"]["fdPwr"] == 3000.0


def test_price_conversion_emits_scheduler_v3_extra_params(foxess_api_module):
    groups = foxess_api_module.convert_prices_to_foxess_schedule(
        buy_prices=[
            {"timeRange": "00:00-00:30", "price": 1.0},
            {"timeRange": "00:30-01:00", "price": 30.0},
        ],
        sell_prices=[
            {"timeRange": "00:00-00:30", "price": 0.0},
            {"timeRange": "00:30-01:00", "price": 50.0},
        ],
        min_soc=25,
        charge_soc=85,
    )

    assert [group["workMode"] for group in groups] == [
        "ForceCharge",
        "ForceDischarge",
    ]
    assert groups[0]["extraParam"]["minSocOnGrid"] == 25.0
    assert groups[0]["extraParam"]["fdSoc"] == 85.0
    assert groups[1]["extraParam"]["fdSoc"] == 25.0
    assert "fdPwr" not in groups[0]
