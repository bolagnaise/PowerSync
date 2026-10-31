"""Exercise the real Boost handler and EV gate with fake charger I/O/timers."""

from __future__ import annotations

import ast
import asyncio
from contextlib import nullcontext
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import wraps
from inspect import signature
import logging
from pathlib import Path
from types import MethodType, SimpleNamespace
from uuid import uuid4

import pytest

from conftest import load_power_sync_submodule, load_source_node as source_node


ROOT = Path(__file__).resolve().parents[1] / "custom_components/power_sync"
INIT = ROOT / "__init__.py"
VIN_A = "5YJ3E1EA0KF000001"
VIN_B = "5YJ3E1EA0KF000002"
own = load_power_sync_submodule(
    "automations.ev_ownership", package="boost_timer_actual"
)
mapping = load_power_sync_submodule("tesla_ble_mapping", package="boost_timer_actual")
ev_load = load_power_sync_submodule("ev_load", package="boost_timer_actual")


class Store:
    def __init__(self):
        self._data = {}
        self.saved = {}
        self.fail = False
        self.pause = None
        self.pause_after = None
        self.saves = 0
        self.fail_after = None

    async def async_save(self):
        self.saves += 1
        if self.pause and (self.pause_after is None or self.saves >= self.pause_after):
            entered, release = self.pause
            entered.set()
            await release.wait()
        if self.fail or (self.fail_after is not None and self.saves >= self.fail_after):
            raise OSError("storage unavailable")
        self.saved = deepcopy(self._data)


class StripImports(ast.NodeTransformer):
    def visit_ImportFrom(self, node):
        return ast.copy_location(ast.Pass(), node)


def compile_node(node, namespace):
    module = ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[]))
    exec(compile(module, str(INIT), "exec"), namespace)
    return namespace[node.name]


@dataclass
class Timer:
    callback: object
    deadline: datetime
    cancelled: bool = False
    fired: bool = False

    def cancel(self):
        self.cancelled = True


class BoostHarness:
    def __init__(self):
        self.entry = SimpleNamespace(entry_id="entry", data={}, options={})
        self.store = Store()
        self.runtime = {"automation_store": self.store}
        self.hass = SimpleNamespace(data={"power_sync": {"entry": self.runtime}})
        self.view = SimpleNamespace(_hass=self.hass, _config_entry=self.entry)
        self.now = datetime(2026, 10, 31, tzinfo=timezone.utc)
        self.states = {"entry": {}}
        self.timers = []
        self.calls = []
        self.fail_start = set()
        self.ambiguous_start = set()
        self.start_hook = None
        self.fail_stop = set()
        self.unknown_stop = set()
        self.default_vin = VIN_A
        self.physical = {}
        self.observation_overrides = {}
        self.start_pause = None
        self.gate_requested = asyncio.Event()
        self.gate = SimpleNamespace(
            lock=asyncio.Lock(), owner=None, blocked=lambda _vin: False
        )

        async def load():
            pass

        async def ownership(_hass, _entry_id):
            self.gate_requested.set()
            return self.gate

        self.gate.load = load
        self.guard = compile_node(
            source_node(ROOT / "teslemetry_solar.py", "guard_ev_control"),
            {
                "signature": signature,
                "wraps": wraps,
                "asyncio": asyncio,
                "ownership": ownership,
                "_ev_submission_scope": lambda *_args: nullcontext(),
            },
        )
        self.canonical = compile_node(
            source_node(ROOT / "automations/actions.py", "_canonical_dynamic_tesla_vehicle_id"),
            {
                "canonical_tesla_vehicle_id": mapping.canonical_tesla_vehicle_id,
                "_LOGGER": logging.getLogger(__name__),
            },
        )
        self.ev_actions = SimpleNamespace(
            _dynamic_ev_state=self.states,
            _canonical_dynamic_tesla_vehicle_id=self.canonical,
        )
        self.apply = self.guard(self._apply)
        namespace = {
            "execute_actions": self.execute,
            "ev_actions": self.ev_actions,
            "guard_ev_control": self.guard,
            "DOMAIN": "power_sync",
            "CONF_ZAPTEC_STANDALONE_ENABLED": "zaptec_standalone_enabled",
            "CONF_ZAPTEC_USERNAME": "zaptec_username",
            "async_track_point_in_utc_time": self.track,
            "dt_util": SimpleNamespace(utcnow=lambda: self.now),
            "timedelta": timedelta,
            "asyncio": asyncio,
            "datetime": datetime,
            "uuid4": uuid4,
            "deepcopy": deepcopy,
            "persist_ev_runtime_state": own.persist_ev_runtime_state,
            "is_current_ev_power_observation": ev_load.is_current_ev_power_observation,
            "_get_ev_vehicles_status": lambda *_args, **_kwargs: self.observations(),
            "_LOGGER": logging.getLogger(__name__),
            "web": SimpleNamespace(
                json_response=lambda body, **kwargs: SimpleNamespace(body=body, **kwargs)
            ),
        }
        for name in (
            "claim_ev_ownership", "get_active_ev_owner_mode", "get_ev_ownership",
            "owner_family", "record_ev_command", "release_ev_ownership",
            "reconcile_boost_supersession", "boost_recovery_observation_after",
        ):
            namespace[name] = getattr(own, name)
        self.post = compile_node(
            StripImports().visit(source_node(INIT, "post", parent="ChargingBoostView")),
            namespace,
        )
        for name in ("_boost_pairing_identity", "_boost_observation", "_save_boost_state",
                     "_arm_boost_stop", "restore_boost_sessions", "_start_finite_boost"):
            method = compile_node(
                StripImports().visit(source_node(INIT, name, parent="ChargingBoostView")),
                namespace,
            )
            setattr(self.view, name, MethodType(method, self.view))

    def observations(self):
        return [{"vehicle_id": vin, "is_charging": charging,
                 "is_connected": True, "_charging_state_known": True,
                 "_boost_charging_state_evidence": {
                     "known": self.observation_overrides.get(vin, {}).get("_charging_state_known", True),
                     "charging": self.observation_overrides.get(vin, {}).get("is_charging", charging),
                     "observed_at": self.observation_overrides.get(vin, {}).get("_charging_observed_at", self.now),
                 },
                 "_charging_observed_at": self.now, "site_presence": "home",
                 **self.observation_overrides.get(vin, {})}
                for vin, charging in self.physical.items()]

    async def reload(self, at=None, fail_stop=(), blocked=()):
        await own.persist_ev_runtime_state(self.hass, self.entry, self.store)
        self.unload_timers()
        restored = BoostHarness()
        restored.now = at or self.now
        restored.entry.data = dict(self.entry.data)
        restored.entry.options = dict(self.entry.options)
        restored.physical = dict(self.physical)
        restored.fail_stop.update(fail_stop)
        restored.gate.blocked = lambda vin: vin in blocked
        restored.observation_overrides = deepcopy(self.observation_overrides)
        restored.store._data = deepcopy(self.store.saved)
        # Execute the actual setup restore call and its Boost dispatch branch.
        setup = source_node(INIT, "async_setup_entry")
        restore = next(node for node in ast.walk(setup)
                       if isinstance(node, ast.Assign) and any(
                           isinstance(target, ast.Name) and target.id == "restored_ev_runtime"
                           for target in node.targets))
        dispatch = next(node for node in ast.walk(setup)
                        if isinstance(node, ast.If) and isinstance(node.test, ast.NamedExpr)
                        and isinstance(node.test.target, ast.Name)
                        and node.test.target.id == "boost_sessions")
        method = ast.AsyncFunctionDef(
            name="restore_boost_setup",
            args=ast.arguments(posonlyargs=[], args=[ast.arg(arg=name) for name in
                               ("hass", "entry", "automation_store")],
                               kwonlyargs=[], kw_defaults=[], defaults=[]),
            body=[restore, dispatch], decorator_list=[],
        )
        actual_setup = compile_node(method, {
            "restore_ev_runtime_state": own.restore_ev_runtime_state,
            "ChargingBoostView": lambda *_args: restored.view,
        })
        await actual_setup(restored.hass, restored.entry, restored.store)
        return restored

    def track(self, _hass, callback, deadline):
        timer = Timer(callback, deadline)
        self.timers.append(timer)
        return timer.cancel

    async def _apply(self, hass, config_entry, params):
        kind = params["_kind"]
        requested_vin = params.get("vehicle_vin") or params.get("vehicle_id")
        vin = self.canonical(hass, config_entry, requested_vin or self.default_vin)
        self.calls.append((kind, vin, dict(params)))
        if kind == "start_ev_charging_dynamic":
            if self.start_hook:
                self.start_hook(vin, params)
            if self.start_pause:
                entered, release = self.start_pause
                entered.set()
                await release.wait()
            if vin in self.ambiguous_start:
                self.physical[vin] = True
                return False
            if vin in self.fail_start:
                return False
            # A duplicate start can reuse the actual controller's state object.
            state = self.states["entry"].setdefault(vin, {})
            state.update(active=True, current_amps=6, params=dict(params))
            self.physical[vin] = True
            state["ownership"] = own.claim_ev_ownership(
                hass, config_entry, vin, owner_mode="boost"
            )
            return True
        if kind == "stop_ev_charging_dynamic":
            if vin in self.unknown_stop:
                return None
            if vin in self.fail_stop:
                return False
            expected = params.get("_expected_state")
            if expected is not None and self.states["entry"].get(vin) is not expected:
                return True
            self.states["entry"].pop(vin, None)
            self.physical[vin] = False
            self.now += timedelta(microseconds=1)
            self.observation_overrides.setdefault(vin, {})["_charging_observed_at"] = self.now
            own.release_ev_ownership(hass, config_entry, vin)
            return True
        if kind == "set_ev_charge_limit":
            return True
        raise AssertionError(f"Unexpected charger action: {kind}")

    async def execute(self, hass, config_entry, actions):
        results = []
        for action in actions:
            results.append(await self.apply(
                hass, config_entry,
                {**action["parameters"], "_kind": action["action_type"]},
            ))
        return True if any(result is True for result in results) else (
            None if any(result is None for result in results) else False
        )

    async def start(self, vin, minutes=1):
        async def payload():
            return {"vehicle_id": vin, "duration_minutes": minutes}

        return (await self.post(self.view, SimpleNamespace(json=payload))).body

    async def due(self):
        for timer in list(self.timers):
            if timer.deadline <= self.now and not timer.cancelled and not timer.fired:
                timer.fired = True
                await timer.callback(self.now)

    def stops(self):
        return [vin for kind, vin, _params in self.calls if kind == "stop_ev_charging_dynamic"]

    def owner(self, vin):
        return own.get_ev_ownership(self.hass, self.entry, vin)[1]

    def unload_timers(self):
        unload = source_node(INIT, "async_unload_entry")
        keys = {"ev_boost_timers", "ev_boost_cancel"}
        statements = [
            node for node in unload.body
            if isinstance(node, (ast.If, ast.For))
            and any(isinstance(child, ast.Constant) and isinstance(child.value, str)
                    and child.value in keys for child in ast.walk(node))
        ]
        assert statements, "Boost unload cleanup must be exercised from production source"
        function = ast.FunctionDef(
            name="unload_boost_timers",
            args=ast.arguments(posonlyargs=[], args=[ast.arg(arg="entry_data")],
                               kwonlyargs=[], kw_defaults=[], defaults=[]),
            body=statements, decorator_list=[],
        )
        compile_node(function, {"_LOGGER": logging.getLogger(__name__)})(self.runtime)


