"""Pairing changes must not strand an existing Native Max restore owner."""
import ast
import asyncio
import logging
from contextlib import asynccontextmanager
from copy import deepcopy
from enum import Enum
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiohttp import web
import pytest


SOURCE = Path(__file__).parents[1] / "custom_components/power_sync/powerwall_local/views.py"
VIEWS = ("PowerwallPairStartView", "PowerwallPairUnpairView", "PowerwallSetGatewayIpView")


def fixture(monkeypatch, *, journal=None, runtime=None, load_error=False):
    entry = SimpleNamespace(entry_id="entry", data={
        "powerwall_local_paired": True, "powerwall_local_din": "gateway",
        "powerwall_local_public_key_der": "original-key", "powerwall_local_private_key_pem": "private",
        "powerwall_local_ip": "192.0.2.1", "tesla_energy_site_id": "123",
    })
    root = runtime if runtime is not None else {}
    hass = SimpleNamespace(data={"power_sync": {entry.entry_id: root}})
    updates, managers, warmups = [], [], []
    def update(target, *, data):
        updates.append(deepcopy(data))
        target.data = data
    hass.config_entries = SimpleNamespace(async_update_entry=update)
    store = SimpleNamespace(data=deepcopy(journal))
    async def load():
        if load_error:
            raise OSError("storage unavailable")
        return deepcopy(store.data)
    store.async_load = load
    lock = asyncio.Lock()
    root["powerwall_battery_dispatch_route_lock"] = lock

    class Version(Enum):
        PW3 = "pw3"
    class Manager:
        is_running = False
        def __init__(self, *args, on_success, **kwargs):
            self.on_success = on_success
            managers.append(self)
        async def start(self):
            self.is_running = True
            return SimpleNamespace(to_dict=lambda: {"state": "generating_key"})
        async def cancel(self):
            self.is_running = False
    import sys
    monkeypatch.setitem(sys.modules, "power_sync.powerwall_control_http",
        SimpleNamespace(controlled_session=lambda *a, **k: object()))
    ns = {
        "__name__": "pairing_recovery_test", "__package__": "power_sync.powerwall_local",
        "HomeAssistantView": object, "HomeAssistant": object, "ConfigEntry": object,
        "web": web, "DOMAIN": "power_sync", "time": SimpleNamespace(time=lambda: 1000),
        "_LOGGER": logging.getLogger(__name__),
        "asynccontextmanager": asynccontextmanager, "PowerwallVersion": Version,
        "BatteryRouteLock": asyncio.Lock,
        "PowerwallPairingError": RuntimeError, "PowerwallPairingManager": Manager,
        "Store": lambda *a: store,
        "coordinator_for": lambda *a, **k: SimpleNamespace(route_lock=lock),
        "_get_entry": lambda h: entry,
        "_runtime": lambda h, e: h.data.setdefault("power_sync", {}).setdefault(e.entry_id, {}).setdefault("powerwall_local", {}),
        "_control_site_id": lambda e: e.data.get("tesla_energy_site_id"),
        "normalize_powerwall_gateway_host": lambda value: value or "",
        "_get_fleet_api_context": lambda *a: ("token", "base", "123"),
        "async_get_clientsession": lambda h: object(),
        "_schedule_local_coordinator_warmup": lambda *a: warmups.append(True),
        "POWERWALL_PAIRING_WINDOW_SECONDS": 120, "TESLA_PROVIDER_TESLEMETRY": "teslemetry",
        "CONF_TESLA_API_PROVIDER": "tesla_api_provider",
    }
    for suffix, key in {
        "DIN": "din", "ENERGY_SITE_ID": "energy_site_id", "IP": "ip", "PAIRED": "paired",
        "PAIRED_AT": "paired_at", "PRIVATE_KEY": "private_key_pem",
        "PUBLIC_KEY": "public_key_der", "VERSION": "version",
    }.items():
        ns["CONF_POWERWALL_LOCAL_" + suffix] = "powerwall_local_" + key
    tree = ast.parse(SOURCE.read_text())
    nodes = [ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)]
    nodes += [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
              and (n.name in VIEWS or n.name == "_pairing_configuration_change")]
    exec(compile(ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])), str(SOURCE), "exec"), ns)
    request = SimpleNamespace(json=AsyncMock(return_value={"gateway_ip": "192.0.2.2"}))
    return SimpleNamespace(ns=ns, hass=hass, entry=entry, root=root, store=store, lock=lock,
        updates=updates, managers=managers, warmups=warmups, request=request)


