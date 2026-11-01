"""Focused durability tests for finite manual EV cleanup obligations."""

from __future__ import annotations

import asyncio
import importlib
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


ROOT = Path(__file__).resolve().parent.parent / "custom_components" / "power_sync"

_ps = types.ModuleType("power_sync")
_ps.__path__ = [str(ROOT)]
sys.modules["power_sync"] = _ps

_automations = types.ModuleType("power_sync.automations")
_automations.__path__ = [str(ROOT / "automations")]
sys.modules["power_sync.automations"] = _automations

_ha = sys.modules.setdefault("homeassistant", types.ModuleType("homeassistant"))
_ha_helpers = sys.modules.setdefault(
    "homeassistant.helpers",
    types.ModuleType("homeassistant.helpers"),
)
_ha_event = sys.modules.setdefault(
    "homeassistant.helpers.event",
    types.ModuleType("homeassistant.helpers.event"),
)
setattr(_ha, "helpers", _ha_helpers)
setattr(_ha_helpers, "event", _ha_event)
# Keep the lightweight Home Assistant stub patchable by ``monkeypatch``.
_ha_event.async_track_point_in_utc_time = lambda *_args, **_kwargs: None

ev_ownership = importlib.import_module("power_sync.automations.ev_ownership")


@pytest.fixture(autouse=True)
def _bind_current_homeassistant_event_module(monkeypatch):
    """Patch the event module object used by each full-collection test.

    Several AST-style tests install lightweight Home Assistant helper modules
    while the suite is collected.  A later test can replace
    ``sys.modules['homeassistant.helpers.event']`` and leave the module object
    cached above detached from the import path used by ``ev_ownership``.
    Bind both the canonical module entry and its parent package immediately
    before each test so the existing per-test monkeypatches affect the object
    that production code imports.
    """
    module = sys.modules[__name__]
    ha_root = sys.modules.get("homeassistant")
    if ha_root is None:
        ha_root = types.ModuleType("homeassistant")
    monkeypatch.setitem(sys.modules, "homeassistant", ha_root)
    helpers = sys.modules.get("homeassistant.helpers")
    if helpers is None:
        helpers = types.ModuleType("homeassistant.helpers")
    monkeypatch.setitem(sys.modules, "homeassistant.helpers", helpers)
    event = sys.modules.get("homeassistant.helpers.event")
    if event is None:
        event = types.ModuleType("homeassistant.helpers.event")
    monkeypatch.setitem(sys.modules, "homeassistant.helpers.event", event)
    if not hasattr(event, "async_track_point_in_utc_time"):
        monkeypatch.setattr(
            event,
            "async_track_point_in_utc_time",
            lambda *_args, **_kwargs: None,
            raising=False,
        )
    monkeypatch.setattr(ha_root, "helpers", helpers, raising=False)
    monkeypatch.setattr(helpers, "event", event, raising=False)
    monkeypatch.setattr(module, "_ha_helpers", helpers)
    monkeypatch.setattr(module, "_ha_event", event)


class _Entry:
    entry_id = "entry-1"
    data = {}
    options = {}


class _Store:
    def __init__(self, data=None):
        self._data = data or {}
        self.saved = 0

    async def async_save(self):
        self.saved += 1


class _Hass:
    def __init__(self, store):
        self.data = {
            "power_sync": {
                "entry-1": {"automation_store": store},
            }
        }
        self.created_tasks = []

    def async_create_task(self, coro):
        self.created_tasks.append(coro)


def _close_created_tasks(hass):
    for task in hass.created_tasks:
        task.close()


def test_finite_obligation_is_persisted_before_cleanup_timer_and_keeps_original_deadline(
    monkeypatch,
):
    scheduled = []

    def track(_hass, callback, when):
        cancelled = []

        def cancel():
            cancelled.append(True)

        scheduled.append((callback, when, cancelled))
        return cancel

    monkeypatch.setattr(_ha_event, "async_track_point_in_utc_time", track)
    now = datetime(2026, 11, 1, 23, 0, tzinfo=timezone.utc)
    deadline = now + timedelta(minutes=3)
    store = _Store()
    hass = _Hass(store)

    record = asyncio.run(
        ev_ownership.async_begin_ev_quick_control_obligation(
            hass,
            _Entry(),
            "VIN-A",
            {
                "vehicle_vin": "VIN-A",
                "charger_type": "tesla",
                "quick_control": True,
                "duration_minutes": 3,
                "expires_at": deadline.isoformat(),
                "_boost_start_deadline": deadline.timestamp(),
            },
        )
    )

    assert record is not None
    assert record["expires_at"] == deadline.isoformat()
    assert store.saved == 1
    assert store._data["ev_runtime_state"]["ev_quick_control_obligations"]["VIN-A"][
        "expires_at"
    ] == deadline.isoformat()
    assert scheduled[0][1] == deadline
    _close_created_tasks(hass)