@pytest.mark.asyncio
async def test_two_vins_keep_original_distinct_deadlines_and_both_expire():
    h = BoostHarness()
    first = await h.start(VIN_A, 1)
    first_state, first_lease = h.states["entry"][VIN_A], h.owner(VIN_A)
    h.now += timedelta(seconds=20)
    second = await h.start(VIN_B, 3)
    assert first["success"] and second["success"]
    assert not h.timers[0].cancelled
    assert datetime.fromisoformat(first["stops_at"]) == h.timers[0].deadline
    assert datetime.fromisoformat(second["stops_at"]) == h.timers[1].deadline
    second_record = h.runtime["ev_boost_timers"][VIN_B]
    second_state, second_lease = h.states["entry"][VIN_B], h.owner(VIN_B)

    h.now += timedelta(seconds=40)
    await h.due()
    assert h.stops() == [VIN_A]
    assert VIN_A not in h.states["entry"] and h.owner(VIN_A) is None
    assert h.states["entry"][VIN_B] is second_state and h.owner(VIN_B) is second_lease
    assert h.runtime["ev_boost_timers"] == {VIN_B: second_record}
    assert not h.timers[1].cancelled
    stop_params = h.calls[-1][2]
    assert stop_params["_expected_state"] is first_state
    assert first_lease["owner_mode"] == "boost"

    h.now += timedelta(minutes=3)
    await h.due()
    assert h.stops() == [VIN_A, VIN_B]
    assert h.states["entry"] == {} and h.runtime["ev_boost_timers"] == {}


@pytest.mark.asyncio
async def test_stopped_vin_expiry_leaves_other_vehicle_timer_and_owner():
    h = BoostHarness()
    await h.start(VIN_A)
    await h.start(VIN_B, 2)
    second_state, second_lease = h.states["entry"][VIN_B], h.owner(VIN_B)
    await h.execute(h.hass, h.entry, [{
        "action_type": "stop_ev_charging_dynamic",
        "parameters": {"vehicle_id": VIN_A, "stop_charging": True},
    }])
    await h.timers[0].callback(h.now + timedelta(minutes=1))
    assert h.stops() == [VIN_A]
    assert h.states["entry"][VIN_B] is second_state and h.owner(VIN_B) is second_lease
    assert set(h.runtime["ev_boost_timers"]) == {VIN_B}
    assert not h.timers[1].cancelled


@pytest.mark.asyncio
async def test_same_vin_replacement_cancels_only_old_timer_and_stale_callback_is_inert():
    h = BoostHarness()
    await h.start(VIN_A)
    state = h.states["entry"][VIN_A]
    h.now += timedelta(seconds=10)
    await h.start(VIN_A, 2)
    replacement = h.runtime["ev_boost_timers"][VIN_A]
    lease = h.owner(VIN_A)
    assert h.states["entry"][VIN_A] is state
    assert h.timers[0].cancelled and not h.timers[1].cancelled
    await h.timers[0].callback(h.now + timedelta(minutes=1))
    assert h.stops() == []
    assert h.runtime["ev_boost_timers"][VIN_A] is replacement
    assert h.owner(VIN_A) is lease and h.states["entry"][VIN_A] is state
    h.now = h.timers[1].deadline
    await h.due()
    assert h.stops() == [VIN_A]


