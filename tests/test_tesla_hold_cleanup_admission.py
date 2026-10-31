"""Automatic Hold cleanup reaches the same final gate as user restoration."""
from __future__ import annotations

import ast
import asyncio
from functools import lru_cache
import importlib
import logging
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from conftest import load_power_sync_function
from test_native_rate_admission import admission
from test_tesla_queued_settings import Reply


ROOT = Path(__file__).parents[1] / "custom_components/power_sync"
HOLD_CLEANUP_PATHS = (
    "auto_restore_hold_soc", "auto_restore_hold_soc_persisted",
    "_retry_tesla_hold_restore", "restore_force_mode_from_persistence",
)


@lru_cache(maxsize=1)
def cleanup_payloads():
    """Evaluate each actual automatic Hold caller's service payload."""
    tree = ast.parse((ROOT / "__init__.py").read_text())
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    payloads = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        fields = {key.value: value for key, value in zip(node.keys, node.values)
                  if isinstance(key, ast.Constant)}
        source = fields.get("source")
        if not isinstance(source, ast.Constant) or source.value != "hold_soc_cleanup":
            continue
        parent = parents[node]
        while not isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef)):
            parent = parents[parent]
        assert parent.name not in payloads
        payloads[parent.name] = eval(compile(ast.Expression(node), str(ROOT / "__init__.py"), "eval"),
                                     {"solaredge_generation": None, "next_retry": 1})
    assert set(payloads) == set(HOLD_CLEANUP_PATHS)
    return payloads


@pytest.fixture
def cleanup(admission):
    a = admission
    core = importlib.import_module(a.m.__package__ + ".powerwall_control")
    http = importlib.import_module(a.m.__package__ + ".powerwall_control_http")
    runtime, entry, hass = a.runtime, a.entry, a.hass
    site = core.coordinator_for(hass, entry)
    runtime["powerwall_site_control"] = site
    runtime["entry"] = entry
    posts, queued, child_queued = [], asyncio.Event(), asyncio.Event()
    state = NS(monitoring=True, confirmed=0, post_attempts=0, sleep_hook=None, generation=1)

    def post(url, **kwargs):
        posts.append((url, kwargs))
        state.confirmed = kwargs["json"]["backup_reserve_percent"]
        return Reply()

    controlled = http.controlled_session(hass, NS(post=post), entry_id=entry.entry_id)

    class Session:
        def post(self, *args, **kwargs):
            state.post_attempts += 1
            queued.set()
            if state.post_attempts == 2:
                child_queued.set()
            return controlled.post(*args, **kwargs)

    session = Session()
    env = {
        "__package__": a.m.__package__, "DOMAIN": "power_sync", "hass": hass, "entry": entry,
        "_local_powerwall_runtime": runtime, "_is_monitoring_mode": lambda: state.monitoring,
        "battery_connection_profile": NS(monitoring_only=False), "_hold_soc_transition_token": object(),
        "asyncio": asyncio, "_LOGGER": logging.getLogger(__name__),
        "get_tesla_api_base_url": lambda *_args: "https://tesla.test",
        "CONF_FLEET_API_BASE_URL": "fleet_api_base_url",
        "aiohttp": NS(ClientTimeout=lambda **kwargs: NS(**kwargs), ClientError=ConnectionError),
        "async_get_clientsession": lambda _hass: session,
    }
    for name in ("_local_powerwall_generation_current", "_control_call_source",
                 "_monitoring_mode_should_block_control", "_site_control_admission",
                 "_tesla_force_set_backup_reserve_cloud", "_tesla_force_apply_backup_reserve_unlocked"):
        load_power_sync_function(name, env)

    async def confirm(_session, _base, _site, _headers, expected, *, is_current=None):
        return state.confirmed == expected and (is_current is None or is_current())

    env["_tesla_force_confirm_backup_reserve"] = confirm
    runtime["powerwall_control_admission"] = env["_site_control_admission"]

    async def first_pulse(_call):
        # This is the real first mutation from _tesla_force_pulse_backup_reserve.
        # Exercise it directly so a shielded finally cannot hide its refusal.
        return await env["_tesla_force_apply_backup_reserve_unlocked"](
            [("site", "test-token", "fleet")], 1, reason="Hold cleanup temporary 1%")

    first_pulse.__name__ = "handle_restore_normal"
    guarded = a.m.guard_battery_transition(hass, entry, first_pulse)
    return NS(a=a, core=core, hass=hass, entry=entry, runtime=runtime, site=site,
              env=env, state=state, posts=posts, queued=queued, child_queued=child_queued,
              handler=first_pulse, guarded=guarded)


def cleanup_call(p, path="auto_restore_hold_soc"):
    return NS(data={**cleanup_payloads()[path], "entry_id": p.entry.entry_id},
              context=NS(user_id=None, parent_id=None))