@pytest.mark.asyncio
@pytest.mark.parametrize("view_name", VIEWS)
@pytest.mark.parametrize("phase", ["prepared", "applying", "active", "restoring", "settling", "restored"])
async def test_durable_native_owner_blocks_pairing_changes_before_any_side_effect(monkeypatch, view_name, phase):
    journal = {"version": 1, "id": "owner", "identity": "original", "phase": phase,
               "expires_at": 1, "baseline": {"reserve": 5}}
    f = fixture(monkeypatch, journal=journal)
    before = deepcopy(f.entry.data)
    response = await f.ns[view_name](f.hass).post(f.request)
    assert response.status == 409
    assert f.entry.data == before and f.store.data == journal
    assert not f.updates and not f.managers and not f.warmups


@pytest.mark.asyncio
@pytest.mark.parametrize("view_name", VIEWS)
@pytest.mark.parametrize("runtime", [
    {"powerwall_native_max_charge": SimpleNamespace(pending=True)},
    {"native_max_startup_pending": True}, {"native_max_recovery_unavailable": True},
])
async def test_runtime_recovery_blocks_pairing_changes(monkeypatch, view_name, runtime):
    f = fixture(monkeypatch, runtime=dict(runtime))
    assert (await f.ns[view_name](f.hass).post(f.request)).status == 409
    assert not f.updates and not f.managers


@pytest.mark.asyncio
@pytest.mark.parametrize("view_name", VIEWS)
async def test_unreadable_journal_blocks_pairing_changes(monkeypatch, view_name):
    f = fixture(monkeypatch, load_error=True)
    assert (await f.ns[view_name](f.hass).post(f.request)).status == 409
    assert not f.updates and not f.managers


@pytest.mark.asyncio
@pytest.mark.parametrize("view_name", VIEWS)
async def test_pairing_changes_still_work_after_cleanup(monkeypatch, view_name):
    f = fixture(monkeypatch)
    assert (await f.ns[view_name](f.hass).post(f.request)).status == 200
    assert f.updates


@pytest.mark.asyncio
async def test_pairing_callback_rechecks_owner_acquired_during_handshake(monkeypatch):
    f = fixture(monkeypatch)
    assert (await f.ns["PowerwallPairStartView"](f.hass).post(f.request)).status == 200
    before = deepcopy(f.entry.data)
    f.store.data = {"id": "new-owner"}
    result = SimpleNamespace(private_key_pem=b"new-private", public_key_der=b"new-key", din="gateway", energy_site_id=123)
    with pytest.raises(RuntimeError, match="recovery"):
        await f.managers[0].on_success(result)
    assert f.entry.data == before and not f.warmups


@pytest.mark.asyncio
async def test_pairing_waits_for_shared_route_then_rechecks_durable_owner(monkeypatch):
    f = fixture(monkeypatch)
    async with f.lock:
        task = asyncio.create_task(f.ns["PowerwallPairUnpairView"](f.hass).post(f.request))
        await asyncio.sleep(0)
        assert not task.done() and not f.updates
        f.store.data = {"id": "owner-created-under-route-lock"}
    assert (await task).status == 409
    assert not f.updates


@pytest.mark.asyncio
async def test_first_pairing_without_site_or_runtime_route_lock_remains_supported(monkeypatch):
    f = fixture(monkeypatch)
    f.entry.data = {}
    f.root.pop("powerwall_battery_dispatch_route_lock")
    assert (await f.ns["PowerwallPairStartView"](f.hass).post(f.request)).status == 200
    result = SimpleNamespace(private_key_pem=b"new-private", public_key_der=b"new-key", din="gateway", energy_site_id=123)
    await f.managers[0].on_success(result)
    assert f.entry.data["powerwall_local_public_key_der"] == b"new-key".hex()
    assert f.entry.data["powerwall_local_energy_site_id"] == 123
    assert f.warmups == [True]


@pytest.mark.asyncio
async def test_reload_during_pairing_prevents_stale_callback_from_replacing_keys(monkeypatch):
    f = fixture(monkeypatch)
    assert (await f.ns["PowerwallPairStartView"](f.hass).post(f.request)).status == 200
    before = deepcopy(f.entry.data)
    f.hass.data["power_sync"][f.entry.entry_id] = {}
    result = SimpleNamespace(private_key_pem=b"new-private", public_key_der=b"new-key", din="gateway", energy_site_id=123)
    with pytest.raises(RuntimeError, match="superseded"):
        await f.managers[0].on_success(result)
    assert f.entry.data == before and not f.warmups
