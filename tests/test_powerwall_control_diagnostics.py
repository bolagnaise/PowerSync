"""Settings diagnostics follow current journal owners without control I/O."""
from __future__ import annotations

import ast
import asyncio
from copy import deepcopy
import importlib
import json
import logging
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace as NS

import aiohttp
from aiohttp import web
import pytest


ROOT = Path(__file__).resolve().parents[1] / "custom_components/power_sync"
PKG = "powerwall_control_diagnostics_probe"
package = ModuleType(PKG)
package.__path__ = [str(ROOT)]
sys.modules[PKG] = package
core = importlib.import_module(f"{PKG}.powerwall_control")


class Reply:
    status = 200

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        pass

    async def json(self):
        return {"response": {"backup_reserve_percent": 30, "default_real_mode": "autonomous", "components": {}}}


@pytest.fixture(scope="module")
def settings_view_type():
    tree = ast.parse((ROOT / "__init__.py").read_text())
    node = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "PowerwallSettingsView")
    namespace = {
        "__package__": PKG, "HomeAssistantView": object, "web": web, "aiohttp": aiohttp,
        "DOMAIN": "power_sync", "_LOGGER": logging.getLogger(__name__),
        "_get_neovolt_entry_ids": lambda *_args: [],
        "get_effective_solar_curtailment_configuration": lambda _entry: (False, False),
        "get_tesla_api_token": lambda *_args: ("test-token", "fleet"),
        "get_tesla_api_base_url": lambda *_args: "https://tesla.invalid",
        "async_get_clientsession": lambda _hass: NS(get=lambda *_args, **_kwargs: Reply()),
        "tesla_grid_charging_enabled_from_site_info": lambda _info: True,
        "confirmed_local_powerwall_setting": lambda *_args, **_kwargs: None,
        "TESLA_LOCAL_CONTROL_MAX_AGE_SECONDS": 30,
    }
    for item in ast.walk(node):
        if isinstance(item, ast.Name) and item.id.startswith("CONF_"):
            namespace[item.id] = item.id[5:].lower()
        elif isinstance(item, ast.Name) and item.id.startswith("BATTERY_SYSTEM_"):
            namespace[item.id] = item.id[15:].lower()
    exec(compile(ast.fix_missing_locations(ast.Module([node], type_ignores=[])), "<actual settings GET>", "exec",
                 flags=__import__("__future__").annotations.compiler_flag), namespace)
    return namespace["PowerwallSettingsView"]


def setup_site():
    entry = NS(entry_id="entry-a", data={"tesla_energy_site_id": "00123", "battery_system": "tesla"}, options={})
    hass = NS(data={"power_sync": {}}, config_entries=NS(async_entries=lambda _domain: [entry]))
    site = core.coordinator_for(hass, entry=entry)
    native = NS(pending=True)
    runtime = {"powerwall_site_control": site, "powerwall_native_max_charge": native}
    hass.data["power_sync"][entry.entry_id] = runtime
    return hass, site, runtime, native


async def read_status(view_type, hass):
    response = await view_type(hass).get(None)
    assert response.status == 200
    body = json.loads(response.body)
    assert body["success"] is True
    return body["control_status"]


@pytest.mark.asyncio
async def test_actual_settings_get_follows_watcher_completion_without_new_control(settings_view_type):
    hass, site, runtime, native = setup_site()
    site.set_obligations("entry-a", core.control_obligations(runtime))
    assert (await read_status(settings_view_type, hass))["obligations"]["entry-a"]["native_restore_pending"] is True
    # The watcher retires its own journal after the service has returned.
    native.pending = False
    assert site.snapshot()["obligations"]["entry-a"]["native_restore_pending"] is True
    current = await read_status(settings_view_type, hass)
    assert current["obligations"]["entry-a"]["native_restore_pending"] is False
    assert current["obligations"]["entry-a"]["runtime_available"] is True
    assert current["effects"] == [] and current["operations"] == []
    assert runtime["powerwall_native_max_charge"] is native