@pytest.mark.asyncio
@pytest.mark.parametrize("path", HOLD_CLEANUP_PATHS)
@pytest.mark.parametrize("monitoring", [True, False])
async def test_actual_hold_cleanup_payload_reaches_first_cloud_pulse(cleanup, path, monitoring):
    p = cleanup
    p.state.monitoring = monitoring
    call = cleanup_call(p, path)
    assert call.data["source"] == "hold_soc_cleanup" and call.data["_force_restore"] is True
    result = await p.guarded(call)
    assert p.env["_site_control_admission"](call, restoring=True) is True
    assert result["confirmed_sites"] == ["site"]
    assert len(p.posts) == 1
    url, kwargs = p.posts[0]
    assert url == "https://tesla.test/api/1/energy_sites/site/backup"
    assert kwargs["json"] == {"backup_reserve_percent": 1}
    assert not any(key.startswith("_power_sync_") for key in kwargs)


@pytest.mark.asyncio
@pytest.mark.parametrize("handler_name", ["handle_force_charge", "handle_hold_battery_soc"])
async def test_cleanup_markers_cannot_authorize_new_forcing_in_monitoring(cleanup, handler_name):
    p = cleanup
    p.handler.__name__ = handler_name
    guarded = p.a.m.guard_battery_transition(p.hass, p.entry, p.handler)
    call = cleanup_call(p)
    call.data.update(_allow_monitoring_restore=True, power_w=1000, duration=5)
    assert p.env["_site_control_admission"](call, restoring=False) is False
    with pytest.raises(p.core.ControlRequestStale):
        await guarded(call)
    assert p.posts == []


@pytest.mark.asyncio
@pytest.mark.parametrize("revocation", ["runtime", "site", "cleanup-marker", "foreign-owner"])
async def test_cleanup_first_pulse_rechecks_currency_after_mutation_queue(cleanup, revocation):
    p = cleanup
    call = cleanup_call(p)
    async with p.site.write_context("preceding mutation"):
        pending = asyncio.create_task(p.guarded(call))
        await asyncio.wait_for(p.queued.wait(), timeout=2)
        assert p.posts == []
        if revocation == "runtime":
            p.hass.data["power_sync"][p.entry.entry_id] = dict(p.runtime)
        elif revocation == "site":
            p.entry.data["tesla_energy_site_id"] = "new-site"
        elif revocation == "cleanup-marker":
            call.data.pop("_allow_monitoring_restore", None)
        else:
            p.hass.data["power_sync"]["foreign"] = {
                "entry": NS(data={"tesla_energy_site_id": "site"}, options={}),
                "force_charge_state": {"active": True},
            }
    with pytest.raises((p.core.ControlRequestStale, p.core.ControlIdentityChanged)):
        await pending
    assert p.posts == []


def real_pulse(p):
    async def sleep(delay):
        if p.state.sleep_hook is not None:
            await p.state.sleep_hook(delay)
        else:
            await asyncio.sleep(0)

    p.env.update(asyncio=NS(current_task=asyncio.current_task, create_task=asyncio.create_task,
        shield=asyncio.shield, CancelledError=asyncio.CancelledError, TimeoutError=asyncio.TimeoutError,
        sleep=sleep), _tesla_reserve_write_lock=asyncio.Lock(), _tesla_reserve_write_tasks=set(),
        _tesla_reserve_pulse_runtime=p.runtime)
    for name in ("_entry_percent_int", "_normalize_tesla_backup_reserve_percent",
                 "_tesla_backup_reserve_pulse_percent", "_tesla_force_result_all_confirmed",
                 "_tesla_force_apply_backup_reserve", "_tesla_force_pulse_backup_reserve"):
        load_power_sync_function(name, p.env)

    async def pulse(_call):
        return await p.env["_tesla_force_pulse_backup_reserve"](
            [("site", "test-token", "fleet")], 0, reason="Hold cleanup",
            is_current=lambda: p.state.generation == 1)

    pulse.__name__ = "handle_restore_normal"
    return p.a.m.guard_battery_transition(p.hass, p.entry, pulse)


@pytest.mark.asyncio
@pytest.mark.parametrize("revocation", ["site", "runtime", "store", "coordinator", "foreign-owner"])
async def test_shielded_exact_restore_rechecks_original_owner_while_queued(cleanup, revocation):
    p = cleanup
    held, release = asyncio.Event(), asyncio.Event()
    holders = []

    async def hold():
        async with p.site.write_context("preceding exact restore"):
            held.set()
            await release.wait()

    async def pause(_delay):
        holders.append(asyncio.create_task(hold()))
        await held.wait()

    p.state.sleep_hook = pause
    pending = asyncio.create_task(real_pulse(p)(cleanup_call(p)))
    await asyncio.wait_for(p.child_queued.wait(), timeout=2)
    assert [kwargs["json"]["backup_reserve_percent"] for _url, kwargs in p.posts] == [1]
    if revocation == "site":
        p.entry.data["tesla_energy_site_id"] = "new-site"
    elif revocation == "runtime":
        p.hass.data["power_sync"][p.entry.entry_id] = dict(p.runtime)
    elif revocation == "store":
        p.runtime["store"] = object()
    elif revocation == "coordinator":
        p.runtime["powerwall_site_control"] = object()
    else:
        p.hass.data["power_sync"]["foreign"] = {
            "entry": NS(data={"tesla_energy_site_id": "site"}, options={}),
            "force_charge_state": {"active": True},
        }
    release.set()
    await asyncio.gather(*holders)
    with pytest.raises((p.core.ControlRequestStale, p.core.ControlIdentityChanged)):
        await pending
    # The temporary write preceded revocation; neither old nor new site gets
    # an exact-target write after the captured owner is no longer current.
    assert [kwargs["json"]["backup_reserve_percent"] for _url, kwargs in p.posts] == [1]
    assert not p.env["_tesla_reserve_write_tasks"]


