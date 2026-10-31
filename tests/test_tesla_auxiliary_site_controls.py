"""Actual auxiliary Tesla setters/getters use the shared final mutation gate."""
from __future__ import annotations

import ast
import asyncio
import copy
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from conftest import load_source_node
from test_tesla_queued_settings import controls, energy_coordinator, Reply


ROOT = Path(__file__).parents[1] / "custom_components/power_sync"
METHODS = (
    "async_set_storm_watch", "async_get_storm_watch_status",
    "async_set_off_grid_ev_reserve", "async_set_vpp_enrollment",
    "async_get_vpp_programs", "invalidate_site_info_cache",
)


def auxiliary(controls, *, status=200):
    obj, entries, runtime, _queued, _calls = energy_coordinator(controls)
    namespace = obj._tesla_api_call.__globals__
    nodes = [load_source_node(ROOT / "coordinator.py", name, parent="TeslaEnergyCoordinator")
             for name in METHODS]
    exec(compile(ast.fix_missing_locations(ast.Module(nodes, type_ignores=[])),
                 "auxiliary-coordinator-controls", "exec"), namespace)
    for name in METHODS:
        setattr(type(obj), name, namespace[name])
    obj._storm_mode_enabled = False
    obj._off_grid_reserve_percent = 11
    obj._vpp_programs_cache = [{"program_id": "original", "enrolled": False}]
    obj._site_info_cache = {"baseline": "original"}
    obj._site_info_last_fetch = 50
    obj._site_info_fetch_failed = True
    obj._site_info_retry_at = 60
    calls, queued = [], asyncio.Event()
    response = NS(status=status, body={"response": {"result": True}})

    def request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        result = Reply(copy.deepcopy(response.body))
        result.status = response.status
        return result

    session = controls.http.controlled_session(obj.hass, NS(request=request), entry_id=obj._entry_id)

    class Observed:
        def request(self, method, *args, **kwargs):
            result = session.request(method, *args, **kwargs)
            if method.upper() not in ("GET", "HEAD", "OPTIONS"):
                queued.set()
            return result

    obj.session = Observed()
    return NS(obj=obj, entries=entries, runtime=runtime, calls=calls, queued=queued,
              response=response, site=controls.site.coordinator_for(obj.hass, site_id="site"))


def cached_state(obj):
    return copy.deepcopy((obj._storm_mode_enabled, obj._off_grid_reserve_percent,
        obj._vpp_programs_cache, obj._site_info_cache, obj._site_info_last_fetch,
        obj._site_info_fetch_failed, obj._site_info_retry_at))


