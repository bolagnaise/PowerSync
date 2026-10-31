"""Caller currency survives the shared Tesla mutation queue's await boundary."""
from __future__ import annotations

import ast
import asyncio
import importlib
import logging
from pathlib import Path
import sys
import time
from types import ModuleType, SimpleNamespace as NS

import pytest
from conftest import load_source_node

ROOT = Path(__file__).parents[1] / "custom_components/power_sync"
HTTP = NS(ClientTimeout=lambda **kw: NS(**kw), ClientError=ConnectionError)


@pytest.fixture(scope="module")
def controls():
    prefix = "tesla_queued_settings_probe"
    package = ModuleType(prefix)
    package.__path__ = [str(ROOT)]
    sys.modules[prefix] = package
    try:
        grid = importlib.import_module(prefix + ".tesla_grid_control")
        grid.aiohttp = HTTP
        yield NS(grid=grid,
                 site=importlib.import_module(prefix + ".powerwall_control"),
                 http=importlib.import_module(prefix + ".powerwall_control_http"))
    finally:
        for name in list(sys.modules):
            if name == prefix or name.startswith(prefix + "."):
                sys.modules.pop(name)


class Reply:
    status = 200

    def __init__(self, payload=None, before_enter=None):
        self.payload = payload or {"response": {"result": True}}
        self.before_enter = before_enter

    async def __aenter__(self):
        if self.before_enter:
            await self.before_enter()
        return self

    async def __aexit__(self, *args):
        return False

    async def json(self):
        return self.payload


@pytest.mark.asyncio
@pytest.mark.parametrize("resend", [False, True])
@pytest.mark.parametrize("revocation", ["generation", "deadline"])
async def test_grid_initial_and_resend_recheck_caller_after_queue_wait(controls, resend, revocation):
    clock = [0.0]
    current = [True]
    hass = NS(data={})
    site = controls.site.coordinator_for(hass, site_id="site")
    held, release, queued = asyncio.Event(), asyncio.Event(), asyncio.Event()
    posts, holders = [], []

    async def hold():
        async with site.write_context("preceding write"):
            held.set()
            await release.wait()

    async def before_read():
        if resend:
            holders.append(asyncio.create_task(hold()))
            await held.wait()

    def post(url, **kwargs):
        posts.append(kwargs)
        return Reply()

    raw = NS(post=post, get=lambda *a, **kw: Reply({"response": {"components": {
        "disallow_charge_from_grid_with_solar_installed": False
    }}}, before_enter=before_read))
    controlled = controls.http.controlled_session(hass, raw)
    attempts = [0]

    class Observed:
        def post(self, *args, **kwargs):
            attempts[0] += 1
            result = controlled.post(*args, **kwargs)
            if attempts[0] == (2 if resend else 1):
                queued.set()
            return result

        def get(self, *args, **kwargs):
            return controlled.get(*args, **kwargs)

    async def sleep(delay):
        clock[0] += delay

    if not resend:
        holders.append(asyncio.create_task(hold()))
        await held.wait()
    pending = asyncio.create_task(controls.grid.async_set_tesla_grid_charging_confirmed(
        Observed(), "https://tesla.test", "site", {}, False,
        confirmation_deadline=60, poll_offsets=(45,), retry_after=45,
        operation_deadline=60, sleep=sleep, monotonic=lambda: clock[0],
        is_current=lambda: current[0],
    ))
    await asyncio.wait_for(queued.wait(), 2)
    assert len(posts) == int(resend)
    if revocation == "generation":
        current[0] = False
    else:
        clock[0] = 61
    release.set()
    with pytest.raises(controls.site.ControlWriteRejected):
        await asyncio.wait_for(pending, 2)
    await asyncio.gather(*holders)
    assert len(posts) == int(resend)


@pytest.mark.asyncio
async def test_grid_queue_deadline_uses_native_clock_when_confirmation_clock_is_injected(controls):
    hass = NS(data={})
    posts = []
    raw = NS(post=lambda *a, **kw: (posts.append(kw) or Reply()),
             get=lambda *a, **kw: Reply({"response": {"components": {
                 "disallow_charge_from_grid_with_solar_installed": True}}}))
    session = controls.http.controlled_session(hass, raw)
    outcome = await controls.grid.async_set_tesla_grid_charging_confirmed(
        session, "https://tesla.test", "site", {}, False,
        operation_deadline=60, monotonic=lambda: 0.0, poll_offsets=(0,),
    )
    assert outcome.applied
    assert len(posts) == 1
    assert not any(name.startswith("_power_sync_") for name in posts[0])