@pytest.mark.asyncio
@pytest.mark.parametrize("revocation", ["site", "runtime"])
async def test_rejected_initial_pulse_cannot_escape_through_shielded_finally(cleanup, revocation):
    p = cleanup
    async with p.site.write_context("preceding pulse"):
        pending = asyncio.create_task(real_pulse(p)(cleanup_call(p)))
        await asyncio.wait_for(p.queued.wait(), timeout=2)
        if revocation == "site":
            p.entry.data["tesla_energy_site_id"] = "new-site"
        else:
            p.hass.data["power_sync"][p.entry.entry_id] = dict(p.runtime)
    with pytest.raises((p.core.ControlRequestStale, p.core.ControlIdentityChanged)):
        await pending
    assert p.posts == []


@pytest.mark.asyncio
async def test_same_site_cancellation_still_drains_exact_target(cleanup):
    p = cleanup
    paused = asyncio.Event()

    async def pause(_delay):
        paused.set()
        await asyncio.Event().wait()

    p.state.sleep_hook = pause
    pending = asyncio.create_task(real_pulse(p)(cleanup_call(p)))
    await asyncio.wait_for(paused.wait(), timeout=2)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert [kwargs["json"]["backup_reserve_percent"] for _url, kwargs in p.posts] == [1, 0]
    assert not p.env["_tesla_reserve_write_tasks"]


@pytest.mark.asyncio
async def test_same_site_supersession_drains_before_newer_reserve_write(cleanup):
    p = cleanup
    newer = []

    async def pause(_delay):
        p.state.generation = 2
        newer.append(asyncio.create_task(p.env["_tesla_force_apply_backup_reserve"](
            [("site", "test-token", "fleet")], 37, reason="newer user reserve")))
        await asyncio.sleep(0)

    p.state.sleep_hook = pause
    result = await real_pulse(p)(cleanup_call(p))
    await asyncio.gather(*newer)
    assert result["failed_sites"] == ["site"]
    assert [kwargs["json"]["backup_reserve_percent"] for _url, kwargs in p.posts] == [1, 0, 37]


@pytest.mark.asyncio
async def test_exact_cleanup_survives_forcing_deadline_without_renewing_it(cleanup):
    p = cleanup
    now = [0.0]
    p.site._clock = lambda: now[0]

    async def pause(_delay):
        now[0] = 2.0

    p.state.sleep_hook = pause
    real_pulse(p)
    async with p.site.operation(label="original force", entry_id=p.entry.entry_id,
                              target=p.core.BatteryPowerTarget(-1000), deadline=1.0):
        result = await p.env["_tesla_force_pulse_backup_reserve"](
            [("site", "test-token", "fleet")], 0, reason="deadline cleanup")
        assert result["confirmed_sites"] == ["site"]
        with pytest.raises(p.core.ControlDeadlineExpired):
            await p.env["_tesla_force_apply_backup_reserve_unlocked"](
                [("site", "test-token", "fleet")], 1, reason="expired renewal")
    assert [kwargs["json"]["backup_reserve_percent"] for _url, kwargs in p.posts] == [1, 0]


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["coordinator", "site-identity"])
async def test_pulse_without_original_site_binding_never_starts(cleanup, missing):
    p = cleanup
    if missing == "coordinator":
        p.runtime.pop("powerwall_site_control")
    else:
        p.entry.data["tesla_energy_site_id"] = None
    result = await real_pulse(p)(cleanup_call(p))
    assert result["failed_sites"] == ["site"] and p.posts == []
    assert not p.env["_tesla_reserve_write_tasks"]


@pytest.mark.asyncio
async def test_exact_cleanup_keeps_canonical_equivalent_site_identity(cleanup):
    p = cleanup
    p.entry.data["tesla_energy_site_id"] = " site "
    result = await real_pulse(p)(cleanup_call(p))
    assert result["confirmed_sites"] == ["site"]
    assert [kwargs["json"]["backup_reserve_percent"] for _url, kwargs in p.posts] == [1, 0]