def test_expiry_callback_uses_exact_vehicle_cleanup_and_clears_only_after_terminal_stop(
    monkeypatch,
):
    callbacks = []
    timers = []

    def track(_hass, callback, when):
        callbacks.append((callback, when))

        def cancel():
            timers.append(when)

        return cancel

    monkeypatch.setattr(_ha_event, "async_track_point_in_utc_time", track)
    actions = types.ModuleType("power_sync.automations.actions")
    stop = AsyncMock(return_value=True)
    actions._action_stop_ev_charging_dynamic = stop
    monkeypatch.setitem(sys.modules, "power_sync.automations.actions", actions)
    deadline = datetime.now(timezone.utc) + timedelta(minutes=2)
    store = _Store()
    hass = _Hass(store)

    record = asyncio.run(
        ev_ownership.async_begin_ev_quick_control_obligation(
            hass,
            _Entry(),
            "VIN-A",
            {
                "vehicle_vin": "VIN-A",
                "charger_type": "tesla",
                "quick_control": True,
                "expires_at": deadline.isoformat(),
                "_boost_start_deadline": deadline.timestamp(),
            },
        )
    )
    assert record is not None

    asyncio.run(callbacks[0][0](deadline))

    stop.assert_awaited_once()
    cleanup_params = stop.await_args.args[2]
    assert cleanup_params["vehicle_id"] == "VIN-A"
    assert cleanup_params["vehicle_vin"] == "VIN-A"
    assert cleanup_params["stop_untracked"] is True
    assert cleanup_params["_force_tesla_stop_request"] is True
    assert cleanup_params["_quick_control_obligation_id"] == record[
        "obligation_id"
    ]
    assert "VIN-A" not in hass.data["power_sync"]["entry-1"].get(
        "ev_quick_control_obligations", {}
    )
    assert timers
    _close_created_tasks(hass)


def test_failed_expiry_stop_retries_without_moving_original_deadline(monkeypatch):
    scheduled = []

    def track(_hass, callback, when):
        scheduled.append((callback, when))
        return lambda: None

    monkeypatch.setattr(_ha_event, "async_track_point_in_utc_time", track)
    actions = types.ModuleType("power_sync.automations.actions")
    stop = AsyncMock(return_value=False)
    actions._action_stop_ev_charging_dynamic = stop
    monkeypatch.setitem(sys.modules, "power_sync.automations.actions", actions)
    # Exercise the retry branch as it runs at/after the original expiry.
    deadline = datetime.now(timezone.utc) - timedelta(seconds=1)
    store = _Store()
    hass = _Hass(store)

    record = asyncio.run(
        ev_ownership.async_begin_ev_quick_control_obligation(
            hass,
            _Entry(),
            "VIN-A",
            {
                "vehicle_vin": "VIN-A",
                "charger_type": "tesla",
                "quick_control": True,
                "expires_at": deadline.isoformat(),
                "_boost_start_deadline": deadline.timestamp(),
            },
        )
    )
    asyncio.run(scheduled[0][0](deadline))

    assert stop.await_count == 1
    assert len(scheduled) == 2
    assert scheduled[1][1] > deadline
    assert scheduled[1][1] != datetime.fromisoformat(record["expires_at"])
    retained = ev_ownership.get_ev_quick_control_obligation(
        hass,
        _Entry(),
        "VIN-A",
    )
    assert retained["expires_at"] == record["expires_at"]
    assert retained["retry_count"] == 1
    _close_created_tasks(hass)