@pytest.mark.asyncio
async def test_expiry_waiting_on_ev_gate_rechecks_replacement_lease_and_timer():
    h = BoostHarness()
    await h.start(VIN_A)
    old_timer = h.timers[0]
    entered, release = asyncio.Event(), asyncio.Event()
    h.start_pause = (entered, release)
    replacement_task = asyncio.create_task(h.start(VIN_A, 2))
    await asyncio.wait_for(entered.wait(), timeout=1)
    h.gate_requested.clear()
    expiry_task = asyncio.create_task(old_timer.callback(h.now + timedelta(minutes=1)))
    await asyncio.wait_for(h.gate_requested.wait(), timeout=1)
    assert not expiry_task.done()
    release.set()
    assert (await asyncio.wait_for(replacement_task, timeout=1))["success"]
    lease, record = h.owner(VIN_A), h.runtime["ev_boost_timers"][VIN_A]
    await asyncio.wait_for(expiry_task, timeout=1)
    assert h.stops() == []
    assert h.owner(VIN_A) is lease and h.runtime["ev_boost_timers"][VIN_A] is record


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_vin", [VIN_A, VIN_B])
async def test_failed_start_leaves_current_boost_owner_and_deadline(failed_vin):
    h = BoostHarness()
    await h.start(VIN_A)
    state, lease = h.states["entry"][VIN_A], h.owner(VIN_A)
    first_deadline = h.timers[0].deadline
    h.fail_start.add(failed_vin)
    response = await h.start(failed_vin, 2)
    assert response["success"] is False and response["recovery_pending"] is True
    assert not h.timers[0].cancelled
    assert h.runtime["ev_boost_timers"][VIN_A]["stops_at"] == first_deadline
    assert h.timers[0].deadline == first_deadline
    assert h.states["entry"][VIN_A] is state and h.owner(VIN_A) is lease
    h.now = first_deadline
    await h.due()
    assert h.stops() == [VIN_A]


@pytest.mark.asyncio
@pytest.mark.parametrize("replacement", ["lease", "state", "runtime"])
async def test_expiry_cannot_stop_changed_owner_session_or_runtime(replacement):
    h = BoostHarness()
    await h.start(VIN_A)
    if replacement == "lease":
        # Keep the same business mode; object identity still distinguishes it.
        own.claim_ev_ownership(h.hass, h.entry, VIN_A, owner_mode="boost")
    elif replacement == "state":
        h.states["entry"][VIN_A] = dict(h.states["entry"][VIN_A])
    else:
        h.hass.data["power_sync"]["entry"] = dict(h.runtime)
    current_state, current_lease = h.states["entry"][VIN_A], h.owner(VIN_A)
    await h.timers[0].callback(h.now + timedelta(minutes=1))
    assert h.stops() == []
    assert h.states["entry"][VIN_A] is current_state and h.owner(VIN_A) is current_lease


@pytest.mark.asyncio
async def test_ble_alias_and_physical_vin_share_one_boost_timer():
    h = BoostHarness()
    h.entry.options = {
        mapping.CONF_EV_PROVIDER: mapping.EV_PROVIDER_BOTH,
        mapping.CONF_TESLA_BLE_ENTITY_PREFIX: "car_a",
        mapping.CONF_TESLA_BLE_VEHICLE_MAPPING: f"{VIN_A}=car_a",
    }
    assert h.canonical(h.hass, h.entry, "ble_car_a") == VIN_A
    await h.start("ble_car_a")
    assert set(h.runtime["ev_boost_timers"]) == {VIN_A}
    await h.start(VIN_A, 2)
    assert set(h.runtime["ev_boost_timers"]) == {VIN_A}
    assert h.timers[0].cancelled and not h.timers[1].cancelled
    h.now = h.timers[1].deadline
    await h.due()
    assert h.stops() == [VIN_A]
    assert h.calls[-1][2]["vehicle_vin"] == VIN_A


@pytest.mark.asyncio
async def test_default_boost_timer_targets_the_resolved_physical_vin():
    h = BoostHarness()
    h.physical[VIN_A] = False
    assert (await h.start(None))["success"]
    assert set(h.runtime["ev_boost_timers"]) == {VIN_A}
    h.now = h.timers[0].deadline
    await h.due()
    assert h.stops() == [VIN_A]


@pytest.mark.asyncio
async def test_failed_expiry_stop_preserves_failure_diagnostic():
    h = BoostHarness()
    await h.start(VIN_A)
    h.fail_stop.add(VIN_A)
    h.now = h.timers[0].deadline
    await h.due()
    assert h.stops() == [VIN_A]
    command = h.runtime["ev_last_command"][VIN_A]
    assert command["command"] == "stop_boost" and command["success"] is False


@pytest.mark.asyncio
async def test_expiry_preserves_native_ownership_admission():
    h = BoostHarness()
    await h.start(VIN_A)
    state, lease = h.states["entry"][VIN_A], h.owner(VIN_A)
    h.gate.blocked = lambda vin: vin == VIN_A
    await h.timers[0].callback(h.timers[0].deadline)
    assert h.stops() == []
    assert h.states["entry"][VIN_A] is state and h.owner(VIN_A) is lease


@pytest.mark.asyncio
async def test_unload_cancels_all_owned_timers_and_legacy_timer_without_commands():
    h = BoostHarness()
    await h.start(VIN_A)
    await h.start(VIN_B, 2)
    legacy_cancelled = []
    h.runtime["ev_boost_cancel"] = lambda: legacy_cancelled.append(True)
    h.unload_timers()
    assert all(timer.cancelled for timer in h.timers)
    assert legacy_cancelled == [True]
    assert "ev_boost_timers" not in h.runtime and "ev_boost_cancel" not in h.runtime
    for timer in h.timers:
        await timer.callback(timer.deadline)
    assert h.stops() == []
    assert set(h.states["entry"]) == {VIN_A, VIN_B}
    h.unload_timers()
    assert legacy_cancelled == [True]


@pytest.mark.asyncio
async def test_reload_before_expiry_preserves_both_deadlines_without_start_or_rate_replay():
    h = BoostHarness()
    first = await h.start(VIN_A, 2)
    h.now += timedelta(seconds=10)
    second = await h.start(VIN_B, 3)
    restored = await h.reload(at=h.now + timedelta(seconds=20))
    assert restored.calls == [] and restored.states["entry"] == {}
    for vin, response in ((VIN_A, first), (VIN_B, second)):
        assert restored.owner(vin)["owner_mode"] == "boost"
        assert restored.owner(vin)["expires_at"] == response["stops_at"]
        assert restored.runtime["ev_boost_timers"][vin]["stops_at"].isoformat() == response["stops_at"]
        assert restored.store.saved["ev_runtime_state"]["boost_sessions"][vin]["expires_at"] == response["stops_at"]
    restored.now = datetime.fromisoformat(first["stops_at"])
    await restored.due()
    assert restored.stops() == [VIN_A]
    assert restored.owner(VIN_B)["owner_mode"] == "boost"
    assert set(restored.runtime["ev_boost_sessions"]) == {VIN_B}
    restored.now = datetime.fromisoformat(second["stops_at"])
    await restored.due()
    assert restored.stops() == [VIN_A, VIN_B]
    assert restored.runtime["ev_boost_sessions"] == {}
    assert all(kind == "stop_ev_charging_dynamic" for kind, _vin, _params in restored.calls)


