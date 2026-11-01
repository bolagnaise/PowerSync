"""Focused evidence gates for the stock Tesla BLE restart coordinator."""
from __future__ import annotations

import itertools
from pathlib import Path
import sys
import types

import pytest

ROOT = Path(__file__).resolve().parents[1] / "custom_components" / "power_sync"
package = sys.modules.setdefault("power_sync", types.ModuleType("power_sync"))
package.__path__ = [str(ROOT)]

from power_sync.tesla_ble_reset import (
    StockResetUnsupported,
    async_execute_stock_reset,
)


IDENTITY = {
    "config_entry_id": "ble-entry",
    "device_id": "ble-device",
    "unique_id": "ble-device",
    "platform": "esphome",
    "vin": "5YJ3E1EA7NF0000A1",
    "api_generation": "api-1",
}
PROFILE = {
    "queue_persistence": "ram_only",
    "host_replay": "none",
    "exercised": True,
}


def _callbacks(*, identities=None, uptimes=None, restart_result=True,
                disconnected=True, connected=True):
    identity_values = iter(identities or [IDENTITY, IDENTITY, IDENTITY])
    uptime_values = iter(uptimes or [
        {"uptime": 100, "observed_at": 10, "fresh": True},
        {"uptime": 2, "observed_at": 20, "fresh": True},
        {"uptime": 3, "observed_at": 21, "fresh": True},
    ])
    calls = []

    async def restart():
        calls.append("restart")
        return restart_result

    async def wait_disconnected():
        calls.append("disconnected")
        return disconnected

    async def wait_connected():
        calls.append("connected")
        return connected

    async def read_identity():
        calls.append("identity")
        return next(identity_values)

    async def read_uptime():
        calls.append("uptime")
        return next(uptime_values)

    return restart, wait_disconnected, wait_connected, read_identity, read_uptime, calls


@pytest.mark.asyncio
async def test_stock_reset_requires_disconnect_fresh_restart_and_advancing_uptime():
    callbacks = _callbacks()
    restart, disconnected, connected, identity, uptime, calls = callbacks
    result = await async_execute_stock_reset(
        profile=PROFILE,
        identity=IDENTITY,
        restart=restart,
        wait_disconnected=disconnected,
        wait_connected=connected,
        read_identity=identity,
        read_uptime=uptime,
        sleep=lambda _delay: _completed(),
        now=itertools.count(1).__next__,
    )

    assert result["uptime_before"] == 100
    assert result["uptime_after"] == 2
    assert result["uptime_advancing"] == 3
    assert calls == ["identity", "uptime", "restart", "disconnected", "connected",
                     "identity", "uptime", "uptime", "identity"]


@pytest.mark.asyncio
async def test_stock_reset_rejects_stale_pre_reset_uptime_before_restart():
    callbacks = _callbacks(uptimes=[
        {"uptime": 100, "observed_at": 0, "fresh": False},
        {"uptime": 2, "observed_at": 20, "fresh": True},
        {"uptime": 3, "observed_at": 21, "fresh": True},
    ])
    with pytest.raises(StockResetUnsupported, match="not fresh before"):
        await async_execute_stock_reset(
            profile=PROFILE, identity=IDENTITY,
            restart=callbacks[0], wait_disconnected=callbacks[1],
            wait_connected=callbacks[2], read_identity=callbacks[3],
            read_uptime=callbacks[4], sleep=lambda _delay: _completed(),
            now=itertools.count(1).__next__,
        )
    assert "restart" not in callbacks[-1]


async def _completed():
    return None


@pytest.mark.asyncio
async def test_stock_reset_rejects_cached_uptime_after_reconnect():
    callbacks = _callbacks(uptimes=[
        {"uptime": 100, "observed_at": 10, "fresh": True},
        {"uptime": 2, "observed_at": 1, "fresh": False},
        {"uptime": 3, "observed_at": 21, "fresh": True},
    ])
    with pytest.raises(StockResetUnsupported, match="not fresh"):
        await async_execute_stock_reset(
            profile=PROFILE, identity=IDENTITY,
            restart=callbacks[0], wait_disconnected=callbacks[1],
            wait_connected=callbacks[2], read_identity=callbacks[3],
            read_uptime=callbacks[4], sleep=lambda _delay: _completed(),
            now=itertools.count(1).__next__,
        )


@pytest.mark.asyncio
async def test_stock_reset_rejects_identity_rebind_before_mutation():
    rebound = dict(IDENTITY, device_id="other-device")
    callbacks = _callbacks(identities=[rebound, IDENTITY, IDENTITY])
    with pytest.raises(StockResetUnsupported, match="identity changed before"):
        await async_execute_stock_reset(
            profile=PROFILE, identity=IDENTITY,
            restart=callbacks[0], wait_disconnected=callbacks[1],
            wait_connected=callbacks[2], read_identity=callbacks[3],
            read_uptime=callbacks[4], sleep=lambda _delay: _completed(),
            now=itertools.count(1).__next__,
        )
    assert "restart" not in callbacks[-1]


@pytest.mark.asyncio
async def test_stock_reset_rejects_unqualified_profile_without_restart():
    callbacks = _callbacks()
    with pytest.raises(StockResetUnsupported, match="host replay"):
        await async_execute_stock_reset(
            profile={**PROFILE, "host_replay": "unknown"}, identity=IDENTITY,
            restart=callbacks[0], wait_disconnected=callbacks[1],
            wait_connected=callbacks[2], read_identity=callbacks[3],
            read_uptime=callbacks[4], sleep=lambda _delay: _completed(),
            now=itertools.count(1).__next__,
        )
    assert "restart" not in callbacks[-1]


@pytest.mark.asyncio
async def test_stock_reset_requires_complete_identity_and_profile_bounds():
    callbacks = _callbacks()
    with pytest.raises(StockResetUnsupported, match="identity"):
        await async_execute_stock_reset(
            profile=PROFILE, identity={"vin": IDENTITY["vin"]},
            restart=callbacks[0], wait_disconnected=callbacks[1],
            wait_connected=callbacks[2], read_identity=callbacks[3],
            read_uptime=callbacks[4], sleep=lambda _delay: _completed(),
        )
    callbacks = _callbacks()
    with pytest.raises(StockResetUnsupported, match="uptime bound"):
        await async_execute_stock_reset(
            profile={**PROFILE, "max_uptime_after_reset": "unknown"},
            identity=IDENTITY, restart=callbacks[0],
            wait_disconnected=callbacks[1], wait_connected=callbacks[2],
            read_identity=callbacks[3], read_uptime=callbacks[4],
            sleep=lambda _delay: _completed(),
        )


@pytest.mark.asyncio
async def test_runtime_qualification_marks_exercised_only_after_reset_proof():
    callbacks = _callbacks()
    result = await async_execute_stock_reset(
        profile={
            "queue_persistence": "ram_only",
            "host_replay": "none",
            "qualification": "source_and_runtime_exercise",
        },
        identity=IDENTITY, restart=callbacks[0], wait_disconnected=callbacks[1],
        wait_connected=callbacks[2], read_identity=callbacks[3],
        read_uptime=callbacks[4], sleep=lambda _delay: _completed(),
        now=itertools.count(1).__next__,
    )
    assert result["qualification"] == "source_and_runtime_exercise"
    assert result["exercised"] is True
