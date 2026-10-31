"""Exercise actual external Tesla handlers through the final shared queue."""
from __future__ import annotations

import asyncio
import ast
from contextlib import asynccontextmanager
import importlib
import json
import logging
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace as NS
from unittest.mock import AsyncMock

import aiohttp
from aiohttp import web
import pytest


ROOT = Path(__file__).resolve().parents[1] / "custom_components/power_sync"
PKG = "tesla_control_view_currency_probe"
for name, directory in ((PKG, ROOT), (f"{PKG}.powerwall_local", ROOT / "powerwall_local")):
    package = ModuleType(name)
    package.__path__ = [str(directory)]
    sys.modules[name] = package
core = importlib.import_module(f"{PKG}.powerwall_control")


class Reply:
    status = 200

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def text(self):
        return json.dumps({"token": "test-login", "response": {"result": True}})


class Session:
    def __init__(self):
        self.logins, self.mutations, self.reads = [], [], []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    def post(self, url, **kwargs):
        destination = self.logins if url.endswith("/api/login/Basic") else self.mutations
        destination.append((url, kwargs))
        return Reply()

    def get(self, url, **kwargs):
        self.reads.append((url, kwargs))
        return Reply()


def setup():
    entry = NS(entry_id="entry-one", data={"tesla_energy_site_id": "123",
        "tesla_api_provider": "teslemetry", "teslemetry_api_token": "test-token",
        "powerwall_local_ip": "gateway.invalid"}, options={})
    runtime = {"entry": entry}
    hass = NS(data={"power_sync": {entry.entry_id: runtime}})
    return NS(entry=entry, runtime=runtime, hass=hass, raw=Session(),
              site=core.coordinator_for(hass, entry=entry))


def revoke(probe, change):
    if change == "runtime":
        probe.hass.data["power_sync"][probe.entry.entry_id] = {"entry": probe.entry}
    elif change == "site":
        probe.entry.data["tesla_energy_site_id"] = "456"
    elif change == "network":
        probe.runtime["network_envelope_manager"] = NS(snapshot=NS(mode="active"))
    elif change == "foreign":
        probe.hass.data["power_sync"]["entry-two"] = {
            "entry": NS(data={"tesla_energy_site_id": "123"}, options={}),
            "force_discharge_state": {"active": True},
        }
    else:
        assert change == "unchanged"


def observe_queue(monkeypatch, probe):
    queued = asyncio.Event()
    original = probe.site.write_context

    @asynccontextmanager
    async def context(label, *args, **kwargs):
        if label != "preceding physical mutation":
            queued.set()
        async with original(label, *args, **kwargs) as receipt:
            yield receipt

    monkeypatch.setattr(probe.site, "write_context", context)
    return queued


def options_flow(probe):
    tree = ast.parse((ROOT / "config_flow.py").read_text())
    owner = next(node for node in tree.body if isinstance(node, ast.ClassDef)
                 and node.name == "PowerSyncOptionsFlow")
    method = next(node for node in owner.body if isinstance(node, ast.AsyncFunctionDef)
                  and node.name == "_restore_export_rule")
    namespace = {"__package__": PKG, "aiohttp": aiohttp, "DOMAIN": "power_sync",
        "_LOGGER": logging.getLogger(__name__), "async_get_clientsession": lambda _hass: probe.raw,
        "CONF_BATTERY_SYSTEM": "battery_system", "BATTERY_SYSTEM_TESLA": "tesla",
        "CONF_TESLA_ENERGY_SITE_ID": "tesla_energy_site_id",
        "CONF_TESLA_API_PROVIDER": "tesla_api_provider",
        "CONF_TESLEMETRY_API_TOKEN": "teslemetry_api_token",
        "TESLA_PROVIDER_TESLEMETRY": "teslemetry", "TESLA_PROVIDER_FLEET_API": "fleet_api",
        "TESLA_PROVIDER_POWERSYNC": "powersync", "TESLEMETRY_API_BASE_URL": "https://teslemetry.invalid"}
    exec(compile(ast.Module([method], type_ignores=[]), "config_flow.py", "exec"), namespace)
    flow = NS(hass=probe.hass, config_entry=probe.entry,
              _get_option=lambda _key, default: default, _restore_owned_curtailment_limits=AsyncMock())
    return flow, namespace["_restore_export_rule"]