@pytest.mark.asyncio
async def test_reload_after_expiry_stops_only_expired_exact_vin_and_keeps_second_timer():
    h = BoostHarness()
    await h.start(VIN_A)
    second = await h.start(VIN_B, 3)
    restored = await h.reload(at=h.now + timedelta(minutes=2))
    assert restored.stops() == [VIN_A]
    assert restored.calls[0][2]["vehicle_vin"] == VIN_A
    assert restored.calls[0][2]["stop_untracked"] is True
    assert restored.owner(VIN_A) is None and restored.owner(VIN_B)["owner_mode"] == "boost"
    assert restored.runtime["ev_boost_timers"][VIN_B]["stops_at"].isoformat() == second["stops_at"]


@pytest.mark.asyncio
async def test_failed_expiry_remains_durable_through_reload_until_fresh_stopped_readback():
    h = BoostHarness()
    response = await h.start(VIN_A)
    h.fail_stop.add(VIN_A)
    h.now = datetime.fromisoformat(response["stops_at"])
    await h.due()
    pending = h.store.saved["ev_runtime_state"]["boost_sessions"][VIN_A]
    assert pending["expires_at"] == response["stops_at"]
    assert pending["last_stop_result"] is False and pending["status"] == "pending_stop"
    restored = await h.reload(at=h.now + timedelta(seconds=1), fail_stop=(VIN_A,))
    assert restored.stops() == [VIN_A]
    assert restored.store.saved["ev_runtime_state"]["boost_sessions"][VIN_A]["expires_at"] == response["stops_at"]
    restored.fail_stop.clear()
    restored.now += timedelta(seconds=30)
    await restored.due()
    assert restored.stops() == [VIN_A, VIN_A]
    assert restored.runtime["ev_boost_sessions"] == {}
    assert restored.store.saved["ev_runtime_state"]["boost_sessions"] == {}


@pytest.mark.asyncio
async def test_accepted_stop_without_fresh_readback_keeps_obligation_and_original_deadline():
    h = BoostHarness()
    response = await h.start(VIN_A)
    stale_at = h.now
    h.observation_overrides[VIN_A] = {"is_charging": True, "_charging_observed_at": stale_at}
    h.now = datetime.fromisoformat(response["stops_at"])
    await h.due()
    assert h.stops() == [VIN_A]
    pending = h.store.saved["ev_runtime_state"]["boost_sessions"][VIN_A]
    assert pending["last_stop_result"] is True
    assert pending["status"] == "pending_stop_readback" and pending["expires_at"] == response["stops_at"]
    h.now += timedelta(seconds=30)
    h.observation_overrides.clear()
    await h.due()
    assert h.stops() == [VIN_A]  # Fresh stop retires without sending another command.
    assert h.store.saved["ev_runtime_state"]["boost_sessions"] == {}


@pytest.mark.asyncio
async def test_reload_stale_observation_keeps_pending_record_without_claim_or_start():
    h = BoostHarness()
    response = await h.start(VIN_A)
    h.observation_overrides[VIN_A] = {"_charging_observed_at": h.now - timedelta(minutes=5)}
    restored = await h.reload(at=h.now + timedelta(seconds=10))
    assert restored.calls == [] and restored.owner(VIN_A) is None
    assert restored.runtime["ev_boost_sessions"][VIN_A]["status"] == "pending_fresh_observation"
    restored.observation_overrides.clear()
    restored.now += timedelta(seconds=30)
    await restored.due()
    assert restored.calls == [] and restored.owner(VIN_A)["owner_mode"] == "boost"
    assert restored.runtime["ev_boost_timers"][VIN_A]["stops_at"].isoformat() == response["stops_at"]


@pytest.mark.asyncio
async def test_reload_fresh_stopped_car_clears_obligation_without_owning_or_starting_it():
    h = BoostHarness()
    await h.start(VIN_A)
    h.physical[VIN_A] = False
    restored = await h.reload(at=h.now + timedelta(seconds=1))
    assert restored.calls == [] and restored.owner(VIN_A) is None
    assert restored.store.saved["ev_runtime_state"]["boost_sessions"] == {}


@pytest.mark.asyncio
async def test_save_failure_returns_failure_compensates_owned_start_and_keeps_unsaved_obligation():
    h = BoostHarness()
    h.store.fail_after = 3
    response = await h.start(VIN_A)
    assert response["success"] is False and response["recovery_pending"] is True
    assert "saved" in response["error"]
    assert h.stops() == [VIN_A]
    pending = h.runtime["ev_boost_sessions"][VIN_A]
    assert pending["expires_at"] == response["stops_at"] and pending["status"] == "pending_persistence"
    h.store.fail = False
    h.store.fail_after = None
    h.now += timedelta(seconds=30)
    await h.due()
    assert h.stops() == [VIN_A]
    assert h.store.saved["ev_runtime_state"]["boost_sessions"] == {}


@pytest.mark.asyncio
async def test_save_and_compensating_stop_failure_keep_original_recovery_obligation():
    h = BoostHarness()
    h.store.fail_after = 3
    h.fail_stop.add(VIN_A)
    response = await h.start(VIN_A)
    assert response["success"] is False
    record = h.runtime["ev_boost_sessions"][VIN_A]
    assert record["last_stop_result"] is False
    h.store.fail = False
    h.store.fail_after = None
    h.now += timedelta(seconds=30)
    await h.due()
    saved = h.store.saved["ev_runtime_state"]["boost_sessions"][VIN_A]
    assert saved["expires_at"] == response["stops_at"] and saved["stop_required"] is True
    h.fail_stop.clear()
    h.now += timedelta(seconds=30)
    await h.due()
    assert h.runtime["ev_boost_sessions"] == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", ["generation", "expires_at", "pairing", "vehicle_id"])
async def test_changed_recovery_identity_or_deadline_cannot_gain_stop_authority(changed):
    h = BoostHarness()
    await h.start(VIN_A)
    record = h.runtime["ev_boost_sessions"][VIN_A]
    if changed == "generation":
        record[changed] = "replacement-generation"
    elif changed == "expires_at":
        record[changed] = (h.now + timedelta(minutes=5)).isoformat()
    elif changed == "pairing":
        h.entry.options["tesla_ble_entity_prefix"] = "replacement_bridge"
    else:
        record[changed] = VIN_B
    await h.timers[0].callback(h.now + timedelta(minutes=1))
    assert h.stops() == [] and h.physical[VIN_A] is True


@pytest.mark.asyncio
async def test_restored_same_vin_replacement_stale_callback_cannot_clear_new_obligation():
    h = BoostHarness()
    await h.start(VIN_A)
    restored = await h.reload()
    old_callback = restored.timers[-1].callback
    replacement = await restored.start(VIN_A, 3)
    record, lease = restored.runtime["ev_boost_sessions"][VIN_A], restored.owner(VIN_A)
    await old_callback(restored.now + timedelta(minutes=2))
    assert restored.stops() == []
    assert restored.runtime["ev_boost_sessions"][VIN_A] is record and restored.owner(VIN_A) is lease
    assert record["expires_at"] == replacement["stops_at"]


