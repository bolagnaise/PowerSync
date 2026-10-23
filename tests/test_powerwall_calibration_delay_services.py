"""Selected entry, Monitoring Mode, durable nonce and calibration guard behavior."""
import ast
import asyncio
import copy
import hashlib
import time
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_powerwall_calibration_delay import calibration, client_mod


def fixture(*, monitoring=False, user="owner", save_started=None, save_release=None):
    root = Path(__file__).resolve().parents[1] / "custom_components/power_sync/powerwall_local/services.py"
    storage = {}
    class Store:
        def __init__(self, *args): pass
        async def async_load(self): return copy.deepcopy(storage.get("requests"))
        async def async_save(self, value):
            if save_started is not None:
                save_started.set()
            if save_release is not None:
                await save_release.wait()
            storage["requests"] = copy.deepcopy(value)
    entry = SimpleNamespace(domain="power_sync", entry_id="target", data={"powerwall_local_paired": True}, options={"monitoring_mode": monitoring})
    status = {"supported": True, "calibration_delays_left": 2, "max_calibration_delays": 3, "calibration_delay_remaining_seconds": 0}
    result = {"command_acknowledged": True, "requested_delay_hours": 1, "status": status, "retry_safe": False}
    client = SimpleNamespace(local_access_enabled=True, get_battery_calibration_delay_status=AsyncMock(return_value=status))
    async def delay(hours, *, before_write, before_command):
        await before_command(); before_write(); return result
    client.delay_battery_calibration = AsyncMock(side_effect=delay)
    coordinator = SimpleNamespace(client=client)
    hass = SimpleNamespace(data={"power_sync": {"target": {"calibration_suspected": True}}},
        config_entries=SimpleNamespace(async_entries=lambda domain: [entry], async_get_entry=lambda identity: entry if identity=="target" else None))
    namespace = dict(HomeAssistant=object, ServiceCall=object, HomeAssistantError=RuntimeError, PowerwallLocalError=client_mod.PowerwallLocalError,
        asyncio=asyncio, hashlib=hashlib, uuid=uuid, Store=Store, DOMAIN="power_sync", CONF_POWERWALL_LOCAL_PAIRED="powerwall_local_paired", CONF_MONITORING_MODE="monitoring_mode",
        ensure_coordinator=AsyncMock(return_value=coordinator), ensure_client=AsyncMock(),
        validate_delay_hours=calibration.validate_delay_hours, resolve_connection_profile=lambda *args: SimpleNamespace(monitoring_only=False))
    names = {"_service_delay_hours", "_paired_client_for_call", "_handle_calibration_delay_status", "_handle_delay_calibration"}
    nodes = [node for node in ast.parse(root.read_text()).body if isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef)) and node.name in names]
    async def durable_save(store, requests):
        task = asyncio.create_task(store.async_save(copy.deepcopy(requests)))
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            await asyncio.shield(task)
            raise
    def prune(requests):
        keys = [key for key in requests if key != "__acceptance_barrier_until"]
        while len(keys) > 64:
            removable = next((key for key in keys if requests.get(key, {}).get("terminal")), None)
            if removable is None: return False
            requests.pop(removable); keys.remove(removable)
        return True
    exec(compile(ast.Module(body=nodes,type_ignores=[]),str(root),"exec"),namespace)
    namespace.update(_CALIBRATION_DELAY_LOCKS={}, _CALIBRATION_DELAY_BARRIER_KEY="__acceptance_barrier_until",
                     _CALIBRATION_DELAY_ACCEPTANCE_WINDOW_SECONDS=60, _CALIBRATION_DELAY_SIGNATURE_SAFETY_SECONDS=13,
                     _CALIBRATION_DELAY_JOURNAL_LIMIT=64,
                     _durable_calibration_save=durable_save, _prune_calibration_journal=prune,
                     copy=copy, time=time)
    call = SimpleNamespace(data={"delay_hours":"1", "request_id":"intent-1"},context=SimpleNamespace(user_id=user,id="context-1"))
    return namespace,hass,call,client,entry,storage


def test_idempotent_delay_survives_reload_and_keeps_calibration_guard():
    async def run():
        ns,hass,call,client,entry,storage=fixture()
        first = await ns["_handle_delay_calibration"](hass,call)
        assert first["command_acknowledged"]
        assert storage["requests"]["intent-1"]["result"] == first
        assert hass.data["power_sync"]["target"]["calibration_suspected"]
        hass.data["power_sync"]["target"]={"calibration_suspected":True}
        second = await ns["_handle_delay_calibration"](hass,call)
        assert second["replayed"] and client.delay_battery_calibration.await_count == 1
        call.data["delay_hours"]="2"
        with pytest.raises(RuntimeError,match="different request"): await ns["_handle_delay_calibration"](hass,call)
        assert client.delay_battery_calibration.await_count == 1
    asyncio.run(run())


def test_crash_after_intent_is_never_replayed():
    async def run():
        ns,hass,call,client,entry,storage=fixture()
        async def crash(hours,*,before_write,before_command):
            await before_command(); raise RuntimeError("simulated_restart")
        client.delay_battery_calibration.side_effect=crash
        with pytest.raises(RuntimeError,match="simulated_restart"): await ns["_handle_delay_calibration"](hass,call)
        hass.data["power_sync"]["target"]={}
        with pytest.raises(RuntimeError,match="unconfirmed"): await ns["_handle_delay_calibration"](hass,call)
        assert client.delay_battery_calibration.await_count == 1
    asyncio.run(run())