CASES = [
    pytest.param("async_set_storm_watch", (True,), "storm_mode", {"enabled": True}, id="storm-on"),
    pytest.param("async_set_storm_watch", (False,), "storm_mode", {"enabled": False}, id="storm-off"),
    pytest.param("async_set_off_grid_ev_reserve", (-5,), "off_grid_vehicle_charging_reserve",
                 {"off_grid_vehicle_charging_reserve_percent": 0}, id="ev-reserve-floor"),
    pytest.param("async_set_off_grid_ev_reserve", (47,), "off_grid_vehicle_charging_reserve",
                 {"off_grid_vehicle_charging_reserve_percent": 47}, id="ev-reserve"),
    pytest.param("async_set_off_grid_ev_reserve", (150,), "off_grid_vehicle_charging_reserve",
                 {"off_grid_vehicle_charging_reserve_percent": 100}, id="ev-reserve-cap"),
    pytest.param("async_set_vpp_enrollment", ("program-new", True), "programs",
                 {"program_id": "program-new", "enrolled": True}, id="vpp-enroll"),
    pytest.param("async_set_vpp_enrollment", ("program-new", False), "programs",
                 {"program_id": "program-new", "enrolled": False}, id="vpp-unenroll"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("method,args,suffix,payload", CASES)
@pytest.mark.parametrize("status", [200, 400])
async def test_actual_auxiliary_setter_endpoint_payload_and_http_outcome(controls, method, args, suffix, payload, status):
    p = auxiliary(controls, status=status)
    baseline = cached_state(p.obj)
    assert await getattr(p.obj, method)(*args) is (status == 200)
    assert len(p.calls) == 1
    verb, url, kwargs = p.calls[0]
    assert verb == "POST" and url == f"https://tesla.test/api/1/energy_sites/site/{suffix}"
    assert kwargs["json"] == payload
    assert not any(key.startswith("_power_sync_") for key in kwargs)
    if status != 200:
        assert cached_state(p.obj) == baseline
        return
    assert p.obj._site_info_cache is None
    if method == "async_set_storm_watch":
        assert p.obj._storm_mode_enabled is payload["enabled"]
        assert p.obj._off_grid_reserve_percent == 11
    elif method == "async_set_off_grid_ev_reserve":
        assert p.obj._off_grid_reserve_percent == payload["off_grid_vehicle_charging_reserve_percent"]
        assert p.obj._storm_mode_enabled is False
    else:
        assert p.obj._vpp_programs_cache is None
        assert p.obj._off_grid_reserve_percent == 11


@pytest.mark.asyncio
@pytest.mark.parametrize("method,args", [
    ("async_set_off_grid_ev_reserve", ("unreadable",)),
    ("async_set_off_grid_ev_reserve", (None,)),
    ("async_set_vpp_enrollment", ("", True)),
])
async def test_invalid_auxiliary_request_never_contacts_provider(controls, method, args):
    p = auxiliary(controls)
    baseline = cached_state(p.obj)
    assert await getattr(p.obj, method)(*args) is False
    assert p.calls == [] and cached_state(p.obj) == baseline


SETTERS = [
    pytest.param("async_set_storm_watch", (True,), id="storm"),
    pytest.param("async_set_off_grid_ev_reserve", (47,), id="ev-reserve"),
    pytest.param("async_set_vpp_enrollment", ("program-new", True), id="vpp"),
]


def foreign_owner(p):
    p.obj.hass.data["power_sync"]["foreign-entry"] = {
        "entry": NS(data={"tesla_energy_site_id": "site"}, options={}),
        "force_discharge_state": {"active": True},
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("method,args", SETTERS)
@pytest.mark.parametrize("revocation", ["runtime", "site", "foreign"])
async def test_actual_auxiliary_setter_rechecks_final_owner_after_queue(controls, method, args, revocation):
    p = auxiliary(controls)
    baseline = cached_state(p.obj)
    async with p.site.write_context("preceding physical mutation"):
        pending = asyncio.create_task(getattr(p.obj, method)(*args))
        await asyncio.wait_for(p.queued.wait(), timeout=2)
        assert p.calls == []
        if revocation == "runtime":
            p.obj.hass.data["power_sync"][p.obj._entry_id] = {"tesla_coordinator": p.obj}
        elif revocation == "site":
            p.obj.site_id = "different-site"
        else:
            foreign_owner(p)
    with pytest.raises(controls.site.ControlWriteRejected):
        await asyncio.wait_for(pending, timeout=2)
    assert p.calls == [] and cached_state(p.obj) == baseline


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["storm", "ev-reserve", "vpp"])
async def test_actual_auxiliary_status_get_remains_available_behind_foreign_owner(controls, kind):
    p = auxiliary(controls)
    method, args = {
        "storm": ("async_set_storm_watch", (False,)),
        "ev-reserve": ("async_set_off_grid_ev_reserve", (47,)),
        "vpp": ("async_set_vpp_enrollment", ("program-new", False)),
    }[kind]
    async with p.site.write_context("preceding physical mutation"):
        pending = asyncio.create_task(getattr(p.obj, method)(*args))
        await asyncio.wait_for(p.queued.wait(), timeout=2)
        foreign_owner(p)
        if kind == "storm":
            p.response.body = {"response": {"enabled": True, "active": False}}
            result = await asyncio.wait_for(p.obj.async_get_storm_watch_status(), timeout=2)
            assert result == {"enabled": True, "active": False}
            assert p.obj._storm_mode_enabled is True
            suffix = "storm_mode"
        elif kind == "vpp":
            p.response.body = {"response": {"programs": [{"program_id": "program-new", "enrolled": True}]}}
            result = await asyncio.wait_for(p.obj.async_get_vpp_programs(force_refresh=True), timeout=2)
            assert result == [{"program_id": "program-new", "enrolled": True}]
            assert p.obj._vpp_programs_cache == result
            suffix = "programs"
        else:
            # This control has no dedicated getter: site_info is its independent read path.
            p.response.body = {"response": {"off_grid_vehicle_charging_reserve_percent": 11}}
            status, body = await asyncio.wait_for(p.obj._tesla_api_call(
                "GET", "/api/1/energy_sites/site/site_info"), timeout=2)
            assert status == 200 and body == p.response.body
            assert p.obj._off_grid_reserve_percent == 11
            suffix = "site_info"
        assert [call[0] for call in p.calls] == ["GET"]
        assert p.calls[0][1].endswith("/energy_sites/site/" + suffix)
        assert not pending.done()
    with pytest.raises(controls.site.ControlWriteRejected):
        await asyncio.wait_for(pending, timeout=2)
    assert [call[0] for call in p.calls] == ["GET"]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["storm", "vpp"])
async def test_actual_auxiliary_getter_error_preserves_last_known_setting(controls, kind):
    p = auxiliary(controls, status=400)
    baseline = cached_state(p.obj)
    if kind == "storm":
        assert await p.obj.async_get_storm_watch_status() is None
    else:
        assert await p.obj.async_get_vpp_programs(force_refresh=True) == baseline[2]
    assert cached_state(p.obj) == baseline
    assert len(p.calls) == 1 and p.calls[0][0] == "GET"


@pytest.mark.asyncio
async def test_actual_vpp_forced_get_bypasses_cached_programs_without_issuing_mutation(controls):
    p = auxiliary(controls)
    assert await p.obj.async_get_vpp_programs() == [{"program_id": "original", "enrolled": False}]
    assert p.calls == []
    p.response.body = {"response": {"enrolled_programs": [{"program_id": "new", "enrolled": True}, "invalid"]}}
    assert await p.obj.async_get_vpp_programs(force_refresh=True) == [{"program_id": "new", "enrolled": True}]
    assert len(p.calls) == 1 and p.calls[0][0] == "GET"