@pytest.mark.asyncio
async def test_superseded_boost_cannot_revive_after_reload_and_does_not_block_manual_stop():
    h = BoostHarness()
    response = await h.start(VIN_A)
    own.claim_ev_ownership(h.hass, h.entry, VIN_A, owner_mode="manual", command="update_manual")
    restored = await h.reload(at=datetime.fromisoformat(response["stops_at"]) + timedelta(seconds=1))
    assert restored.calls == [] and restored.owner(VIN_A) is None
    assert restored.runtime["ev_boost_sessions"][VIN_A]["superseded"] is True
    await restored.execute(restored.hass, restored.entry, [{
        "action_type": "stop_ev_charging_dynamic",
        "parameters": {"vehicle_id": VIN_A, "stop_charging": True},
    }])
    assert restored.stops() == [VIN_A]
    restored.now += timedelta(seconds=30)
    await restored.due()
    assert restored.stops() == [VIN_A]
    assert restored.runtime["ev_boost_sessions"] == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("copy_lease", [False, True])
async def test_failed_provisional_takeover_rollback_restores_original_boost_deadline(copy_lease):
    h = BoostHarness()
    response = await h.start(VIN_A)
    prior = h.owner(VIN_A)
    own.claim_ev_ownership(h.hass, h.entry, VIN_A, owner_mode="manual", command="start_manual")
    assert h.runtime["ev_boost_sessions"][VIN_A]["superseded"] is True
    own.record_ev_command(h.hass, h.entry, VIN_A, command="start_manual", success=False)
    # Rollback restores the prior accepted lease, including its stable receipt.
    own.get_ev_ownerships(h.hass, h.entry)[VIN_A] = dict(prior) if copy_lease else prior
    if copy_lease:
        await own.persist_ev_runtime_state(h.hass, h.entry, h.store)
        assert not h.store.saved["ev_runtime_state"]["boost_sessions"][VIN_A].get("superseded")
    assert not h.timers[0].cancelled
    assert h.runtime["ev_boost_sessions"][VIN_A]["expires_at"] == response["stops_at"]
    h.now = datetime.fromisoformat(response["stops_at"])
    await h.due()
    assert h.stops() == [VIN_A] and h.runtime["ev_boost_sessions"] == {}


@pytest.mark.asyncio
async def test_real_unsent_manual_start_keeps_prior_boost_owner_timer_and_deadline():
    h = BoostHarness()
    response = await h.start(VIN_A)
    original_lease = h.owner(VIN_A)
    namespace = {
        "guard_ev_control": h.guard, "_start_dynamic_lock": asyncio.Lock(),
        "DEFAULT_VEHICLE_ID": "_default", "_ev_action_loadpoint_id": lambda p: p["vehicle_vin"],
    }
    async def target(*_args):
        return 5
    namespace["_manual_session_target_amps"] = target
    compile_node(source_node(ROOT / "automations/actions.py", "_action_start_ev_charging_dynamic"), namespace)
    manual = compile_node(
        StripImports().visit(source_node(ROOT / "automations/actions.py", "_start_manual_ev_charging")),
        namespace,
    )
    h.gate.blocked = lambda _vin: True
    assert await manual(h.hass, h.entry, {"vehicle_vin": VIN_A}) is False
    h.gate.blocked = lambda _vin: False
    assert h.owner(VIN_A) is original_lease and not h.timers[0].cancelled
    assert not h.runtime["ev_boost_sessions"][VIN_A].get("superseded")
    assert h.runtime["ev_boost_sessions"][VIN_A]["expires_at"] == response["stops_at"]
    h.now = datetime.fromisoformat(response["stops_at"])
    await h.due()
    assert h.stops() == [VIN_A]


@pytest.mark.asyncio
async def test_unknown_stop_result_retains_unknown_durable_obligation():
    h = BoostHarness()
    response = await h.start(VIN_A)
    h.unknown_stop.add(VIN_A)
    h.now = datetime.fromisoformat(response["stops_at"])
    await h.due()
    record = h.store.saved["ev_runtime_state"]["boost_sessions"][VIN_A]
    assert record["last_stop_result"] is None and record["status"] == "pending_stop"
    assert record["expires_at"] == response["stops_at"] and h.physical[VIN_A] is True


@pytest.mark.asyncio
async def test_reload_native_admission_denial_retains_obligation_without_lease_or_writes():
    h = BoostHarness()
    response = await h.start(VIN_A)
    restored = await h.reload(blocked=(VIN_A,))
    assert restored.calls == [] and restored.owner(VIN_A) is None
    assert restored.runtime["ev_boost_sessions"][VIN_A]["status"] == "pending_recovery"
    restored.gate.blocked = lambda _vin: False
    restored.now = datetime.fromisoformat(response["stops_at"])
    await restored.due()
    assert restored.stops() == [VIN_A]


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", ["runtime", "unloaded", "owner", "pairing"])
async def test_start_persistence_await_does_not_arm_or_compensate_a_changed_owner(changed):
    h = BoostHarness()
    entered, release = asyncio.Event(), asyncio.Event()
    h.store.pause = (entered, release)
    task = asyncio.create_task(h.start(VIN_A))
    await asyncio.wait_for(entered.wait(), timeout=1)
    if changed == "runtime":
        h.hass.data["power_sync"]["entry"] = dict(h.runtime)
    elif changed == "unloaded":
        h.hass.data["power_sync"].pop("entry")
    elif changed == "owner":
        own.claim_ev_ownership(h.hass, h.entry, VIN_A, owner_mode="manual")
    else:
        h.entry.options["tesla_ble_entity_prefix"] = "different_bridge"
    release.set()
    response = await asyncio.wait_for(task, timeout=1)
    assert response["success"] is False and "superseded" in response["error"]
    assert h.calls == [] and h.timers == []
    if changed == "unloaded":
        assert "entry" not in h.hass.data["power_sync"]


@pytest.mark.asyncio
async def test_malformed_saved_boost_is_retained_without_becoming_control_authority():
    h = BoostHarness()
    await h.start(VIN_A)
    h.runtime["ev_boost_sessions"][VIN_A].pop("expires_at")
    restored = await h.reload()
    assert restored.calls == [] and restored.owner(VIN_A) is None
    assert VIN_A in restored.runtime["ev_boost_sessions"]
    assert restored.runtime["ev_boost_recovery_status"] == "blocked_invalid_identity_or_deadline"


@pytest.mark.asyncio
async def test_provisional_takeover_rollback_after_expiry_check_keeps_original_authority():
    h = BoostHarness()
    response = await h.start(VIN_A)
    prior = h.owner(VIN_A)
    own.claim_ev_ownership(h.hass, h.entry, VIN_A, owner_mode="manual", command="start_manual")
    await h.timers[0].callback(h.now)
    assert h.stops() == []
    own.record_ev_command(h.hass, h.entry, VIN_A, command="start_manual", success=False)
    own.get_ev_ownerships(h.hass, h.entry)[VIN_A] = dict(prior)
    h.now = datetime.fromisoformat(response["stops_at"])
    await h.due()
    assert h.stops() == [VIN_A] and h.runtime["ev_boost_sessions"] == {}