def test_old_obligation_token_cannot_clear_replacement_session(monkeypatch):
    monkeypatch.setattr(
        _ha_event,
        "async_track_point_in_utc_time",
        lambda *_args, **_kwargs: (lambda: None),
    )
    deadline = datetime.now(timezone.utc) + timedelta(minutes=2)
    hass = _Hass(_Store())
    params = {
        "vehicle_vin": "VIN-A",
        "charger_type": "tesla",
        "quick_control": True,
        "expires_at": deadline.isoformat(),
        "_boost_start_deadline": deadline.timestamp(),
    }
    first = asyncio.run(
        ev_ownership.async_begin_ev_quick_control_obligation(
            hass, _Entry(), "VIN-A", params
        )
    )
    second = asyncio.run(
        ev_ownership.async_begin_ev_quick_control_obligation(
            hass, _Entry(), "VIN-A", params
        )
    )
    assert first["obligation_id"] != second["obligation_id"]
    assert not ev_ownership.clear_ev_quick_control_obligation(
        hass,
        _Entry(),
        "VIN-A",
        expected_token=first["obligation_id"],
    )
    retained = ev_ownership.get_ev_quick_control_obligation(
        hass, _Entry(), "VIN-A"
    )
    assert retained["obligation_id"] == second["obligation_id"]
    _close_created_tasks(hass)


def test_replacement_save_cancellation_restores_previous_timer_and_record(monkeypatch):
    scheduled = []

    def track(_hass, callback, when):
        cancelled = []

        def cancel():
            cancelled.append(True)

        scheduled.append((callback, when, cancelled))
        return cancel

    monkeypatch.setattr(_ha_event, "async_track_point_in_utc_time", track)
    old_deadline = datetime.now(timezone.utc) + timedelta(minutes=2)
    new_deadline = old_deadline + timedelta(minutes=5)
    hass = _Hass(_Store())
    params = {
        "vehicle_vin": "VIN-A",
        "charger_type": "tesla",
        "quick_control": True,
        "expires_at": old_deadline.isoformat(),
        "_boost_start_deadline": old_deadline.timestamp(),
    }
    old = asyncio.run(
        ev_ownership.async_begin_ev_quick_control_obligation(
            hass, _Entry(), "VIN-A", params
        )
    )

    async def cancelled_persist(*_args, **_kwargs):
        raise asyncio.CancelledError()

    monkeypatch.setattr(ev_ownership, "persist_ev_runtime_state", cancelled_persist)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            ev_ownership.async_begin_ev_quick_control_obligation(
                hass,
                _Entry(),
                "VIN-A",
                {**params, "expires_at": new_deadline.isoformat(), "_boost_start_deadline": new_deadline.timestamp()},
            )
        )
    retained = ev_ownership.get_ev_quick_control_obligation(hass, _Entry(), "VIN-A")
    assert retained["obligation_id"] == old["obligation_id"]
    assert retained["expires_at"] == old["expires_at"]
    assert scheduled[-1][1] == old_deadline
    _close_created_tasks(hass)


def test_reload_restores_obligation_without_renewing_deadline(monkeypatch):
    scheduled = []

    def track(_hass, _callback, when):
        scheduled.append(when)
        return lambda: None

    monkeypatch.setattr(_ha_event, "async_track_point_in_utc_time", track)
    deadline = datetime.now(timezone.utc) + timedelta(minutes=4)
    store = _Store(
        {
            "ev_runtime_state": {
                "ev_quick_control_obligations": {
                    "VIN-A": {
                        "vehicle_id": "VIN-A",
                        "expires_at": deadline.isoformat(),
                        "_boost_start_deadline": deadline.timestamp(),
                        "params": {
                            "vehicle_vin": "VIN-A",
                            "charger_type": "tesla",
                        },
                    }
                }
            }
        }
    )
    hass = _Hass(store)
    restored = ev_ownership.restore_ev_runtime_state(hass, _Entry(), store)
    obligations = restored["ev_quick_control_obligations"]
    assert obligations["VIN-A"]["expires_at"] == deadline.isoformat()

    ev_ownership.arm_ev_quick_control_obligation(
        hass,
        _Entry(),
        "VIN-A",
        obligations["VIN-A"],
    )

    assert scheduled == [deadline]
    _close_created_tasks(hass)