def test_status_is_readable_in_monitoring_but_automated_delay_is_blocked():
    async def run():
        ns,hass,call,client,entry,storage=fixture(monitoring=True,user=None)
        assert (await ns["_handle_calibration_delay_status"](hass,call))["supported"]
        with pytest.raises(RuntimeError,match="Monitoring Mode"): await ns["_handle_delay_calibration"](hass,call)
        client.delay_battery_calibration.assert_not_awaited()
        assert not storage
    asyncio.run(run())


def test_reload_runtime_replacement_cannot_overlap_same_entry():
    async def run():
        ns, hass, call, client, entry, storage = fixture()
        started = asyncio.Event()
        release = asyncio.Event()
        async def delayed(hours, *, before_write, before_command):
            await before_command()
            started.set()
            await release.wait()
            return {"command_acknowledged": True, "requested_delay_hours": hours,
                    "status": {"supported": True}, "retry_safe": False}
        client.delay_battery_calibration.side_effect = delayed
        first = asyncio.create_task(ns["_handle_delay_calibration"](hass, call))
        await started.wait()
        hass.data["power_sync"]["target"] = {}
        with pytest.raises(RuntimeError, match="already in progress"):
            await ns["_handle_delay_calibration"](hass, call)
        release.set()
        await first
    asyncio.run(run())


def test_cancelled_save_finishes_before_request_returns():
    async def run():
        started, release = asyncio.Event(), asyncio.Event()
        ns, hass, call, client, entry, storage = fixture(save_started=started, save_release=release)
        task = asyncio.create_task(ns["_handle_delay_calibration"](hass, call))
        await started.wait()
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert storage["requests"]["intent-1"]["hours"] == 1
    asyncio.run(run())


def test_uncertain_write_persists_short_acceptance_barrier():
    async def run():
        ns, hass, call, client, entry, storage = fixture()
        result = {"command_acknowledged": False, "requested_delay_hours": 1,
                  "status": {"supported": True}, "retry_safe": False}
        client.delay_battery_calibration.side_effect = AsyncMock(return_value=result)
        await ns["_handle_delay_calibration"](hass, call)
        assert storage["requests"]["__acceptance_barrier_until"] > time.time()
        call.data.update(delay_hours="2", request_id="intent-2")
        with pytest.raises(RuntimeError, match="acceptance"):
            await ns["_handle_delay_calibration"](hass, call)
    asyncio.run(run())


def test_cancellation_after_signed_send_keeps_acceptance_barrier():
    async def run():
        ns, hass, call, client, entry, storage = fixture()
        async def cancelled_after_send(hours, *, before_write, before_command):
            await before_command()
            # Models cancellation after transport's before_send fence.
            raise asyncio.CancelledError
        client.delay_battery_calibration.side_effect = cancelled_after_send
        with pytest.raises(asyncio.CancelledError):
            await ns["_handle_delay_calibration"](hass, call)
        assert storage["requests"]["__acceptance_barrier_until"] > time.time()
        call.data.update(delay_hours="2", request_id="intent-2")
        with pytest.raises(RuntimeError, match="acceptance"):
            await ns["_handle_delay_calibration"](hass, call)
    asyncio.run(run())


def test_send_guard_fails_closed_if_session_delay_would_expire_signature():
    async def run():
        ns, hass, call, client, entry, storage = fixture()
        real_time = time.time
        async def delayed_session(hours, *, before_write, before_command):
            await before_command()
            ns["time"].time = lambda: real_time() + 100
            before_write()
        client.delay_battery_calibration.side_effect = delayed_session
        try:
            with pytest.raises(RuntimeError, match="acceptance window expired"):
                await ns["_handle_delay_calibration"](hass, call)
        finally:
            ns["time"].time = real_time
        assert storage["requests"]["__acceptance_barrier_until"] > real_time()
    asyncio.run(run())


def test_uncertain_journal_is_not_pruned_or_overwritten():
    async def run():
        ns, hass, call, client, entry, storage = fixture()
        storage["requests"] = {f"old-{i}": {"hours": 1, "pairing": "p"} for i in range(64)}
        with pytest.raises(RuntimeError, match="journal is full"):
            await ns["_handle_delay_calibration"](hass, call)
        assert len(storage["requests"]) == 64
    asyncio.run(run())


def test_reserved_journal_key_cannot_be_used_as_request_id():
    async def run():
        ns, hass, call, client, entry, storage = fixture()
        call.data["request_id"] = "__acceptance_barrier_until"
        with pytest.raises(RuntimeError, match="Invalid calibration delay request_id"):
            await ns["_handle_delay_calibration"](hass, call)
        assert not storage
    asyncio.run(run())


def test_corrupt_journal_fails_closed_without_replay():
    async def run():
        ns, hass, call, client, entry, storage = fixture()
        storage["requests"] = []
        with pytest.raises(RuntimeError, match="journal is invalid"):
            await ns["_handle_delay_calibration"](hass, call)
        client.delay_battery_calibration.assert_not_awaited()
    asyncio.run(run())