@pytest.mark.asyncio
async def test_failed_first_vin_stop_does_not_lose_other_recovered_deadline():
    h = BoostHarness()
    first = await h.start(VIN_A)
    second = await h.start(VIN_B, 3)
    restored = await h.reload(at=h.now + timedelta(minutes=2), fail_stop=(VIN_A,))
    assert restored.stops() == [VIN_A]
    assert restored.runtime["ev_boost_sessions"][VIN_A]["expires_at"] == first["stops_at"]
    assert restored.runtime["ev_boost_timers"][VIN_B]["stops_at"].isoformat() == second["stops_at"]
    restored.now = datetime.fromisoformat(second["stops_at"])
    await restored.due()
    assert restored.stops() == [VIN_A, VIN_A, VIN_B]
    assert set(restored.runtime["ev_boost_sessions"]) == {VIN_A}
    assert restored.owner(VIN_B) is None and restored.physical[VIN_A] is True


@pytest.mark.asyncio
async def test_reload_cannot_borrow_other_vins_fresh_observation():
    h = BoostHarness()
    await h.start(VIN_A)
    h.observation_overrides[VIN_A] = {"vehicle_id": VIN_B}
    restored = await h.reload()
    assert restored.calls == [] and restored.owner(VIN_A) is None
    assert restored.runtime["ev_boost_sessions"][VIN_A]["status"] == "pending_fresh_observation"


@pytest.mark.asyncio
async def test_stale_telemetry_alone_does_not_clear_live_boost_before_its_deadline():
    h = BoostHarness()
    response = await h.start(VIN_A, 3)
    state, lease = h.states["entry"][VIN_A], h.owner(VIN_A)
    h.observation_overrides[VIN_A] = {"_charging_observed_at": h.now - timedelta(minutes=5)}
    await h.timers[0].callback(h.now)
    assert h.stops() == [] and h.owner(VIN_A) is lease
    assert h.states["entry"][VIN_A] is state and state["active"] is True
    assert h.runtime["ev_boost_timers"][VIN_A]["stops_at"].isoformat() == response["stops_at"]


@pytest.mark.asyncio
@pytest.mark.parametrize("unloaded", [False, True])
async def test_late_persistence_cannot_recreate_runtime_or_overwrite_an_old_store(unloaded):
    h = BoostHarness()
    await h.start(VIN_A)
    saved = deepcopy(h.store.saved)
    pending_save = own.persist_ev_runtime_state(h.hass, h.entry, h.store)
    if unloaded:
        h.hass.data["power_sync"].pop("entry")
    else:
        h.hass.data["power_sync"]["entry"] = {"automation_store": Store()}
    assert await pending_save is False
    assert h.store.saved == saved
    if unloaded:
        assert "entry" not in h.hass.data["power_sync"]


@pytest.mark.asyncio
async def test_cancelled_pre_dispatch_persistence_never_grants_stop_authority():
    h = BoostHarness()
    entered, release = asyncio.Event(), asyncio.Event()
    h.store.pause = (entered, release)
    task = asyncio.create_task(h.start(VIN_A))
    await asyncio.wait_for(entered.wait(), timeout=1)
    expiry = h.runtime["ev_boost_sessions"][VIN_A]["expires_at"]
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    record = h.runtime["ev_boost_sessions"][VIN_A]
    assert record["status"] == "pending_persistence" and record["dispatch_started"] is False
    assert record["expires_at"] == expiry and h.calls == []
    h.store.pause = None
    h.now += timedelta(seconds=30)
    await h.due()
    assert h.calls == [] and h.store.saved["ev_runtime_state"]["boost_sessions"] == {}


@pytest.mark.asyncio
async def test_unload_during_persistence_cancellation_keeps_obligation_without_rearming():
    h = BoostHarness()
    entered, release = asyncio.Event(), asyncio.Event()
    h.store.pause = (entered, release)
    task = asyncio.create_task(h.start(VIN_A))
    await asyncio.wait_for(entered.wait(), timeout=1)
    h.runtime["powerwall_local_dispatch_stopping"] = True
    h.unload_timers()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert h.timers == [] and VIN_A in h.runtime["ev_boost_sessions"]
    h.store.pause = None
    assert await own.persist_ev_runtime_state(h.hass, h.entry, h.store) is True
    assert VIN_A in h.store.saved["ev_runtime_state"]["boost_sessions"]


@pytest.mark.asyncio
async def test_legacy_boost_without_absolute_deadline_remains_blocked_without_guessing():
    h = BoostHarness()
    h.store._data = {"ev_runtime_state": {"active_ownership": {VIN_A: {
        "owner": "powersync", "owner_mode": "boost", "duration_minutes": 1,
        "started_at": h.now.isoformat(), "vehicle_id": VIN_A,
    }}}}
    recovered = own.restore_ev_runtime_state(h.hass, h.entry, h.store)
    await h.view.restore_boost_sessions(recovered["boost_sessions"])
    record = h.runtime["ev_boost_sessions"][VIN_A]
    assert record["expires_at"] is None and record["status"] == "blocked_legacy_recovery_identity"
    assert h.calls == [] and h.timers == [] and h.owner(VIN_A) is None
    assert await own.persist_ev_runtime_state(h.hass, h.entry, h.store) is True
    assert h.store.saved["ev_runtime_state"]["boost_sessions"][VIN_A] == record


@pytest.mark.asyncio
async def test_finite_intent_is_durable_before_soc_and_start_with_one_original_deadline():
    h = BoostHarness()
    seen = []
    actual_execute = h.execute

    async def inspect_dispatch(hass, entry, actions):
        for action in actions:
            saved = h.store.saved["ev_runtime_state"]["boost_sessions"][VIN_A]
            params = action["parameters"]
            assert saved["vehicle_id"] == VIN_A and saved["pairing"] == h.view._boost_pairing_identity()
            assert saved["dispatch_started"] is True and saved["start_in_flight"] is True
            assert params["vehicle_vin"] == VIN_A
            assert params["_boost_start_deadline"] == datetime.fromisoformat(saved["expires_at"]).timestamp()
            seen.append(action["action_type"])
        return await actual_execute(hass, entry, actions)

    # The production extracted functions share their globals, including execute_actions.
    h.view._start_finite_boost.__func__.__globals__["execute_actions"] = inspect_dispatch
    response = (await h.post(h.view, SimpleNamespace(json=lambda: asyncio.sleep(
        0, result={"vehicle_id": VIN_A, "duration_minutes": 1, "target_soc": 80})))).body
    assert response["success"] is True
    assert seen == ["set_ev_charge_limit", "start_ev_charging_dynamic"]
    assert response["stops_at"] == (datetime(2026, 10, 31, tzinfo=timezone.utc) + timedelta(minutes=1)).isoformat()


@pytest.mark.asyncio
@pytest.mark.parametrize("vehicles", [(), (VIN_A, VIN_B)])
async def test_anonymous_boost_requires_unique_read_only_vehicle_before_any_journal_or_action(vehicles):
    h = BoostHarness()
    h.physical = {vin: False for vin in vehicles}
    response = await h.start(None)
    assert response["success"] is False and "exact vehicle" in response["error"]
    assert h.calls == [] and h.store.saved == {} and not h.runtime.get("ev_boost_sessions")