@pytest.mark.asyncio
async def test_actual_get_refreshes_shared_site_entries_and_keeps_other_sites_independent(settings_view_type):
    hass, site, runtime, native = setup_site()
    other = core.coordinator_for(hass, site_id=456)
    peer_native = NS(pending=True)
    peer = {"powerwall_site_control": core.coordinator_for(hass, site_id=123), "powerwall_native_max_charge": peer_native}
    unrelated = {"powerwall_site_control": other, "powerwall_native_max_charge": NS(pending=True)}
    hass.data["power_sync"].update({"entry-b": peer, "entry-c": unrelated})
    site.set_obligations("entry-b", core.control_obligations(peer))
    other.set_obligations("entry-c", core.control_obligations(unrelated))
    native.pending = peer_native.pending = unrelated["powerwall_native_max_charge"].pending = False
    status = await read_status(settings_view_type, hass)
    assert status["obligations"]["entry-a"]["native_restore_pending"] is False
    assert status["obligations"]["entry-b"]["native_restore_pending"] is False
    assert other.snapshot()["obligations"]["entry-c"]["native_restore_pending"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["unloaded", "replaced", "rebound"])
async def test_actual_get_marks_unavailable_entry_unknown_and_preserves_incident(settings_view_type, change):
    hass, site, runtime, _ = setup_site()
    runtime["powerwall_settings_pending"] = {"id": "incident", "resume_cloud": True}
    site.set_obligations("entry-a", core.control_obligations(runtime))
    unknown = {"state": "unknown", "receipt": "existing external obligation"}
    site.set_obligations("external", unknown)
    supplied_unknown = {key: True for key in core._OBLIGATION_FLAGS}
    supplied_unknown["state"] = "unknown"
    site.set_obligations("external_owner", supplied_unknown)
    peer_entry = NS(entry_id="entry-b", data={"tesla_energy_site_id": 123}, options={})
    hass.config_entries.async_entries = lambda _domain: [peer_entry]
    hass.data["power_sync"]["entry-b"] = {"powerwall_site_control": site}
    if change == "unloaded":
        del hass.data["power_sync"]["entry-a"]
    elif change == "replaced":
        hass.data["power_sync"]["entry-a"] = {}
    else:
        runtime["powerwall_site_control"] = core.coordinator_for(hass, site_id=456)
    status = await read_status(settings_view_type, hass)
    previous = status["obligations"]["entry-a"]
    assert previous["runtime_available"] is False
    assert previous["native_restore_pending"] is None and previous["local_settings_unresolved"] is None
    assert previous["settings_incident_id"] == "incident"
    assert status["obligations"]["external"] == unknown
    assert status["obligations"]["external_owner"] == supplied_unknown
    assert runtime["powerwall_settings_pending"] == {"id": "incident", "resume_cloud": True}


@pytest.mark.asyncio
async def test_settings_diagnostics_remain_responsive_during_unknown_provider_effect(settings_view_type):
    hass, site, runtime, native = setup_site()
    site.set_obligations("entry-a", core.control_obligations(runtime))
    entered, release = asyncio.Event(), asyncio.Event()
    async def delayed_provider():
        async with site.write_context("existing driver") as receipt:
            site.mark_effect_uncertain(receipt, "provider_outcome_unknown")
            entered.set()
            await release.wait()
    driver = asyncio.create_task(delayed_provider())
    await entered.wait()
    native.pending = False
    try:
        before = deepcopy(site.snapshot()["uncertain_effects"])
        status = await asyncio.wait_for(read_status(settings_view_type, hass), timeout=2)
        assert status["obligations"]["entry-a"]["native_restore_pending"] is False
        assert status["uncertain_effects"] == before
        assert not driver.done()
    finally:
        release.set()
        await driver
    assert site.snapshot()["uncertain_effects"] == before
