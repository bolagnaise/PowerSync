"""Tests for Tesla Hermes signaling diagnostics."""

from __future__ import annotations

import asyncio
import base64
import importlib.util
import json
import sys
from pathlib import Path


ROOT = (
    Path(__file__).resolve().parent.parent
    / "custom_components"
    / "power_sync"
    / "powerwall_local"
)


def _load_signaling_module():
    name = "powerwall_signaling_test_module"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, ROOT / "signaling.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


signaling = _load_signaling_module()


def _jwt_with_scopes(scopes: list[str]) -> str:
    header = {"alg": "RS256", "typ": "JWT"}
    payload = {"scp": scopes}

    def enc(data: dict) -> str:
        raw = json.dumps(data, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    return f"{enc(header)}.{enc(payload)}.sig"


class _FakeResponse:
    status = 403

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def text(self):
        return '{"response":null,"error":"Unauthorized missing scopes","error_description":""}'


class _FakeSession:
    post_calls = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    def post(self, *args, **kwargs):
        type(self).post_calls += 1
        return _FakeResponse()


class _UnsupportedResponse:
    status = 412

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def text(self):
        return json.dumps(
            {
                "error": "hermes_jwt_exchange_failed",
                "status": 412,
                "detail": json.dumps(
                    {
                        "response": None,
                        "error": (
                            "Not supported. Use signed_command endpoint to send commands."
                        ),
                    }
                ),
            }
        )


class _UnsupportedSession:
    post_calls = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    def post(self, *args, **kwargs):
        type(self).post_calls += 1
        return _UnsupportedResponse()


class _UnrelatedPreconditionResponse(_UnsupportedResponse):
    async def text(self):
        return '{"error":"precondition_failed","detail":"vehicle unavailable"}'


class _UnrelatedPreconditionSession(_UnsupportedSession):
    def post(self, *args, **kwargs):
        type(self).post_calls += 1
        return _UnrelatedPreconditionResponse()


def test_decode_jwt_scopes_supports_tesla_scp_claim():
    token = _jwt_with_scopes(["openid", "energy_device_data"])

    assert signaling._decode_jwt_scopes(token) == [
        "openid",
        "energy_device_data",
    ]


def test_unsupported_response_matches_direct_and_proxy_shapes():
    direct_body = json.dumps(
        {
            "response": None,
            "error": "Not supported. Use signed_command endpoint to send commands.",
        }
    )
    proxy_body = json.dumps(
        {
            "error": "hermes_jwt_exchange_failed",
            "status": 412,
            "detail": direct_body,
        }
    )

    assert signaling._is_hermes_unsupported_response(412, direct_body) is True
    assert signaling._is_hermes_unsupported_response(412, proxy_body) is True
    assert signaling._is_hermes_unsupported_response(200, direct_body) is False
    assert (
        signaling._is_hermes_unsupported_response(
            412,
            '{"error":"precondition_failed","detail":"vehicle unavailable"}',
        )
        is False
    )


def test_missing_scope_response_stops_before_raw_websocket_fallback(monkeypatch, caplog):
    async def get_token():
        return _jwt_with_scopes(["openid", "offline_access", "energy_device_data"])

    caplog.set_level("WARNING")
    _FakeSession.post_calls = 0
    monkeypatch.setattr(signaling.aiohttp, "ClientSession", _FakeSession)
    monkeypatch.setattr(signaling, "HERMES_JWT_URLS", ["https://fleet.test/hermes"])

    client = signaling.TeslaSignalingClient(get_token, "1152100--TEST")

    result = asyncio.run(client._get_hermes_jwt())

    assert result is None
    assert client._auth_denied is True
    assert client._stop_event.is_set() is True
    assert client.state == signaling.SignalingState.UNAVAILABLE
    assert client._hermes_jwt is None
    assert client._hermes_jwt_is_fallback is False
    assert client.health_status()["unavailable_reason"] == (
        "Tesla rejected the access token for Hermes JWT exchange "
        "because it is missing required scopes"
    )
    assert _FakeSession.post_calls == 1
    assert "missing required scopes" in caplog.text
    assert "Fleet API telemetry may still work" in caplog.text
    assert "Likely missing scope(s): user_data" in caplog.text
    assert not [record for record in caplog.records if record.levelname == "ERROR"]


def test_unsupported_exchange_stops_endpoint_and_raw_token_fallback(monkeypatch, caplog):
    token = _jwt_with_scopes(
        [
            "openid",
            "user_data",
            "vehicle_cmds",
            "vehicle_charging_cmds",
        ]
    )

    async def get_token():
        return token

    caplog.set_level("WARNING")
    _UnsupportedSession.post_calls = 0
    monkeypatch.setattr(signaling.aiohttp, "ClientSession", _UnsupportedSession)
    monkeypatch.setattr(
        signaling,
        "HERMES_JWT_URLS",
        ["https://proxy.test/hermes", "https://fleet.test/hermes"],
    )

    client = signaling.TeslaSignalingClient(get_token, "1152100--TEST")

    result = asyncio.run(client._get_hermes_jwt())

    assert result is None
    assert client._auth_denied is True
    assert client._stop_event.is_set() is True
    assert client.state == signaling.SignalingState.UNAVAILABLE
    assert client._hermes_jwt is None
    assert client._hermes_jwt_is_fallback is False
    assert client.health_status()["unavailable_reason"] == (
        "Tesla does not support Hermes JWT exchange for this access token; "
        "signed_command is required"
    )
    assert _UnsupportedSession.post_calls == 1
    assert "permanent signed_command requirement" in caplog.text
    assert "Stopping endpoint retries and raw-token fallback" in caplog.text


def test_unrelated_412_still_uses_existing_fallback(monkeypatch):
    token = _jwt_with_scopes(["openid", "user_data"])

    async def get_token():
        return token

    _UnrelatedPreconditionSession.post_calls = 0
    monkeypatch.setattr(
        signaling.aiohttp,
        "ClientSession",
        _UnrelatedPreconditionSession,
    )
    monkeypatch.setattr(signaling, "HERMES_JWT_URLS", ["https://fleet.test/hermes"])

    client = signaling.TeslaSignalingClient(get_token, "1152100--TEST")

    result = asyncio.run(client._get_hermes_jwt())

    assert result == token
    assert client._auth_denied is False
    assert client.state == signaling.SignalingState.DISCONNECTED
    assert client._hermes_jwt == token
    assert client._hermes_jwt_is_fallback is True
    assert _UnrelatedPreconditionSession.post_calls == 1