@pytest.mark.asyncio
async def test_preintent_save_failure_refuses_every_actuator_and_does_not_replace_old_timer():
    h = BoostHarness()
    old = await h.start(VIN_A)
    receipt = h.runtime["ev_boost_sessions"][VIN_A]
    timer = h.runtime["ev_boost_timers"][VIN_A]
    lease = h.owner(VIN_A)
    h.calls.clear()
    h.store.fail = True
    response = (await h.post(h.view, SimpleNamespace(json=lambda: asyncio.sleep(
        0, result={"vehicle_id": VIN_A, "duration_minutes": 3, "target_soc": 80})))).body
    assert response["success"] is False and response["recovery_pending"] is True
    assert h.calls == [] and h.owner(VIN_A) is lease
    assert h.runtime["ev_boost_sessions"][VIN_A] is receipt
    assert receipt["expires_at"] == old["stops_at"] and receipt["pending_intent"]["dispatch_started"] is False
    assert h.runtime["ev_boost_timers"][VIN_A] is timer and not h.timers[0].cancelled
    h.store.fail = False
    restored = await h.reload(at=datetime.fromisoformat(old["stops_at"]))
    assert restored.stops() == [VIN_A]
    assert not restored.runtime["ev_boost_sessions"]


@pytest.mark.asyncio
async def test_cancelled_provider_await_retains_durable_exact_vin_and_second_vehicle_timer():
    h = BoostHarness()
    second = await h.start(VIN_B, 3)
    entered, release = asyncio.Event(), asyncio.Event()
    h.start_pause = entered, release
    task = asyncio.create_task(h.start(VIN_A))
    await asyncio.wait_for(entered.wait(), timeout=1)
    saved = deepcopy(h.store.saved["ev_runtime_state"]["boost_sessions"])
    assert set(saved) == {VIN_A, VIN_B} and saved[VIN_A]["start_in_flight"] is True
    deadline = saved[VIN_A]["expires_at"]
    # A provider can accept a command before cancellation returns to HA.
    h.physical[VIN_A] = True
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert h.runtime["ev_boost_sessions"][VIN_A]["unknown_start_outcome"] is True
    assert h.runtime["ev_boost_timers"][VIN_B]["stops_at"].isoformat() == second["stops_at"]
    restored = await h.reload(at=h.now + timedelta(seconds=10))
    assert restored.calls == []
    assert restored.runtime["ev_boost_sessions"][VIN_A]["expires_at"] == deadline
    assert restored.runtime["ev_boost_timers"][VIN_B]["stops_at"].isoformat() == second["stops_at"]
    restored.now = datetime.fromisoformat(deadline) + timedelta(seconds=1)
    await restored.due()
    assert restored.stops() == [VIN_A] and set(restored.runtime["ev_boost_sessions"]) == {VIN_B}


@pytest.mark.asyncio
async def test_unknown_inflight_restart_cannot_retire_on_stopped_sample_before_original_deadline():
    h = BoostHarness()
    entered, release = asyncio.Event(), asyncio.Event()
    h.start_pause = entered, release
    task = asyncio.create_task(h.start(VIN_A))
    await asyncio.wait_for(entered.wait(), timeout=1)
    disk_intent = deepcopy(h.store.saved["ev_runtime_state"]["boost_sessions"][VIN_A])
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    h.physical[VIN_A] = False
    restored = await h.reload(at=h.now + timedelta(seconds=10))
    record = restored.runtime["ev_boost_sessions"][VIN_A]
    assert record["generation"] == disk_intent["generation"]
    assert record["expires_at"] == disk_intent["expires_at"]
    assert restored.calls == [] and restored.owner(VIN_A) is None
    # Even a newer, exact-VIN stopped sample can precede an old queued start.
    restored.now += timedelta(seconds=30)
    await restored.due()
    assert VIN_A in restored.runtime["ev_boost_sessions"] and restored.calls == []
    restored.now = datetime.fromisoformat(record["expires_at"]) + timedelta(seconds=31)
    await restored.due()
    assert not restored.runtime["ev_boost_sessions"] and restored.calls == []


@pytest.mark.asyncio
async def test_ambiguous_failed_start_and_failed_stop_remain_durable_until_fresh_stopped_readback():
    h = BoostHarness()
    h.ambiguous_start.add(VIN_A)
    h.fail_stop.add(VIN_A)
    response = await h.start(VIN_A)
    assert response["success"] is False and response["recovery_pending"] is True
    deadline = response["stops_at"]
    restored = await h.reload(at=datetime.fromisoformat(deadline) + timedelta(seconds=1), fail_stop=(VIN_A,))
    assert restored.stops() == [VIN_A]
    record = restored.runtime["ev_boost_sessions"][VIN_A]
    assert record["last_start_result"] is False and record["last_stop_result"] is False
    assert record["expires_at"] == deadline and restored.physical[VIN_A] is True
    restored.fail_stop.clear()
    restored.now += timedelta(seconds=30)
    await restored.due()
    assert restored.stops() == [VIN_A, VIN_A] and not restored.runtime["ev_boost_sessions"]


@pytest.mark.asyncio
async def test_cancelled_replacement_preserves_prior_receipt_timer_and_original_stop_across_reload():
    h = BoostHarness()
    old = await h.start(VIN_A)
    prior = h.runtime["ev_boost_sessions"][VIN_A]
    timer, lease = h.runtime["ev_boost_timers"][VIN_A], h.owner(VIN_A)
    h.now += timedelta(seconds=10)
    entered, release = asyncio.Event(), asyncio.Event()
    h.start_pause = entered, release
    task = asyncio.create_task(h.start(VIN_A, 3))
    await asyncio.wait_for(entered.wait(), timeout=1)
    disk = h.store.saved["ev_runtime_state"]["boost_sessions"][VIN_A]
    assert disk["generation"] == prior["generation"] and disk["expires_at"] == old["stops_at"]
    assert disk["pending_intent"]["start_in_flight"] is True
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert h.runtime["ev_boost_timers"][VIN_A] is timer and h.owner(VIN_A) is lease
    assert h.runtime["ev_boost_sessions"][VIN_A] is prior and not h.timers[0].cancelled
    restored = await h.reload(at=datetime.fromisoformat(old["stops_at"]))
    # Original successful Boost still stops at its original deadline. Unknown
    # later submission evidence remains until after that request's own deadline.
    assert restored.stops() == [VIN_A]
    record = restored.runtime["ev_boost_sessions"][VIN_A]
    assert record["expires_at"] == old["stops_at"] and record["pending_intent"]["start_in_flight"] is True
    restored.now = datetime.fromisoformat(record["pending_intent"]["expires_at"]) + timedelta(seconds=31)
    restored.observation_overrides.clear()
    await restored.due()
    assert restored.stops() == [VIN_A] and not restored.runtime["ev_boost_sessions"]


