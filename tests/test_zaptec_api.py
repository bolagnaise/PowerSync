"""Tests for Zaptec Cloud state parsing."""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
from unittest.mock import AsyncMock

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