@pytest.mark.parametrize("params", [{}, "malformed"])
def test_malformed_quick_obligation_is_retained_but_not_armed(monkeypatch, params):
    """Malformed persisted cleanup evidence cannot become an executable Stop."""
    scheduled = []

    monkeypatch.setattr(
        _ha_event,
        "async_track_point_in_utc_time",
        lambda *_args, **_kwargs: scheduled.append(True) or (lambda: None),
    )
    deadline = datetime.now(timezone.utc) + timedelta(minutes=2)
    record = {
        "vehicle_id": "VIN-A",
        "obligation_id": "malformed",
        "expires_at": deadline.isoformat(),
        "params": params,
    }
    store = _Store({"ev_runtime_state": {"ev_quick_control_obligations": {"VIN-A": record}}})
    hass = _Hass(store)
    restored = ev_ownership.restore_ev_runtime_state(hass, _Entry(), store)
    assert restored["ev_quick_control_obligations"]["VIN-A"] == record
    assert ev_ownership.arm_ev_quick_control_obligation(
        hass, _Entry(), "VIN-A", record
    ) is None
    assert scheduled == []


def test_expiry_callback_is_inert_after_runtime_store_replacement(monkeypatch):
    """A callback from an old setup cannot act on a reloaded runtime."""
    callbacks = []

    def track(_hass, callback, when):
        callbacks.append(callback)
        return lambda: None

    monkeypatch.setattr(_ha_event, "async_track_point_in_utc_time", track)
    actions = types.ModuleType("power_sync.automations.actions")
    stop = AsyncMock(return_value=True)
    actions._action_stop_ev_charging_dynamic = stop
    monkeypatch.setitem(sys.modules, "power_sync.automations.actions", actions)

    deadline = datetime.now(timezone.utc) - timedelta(seconds=1)
    store = _Store()
    hass = _Hass(store)
    record = asyncio.run(
        ev_ownership.async_begin_ev_quick_control_obligation(
            hass,
            _Entry(),
            "VIN-A",
            {
                "vehicle_vin": "VIN-A",
                "charger_type": "tesla",
                "quick_control": True,
                "expires_at": deadline.isoformat(),
                "_boost_start_deadline": deadline.timestamp(),
            },
        )
    )
    assert record is not None

    # Simulate HA replacing the config-entry runtime while retaining the
    # persisted token. The callback captured the old entry/store scope.
    hass.data["power_sync"]["entry-1"] = {
        "automation_store": _Store(),
        "ev_quick_control_obligations": {
            "VIN-A": record,
        },
    }
    asyncio.run(callbacks[0](deadline))

    stop.assert_not_awaited()
    _close_created_tasks(hass)

# Compose the real quick timer/reload path with the real independent cloud reader.
from test_ev_boost_native_cleanup import cloud_boost
from test_ev_boost_timers import StripImports, compile_node, source_node
from test_tesla_ev_observation import (
    independent_source, stub_native_vehicle_sources, bind_current_registry_helpers,
)