@pytest.mark.asyncio
async def test_cancelled_unsent_replacement_does_not_supersede_prior_deadline_after_reload():
    h = BoostHarness()
    old = await h.start(VIN_A)
    prior_generation = h.runtime["ev_boost_sessions"][VIN_A]["generation"]
    entered, release = asyncio.Event(), asyncio.Event()
    h.store.pause = entered, release
    task = asyncio.create_task(h.start(VIN_A, 3))
    await asyncio.wait_for(entered.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert h.runtime["ev_boost_sessions"][VIN_A]["pending_intent"]["dispatch_started"] is False
    assert len(h.calls) == 1 and not h.timers[0].cancelled
    h.store.pause = None
    restored = await h.reload(at=h.now + timedelta(seconds=10))
    record = restored.runtime["ev_boost_sessions"][VIN_A]
    assert record["generation"] == prior_generation and record["expires_at"] == old["stops_at"]
    assert "pending_intent" not in record and restored.calls == []
    restored.now = datetime.fromisoformat(old["stops_at"])
    await restored.due()
    assert restored.stops() == [VIN_A]


@pytest.mark.asyncio
async def test_boost_expired_waiting_for_existing_ev_gate_never_saves_or_dispatches():
    h = BoostHarness()
    await h.gate.lock.acquire()
    task = asyncio.create_task(h.start(VIN_A))
    await asyncio.wait_for(h.gate_requested.wait(), timeout=1)
    h.now += timedelta(minutes=2)
    h.gate.lock.release()
    response = await asyncio.wait_for(task, timeout=1)
    assert response["success"] is False and "expired" in response["error"]
    assert h.calls == [] and h.store.saved == {} and not h.runtime.get("ev_boost_sessions")


@pytest.mark.asyncio
async def test_provider_reply_after_original_deadline_cannot_report_success_or_extend_window():
    h = BoostHarness()
    requested_at = h.now
    entered, release = asyncio.Event(), asyncio.Event()
    h.start_pause = entered, release
    task = asyncio.create_task(h.start(VIN_A))
    await asyncio.wait_for(entered.wait(), timeout=1)
    h.now += timedelta(minutes=2)
    release.set()
    response = await asyncio.wait_for(task, timeout=1)
    assert response["success"] is False and response["recovery_pending"] is True
    assert response["stops_at"] == (requested_at + timedelta(minutes=1)).isoformat()
    assert h.stops() == [VIN_A] and h.physical[VIN_A] is False


@pytest.mark.asyncio
async def test_cancelled_acknowledgement_save_after_successful_replacement_arms_promoted_obligation():
    h = BoostHarness()
    old = await h.start(VIN_A)
    old_timer = h.timers[0]
    entered, release = asyncio.Event(), asyncio.Event()
    h.store.pause, h.store.pause_after = (entered, release), h.store.saves + 3
    task = asyncio.create_task(h.start(VIN_A, 3))
    await asyncio.wait_for(entered.wait(), timeout=1)
    record = h.runtime["ev_boost_sessions"][VIN_A]
    assert record["expires_at"] != old["stops_at"] and h.physical[VIN_A] is True
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert old_timer.cancelled
    timer = h.runtime["ev_boost_timers"][VIN_A]
    assert timer["record"] is record and record["unknown_start_outcome"] is True
    assert timer["stops_at"].isoformat() == record["expires_at"]
    h.store.pause = None
    h.now = datetime.fromisoformat(record["expires_at"]) + timedelta(seconds=1)
    await h.due()
    assert h.stops() == [VIN_A] and not h.runtime["ev_boost_sessions"]


@pytest.mark.asyncio
async def test_preintent_storage_wait_cannot_extend_deadline_or_submit_expired_start():
    h = BoostHarness()
    requested_at = h.now
    entered, release = asyncio.Event(), asyncio.Event()
    h.store.pause = entered, release
    task = asyncio.create_task(h.start(VIN_A))
    await asyncio.wait_for(entered.wait(), timeout=1)
    h.now += timedelta(minutes=2)
    release.set()
    response = await asyncio.wait_for(task, timeout=1)
    assert response["success"] is False and "expired" in response["error"]
    record = h.runtime["ev_boost_sessions"][VIN_A]
    assert record["expires_at"] == (requested_at + timedelta(minutes=1)).isoformat()
    assert record["dispatch_started"] is False and h.calls == []
    h.store.pause = None
    await h.due()
    assert h.calls == [] and not h.runtime["ev_boost_sessions"]


@pytest.mark.parametrize("key,value", [("expires_at", None), ("attempted_at", None),
                                        ("expires_at", "2026-10-31T00:01:00")])
def test_unknown_start_cannot_weaken_readback_fence_with_malformed_required_timestamp(key, value):
    record = {"dispatch_started": True, "start_in_flight": True,
              "attempted_at": "2026-10-31T00:00:00+00:00",
              "expires_at": "2026-10-31T00:01:00+00:00"}
    record[key] = value
    with pytest.raises(ValueError):
        own.boost_recovery_observation_after(record)


@pytest.mark.asyncio
async def test_pairing_mapping_mutation_during_preintent_save_cannot_change_journal_identity_or_dispatch():
    h = BoostHarness()
    h.entry.options["tesla_ble_vehicle_mapping"] = {"first_bridge": VIN_A}
    entered, release = asyncio.Event(), asyncio.Event()
    h.store.pause = entered, release
    task = asyncio.create_task(h.start(VIN_A))
    await asyncio.wait_for(entered.wait(), timeout=1)
    record = h.runtime["ev_boost_sessions"][VIN_A]
    h.entry.options["tesla_ble_vehicle_mapping"]["first_bridge"] = VIN_B
    release.set()
    response = await asyncio.wait_for(task, timeout=1)
    assert response["success"] is False and "superseded" in response["error"]
    assert record["pairing"]["tesla_ble_vehicle_mapping"] == {"first_bridge": VIN_A}
    assert h.calls == [] and h.timers == []


@pytest.mark.asyncio
async def test_restart_from_pre_reply_disk_snapshot_recovers_crashed_start_without_replay():
    h = BoostHarness()
    entered, release = asyncio.Event(), asyncio.Event()
    h.start_pause = entered, release
    task = asyncio.create_task(h.start(VIN_A))
    await asyncio.wait_for(entered.wait(), timeout=1)
    disk = deepcopy(h.store.saved)
    intent = disk["ev_runtime_state"]["boost_sessions"][VIN_A]
    assert intent["start_in_flight"] is True and intent["dispatch_started"] is True
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    h.unload_timers()
    # Restore the actual pre-reply disk bytes, without any graceful-unload or
    # cancellation-handler save. This is the process-crash recovery boundary.
    restored = BoostHarness()
    restored.store._data = disk
    restored.now += timedelta(seconds=10)
    restored.physical[VIN_A] = False
    recovered = own.restore_ev_runtime_state(restored.hass, restored.entry, restored.store)
    await restored.view.restore_boost_sessions(recovered["boost_sessions"])
    record = restored.runtime["ev_boost_sessions"][VIN_A]
    assert record["generation"] == intent["generation"] and record["expires_at"] == intent["expires_at"]
    assert restored.calls == [] and restored.owner(VIN_A) is None
    # A previously handed-off start can appear later. Recovery issues only an
    # exact-VIN Stop, after the original finite deadline, and never a start/rate.
    restored.physical[VIN_A] = True
    restored.now = datetime.fromisoformat(intent["expires_at"]) + timedelta(seconds=31)
    await restored.due()
    assert restored.stops() == [VIN_A] and not restored.runtime["ev_boost_sessions"]