def energy_coordinator(controls):
    entry = NS(entry_id="entry", data={"tesla_energy_site_id": "site"})
    entries = {"entry": entry}
    hass = NS(data={"power_sync": {"entry": {}}},
              config_entries=NS(async_get_entry=entries.get))
    queued = asyncio.Event()
    calls = []

    def request(method, url, **kwargs):
        calls.append((method, kwargs))
        return Reply({"response": {"enabled": True}})

    session = controls.http.controlled_session(hass, NS(request=request))

    class Observed:
        def request(self, method, *args, **kwargs):
            result = session.request(method, *args, **kwargs)
            if method.upper() not in ("GET", "HEAD", "OPTIONS"):
                queued.set()
            return result

    ns = dict(asyncio=asyncio, aiohttp=HTTP, DOMAIN="power_sync",
              _LOGGER=logging.getLogger(__name__), _parse_retry_after=lambda response: None)
    node = load_source_node(ROOT / "coordinator.py", "_tesla_api_call", parent="TeslaEnergyCoordinator")
    exec(compile(ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[])),
                 "coordinator_api", "exec", flags=__import__("__future__").annotations.compiler_flag), ns)
    cls = type("Coordinator", (), {"_tesla_api_call": ns["_tesla_api_call"]})
    obj = cls()
    obj.hass, obj._entry_id, obj.site_id = hass, "entry", "site"
    obj.api_base_url, obj.session = "https://tesla.test", Observed()
    obj._tesla_headers = lambda: {}
    runtime = hass.data["power_sync"]["entry"]
    runtime["tesla_coordinator"] = obj
    return obj, entries, runtime, queued, calls


@pytest.mark.asyncio
@pytest.mark.parametrize("revocation", ["runtime", "coordinator", "entry", "site", "entry_site", "session", "stopping"])
async def test_generic_coordinator_write_rechecks_owner_and_identity_after_queue(controls, revocation):
    obj, entries, runtime, queued, calls = energy_coordinator(controls)
    site = controls.site.coordinator_for(obj.hass, site_id="site")
    async with site.write_context("preceding write"):
        pending = asyncio.create_task(obj._tesla_api_call("POST", "/api/1/energy_sites/site/storm_mode"))
        await asyncio.wait_for(queued.wait(), 2)
        assert calls == []
        if revocation == "runtime":
            obj.hass.data["power_sync"]["entry"] = {"tesla_coordinator": obj}
        elif revocation == "coordinator":
            runtime["tesla_coordinator"] = object()
        elif revocation == "entry":
            entries["entry"] = NS(data={"tesla_energy_site_id": "site"})
        elif revocation == "site":
            obj.site_id = "other"
        elif revocation == "entry_site":
            entries["entry"].data["tesla_energy_site_id"] = "other"
        elif revocation == "session":
            obj.session = object()
        else:
            runtime["powerwall_local_dispatch_stopping"] = True
    with pytest.raises(controls.site.ControlWriteRejected):
        await asyncio.wait_for(pending, 2)
    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("read", ["GET", "HEAD", "OPTIONS"])
async def test_generic_coordinator_status_reads_progress_while_mutation_waits(controls, read):
    obj, _, _, queued, calls = energy_coordinator(controls)
    site = controls.site.coordinator_for(obj.hass, site_id="site")
    path = "/api/1/energy_sites/site/storm_mode"
    async with site.write_context("preceding write"):
        pending = asyncio.create_task(obj._tesla_api_call("POST", path))
        await asyncio.wait_for(queued.wait(), 2)
        status, body = await obj._tesla_api_call(read, path)
        assert status == 200 and body == {"response": {"enabled": True}}
        assert [verb for verb, _ in calls] == [read]
    assert (await asyncio.wait_for(pending, 2))[0] == 200
    assert [verb for verb, _ in calls] == [read, "POST"]
    assert not any(name.startswith("_power_sync_") for _, kwargs in calls for name in kwargs)