@pytest.fixture
def native_quick(cloud_boost, monkeypatch):
    import ast
    import copy
    import re
    from collections.abc import Mapping
    from test_ev_boost_timers import VIN_A, VIN_B

    p = cloud_boost()
    h = p.h
    actions = types.ModuleType('power_sync.automations.actions')
    actions._dynamic_ev_state = {}
    actions._action_stop_ev_charging_dynamic = AsyncMock(return_value=False)
    registration = SimpleNamespace(entity_id='switch.native_charge', platform='tesla_fleet',
        config_entry_id=p.native.entry_id, device_id=p.vehicle.device.id,
        unique_id='native-charge', disabled_by=None)
    registrations = {registration.entity_id: registration}
    h.hass.entity_registry = SimpleNamespace(async_get=registrations.get)
    h.hass.device_registry.devices[p.vehicle.device.id] = p.vehicle.device
    h.hass.states = SimpleNamespace(get=lambda _entity: None)
    h.hass.async_create_task = lambda coroutine: coroutine.close()
    namespace = {'Mapping': Mapping, 're': re,
        'CONF_EV_PROVIDER': 'ev_provider', 'EV_PROVIDER_FLEET_API': 'fleet_api',
        'CONF_TESLA_BLE_VEHICLE_MAPPING': 'tesla_ble_vehicle_mapping',
        'resolve_ble_prefixes': lambda *_args: [],
        'er': SimpleNamespace(async_get=lambda _hass: h.hass.entity_registry),
        'dr': SimpleNamespace(async_get=lambda _hass: SimpleNamespace(
            async_get=h.hass.device_registry.devices.get))}
    for name in ('_tesla_legacy_entity_identity', '_tesla_native_binding',
                 '_tesla_native_binding_identity', '_tesla_quick_control_binding_current'):
        node = source_node(ROOT/'automations/actions.py', name)
        exec(compile(ast.Module(body=[node], type_ignores=[]), str(ROOT/'automations/actions.py'),
             'exec', flags=__import__('__future__').annotations.compiler_flag), namespace)
    actions._tesla_quick_control_binding_current = namespace['_tesla_quick_control_binding_current']
    monkeypatch.setitem(sys.modules, actions.__name__, actions)
    monkeypatch.setitem(sys.modules, 'power_sync.automations.ev_ownership', ev_ownership)
    passive = h.view._boost_observation.__func__.__globals__['_passive_ev_cleanup_observation']
    monkeypatch.setattr(sys.modules['power_sync'], '_passive_ev_cleanup_observation', passive, raising=False)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None): return h.now
    monkeypatch.setattr(ev_ownership, 'datetime', Clock)
    scheduled = []
    def track(_hass, callback, when):
        timer = SimpleNamespace(callback=callback, when=when, cancelled=False)
        timer.cancel = lambda: setattr(timer, 'cancelled', True)
        scheduled.append(timer)
        return timer.cancel
    monkeypatch.setattr(_ha_event, 'async_track_point_in_utc_time', track)
    identity = {key:getattr(registration,key) for key in (
        'entity_id','platform','config_entry_id','device_id','unique_id','disabled_by')}
    deadline = h.now+timedelta(minutes=8)
    params = {'vehicle_vin': VIN_A, 'charger_type':'tesla', 'quick_control':True,
        'expires_at':deadline.isoformat(), '_boost_start_deadline':deadline.timestamp(),
        '_quick_control_binding':{'vehicle_id':VIN_A, 'provider':'both',
            'ble_prefixes':(), 'ble_mapping':None, 'native_entity':identity}}

    async def begin():
        record = await ev_ownership.async_begin_ev_quick_control_obligation(h.hass,h.entry,VIN_A,params)
        assert record is not None
        h.now = deadline+timedelta(seconds=1)
        p.fresh(state='Disconnected')
        return record

    def restore_method():
        recovery = importlib.import_module('power_sync.tesla_ev_recovery')
        ns = {'DOMAIN':'power_sync', 'Mapping':Mapping, 'copy':copy, 'datetime':Clock,
            'dt_util':SimpleNamespace(utcnow=lambda:h.now, UTC=timezone.utc),
            '_LOGGER':SimpleNamespace(warning=lambda *_args:None,info=lambda *_args:None),
            '_tesla_quick_control_binding_current':actions._tesla_quick_control_binding_current,
            'record_manual_ev_charging_session':AsyncMock(),
            'async_reconcile_quick_control_stopped':ev_ownership.async_reconcile_quick_control_stopped,
            'get_vehicle_route':recovery.get_vehicle_route,
            'async_rebind_native_session_route':recovery.async_rebind_native_session_route,
            **{name:getattr(ev_ownership,name) for name in (
                'claim_ev_ownership','get_ev_quick_control_obligation','get_ev_ownership',
                'release_ev_ownership','clear_ev_quick_control_obligation')}}
        method = compile_node(StripImports().visit(source_node(
            ROOT/'__init__.py','restore_manual_quick_sessions',parent='EVVehicleCommandView')),ns)
        view = SimpleNamespace(_hass=h.hass,
            _manual_action_params=lambda _vin:{'charger_type':'tesla','vehicle_vin':VIN_A},
            _execute_manual_ev_action_for_entry=AsyncMock(return_value=(False,'unconfirmed')))
        return method,view

    return SimpleNamespace(p=p,h=h,actions=actions,scheduled=scheduled,begin=begin,
        restore_method=restore_method,VIN=VIN_A,other=VIN_B,registration=registration,
        namespace=namespace,params=params,deadline=deadline)


