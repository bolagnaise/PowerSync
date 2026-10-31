"""Exercise the real Boost handler and EV gate with fake charger I/O/timers."""

from __future__ import annotations

import ast
import asyncio
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import wraps
from inspect import signature
import logging
from pathlib import Path
from types import SimpleNamespace

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
        self.runtime = {}
        self.hass = SimpleNamespace(data={"power_sync": {"entry": self.runtime}})
        self.view = SimpleNamespace(_hass=self.hass, _config_entry=self.entry)
        self.now = datetime(2026, 10, 31, tzinfo=timezone.utc)
        self.states = {"entry": {}}
        self.timers = []
        self.calls = []
        self.fail_start = set()
        self.fail_stop = set()
        self.default_vin = VIN_A
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
            "_LOGGER": logging.getLogger(__name__),
            "web": SimpleNamespace(
                json_response=lambda body, **kwargs: SimpleNamespace(body=body, **kwargs)
            ),
        }
        for name in (
            "claim_ev_ownership", "get_active_ev_owner_mode", "get_ev_ownership",
            "owner_family", "record_ev_command",
        ):
            namespace[name] = getattr(own, name)
        self.post = compile_node(
            StripImports().visit(source_node(INIT, "post", parent="ChargingBoostView")),
            namespace,
        )

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
            if self.start_pause:
                entered, release = self.start_pause
                entered.set()
                await release.wait()
            if vin in self.fail_start:
                return False
            # A duplicate start can reuse the actual controller's state object.
            state = self.states["entry"].setdefault(vin, {})
            state.update(active=True, current_amps=6, params=dict(params))
            state["ownership"] = own.claim_ev_ownership(
                hass, config_entry, vin, owner_mode="boost"
            )
            return True
        if kind == "stop_ev_charging_dynamic":
            if vin in self.fail_stop:
                return False
            expected = params.get("_expected_state")
            if expected is not None and self.states["entry"].get(vin) is not expected:
                return True
            self.states["entry"].pop(vin, None)
            own.release_ev_ownership(hass, config_entry, vin)
            return True
        raise AssertionError(f"Unexpected charger action: {kind}")

    async def execute(self, hass, config_entry, actions):
        results = []
        for action in actions:
            results.append(await self.apply(
                hass, config_entry,
                {**action["parameters"], "_kind": action["action_type"]},
            ))
        return any(results)

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
    assert response["success"] is False and response["stops_at"] is None
    assert len(h.timers) == 1 and not h.timers[0].cancelled
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
