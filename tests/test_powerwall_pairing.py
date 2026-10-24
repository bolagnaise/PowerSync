"""Tests for Powerwall local pairing Fleet API payloads."""

from __future__ import annotations

import importlib.util
import asyncio
import base64
import sys
import types
from pathlib import Path

import pytest


ROOT = (
    Path(__file__).resolve().parent.parent
    / "custom_components"
    / "power_sync"
    / "powerwall_local"
)


def _load_pairing_module():
    pkg = types.ModuleType("power_sync")
    pkg.__path__ = [str(ROOT.parent)]
    sys.modules.setdefault("power_sync", pkg)

    local_pkg = types.ModuleType("power_sync.powerwall_local")
    local_pkg.__path__ = [str(ROOT)]
    sys.modules.setdefault("power_sync.powerwall_local", local_pkg)

    exc_spec = importlib.util.spec_from_file_location(
        "power_sync.powerwall_local.exceptions", ROOT / "exceptions.py"
    )
    assert exc_spec is not None and exc_spec.loader is not None
    exceptions = importlib.util.module_from_spec(exc_spec)
    sys.modules["power_sync.powerwall_local.exceptions"] = exceptions
    exc_spec.loader.exec_module(exceptions)

    spec = importlib.util.spec_from_file_location(
        "power_sync.powerwall_local.pairing", ROOT / "pairing.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["power_sync.powerwall_local.pairing"] = module
    spec.loader.exec_module(module)
    return module


pairing = _load_pairing_module()


def test_authorization_command_payload_includes_fleet_api_metadata():
    payload = pairing._authorization_command_payload(
        "add_authorized_client_request",
        {"add_authorized_client_request": {"public_key": "abc"}},
    )

    assert payload["category"] == "authorization"
    assert payload["command_name"] == "add_authorized_client_request"
    assert payload["command_type"] == "grpc_command"
    assert payload["command_properties"] == {
        "message": {
            "authorization": {
                "add_authorized_client_request": {"public_key": "abc"}
            }
        },
        "identifier_type": 1,
    }


def test_list_authorized_clients_payload_uses_same_envelope():
    payload = pairing._authorization_command_payload(
        "list_authorized_clients_request",
        {"list_authorized_clients_request": {}},
    )

    assert payload["category"] == "authorization"
    assert payload["command_name"] == "list_authorized_clients_request"
    assert payload["command_properties"]["message"]["authorization"] == {
        "list_authorized_clients_request": {}
    }


def test_pairing_success_schedules_local_warmup_without_blocking():
    views_source = (ROOT / "views.py").read_text()
    button_source = (ROOT.parent / "button.py").read_text()

    # Background task (not async_create_task) so a slow first poll on an
    # unreachable gateway can never stall HA bootstrap.
    assert "hass.async_create_background_task(" in views_source
    assert "_warm_powerwall_local_coordinator(hass, entry)" in views_source
    assert "_schedule_local_coordinator_warmup(self._hass, entry)" in views_source
    assert "_schedule_local_coordinator_warmup(hass, entry)" in button_source


PUBLIC_KEY = b"this attempt's DER public key"
PUBLIC_KEY_B64 = base64.b64encode(PUBLIC_KEY).decode()


class Reply:
    def __init__(self, data, status=200):
        self.data, self.status = data, status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def json(self):
        return self.data

    async def text(self):
        return "Rejected request"


def client(state, key=PUBLIC_KEY_B64):
    return {"public_key": key, "state": state}


class TeslemetrySession:
    """Only accepts Teslemetry's documented custom pairing endpoints."""

    def __init__(self, polls, register_status=200):
        self.polls = iter(polls)
        self.register_status = register_status
        self.calls = []

    def get(self, url, *, headers):
        self.calls.append(("GET", url, headers))
        assert headers["Authorization"] == "Bearer provider-token"
        if url.endswith("/products"):
            return Reply({"response": [{"energy_site_id": 123, "gateway_id": "gateway-din"}]})
        assert url == "https://api.teslemetry.com/api/1/energy_sites/123/command/authorized_clients"
        poll = next(self.polls)
        return poll if isinstance(poll, Reply) else Reply(poll)

    def post(self, url, *, json, headers):
        self.calls.append(("POST", url, json))
        assert headers["Authorization"] == "Bearer provider-token"
        if not url.endswith("/command/add_authorized_client"):
            return Reply({}, 422)
        assert json == {
            "key_type": 1, "public_key": PUBLIC_KEY_B64,
            "authorized_client_type": 1, "description": "PowerSync Local Client",
        }
        return Reply({"response": {"client": client(2)}}, self.register_status)


def prepare_attempt(monkeypatch, session, provider="teslemetry"):
    clock = types.SimpleNamespace(now=100.0)
    monkeypatch.setattr(pairing, "time", types.SimpleNamespace(time=lambda: clock.now))

    async def sleep(_):
        clock.now += 1

    monkeypatch.setattr(pairing.asyncio, "sleep", sleep)
    private = types.SimpleNamespace(private_bytes=lambda **kwargs: b"private-pem")
    monkeypatch.setattr(pairing, "_generate_rsa_4096", lambda: (private, PUBLIC_KEY))
    completed = []

    async def on_success(result):
        completed.append(result)

    manager = pairing.PowerwallPairingManager(
        session, "https://api.teslemetry.com", "provider-token",
        provider=provider, energy_site_id=123, window_seconds=3, on_success=on_success,
    )
    return manager, completed


def test_teslemetry_pairing_uses_provider_contract_and_verifies_own_key(monkeypatch):
    session = TeslemetrySession([
        {"response": {"clients": [client(2), client(3, "b2xkLWtleQ==")]}},
        {"response": {"clients": [client(3), client(2, "b2xkLWtleQ==")]}},
    ])
    manager, completed = prepare_attempt(monkeypatch, session)

    async def run():
        await manager.start()
        await manager._task_record.task

    asyncio.run(run())
    assert manager.status().state == pairing.PairingState.VERIFIED
    assert manager.status().key_state == 3
    assert len(session.calls) == 4  # products, register, pending, verified
    assert len(completed) == 1
    assert completed[0].public_key_der == PUBLIC_KEY
    assert completed[0].din == "gateway-din"


@pytest.mark.parametrize("poll", [
    None, {}, {"response": None},
    {"response": {"clients": [client(3, "b2xkLWtleQ=="), client(2)]}},
    {"response": {"clients": [client(3, "b2xkLWtleQ==")]}},
    {"response": {"clients": [{"state": 3}]}},
    {"response": {"clients": [client(True)]}},
    {"response": {"clients": [client(3.7)]}},
])
def test_teslemetry_foreign_or_missing_key_never_pairs(monkeypatch, poll):
    session = TeslemetrySession([poll] * 5)
    manager, completed = prepare_attempt(monkeypatch, session)

    async def run():
        await manager.start()
        await manager._task_record.task

    asyncio.run(run())
    assert manager.status().state == pairing.PairingState.TIMEOUT
    assert manager.result() is None
    assert not completed


def test_teslemetry_registration_failure_never_polls_or_persists(monkeypatch):
    session = TeslemetrySession([], register_status=403)
    manager, completed = prepare_attempt(monkeypatch, session)

    async def run():
        await manager.start()
        await manager._task_record.task

    asyncio.run(run())
    assert manager.status().state == pairing.PairingState.FAILED
    assert len(session.calls) == 2
    assert not completed


def test_teslemetry_poll_http_failure_can_recover(monkeypatch):
    session = TeslemetrySession([
        Reply({}, 503), {"response": {"clients": [client(3)]}},
    ])
    manager, completed = prepare_attempt(monkeypatch, session)

    async def run():
        await manager.start()
        await manager._task_record.task

    asyncio.run(run())
    assert manager.status().state == pairing.PairingState.VERIFIED
    assert len(session.calls) == 4
    assert len(completed) == 1


def test_teslemetry_cancel_during_poll_never_persists(monkeypatch):
    session = TeslemetrySession([{"response": {"clients": [client(2)]}}])
    manager, completed = prepare_attempt(monkeypatch, session)

    async def run():
        waiting = asyncio.Event()

        async def sleep(_):
            waiting.set()
            await asyncio.Future()

        monkeypatch.setattr(pairing.asyncio, "sleep", sleep)
        await manager.start()
        await waiting.wait()
        await manager.cancel()

    asyncio.run(run())
    assert manager.status().state == pairing.PairingState.CANCELLED
    assert manager.result() is None
    assert not completed


@pytest.mark.parametrize("provider", ["fleet_api", "powersync"])
def test_other_providers_keep_fleet_transport_and_response(monkeypatch, provider):
    class FleetSession(TeslemetrySession):
        def post(self, url, *, json, headers):
            self.calls.append(("POST", url, json))
            assert url.endswith("/api/1/energy_sites/123/command")
            assert headers["Authorization"] == "Bearer provider-token"
            name = json["command_name"]
            fields = (
                {"key_type": 1, "public_key": PUBLIC_KEY_B64,
                 "authorized_client_type": 1, "description": "PowerSync Local Client"}
                if name == "add_authorized_client_request" else {}
            )
            assert json == pairing._authorization_command_payload(name, {name: fields})
            message = (
                {"AddAuthorizedClientResponse": {"client": client(2)}}
                if name == "add_authorized_client_request"
                else {"ListAuthorizedClientsResponse": {"clients": [client(3)]}}
            )
            return Reply({"response": {"message": {"Payload": {
                "Authorization": {"Message": message},
            }}}})

    session = FleetSession([])
    manager, completed = prepare_attempt(monkeypatch, session, provider)

    async def run():
        await manager.start()
        await manager._task_record.task

    asyncio.run(run())
    assert manager.status().state == pairing.PairingState.VERIFIED
    assert len(session.calls) == 3
    assert len(completed) == 1