@pytest.mark.asyncio
@pytest.mark.parametrize("initial_active", [False, True])
@pytest.mark.parametrize("resend", [False, True])
async def test_demand_protection_flip_blocks_queued_initial_or_resend_and_converges(controls, initial_active, resend):
    obj, _, runtime, _, _ = energy_coordinator(controls)
    state = {"active": initial_active}
    clock, attempts, posts, holders = [0.0], [0], [], []
    held, release, queued = asyncio.Event(), asyncio.Event(), asyncio.Event()
    site = controls.site.coordinator_for(obj.hass, site_id="site")

    async def hold():
        async with site.write_context("preceding write"):
            held.set()
            await release.wait()

    async def before_read():
        if resend and attempts[0] == 1:
            holders.append(asyncio.create_task(hold()))
            await held.wait()

    def post(url, **kwargs):
        posts.append(kwargs)
        return Reply()

    def get(*args, **kwargs):
        disallowed = posts[-1]["json"]["disallow_charge_from_grid_with_solar_installed"]
        if resend and attempts[0] == 1:
            disallowed = not disallowed
        return Reply({"response": {"components": {
            "disallow_charge_from_grid_with_solar_installed": disallowed}}}, before_enter=before_read)

    session = controls.http.controlled_session(obj.hass, NS(post=post, get=get))
    submissions = [0]

    class Observed:
        def post(self, *args, **kwargs):
            submissions[0] += 1
            request = session.post(*args, **kwargs)
            if submissions[0] == (2 if resend else 1):
                queued.set()
            return request

        def get(self, *args, **kwargs):
            return session.get(*args, **kwargs)

    obj.session = Observed()
    obj._get_current_token = lambda: "token"
    obj._tesla_headers = lambda *args: {}
    obj.invalidate_site_info_cache = lambda: None

    async def sleep(delay):
        clock[0] += delay

    async def grid_write(*args, **kwargs):
        attempts[0] += 1
        return await controls.grid.async_set_tesla_grid_charging_confirmed(
            *args, **kwargs, confirmation_deadline=60, operation_deadline=clock[0] + 60,
            retry_after=45, poll_offsets=(45,) if resend and attempts[0] == 1 else (0,),
            sleep=sleep, monotonic=lambda: clock[0])

    ns = dict(async_set_tesla_grid_charging_confirmed=grid_write,
              TeslaGridWriteOutcome=controls.grid.TeslaGridWriteOutcome,
              TeslaGridWriteStatus=controls.grid.TeslaGridWriteStatus,
              _LOGGER=logging.getLogger(__name__))
    node = load_source_node(ROOT / "coordinator.py", "set_grid_charging_enabled_outcome", parent="TeslaEnergyCoordinator")
    exec(compile(ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[])),
                 "coordinator_grid", "exec", flags=__import__("__future__").annotations.compiler_flag), ns)
    obj.set_grid_charging_enabled_outcome = ns["set_grid_charging_enabled_outcome"].__get__(obj)
    ns = dict(__package__=controls.site.__package__, hass=obj.hass, time=time,
              entry=obj.hass.config_entries.async_get_entry(obj._entry_id),
              demand_grid_charging_control_lock=asyncio.Lock(),
              _aemo_dispatch_entry_data=lambda: obj.hass.data["power_sync"].get("entry"),
              _demand_grid_charging_protection_active=lambda **kwargs: state["active"],
              DEMAND_GRID_CHARGING_MAX_RESTORE_ATTEMPTS=3,
              tesla_grid_charging_enabled_from_site_info=controls.grid.tesla_grid_charging_enabled_from_site_info,
              _LOGGER=logging.getLogger(__name__))
    node = load_source_node(ROOT / "__init__.py", "_enforce_demand_grid_charging_protection")
    exec(compile(ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[])),
                 "demand_enforcement", "exec", flags=__import__("__future__").annotations.compiler_flag), ns)
    if not resend:
        holders.append(asyncio.create_task(hold()))
        await held.wait()
    pending = asyncio.create_task(ns["_enforce_demand_grid_charging_protection"](obj))
    await asyncio.wait_for(queued.wait(), 2)
    assert len(posts) == int(resend)
    state["active"] = not initial_active
    release.set()
    assert await asyncio.wait_for(pending, 2) == (True, not initial_active)
    await asyncio.gather(*holders)
    assert len(posts) == 1 + int(resend)
    assert posts[-1]["json"] == {"disallow_charge_from_grid_with_solar_installed": not initial_active}
    assert runtime["grid_charging_disabled_for_demand"] is (not initial_active)


@pytest.mark.asyncio
async def test_demand_runtime_replacement_cannot_publish_old_legacy_write_success():
    runtime, replacement = {}, {"grid_charging_disabled_for_demand": "new-owner"}
    hass = NS(data={"power_sync": {"entry": runtime}})
    calls = []

    async def write(enabled):
        calls.append(enabled)
        hass.data["power_sync"]["entry"] = replacement
        return True

    coordinator = NS(set_grid_charging_enabled=write)
    runtime["tesla_coordinator"] = coordinator
    ns = dict(hass=hass, time=time, demand_grid_charging_control_lock=asyncio.Lock(),
              _aemo_dispatch_entry_data=lambda: hass.data["power_sync"].get("entry"),
              _demand_grid_charging_protection_active=lambda **kwargs: True,
              DEMAND_GRID_CHARGING_MAX_RESTORE_ATTEMPTS=3,
              _LOGGER=logging.getLogger(__name__))
    node = load_source_node(ROOT / "__init__.py", "_enforce_demand_grid_charging_protection")
    exec(compile(ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[])),
                 "demand_enforcement", "exec", flags=__import__("__future__").annotations.compiler_flag), ns)
    assert await ns["_enforce_demand_grid_charging_protection"](coordinator) == (False, True)
    assert calls == [False]
    assert replacement == {"grid_charging_disabled_for_demand": "new-owner"}