@pytest.mark.asyncio
@pytest.mark.parametrize('path',['timer','reload'])
async def test_expired_quick_unplugged_cloud_proof_avoids_redundant_stop(native_quick,path):
    q=native_quick
    record=await q.begin()
    if path=='timer': await q.scheduled[0].callback(q.h.now)
    else:
        restore,view=q.restore_method()
        lease={'resume_params':{'quick_control':True,'vehicle_vin':q.VIN},
            'quick_control':True,'expires_at':q.deadline.isoformat(),'duration_minutes':8}
        await restore(view,q.h.entry,{}, {q.VIN:lease})
        view._execute_manual_ev_action_for_entry.assert_not_awaited()
    q.actions._action_stop_ev_charging_dynamic.assert_not_awaited()
    assert q.h.runtime['ev_quick_control_obligations']=={}
    assert q.h.store.saved['ev_runtime_state']['ev_quick_control_obligations']=={}
    assert q.p.coordinator.api.wake_calls==[]
    assert len(q.p.coordinator.api.calls)==1
    assert record['expires_at']==q.deadline.isoformat()


@pytest.mark.asyncio
@pytest.mark.parametrize('passive',['charging','stopped'])
async def test_fresh_passive_quick_proof_never_pays_for_native_read(native_quick,passive):
    q=native_quick
    await q.begin()
    q.h.physical[q.VIN]=passive=='charging'
    q.h.observation_overrides[q.VIN]['_charging_observed_at']=q.h.now
    await q.scheduled[0].callback(q.h.now)
    assert q.p.coordinator.api.calls==q.p.coordinator.api.refresh_calls==[]
    if passive=='charging': q.actions._action_stop_ev_charging_dynamic.assert_awaited_once()
    else:
        q.actions._action_stop_ev_charging_dynamic.assert_not_awaited()
        assert q.h.runtime['ev_quick_control_obligations']=={}


@pytest.mark.asyncio
@pytest.mark.parametrize('case',['stale','charging','source','pairing','owner','replacement','runtime'])
async def test_quick_native_cleanup_retains_unknown_or_changed_scope(native_quick,case):
    q=native_quick
    record=await q.begin()
    if case=='stale': q.p.fresh(stamp=q.deadline.timestamp()-1)
    if case=='charging': q.p.fresh(state='Charging',current=5,power=1)
    async def change():
        if case=='source': q.p.vehicle.device.identifiers={('tesla_fleet',q.other)}
        if case=='pairing': q.registration.unique_id='replacement'
        if case=='owner': q.h.runtime['ev_ownership']={q.VIN:{'owner':'powersync','owner_mode':'boost'}}
        if case=='replacement':
            q.h.runtime['ev_quick_control_obligations'][q.VIN]={**record,'obligation_id':'new'}
        if case=='runtime': q.h.hass.data['power_sync'][q.h.entry.entry_id]=dict(q.h.runtime)
    q.p.coordinator.api.before_return=change
    await q.scheduled[0].callback(q.h.now)
    assert q.VIN in q.h.runtime['ev_quick_control_obligations']
    assert record['expires_at']==q.deadline.isoformat()
    if case not in ('stale','charging'):
        q.actions._action_stop_ev_charging_dynamic.assert_not_awaited()


@pytest.mark.asyncio
async def test_pending_positive_hazard_is_not_retired_by_stopped_quick_read(native_quick):
    q=native_quick
    await q.begin()
    recovery=importlib.import_module('power_sync.tesla_ev_recovery')
    ticket=await recovery.async_begin_attempt(q.h.hass,q.h.entry,q.VIN,
        {'kind':'ha','entity_id':q.registration.entity_id},'start',q.registration.entity_id)
    assert ticket is not None
    await q.scheduled[0].callback(q.h.now)
    assert q.VIN in q.h.runtime['ev_quick_control_obligations']
    assert recovery.get_vehicle_route(q.h.hass,q.h.entry,q.VIN)['kind']=='blocked'
    assert q.p.coordinator.api.calls==[]