def diagnostic_view(probe, kind):
    tree = ast.parse((ROOT / "powerwall_local/views.py").read_text())
    class_name = "PowerwallCloudProbeView" if kind == "cloud" else "PowerwallDebugProbeView"
    nodes = [node for node in tree.body if
             isinstance(node, ast.ClassDef) and node.name == class_name
             or isinstance(node, ast.FunctionDef) and node.name == "_control_site_id"]
    namespace = {"__package__": f"{PKG}.powerwall_local", "HomeAssistantView": object,
        "HomeAssistant": object, "ConfigEntry": object, "web": web, "DOMAIN": "power_sync",
        "_LOGGER": logging.getLogger(__name__), "CONF_POWERWALL_LOCAL_IP": "powerwall_local_ip",
        "CONF_POWERWALL_LOCAL_ENERGY_SITE_ID": "powerwall_local_energy_site_id",
        "_get_entry": lambda _hass: probe.entry,
        "_get_fleet_api_context": lambda *_args: ("test-token", "https://fleet.invalid", "123"),
        "normalize_powerwall_gateway_host": lambda host: host,
        "async_get_clientsession": lambda _hass: probe.raw}
    exec(compile(ast.Module(nodes, type_ignores=[]), "views.py", "exec"), namespace)
    return namespace[class_name](probe.hass)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["unchanged", "runtime", "site", "network", "foreign"])
async def test_actual_options_export_reset_rechecks_after_shared_queue(monkeypatch, change):
    probe = setup()
    queued = observe_queue(monkeypatch, probe)
    flow, restore = options_flow(probe)
    async with probe.site.write_context("preceding physical mutation"):
        pending = asyncio.create_task(restore(flow))
        await asyncio.wait_for(queued.wait(), timeout=2)
        assert probe.raw.mutations == []
        revoke(probe, change)
    await asyncio.wait_for(pending, timeout=2)
    flow._restore_owned_curtailment_limits.assert_awaited_once()
    if change == "unchanged":
        assert len(probe.raw.mutations) == 1
        url, kwargs = probe.raw.mutations[0]
        assert url.endswith("/energy_sites/123/grid_import_export")
        assert kwargs["json"] == {"customer_preferred_export_rule": "battery_ok"}
        assert not any(key.startswith("_power_sync_") for key in kwargs)
    else:
        assert probe.raw.mutations == []


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["cloud", "local"])
@pytest.mark.parametrize("change", ["unchanged", "runtime", "site", "foreign"])
async def test_actual_diagnostic_write_rechecks_after_shared_queue(monkeypatch, kind, change):
    probe = setup()
    queued = observe_queue(monkeypatch, probe)
    monkeypatch.setattr(aiohttp, "ClientSession", lambda **_kwargs: probe.raw)
    monkeypatch.setattr(aiohttp, "TCPConnector", lambda **_kwargs: object())
    view = diagnostic_view(probe, kind)
    request = NS(json=AsyncMock(return_value={"method": "POST",
        "path": "operation" if kind == "cloud" else "/api/operation", "body": {"mode": "backup"}}))
    async with probe.site.write_context("preceding physical mutation"):
        pending = asyncio.create_task(view.post(request))
        await asyncio.wait_for(queued.wait(), timeout=2)
        assert probe.raw.mutations == []
        if kind == "local":
            assert len(probe.raw.logins) == 1
        revoke(probe, change)
    if change == "unchanged":
        response = await asyncio.wait_for(pending, timeout=2)
        assert response.status == 200 and len(probe.raw.mutations) == 1
    else:
        with pytest.raises(core.ControlWriteRejected):
            await asyncio.wait_for(pending, timeout=2)
        assert probe.raw.mutations == []


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["cloud", "local"])
async def test_actual_diagnostic_reads_remain_available_while_foreign_owner_writes(monkeypatch, kind):
    probe = setup()
    revoke(probe, "foreign")
    monkeypatch.setattr(aiohttp, "ClientSession", lambda **_kwargs: probe.raw)
    monkeypatch.setattr(aiohttp, "TCPConnector", lambda **_kwargs: object())
    view = diagnostic_view(probe, kind)
    request = NS(json=AsyncMock(return_value={"method": "GET",
        "path": "site_info" if kind == "cloud" else "/api/system_status/grid_status"}))
    async with probe.site.write_context("preceding physical mutation"):
        response = await asyncio.wait_for(view.post(request), timeout=2)
    assert response.status == 200 and len(probe.raw.reads) == 1
    assert probe.raw.mutations == []
