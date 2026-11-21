"""Tests for Zaptec Cloud state parsing."""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest


MODULE_PATH = (
    Path(__file__).resolve().parent.parent
    / "custom_components"
    / "power_sync"
    / "zaptec_api.py"
)
SPEC = importlib.util.spec_from_file_location("zaptec_api", MODULE_PATH)
zaptec_api = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(zaptec_api)


def test_charger_state_accepts_vendor_casing_and_excludes_other_chargers():
    client = zaptec_api.ZaptecCloudClient("user@example.com", "password")
    client._request = AsyncMock(return_value=[
        {"chargerId": "charger-1", "stateId": 710, "valueAsString": "3"},
        {"ChargerId": "CHARGER-1", "StateId": 513, "ValueAsString": "0"},
        {"chargerId": "charger-2", "stateId": 513, "valueAsString": "11000"},
        {"ChargerId": "charger-2", "StateId": 507, "ValueAsString": "16"},
    ])

    assert asyncio.run(client.get_charger_state("charger-1")) == {710: "3", 513: "0"}


@pytest.mark.parametrize("final_stop,event,mode", [
    ("0", None, "connected_finished"),
    ("1", None, "charging_paused"),
    (True, "2026-11-22T03:00:00Z", "charging_paused"),
])
def test_mode_five_distinguishes_pause_from_completion(final_stop, event, mode):
    client = zaptec_api.ZaptecCloudClient("user@example.com", "password")
    parsed = client.parse_charger_state({710: "5", 718: final_stop, 763: event})

    assert parsed["charger_operation_mode"] == mode
    assert parsed["cable_locked"] is True
    assert parsed["schedule_active"] is bool(event)


def test_parse_charger_state_uses_zaptec_observation_ids():
    client = zaptec_api.ZaptecCloudClient("user@example.com", "password")

    parsed = client.parse_charger_state(
        {
            120: "1",
            151: "true",
            507: "6.1",
            508: "7.2",
            509: "8.3",
            520: "99",
            521: "98",
            522: "97",
            553: "1.25",
            710: "3",
            911: "1.2.3",
        }
    )

    assert parsed["charger_operation_mode"] == "charging"
    assert parsed["cable_locked"] is True
    assert parsed["permanent_cable_lock"] is True
    assert parsed["session_energy_wh"] == 1250
    assert parsed["phase_a_current"] == 6.1
    assert parsed["phase_b_current"] == 7.2
    assert parsed["phase_c_current"] == 8.3
    assert parsed["firmware_version"] == "1.2.3"


def test_deauthorize_and_stop_targets_the_charger_command():
    client = zaptec_api.ZaptecCloudClient("user@example.com", "password")
    client._request = AsyncMock(return_value={})

    assert asyncio.run(client.deauthorize_and_stop("charger-1")) == {}
    client._request.assert_awaited_once_with(
        "POST",
        "/api/chargers/charger-1/SendCommand/10001",
    )


def test_installation_binding_caches_verified_charger_association():
    client = zaptec_api.ZaptecCloudClient("user@example.invalid", "unused")
    client._request = AsyncMock(return_value={
        "id": "charger-1", "installationId": "installation-1",
    })

    async def verify():
        assert await client.validate_charger_installation("charger-1", "installation-2") is False
        assert await client.validate_charger_installation("CHARGER-1", "INSTALLATION-1") is True
        client._request.assert_awaited_once_with("GET", "/api/chargers/charger-1")

    asyncio.run(verify())


def test_closed_zaptec_client_cannot_dispatch_a_suspended_write():
    session = SimpleNamespace(closed=False, request=Mock(
        side_effect=AssertionError("the retired client attempted a request"),
    ))
    client = zaptec_api.ZaptecCloudClient("user@example.invalid", "unused", session)

    async def verify():
        auth_started = asyncio.Event()
        allow_dispatch = asyncio.Event()

        async def delayed_auth():
            auth_started.set()
            await allow_dispatch.wait()

        client._ensure_authenticated = delayed_auth
        write = asyncio.create_task(client.set_installation_current("installation-1", 16))
        await auth_started.wait()
        await client.close()
        allow_dispatch.set()
        with pytest.raises(RuntimeError, match="client is closed"):
            await write
        session.request.assert_not_called()

    asyncio.run(verify())


@pytest.mark.parametrize("invalid_details", [
    {"Id": "other-charger", "InstallationId": "installation-1"},
    {"Id": "charger-1", "InstallationId": None},
])
def test_installation_binding_retries_unverified_identity_with_cooldown(monkeypatch, invalid_details):
    clock = [100.0]
    monkeypatch.setattr(zaptec_api.time, "monotonic", lambda: clock[0])
    client = zaptec_api.ZaptecCloudClient("user@example.invalid", "unused")
    client._request = AsyncMock(side_effect=[
        invalid_details, {"Id": "charger-1", "InstallationId": "installation-1"},
    ])

    async def verify():
        assert await client.validate_charger_installation("charger-1", "installation-1") is False
        clock[0] += 30
        assert await client.validate_charger_installation("charger-1", "installation-1") is False
        client._request.assert_awaited_once()
        clock[0] += 31
        assert await client.validate_charger_installation("charger-1", "installation-1") is True
        assert client._request.await_count == 2

    asyncio.run(verify())


@pytest.mark.parametrize("power", [None, "bad", "NaN", "inf", "-1", True])
def test_invalid_power_is_not_reported_as_measured_zero(power):
    client = zaptec_api.ZaptecCloudClient("user@example.com", "password")
    parsed = client.parse_charger_state({710: "2", 513: power})
    assert parsed["total_charge_power_w_valid"] is False


@pytest.mark.parametrize("actual", [16, 10, None, "bad", float("nan"), True])
def test_same_current_during_cooldown_needs_matching_installation_readback(monkeypatch, actual):
    clock = [10000.0]
    monkeypatch.setattr(zaptec_api.time, "time", lambda: clock[0])
    client = zaptec_api.ZaptecCloudClient("user@example.com", "password")
    client._request = AsyncMock(return_value={})

    async def replay():
        await client.set_installation_current("installation-1", 16)
        clock[0] += 30
        client._request.reset_mock()
        client._request.return_value = {
            "AvailableCurrentPhase1": actual,
            "AvailableCurrentPhase2": 16,
            "AvailableCurrentPhase3": 16,
        }
        if type(actual) is int and actual == 16:
            await client.set_installation_current("installation-1", 16)
        else:
            with pytest.raises(Exception, match="rate limit"):
                await client.set_installation_current("installation-1", 16)
        client._request.assert_awaited_once_with("GET", "/api/installation/installation-1")
        assert client._last_current_update_time == 10000
        client._request.reset_mock()
        with pytest.raises(Exception, match="rate limit"):
            await client.set_installation_current("installation-1", 6)
        client._request.assert_not_awaited()

    asyncio.run(replay())