@pytest.mark.asyncio
@pytest.mark.parametrize('failure',['exception','false'])
async def test_quick_retirement_checkpoint_failure_retains_obligation_and_retries(native_quick,monkeypatch,failure):
    q=native_quick
    await q.begin()
    record=q.h.runtime['ev_quick_control_obligations'][q.VIN]
    original_timer=q.scheduled[0]
    save=(AsyncMock(side_effect=RuntimeError('projection Store unavailable'))
        if failure=='exception' else AsyncMock(return_value=False))
    monkeypatch.setattr(q.h.store,'async_save',save)
    await original_timer.callback(q.h.now)
    assert q.h.runtime['ev_quick_control_obligations'][q.VIN] is record
    assert q.h.store.saved['ev_runtime_state']['ev_quick_control_obligations'][q.VIN]['expires_at']==q.deadline.isoformat()
    assert record['expires_at']==q.deadline.isoformat()
    assert len(q.scheduled)==2
    assert q.scheduled[1].when==q.h.now+timedelta(seconds=30)
    assert ev_ownership.get_ev_quick_control_timer(q.h.hass,q.h.entry,q.VIN) is q.scheduled[1].cancel
    q.actions._action_stop_ev_charging_dynamic.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_quick_retirement_save_preserves_replacement_family_owner_and_timer(native_quick,monkeypatch):
    from copy import deepcopy
    q=native_quick
    record=await q.begin()
    records=q.h.runtime['ev_quick_control_obligations']
    replacement=deepcopy(record)
    replacement['obligation_id']='replacement-generation'
    replacement['params']['_quick_control_obligation_id']=replacement['obligation_id']
    replacement['expires_at']=(q.h.now+timedelta(minutes=5)).isoformat()
    replacement['_boost_start_deadline']=(q.h.now+timedelta(minutes=5)).timestamp()
    family={q.VIN:replacement}
    owner={'owner':'powersync','owner_mode':'manual','session_id':'replacement'}
    persist=ev_ownership.persist_ev_runtime_state
    saves=0
    async def save(*args,**kwargs):
        nonlocal saves
        saves+=1
        if saves==1: return await persist(*args,**kwargs)
        assert saves==2
        q.h.runtime['ev_quick_control_obligations']=family
        q.h.runtime['ev_ownership']={q.VIN:owner}
        ev_ownership.arm_ev_quick_control_obligation(q.h.hass,q.h.entry,q.VIN,replacement)
        return False
    monkeypatch.setattr(ev_ownership,'persist_ev_runtime_state',save)
    await q.scheduled[0].callback(q.h.now)
    assert q.h.runtime['ev_quick_control_obligations'] is family
    assert family[q.VIN] is replacement
    assert q.h.runtime['ev_ownership'][q.VIN] is owner
    assert q.VIN not in records
    assert len(q.scheduled)==2
    assert not q.scheduled[1].cancelled
    assert ev_ownership.get_ev_quick_control_timer(q.h.hass,q.h.entry,q.VIN) is q.scheduled[1].cancel
    assert q.scheduled[1].when==datetime.fromisoformat(replacement['expires_at'])
    q.actions._action_stop_ev_charging_dynamic.assert_not_awaited()


@pytest.mark.asyncio
async def test_malformed_quick_family_survives_hydration_and_startup_restore_without_callbacks():
    from collections.abc import Mapping
    from test_tesla_ev_recovery_lifecycle import (
        _context, _function, _close, recovery, ownership,
    )
    malformed='retained malformed evidence'
    state=recovery._new_sidecar()
    state['obligations']={'ev_boost_sessions':{},'ev_quick_control_obligations':malformed}
    hass,entry,runtime,store,_sidecar,_events,tasks=_context(enrolled=True,sidecar_value=state)
    assert not await _function('_async_prepare_ev_recovery_lifecycle')(hass,entry,store)
    restored=ownership.restore_ev_runtime_state(hass,entry,store)
    retained=restored['ev_quick_control_obligations']
    assert retained==malformed
    armed=[]
    restore=compile_node(StripImports().visit(source_node(
        ROOT/'__init__.py','restore_ev_quick_control_obligations',parent='EVVehicleCommandView')),
        {'Mapping':Mapping,'arm_ev_quick_control_obligation':lambda *_args:armed.append(True)})
    if retained:  # The real startup callsite uses this truthiness boundary.
        restore(SimpleNamespace(_hass=hass),entry,retained)
    assert armed==[]
    assert runtime['ev_quick_control_obligations']==malformed
    assert runtime['_tesla_ev_recovery_sidecar_hold']=='finite_obligations_malformed'
    assert not recovery.recovery_control_allowed(hass,entry,'5YJ3E1EA7NF0000A1',0,'start')
    assert recovery.recovery_control_allowed(hass,entry,'5YJ3E1EA7NF0000A1',0,'stop')
    _close(tasks)
