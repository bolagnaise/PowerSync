"""Focused durability and fail-closed tests for Tesla transport recovery."""
from __future__ import annotations

import asyncio
import json
from contextlib import suppress
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
import sys
import time
import types

import pytest


ROOT = Path(__file__).resolve().parents[1] / "custom_components" / "power_sync"
package = sys.modules.setdefault("power_sync", types.ModuleType("power_sync"))
package.__path__ = [str(ROOT)]

from power_sync import tesla_ev_recovery as recovery  # noqa: E402


VIN_A = "5YJ3E1EA7NF0000A1"
VIN_B = "5YJ3E1EA7NF0000B2"


def _fence_value(*, generation=1, isolated=False, **changes):
    return json.dumps({"protocol": 1, "boot_id": "boot-1", "generation": generation,
        "request_id": "",
        "radio_enabled": not isolated, "connected": not isolated, "producer_stopped": isolated,
        "queued_commands": 0 if isolated else 1, "queued_reads": 0,
        "queued_writes": 0, **changes})


class _State:
    def __init__(self, entity_id, value, stamp):
        self.entity_id, self.state = entity_id, value
        self.last_reported = stamp
        self.last_updated = stamp


class _Entity:
    def __init__(self, entity_id, *, device="ble-device", entry="ble-entry", platform="esphome"):
        self.entity_id = entity_id
        self.platform = platform
        self.config_entry_id = entry
        self.device_id = device
        self.unique_id = entity_id
        self.disabled_by = None


class _Services:
    def __init__(self, hass):
        self.hass, self.calls = hass, []
        self.update_states = False

    async def async_call(self, domain, service, data, *, blocking):
        self.calls.append((domain, service, data, blocking))
        if self.update_states:
            # Deliver the provider receipt on the next event-loop turn.  The
            # production isolation check must reject same-timestamp stale
            # observations rather than depending on a synchronous fake.
            await asyncio.sleep(0.001)
            stamp = datetime.now(timezone.utc)
            if domain == "text":
                prefix = data["entity_id"].removeprefix("text.").removesuffix("_transport_isolation_request")
            else:
                prefix = data["entity_id"].removeprefix("switch.").removesuffix("_ble_connection")
            for entity_id in (f"switch.{prefix}_ble_connection", f"binary_sensor.{prefix}_ble_status"):
                self.hass.states_by_id[entity_id] = _State(entity_id, "off", stamp)
            fence_id = f"sensor.{prefix}_transport_fence"
            if self.hass.entity_registry.async_get(fence_id) is not None:
                prior = self.hass.states_by_id.get(fence_id)
                generation = json.loads(prior.state)["generation"] if prior else 0
                self.hass.states_by_id[fence_id] = _State(
                    fence_id, _fence_value(generation=generation + 1, isolated=True,
                        request_id=data.get("value", "")), stamp)


class _Hass:
    __slots__ = ("data", "states_by_id", "states", "entity_registry", "services", "persist_ok", "persisted", "config_entries")

    def __init__(self, registrations):
        self.data = {"power_sync": {"entry-1": {"automation_store": object()}}}
        self.states_by_id = {}
        for prefix in ("bridge_a", "bridge_b"):
            entity_id = f"sensor.{prefix}_transport_fence"
            if entity_id in registrations:
                self.states_by_id[entity_id] = _State(
                    entity_id, _fence_value(), datetime.now(timezone.utc))
        self.states = types.SimpleNamespace(get=self.states_by_id.get)
        self.entity_registry = types.SimpleNamespace(async_get=registrations.get)
        self.persist_ok = True
        self.persisted = []
        self.services = _Services(self)
        self.config_entries = None


class _Entry:
    entry_id = "entry-1"
    data = {"profile": "ev"}
    options = {}


@pytest.fixture
def harness(monkeypatch):
    registrations = {
        "switch.car_a_charger": _Entity("switch.car_a_charger"),
        "switch.car_b_charger": _Entity("switch.car_b_charger", device="ble-b", entry="ble-entry-b"),
        "switch.bridge_a_ble_connection": _Entity("switch.bridge_a_ble_connection"),
        "binary_sensor.bridge_a_ble_status": _Entity("binary_sensor.bridge_a_ble_status"),
        "sensor.bridge_a_transport_fence": _Entity("sensor.bridge_a_transport_fence"),
        "text.bridge_a_transport_isolation_request": _Entity("text.bridge_a_transport_isolation_request"),
        "switch.bridge_b_ble_connection": _Entity("switch.bridge_b_ble_connection", device="ble-b", entry="ble-entry-b"),
        "binary_sensor.bridge_b_ble_status": _Entity("binary_sensor.bridge_b_ble_status", device="ble-b", entry="ble-entry-b"),
        "sensor.bridge_b_transport_fence": _Entity("sensor.bridge_b_transport_fence", device="ble-b", entry="ble-entry-b"),
        "text.bridge_b_transport_isolation_request": _Entity("text.bridge_b_transport_isolation_request", device="ble-b", entry="ble-entry-b"),
        "switch.fallback": _Entity("switch.fallback", device="native-device", entry="native-entry", platform="teslemetry"),
        "switch.fallback_b": _Entity("switch.fallback_b", device="native-device-b", entry="native-entry-b", platform="teslemetry"),
    }
    hass = _Hass(registrations)
    owners = {VIN_A: {"owner": "a", "owner_mode": "manual", "session_id": "sa", "ownership_generation": 1},
              VIN_B: {"owner": "b", "owner_mode": "manual", "session_id": "sb", "ownership_generation": 1}}
    fake = types.ModuleType("power_sync.automations.ev_ownership")

    def get_owner(_hass, _entry, vehicle_id):
        lease = owners.get(vehicle_id, {})
        return lease.get("session_id"), lease

    async def persist(_hass, _entry, _store):
        if not hass.persist_ok:
            return False
        hass.persisted.append(deepcopy(hass.data["power_sync"]["entry-1"].get("ev_transport_recovery")))
        return True

    fake.get_ev_ownership = get_owner
    fake.persist_ev_runtime_state = persist
    automations = sys.modules.setdefault("power_sync.automations", types.ModuleType("power_sync.automations"))
    automations.__path__ = [str(ROOT / "automations")]
    monkeypatch.setitem(sys.modules, "power_sync.automations.ev_ownership", fake)
    # Other standalone tests replace the HA package and its lazy modules while
    # workers are collecting the full suite.  Bind both the module table and
    # the package attribute so relative imports resolve this fixture's fake.
    monkeypatch.setattr(automations, "ev_ownership", fake, raising=False)
    monkeypatch.setattr(recovery, "_registry", lambda _hass: hass.entity_registry)
    entry = _Entry()
    entry.data = {"profile": "ev"}
    entry.options = {}
    return hass, entry, registrations, owners


def _route(prefix, entity_id, fallback):
    return {"kind": "ble", "prefix": prefix, "fallback": {"kind": "ha", "entity_id": fallback}, "entity_id": entity_id}


def _old_bridge_states(hass, prefix="a"):
    stamp = datetime.fromtimestamp(1, timezone.utc)
    for entity_id in (f"switch.bridge_{prefix}_ble_connection", f"binary_sensor.bridge_{prefix}_ble_status"):
        hass.states_by_id[entity_id] = _State(entity_id, "off", stamp)


@pytest.mark.asyncio
async def test_intent_persist_failure_does_not_leave_recovery(harness):
    hass, entry, _, _ = harness
    hass.persist_ok = False
    ticket = await recovery.async_begin_attempt(hass, entry, VIN_A, _route("bridge_a", "switch.car_a_charger", "switch.fallback"), "start", "switch.car_a_charger")
    assert ticket is None
    assert "ev_transport_recovery" not in hass.data["power_sync"][entry.entry_id]


@pytest.mark.asyncio
async def test_store_failure_allows_typed_best_effort_stop_for_durable_hazard(harness):
    hass, entry, store, _, _ = _sidecar_harness(harness)
    assert await recovery.async_load_recovery_state(hass, entry)
    route = _route("bridge_a", "switch.car_a_charger", "switch.fallback")
    start = await recovery.async_begin_attempt(
        hass, entry, VIN_A, route, "start", route["entity_id"],
    )
    assert start is not None
    assert await recovery.async_finish_attempt(start, "sent_unknown")
    durable_before = deepcopy(store.value)
    store.fail = True

    with pytest.raises(recovery.RecoveryPersistenceRefused) as error:
        await recovery.async_begin_attempt(
            hass,
            entry,
            VIN_A,
            route,
            "stop",
            route["entity_id"],
            allow_persist_refused_cleanup=True,
        )
    assert error.value.ev_recovery_persistence_refused is True
    assert error.value.ev_recovery_best_effort_cleanup is True
    assert error.value.vehicle_id == VIN_A
    # The failed Stop did not erase or downgrade the earlier unresolved
    # producer; its durable record remains the cleanup authority.
    assert store.value == durable_before
    assert await recovery.async_begin_attempt(
        hass,
        entry,
        VIN_A,
        route,
        "stop",
        route["entity_id"],
        allow_persist_refused_cleanup=True,
    ) is None


@pytest.mark.asyncio
async def test_best_effort_cleanup_tracker_claims_until_cancelled_child_drains(harness):
    hass, entry, store, _, _ = _sidecar_harness(harness)
    assert await recovery.async_load_recovery_state(hass, entry)
    route = _route("bridge_a", "switch.car_a_charger", "switch.fallback")
    start = await recovery.async_begin_attempt(
        hass, entry, VIN_A, route, "start", route["entity_id"],
    )
    assert start is not None
    assert await recovery.async_finish_attempt(start, "sent_unknown")
    store.fail = True

    with pytest.raises(recovery.RecoveryPersistenceRefused) as refused:
        await recovery.async_begin_attempt(
            hass, entry, VIN_A, route, "stop", route["entity_id"],
            allow_persist_refused_cleanup=True,
        )
    refusal = refused.value
    assert refusal.route["cleanup_scope_valid"] is True
    assert refusal.route["cleanup_obligation_id"] is None
    generation = recovery.current_vehicle_generation(hass, entry, VIN_A)
    started = asyncio.Event()
    release = asyncio.Event()

    async def _submit_stop():
        started.set()
        await release.wait()
        return "accepted"

    first = asyncio.create_task(recovery.async_track_cleanup_submission(
        hass, entry, refusal, _submit_stop,
        vehicle_id=VIN_A, generation=generation,
    ))
    await asyncio.wait_for(started.wait(), timeout=1)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first

    # The child remains in the shared submission registry, so a second Stop
    # cannot consume the same durable cleanup scope while it is still live.
    assert not await recovery.async_drain_submissions(
        hass, entry, vehicle_ids=[VIN_A], timeout=0.01,
    )
    assert await recovery.async_begin_attempt(
        hass, entry, VIN_A, route, "stop", route["entity_id"],
        allow_persist_refused_cleanup=True,
    ) is None

    release.set()
    assert await asyncio.wait_for(
        recovery.async_drain_submissions(hass, entry, vehicle_ids=[VIN_A]),
        timeout=1,
    )

    # Once the real provider child has finished, a fresh Stop may retry the
    # same durable scope.  The callback below completes synchronously and the
    # in-memory claim is released by the shared tracker.
    with pytest.raises(recovery.RecoveryPersistenceRefused) as retried:
        await recovery.async_begin_attempt(
            hass, entry, VIN_A, route, "stop", route["entity_id"],
            allow_persist_refused_cleanup=True,
        )
    assert await recovery.async_track_cleanup_submission(
        hass, entry, retried.value, lambda: "accepted",
        vehicle_id=VIN_A, generation=generation,
    ) == "accepted"


@pytest.mark.asyncio
async def test_best_effort_stop_requires_exact_durable_obligation_and_binding(harness):
    hass, entry, store, registrations, _ = _sidecar_harness(harness)
    assert await recovery.async_load_recovery_state(hass, entry)
    runtime = hass.data["power_sync"][entry.entry_id]
    native_entity = "switch.fallback"
    native_identity = recovery._identity(hass, native_entity)
    state = runtime["_tesla_ev_recovery_sidecar"]
    state["obligations"]["ev_quick_control_obligations"] = {
        VIN_A: {
            "vehicle_id": VIN_A,
            "obligation_id": "cleanup-token-1",
            "params": {
                "_tesla_command_entity_id": native_entity,
                "_tesla_native_command_identity": deepcopy(native_identity),
            },
        }
    }
    store.value = deepcopy(state)
    store.fail = True
    native_route = {"kind": "ha", "entity_id": native_entity}

    # A wrong token cannot turn the current entity into cleanup authority.
    assert await recovery.async_begin_attempt(
        hass,
        entry,
        VIN_A,
        native_route,
        "stop",
        native_entity,
        allow_persist_refused_cleanup=True,
        cleanup_obligation_token="wrong-token",
    ) is None

    with pytest.raises(recovery.RecoveryPersistenceRefused):
        await recovery.async_begin_attempt(
            hass,
            entry,
            VIN_A,
            native_route,
            "stop",
            native_entity,
            allow_persist_refused_cleanup=True,
            cleanup_obligation_token="cleanup-token-1",
        )

    registrations["switch.rebound"] = _Entity(
        "switch.rebound", device="replacement-device", entry="replacement-entry",
        platform="teslemetry",
    )
    assert await recovery.async_begin_attempt(
        hass,
        entry,
        VIN_A,
        {"kind": "ha", "entity_id": "switch.rebound"},
        "stop",
        "switch.rebound",
        allow_persist_refused_cleanup=True,
        cleanup_obligation_token="cleanup-token-1",
    ) is None

    # The cleanup exception cannot be repurposed as a positive-operation
    # persistence bypass.
    assert await recovery.async_begin_attempt(
        hass,
        entry,
        VIN_A,
        native_route,
        "rate",
        native_entity,
        allow_persist_refused_cleanup=True,
        cleanup_obligation_token="cleanup-token-1",
    ) is None


@pytest.mark.asyncio
async def test_best_effort_stop_matches_native_rate_by_durable_session_anchor(harness):
    hass, entry, store, registrations, _ = _sidecar_harness(harness)
    registrations["number.fallback_current"] = _Entity(
        "number.fallback_current", device="native-device", entry="native-entry",
        platform="teslemetry",
    )
    assert await recovery.async_load_recovery_state(hass, entry)
    start_route = {"kind": "ha", "entity_id": "switch.fallback"}
    start = await recovery.async_begin_attempt(
        hass, entry, VIN_A, start_route, "start", "switch.fallback",
    )
    assert start is not None
    assert await recovery.async_finish_attempt(start, "accepted_unconfirmed")
    rate = await recovery.async_begin_attempt(
        hass, entry, VIN_A,
        {"kind": "ha", "entity_id": "number.fallback_current"},
        "rate", "number.fallback_current",
    )
    assert rate is not None
    assert await recovery.async_finish_attempt(rate, "sent_unknown")
    store.fail = True

    with pytest.raises(recovery.RecoveryPersistenceRefused):
        await recovery.async_begin_attempt(
            hass, entry, VIN_A, start_route, "stop", "switch.fallback",
            allow_persist_refused_cleanup=True,
        )

@pytest.mark.asyncio
async def test_pending_reload_is_read_only_and_blocks_route(harness):
    hass, entry, _, _ = harness
    route = _route("bridge_a", "switch.car_a_charger", "switch.fallback")
    ticket = await recovery.async_begin_attempt(hass, entry, VIN_A, route, "start", route["entity_id"])
    assert ticket is not None
    _old_bridge_states(hass)
    restored = _Hass({})
    restored.data["power_sync"][entry.entry_id] = deepcopy(hass.data["power_sync"][entry.entry_id])
    restored.entity_registry = hass.entity_registry
    restored.states_by_id = hass.states_by_id
    restored.states = types.SimpleNamespace(get=restored.states_by_id.get)
    assert recovery.pending_ble_attempt(restored, entry, VIN_A) is not None
    assert recovery.get_vehicle_route(restored, entry, VIN_A)["kind"] == "blocked"
    assert not restored.services.calls


@pytest.mark.asyncio
async def test_exact_attempt_finish_preserves_inherited_hazard(harness):
    hass, entry, _, _ = harness
    route_a = _route("bridge_a", "switch.car_a_charger", "switch.fallback")
    route_b = _route("bridge_b", "switch.car_b_charger", "switch.fallback_b")
    ticket_a = await recovery.async_begin_attempt(hass, entry, VIN_A, route_a, "start", route_a["entity_id"])
    ticket_b = await recovery.async_begin_attempt(hass, entry, VIN_B, route_b, "stop", route_b["entity_id"])
    assert ticket_a and ticket_b
    await recovery.async_finish_attempt(ticket_a, "sent_unknown")
    await recovery.async_finish_attempt(ticket_b, "confirmed")
    ledgers = hass.data["power_sync"][entry.entry_id]["ev_transport_recovery"]["vehicles"]
    assert ticket_a.attempt_id in ledgers[VIN_A]["attempts"]
    assert not ledgers[VIN_B]["attempts"]
    assert recovery.ble_quarantined(hass, "bridge_a")
    assert not recovery.ble_quarantined(hass, "bridge_b")


@pytest.mark.asyncio
async def test_new_intent_cannot_erase_unknown_ble_hazard(harness):
    hass, entry, _, _ = harness
    route = _route("bridge_a", "switch.car_a_charger", "switch.fallback")
    ticket = await recovery.async_begin_attempt(hass, entry, VIN_A, route, "start", route["entity_id"])
    assert ticket is not None
    await recovery.async_finish_attempt(ticket, "sent_unknown")
    replacement = {"kind": "teslemetry_bluetooth"}
    assert await recovery.async_begin_attempt(
        hass, entry, VIN_A, replacement, "start", "switch.fallback"
    ) is None
    assert recovery.pending_ble_attempt(hass, entry, VIN_A) is not None


@pytest.mark.asyncio
async def test_modern_unknown_adoption_keeps_producer_fence_across_stop_reload(harness):
    hass, entry, registrations, _ = harness
    modern_start = "button.modern_start"
    modern_stop = "button.modern_stop"
    modern_rate = "number.modern_rate"
    for entity_id, domain in ((modern_start, "teslemetry_bluetooth"),
                              (modern_stop, "teslemetry_bluetooth"),
                              (modern_rate, "teslemetry_bluetooth")):
        registrations[entity_id] = _Entity(
            entity_id, device="teslemetry-device", entry="commands-entry", platform=domain
        )
    start_route = {"kind": "teslemetry_bluetooth", "entity_id": modern_start}
    stop_route = {"kind": "teslemetry_bluetooth", "entity_id": modern_stop}
    rate_route = {"kind": "teslemetry_bluetooth", "entity_id": modern_rate}

    start = await recovery.async_begin_attempt(
        hass, entry, VIN_A, start_route, "start", modern_start
    )
    assert start is not None
    await recovery.async_finish_attempt(start, "sent_unknown")
    # A fresh native Charging readback may be adopted by the action layer, but
    # it must not erase the command-only producer hazard.
    assert recovery.get_vehicle_route(hass, entry, VIN_A)["kind"] == "blocked"

    stop = await recovery.async_begin_attempt(
        hass, entry, VIN_A, stop_route, "stop", modern_stop
    )
    assert stop is not None
    await recovery.async_finish_attempt(stop, "confirmed")
    assert recovery.get_vehicle_route(hass, entry, VIN_A)["kind"] == "blocked"
    assert await recovery.async_begin_attempt(
        hass, entry, VIN_A, start_route, "start", modern_start
    ) is None
    assert await recovery.async_begin_attempt(
        hass, entry, VIN_A, rate_route, "rate", modern_rate
    ) is None

    restored = _Hass({})
    restored.data["power_sync"][entry.entry_id] = deepcopy(
        hass.data["power_sync"][entry.entry_id]
    )
    restored.entity_registry = hass.entity_registry
    assert recovery.get_vehicle_route(restored, entry, VIN_A)["kind"] == "blocked"


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ("accepted_unconfirmed", "rejected"))
async def test_completed_or_rejected_modern_attempt_does_not_block_next_start(harness, outcome):
    hass, entry, registrations, _ = harness
    entity_id = "button.modern_completed"
    registrations[entity_id] = _Entity(
        entity_id, device="teslemetry-device", entry="commands-entry",
        platform="teslemetry_bluetooth"
    )
    route = {"kind": "teslemetry_bluetooth", "entity_id": entity_id}
    ticket = await recovery.async_begin_attempt(
        hass, entry, VIN_A, route, "start", entity_id
    )
    assert ticket is not None
    assert await recovery.async_finish_attempt(ticket, outcome)
    retry = await recovery.async_begin_attempt(
        hass, entry, VIN_A, route, "start", entity_id
    )
    assert retry is not None


@pytest.mark.asyncio
async def test_stop_intent_remains_available_under_unknown_start(harness):
    hass, entry, _, _ = harness
    route = _route("bridge_a", "switch.car_a_charger", "switch.fallback")
    start = await recovery.async_begin_attempt(
        hass, entry, VIN_A, route, "start", route["entity_id"]
    )
    assert start is not None
    await recovery.async_finish_attempt(start, "sent_unknown")
    stop = await recovery.async_begin_attempt(
        hass, entry, VIN_A, route, "stop", route["entity_id"]
    )
    assert stop is not None
    attempts = hass.data["power_sync"][entry.entry_id][recovery.RECOVERY_KEY]["vehicles"][VIN_A]["attempts"]
    assert start.attempt_id in attempts
    assert stop.attempt_id in attempts


@pytest.mark.asyncio
async def test_owner_or_source_change_blocks_recovery(harness):
    hass, entry, registrations, owners = harness
    route = _route("bridge_a", "switch.car_a_charger", "switch.fallback")
    ticket = await recovery.async_begin_attempt(hass, entry, VIN_A, route, "start", route["entity_id"])
    assert ticket is not None
    owners[VIN_A]["ownership_generation"] = 2
    # Ownership is diagnostic evidence only.  A newly authorized caller may
    # still isolate the exact durable BLE binding after a lease replacement.
    assert recovery.pending_ble_attempt(hass, entry, VIN_A) is not None
    owners[VIN_A]["ownership_generation"] = 1
    registrations[route["entity_id"]].device_id = "different-device"
    assert recovery.pending_ble_attempt(hass, entry, VIN_A) is None
    assert recovery.get_vehicle_route(hass, entry, VIN_A)["kind"] == "blocked"


@pytest.mark.asyncio
async def test_wrong_device_and_stale_off_fail_closed_without_start(harness, monkeypatch):
    hass, entry, registrations, _ = harness
    route = _route("bridge_a", "switch.car_a_charger", "switch.fallback")
    ticket = await recovery.async_begin_attempt(hass, entry, VIN_A, route, "start", route["entity_id"])
    assert ticket is not None
    await recovery.async_finish_attempt(ticket, "sent_unknown")
    registrations["switch.bridge_a_ble_connection"].device_id = "other-device"
    assert not await recovery.async_isolate_ble(ticket, authorize=True)
    registrations["switch.bridge_a_ble_connection"].device_id = "ble-device"
    _old_bridge_states(hass)
    hass.services.update_states = False
    monkeypatch.setattr(recovery, "BLE_ISOLATION_TIMEOUT", 0.01)
    assert not await recovery.async_isolate_ble(ticket, authorize=True)
    assert hass.services.calls[0] == ("switch", "turn_off", {"entity_id": "switch.bridge_a_ble_connection"}, True)
    assert len(hass.services.calls) == 2
    assert hass.services.calls[1][:2] == ("text", "set_value")
    assert recovery.get_vehicle_route(hass, entry, VIN_A)["kind"] == "blocked"


@pytest.mark.asyncio
async def test_fresh_off_proof_pins_ha_and_never_starts_vehicle(harness, monkeypatch):
    hass, entry, _, _ = harness
    route = _route("bridge_a", "switch.car_a_charger", "switch.fallback")
    ticket = await recovery.async_begin_attempt(hass, entry, VIN_A, route, "start", route["entity_id"])
    assert ticket is not None
    await recovery.async_finish_attempt(ticket, "sent_unknown")
    _old_bridge_states(hass)
    hass.services.update_states = True
    monkeypatch.setattr(recovery, "BLE_ISOLATION_TIMEOUT", 0.2)
    assert await recovery.async_isolate_ble(ticket, authorize=True)
    assert hass.services.calls[0][1:] == ("turn_off", {"entity_id": "switch.bridge_a_ble_connection"}, True)
    assert recovery.get_vehicle_route(hass, entry, VIN_A)["kind"] == "ha"
    assert recovery.ble_quarantined(hass, "bridge_a")
    hass.states_by_id["binary_sensor.bridge_a_ble_status"].state = "on"
    assert recovery.get_vehicle_route(hass, entry, VIN_A)["kind"] == "ha"
    fence = hass.states_by_id["sensor.bridge_a_transport_fence"]
    value = json.loads(fence.state)
    value["connected"] = True
    fence.state = json.dumps(value)
    assert recovery.get_vehicle_route(hass, entry, VIN_A)["kind"] == "blocked"


@pytest.mark.asyncio
async def test_two_vehicle_quarantine_is_independent(harness):
    hass, entry, _, _ = harness
    route_a = _route("bridge_a", "switch.car_a_charger", "switch.fallback")
    route_b = _route("bridge_b", "switch.car_b_charger", "switch.fallback_b")
    ticket_a = await recovery.async_begin_attempt(hass, entry, VIN_A, route_a, "start", route_a["entity_id"])
    ticket_b = await recovery.async_begin_attempt(hass, entry, VIN_B, route_b, "start", route_b["entity_id"])
    assert ticket_a and ticket_b
    await recovery.async_finish_attempt(ticket_a, "sent_unknown")
    await recovery.async_finish_attempt(ticket_b, "confirmed")
    assert recovery.ble_quarantined(hass, "bridge_a")
    assert not recovery.ble_quarantined(hass, "bridge_b")
    assert recovery.pending_ble_attempt(hass, entry, VIN_B) is None


@pytest.mark.asyncio
async def test_presence_off_does_not_prove_ble_producer_isolation(harness, monkeypatch):
    """The shipped BLE status can be advertisement presence, not GATT state."""
    hass, entry, registrations, _ = harness
    registrations.pop("sensor.bridge_a_transport_fence")
    hass.states_by_id.pop("sensor.bridge_a_transport_fence")
    route = _route("bridge_a", "switch.car_a_charger", "switch.fallback")
    ticket = await recovery.async_begin_attempt(
        hass, entry, VIN_A, route, "start", route["entity_id"])
    assert ticket is not None
    await recovery.async_finish_attempt(ticket, "sent_unknown")
    _old_bridge_states(hass)
    hass.services.update_states = True
    monkeypatch.setattr(recovery, "BLE_ISOLATION_TIMEOUT", 0.02)

    assert not await recovery.async_isolate_ble(ticket, authorize=True)
    assert recovery.get_vehicle_route(hass, entry, VIN_A)["kind"] == "blocked"
    assert recovery.ble_quarantined(hass, "bridge_a")


@pytest.mark.asyncio
async def test_current_nonce_proof_handles_an_unchanged_off_control(harness):
    hass, entry, _, _ = harness
    route = _route("bridge_a", "switch.car_a_charger", "switch.fallback")
    ticket = await recovery.async_begin_attempt(
        hass, entry, VIN_A, route, "start", route["entity_id"])
    assert ticket is not None
    await recovery.async_finish_attempt(ticket, "sent_unknown")
    _old_bridge_states(hass)
    old_off = hass.states_by_id["switch.bridge_a_ble_connection"]
    hass.services.update_states = True
    original = hass.services.async_call

    async def suppressed_duplicate(*args, **kwargs):
        await original(*args, **kwargs)
        hass.states_by_id["switch.bridge_a_ble_connection"] = old_off

    hass.services.async_call = suppressed_duplicate
    assert await recovery.async_isolate_ble(ticket, authorize=True)
    assert recovery.get_vehicle_route(hass, entry, VIN_A)["kind"] == "ha"


@pytest.mark.parametrize("change", [
    {"protocol": 2}, {"protocol": True}, {"boot_id": "another-boot"},
    {"generation": 1}, {"generation": True}, {"radio_enabled": True},
    {"connected": True}, {"producer_stopped": False},
    {"request_id": "0" * 32},
    {"queued_commands": 1}, {"queued_reads": 1}, {"queued_writes": 1},
    {"queued_writes": False},
])
@pytest.mark.asyncio
async def test_handoff_requires_new_generation_and_all_producer_work_stopped(harness, monkeypatch, change):
    hass, entry, _, _ = harness
    route = _route("bridge_a", "switch.car_a_charger", "switch.fallback")
    ticket = await recovery.async_begin_attempt(
        hass, entry, VIN_A, route, "start", route["entity_id"])
    assert ticket is not None
    await recovery.async_finish_attempt(ticket, "sent_unknown")
    hass.services.update_states = True
    original = hass.services.async_call

    async def incomplete_producer(*args, **kwargs):
        await original(*args, **kwargs)
        state = hass.states_by_id["sensor.bridge_a_transport_fence"]
        value = json.loads(state.state)
        value.update(change)
        state.state = json.dumps(value)

    hass.services.async_call = incomplete_producer
    monkeypatch.setattr(recovery, "BLE_ISOLATION_TIMEOUT", 0.01)
    assert not await recovery.async_isolate_ble(ticket, authorize=True)
    assert recovery.get_vehicle_route(hass, entry, VIN_A)["kind"] == "blocked"
    assert recovery.ble_quarantined(hass, "bridge_a")


@pytest.mark.parametrize("invalid", ["wrong_device", "disabled", "future", "legacy_proof",
    "partial_saved_proof", "float_generation", "running_producer", "unordered_receipt"])
@pytest.mark.asyncio
async def test_fence_proof_is_bound_to_exact_bridge_and_valid_receipt(harness, monkeypatch, invalid):
    hass, entry, registrations, _ = harness
    route = _route("bridge_a", "switch.car_a_charger", "switch.fallback")
    ticket = await recovery.async_begin_attempt(
        hass, entry, VIN_A, route, "start", route["entity_id"])
    assert ticket is not None
    await recovery.async_finish_attempt(ticket, "sent_unknown")
    hass.services.update_states = True
    original_call = hass.services.async_call

    async def ordered_receipt(*args, **kwargs):
        # A real bridge reply arrives after its request. Give the fake that
        # ordering too: datetime's microsecond resolution can otherwise make
        # an immediate receipt equal to the request's time.time() sample.
        await asyncio.sleep(0.001)
        await original_call(*args, **kwargs)

    hass.services.async_call = ordered_receipt
    # Keep receipt timestamps on real wall time, but make the bounded
    # monotonic polling window deterministic under full-suite load.
    import time as wall_clock
    monotonic_tick = 0.0

    def controlled_monotonic():
        nonlocal monotonic_tick
        monotonic_tick += 0.001
        return monotonic_tick

    monkeypatch.setattr(
        recovery,
        "time",
        types.SimpleNamespace(time=wall_clock.time, monotonic=controlled_monotonic),
    )
    monkeypatch.setattr(recovery, "BLE_ISOLATION_TIMEOUT", 0.01)
    if invalid == "wrong_device":
        registrations["sensor.bridge_a_transport_fence"].device_id = "ble-device-b"
    elif invalid == "disabled":
        registrations["sensor.bridge_a_transport_fence"].disabled_by = "user"
    if invalid in {"wrong_device", "disabled"}:
        assert not await recovery.async_isolate_ble(ticket, authorize=True)
    else:
        assert await recovery.async_isolate_ble(ticket, authorize=True)
        if invalid == "future":
            from datetime import timedelta
            hass.states_by_id["sensor.bridge_a_transport_fence"].last_reported = datetime.now(timezone.utc) + timedelta(seconds=60)
        else:
            hazard = hass.data["power_sync"][entry.entry_id][recovery.RECOVERY_KEY]["vehicles"][VIN_A]["hazards"]["ble:bridge_a"]
            if invalid == "legacy_proof":
                hazard.pop("fence_proof")
            elif invalid == "partial_saved_proof":
                hazard["fence_proof"] = {key: hazard["fence_proof"][key] for key in ("boot_id", "generation")}
            elif invalid == "float_generation":
                hazard["fence_proof"]["generation"] = float(hazard["fence_proof"]["generation"])
            elif invalid == "running_producer":
                hazard["fence_proof"]["producer_stopped"] = False
            else:
                hazard["fence_proof"]["observed_at"] = hazard["requested_epoch"] - 1
    assert recovery.get_vehicle_route(hass, entry, VIN_A)["kind"] == "blocked"


@pytest.mark.parametrize("during", ["authority", "persistence"])
@pytest.mark.asyncio
async def test_isolation_rechecks_live_fence_after_awaits(harness, monkeypatch, during):
    hass, entry, _, _ = harness
    route = _route("bridge_a", "switch.car_a_charger", "switch.fallback")
    ticket = await recovery.async_begin_attempt(
        hass, entry, VIN_A, route, "start", route["entity_id"])
    assert ticket is not None
    await recovery.async_finish_attempt(ticket, "sent_unknown")
    hass.services.update_states = True

    def changed_producer():
        state = hass.states_by_id["sensor.bridge_a_transport_fence"]
        value = json.loads(state.state)
        value["queued_writes"] = 1
        state.state = json.dumps(value)

    gates = 0
    async def authority():
        nonlocal gates
        gates += 1
        if gates == 3 and during == "authority":
            changed_producer()
        return True

    if during == "persistence":
        original = recovery._persist
        async def delayed_persist(*args):
            result = await original(*args)
            ledger = hass.data["power_sync"][entry.entry_id][recovery.RECOVERY_KEY]["vehicles"][VIN_A]
            if ledger["hazards"]["ble:bridge_a"]["state"] == "isolated":
                changed_producer()
            return result
        monkeypatch.setattr(recovery, "_persist", delayed_persist)

    assert not await recovery.async_isolate_ble(ticket, authorize=authority)
    assert recovery.get_vehicle_route(hass, entry, VIN_A)["kind"] == "blocked"


@pytest.mark.asyncio
async def test_isolation_cannot_rebind_the_preselected_fallback(harness):
    hass, entry, registrations, _ = harness
    route = _route("bridge_a", "switch.car_a_charger", "switch.fallback")
    ticket = await recovery.async_begin_attempt(
        hass, entry, VIN_A, route, "start", route["entity_id"])
    assert ticket is not None
    await recovery.async_finish_attempt(ticket, "sent_unknown")
    registrations["switch.fallback"].device_id = "another-vehicle"
    _old_bridge_states(hass)
    hass.services.update_states = True

    assert not await recovery.async_isolate_ble(ticket, authorize=True)
    assert recovery.get_vehicle_route(hass, entry, VIN_A)["kind"] == "blocked"


@pytest.mark.asyncio
async def test_malformed_journal_is_preserved_and_fail_closed(harness):
    hass, entry, _, _ = harness
    malformed = {"vehicles": "corrupt", "diagnostic": "keep-me"}
    runtime = hass.data["power_sync"][entry.entry_id]
    runtime[recovery.RECOVERY_KEY] = malformed
    route = _route("bridge_a", "switch.car_a_charger", "switch.fallback")

    assert await recovery.async_begin_attempt(
        hass, entry, VIN_A, route, "start", route["entity_id"]
    ) is None
    assert runtime[recovery.RECOVERY_KEY] is malformed
    blocked = recovery.get_vehicle_route(hass, entry, VIN_A)
    assert blocked["kind"] == "blocked"
    assert blocked["reason"] == "malformed_recovery"
    assert recovery.ble_quarantined(hass, "bridge_a")


@pytest.mark.asyncio
async def test_malformed_vehicle_attempts_are_not_overwritten(harness):
    hass, entry, _, _ = harness
    runtime = hass.data["power_sync"][entry.entry_id]
    malformed = {
        "vehicles": {VIN_A: {"route": None, "attempts": "keep", "hazards": {}}}
    }
    runtime[recovery.RECOVERY_KEY] = malformed
    route = _route("bridge_a", "switch.car_a_charger", "switch.fallback")

    assert await recovery.async_begin_attempt(
        hass, entry, VIN_A, route, "start", route["entity_id"]
    ) is None
    assert malformed["vehicles"][VIN_A]["attempts"] == "keep"
    assert recovery.get_vehicle_route(hass, entry, VIN_A)["kind"] == "blocked"
    assert recovery.ble_quarantined(hass, "bridge_a")


@pytest.mark.asyncio
async def test_identity_requires_real_enabled_registry_metadata(harness):
    hass, entry, registrations, _ = harness
    route = _route("bridge_a", "switch.car_a_charger", "switch.fallback")
    registrations[route["entity_id"]].unique_id = ""
    assert await recovery.async_begin_attempt(
        hass, entry, VIN_A, route, "start", route["entity_id"]
    ) is None
    registrations[route["entity_id"]].unique_id = route["entity_id"]
    registrations[route["entity_id"]].disabled_by = "user"
    assert await recovery.async_begin_attempt(
        hass, entry, VIN_A, route, "start", route["entity_id"]
    ) is None


@pytest.mark.asyncio
async def test_pairing_digest_ignores_tokens_but_tracks_ev_pairing(harness):
    hass, entry, _, _ = harness
    route = _route("bridge_a", "switch.car_a_charger", "switch.fallback")
    ticket = await recovery.async_begin_attempt(
        hass, entry, VIN_A, route, "start", route["entity_id"]
    )
    assert ticket is not None
    record = hass.data["power_sync"][entry.entry_id][recovery.RECOVERY_KEY]["vehicles"][VIN_A]["attempts"][ticket.attempt_id]
    assert "access_token" not in record["route"]

    entry.options["access_token"] = "rotated-secret"
    entry.options["monitoring_mode"] = True
    assert recovery.pending_ble_attempt(hass, entry, VIN_A) is not None

    entry.data["ev_provider"] = "teslemetry_bt"
    assert recovery.pending_ble_attempt(hass, entry, VIN_A) is None


@pytest.mark.asyncio
async def test_pairing_digest_tracks_prefix_mapping_and_producer(harness):
    hass, entry, _, _ = harness
    route = _route("bridge_a", "switch.car_a_charger", "switch.fallback")
    route["producer_identity"] = "esphome:ble-entry:ble-device:car-a"
    ticket = await recovery.async_begin_attempt(
        hass, entry, VIN_A, route, "start", route["entity_id"]
    )
    assert ticket is not None
    entry.data["tesla_ble_entity_prefix"] = "bridge_b"
    assert recovery.pending_ble_attempt(hass, entry, VIN_A) is None


@pytest.mark.asyncio
async def test_reload_new_owner_can_isolate_old_unknown_attempt(harness, monkeypatch):
    hass, entry, _, owners = harness
    route = _route("bridge_a", "switch.car_a_charger", "switch.fallback")
    ticket = await recovery.async_begin_attempt(
        hass, entry, VIN_A, route, "start", route["entity_id"]
    )
    assert ticket is not None
    await recovery.async_finish_attempt(ticket, "sent_unknown")
    _old_bridge_states(hass)

    restored = _Hass({})
    restored.data["power_sync"][entry.entry_id] = deepcopy(
        hass.data["power_sync"][entry.entry_id]
    )
    restored.entity_registry = hass.entity_registry
    restored.states_by_id = hass.states_by_id
    restored.states = types.SimpleNamespace(get=restored.states_by_id.get)
    restored.services.update_states = True
    original_call = restored.services.async_call

    async def ordered_receipt(*args, **kwargs):
        # Keep the fake service's state publication strictly after the
        # isolation request, matching the provider's acknowledgement order.
        await asyncio.sleep(0.001)
        await original_call(*args, **kwargs)

    restored.services.async_call = ordered_receipt
    monkeypatch.setattr(recovery, "BLE_ISOLATION_TIMEOUT", 0.2)
    owners.pop(VIN_A)
    pending = recovery.pending_ble_attempt(restored, entry, VIN_A)
    assert pending is not None
    assert pending.runtime_ref is restored.data["power_sync"][entry.entry_id]
    assert await recovery.async_isolate_ble(pending, authorize=True)
    assert recovery.get_vehicle_route(restored, entry, VIN_A)["kind"] == "ha"
    assert recovery.ble_quarantined(restored, "bridge_a")
    assert all(call[1] in {"turn_off", "set_value"} for call in restored.services.calls)
    # The ticket from the discarded generation cannot mutate the replacement.
    assert not await recovery.async_isolate_ble(ticket, authorize=True)


@pytest.mark.asyncio
async def test_verified_isolation_allows_new_ha_claim_without_clearing_hazard(harness, monkeypatch):
    hass, entry, _, _ = harness
    route = _route("bridge_a", "switch.car_a_charger", "switch.fallback")
    ticket = await recovery.async_begin_attempt(
        hass, entry, VIN_A, route, "start", route["entity_id"]
    )
    assert ticket is not None
    await recovery.async_finish_attempt(ticket, "sent_unknown")
    _old_bridge_states(hass)
    hass.services.update_states = True
    monkeypatch.setattr(recovery, "BLE_ISOLATION_TIMEOUT", 0.2)
    assert await recovery.async_isolate_ble(ticket, authorize=True)

    native = await recovery.async_begin_attempt(
        hass, entry, VIN_A, {"kind": "ha", "entity_id": "switch.fallback"},
        "start", "switch.fallback"
    )
    assert native is not None
    assert recovery.ble_quarantined(hass, "bridge_a")
    assert await recovery.async_finish_attempt(native, "confirmed")
    assert recovery.ble_quarantined(hass, "bridge_a")


@pytest.mark.asyncio
async def test_stale_off_proof_blocks_ha_route_after_isolation(harness, monkeypatch):
    hass, entry, _, _ = harness
    route = _route("bridge_a", "switch.car_a_charger", "switch.fallback")
    ticket = await recovery.async_begin_attempt(
        hass, entry, VIN_A, route, "start", route["entity_id"]
    )
    assert ticket is not None
    await recovery.async_finish_attempt(ticket, "sent_unknown")
    _old_bridge_states(hass)
    hass.services.update_states = True
    monkeypatch.setattr(recovery, "BLE_ISOLATION_TIMEOUT", 0.2)
    assert await recovery.async_isolate_ble(ticket, authorize=True)
    hazard = hass.data["power_sync"][entry.entry_id][recovery.RECOVERY_KEY]["vehicles"][VIN_A]["hazards"]["ble:bridge_a"]
    stale = datetime.fromtimestamp(1, timezone.utc)
    for entity_id in ("switch.bridge_a_ble_connection", "binary_sensor.bridge_a_ble_status"):
        hass.states_by_id[entity_id] = _State(entity_id, "off", stale)
    hass.states_by_id["sensor.bridge_a_transport_fence"].last_reported = stale
    assert recovery.get_vehicle_route(hass, entry, VIN_A)["kind"] == "blocked"
    assert hazard["state"] == "isolated"


@pytest.mark.asyncio
async def test_per_entry_lock_keeps_other_vehicle_when_one_persist_rolls_back(harness, monkeypatch):
    hass, entry, _, _ = harness
    calls = 0

    async def persist(_hass, _entry, _runtime):
        nonlocal calls
        calls += 1
        await asyncio.sleep(0)
        return calls != 1

    monkeypatch.setattr(recovery, "_persist", persist)
    route_a = _route("bridge_a", "switch.car_a_charger", "switch.fallback")
    route_b = _route("bridge_b", "switch.car_b_charger", "switch.fallback_b")
    first, second = await asyncio.gather(
        recovery.async_begin_attempt(hass, entry, VIN_A, route_a, "start", route_a["entity_id"]),
        recovery.async_begin_attempt(hass, entry, VIN_B, route_b, "start", route_b["entity_id"]),
    )
    assert first is None
    assert second is not None
    vehicles = hass.data["power_sync"][entry.entry_id][recovery.RECOVERY_KEY]["vehicles"]
    assert VIN_A not in vehicles
    assert second.attempt_id in vehicles[VIN_B]["attempts"]


@pytest.mark.asyncio
async def test_cancelled_persist_is_drained_and_rollback_is_saved(harness, monkeypatch):
    hass, entry, _, _ = harness
    fake = sys.modules["power_sync.automations.ev_ownership"]
    started = asyncio.Event()
    release = asyncio.Event()

    async def delayed_persist(_hass, _entry, _store):
        started.set()
        await release.wait()
        hass.persisted.append(deepcopy(
            hass.data["power_sync"][entry.entry_id].get(recovery.RECOVERY_KEY)
        ))
        return True

    monkeypatch.setattr(fake, "persist_ev_runtime_state", delayed_persist)
    route = _route("bridge_a", "switch.car_a_charger", "switch.fallback")
    task = asyncio.create_task(
        recovery.async_begin_attempt(hass, entry, VIN_A, route, "start", route["entity_id"])
    )
    try:
        await asyncio.wait_for(started.wait(), timeout=1.0)
    except asyncio.TimeoutError:
        task.cancel()
        release.set()
        with suppress(BaseException):
            await task
        pytest.fail("persistence coroutine did not start within the test bound")
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert recovery.RECOVERY_KEY not in hass.data["power_sync"][entry.entry_id]
    assert hass.persisted and hass.persisted[-1] is None


@pytest.mark.asyncio
async def test_native_session_route_carries_rate_limit_and_stop_per_vehicle(harness):
    hass, entry, _, _ = harness
    native_a = {"kind": "ha", "entity_id": "switch.fallback"}
    native_b = {"kind": "ha", "entity_id": "switch.fallback_b"}

    start_a = await recovery.async_begin_attempt(
        hass, entry, VIN_A, native_a, "start", native_a["entity_id"]
    )
    start_b = await recovery.async_begin_attempt(
        hass, entry, VIN_B, native_b, "start", native_b["entity_id"]
    )
    assert start_a is not None and start_b is not None
    assert await recovery.async_finish_attempt(start_a, "accepted_unconfirmed")
    assert await recovery.async_finish_attempt(start_b, "accepted_unconfirmed")

    routes = hass.data["power_sync"][entry.entry_id][recovery.RECOVERY_KEY]["vehicles"]
    assert routes[VIN_A]["route"]["session_scope"]["session_id"] == "sa"
    assert routes[VIN_B]["route"]["session_scope"]["session_id"] == "sb"

    rate_a = await recovery.async_begin_attempt(
        hass, entry, VIN_A, native_a, "rate", native_a["entity_id"]
    )
    assert rate_a is not None
    assert await recovery.async_finish_attempt(rate_a, "accepted_unconfirmed")
    assert recovery.get_vehicle_route(hass, entry, VIN_A)["kind"] == "ha"
    assert recovery.get_vehicle_route(hass, entry, VIN_B)["kind"] == "ha"

    stop_a = await recovery.async_begin_attempt(
        hass, entry, VIN_A, native_a, "stop", native_a["entity_id"]
    )
    assert stop_a is not None
    assert await recovery.async_finish_attempt(stop_a, "confirmed")
    assert recovery.get_vehicle_route(hass, entry, VIN_A) is None
    assert recovery.get_vehicle_route(hass, entry, VIN_B)["kind"] == "ha"

    # The completed session no longer pins native transport; a later session
    # can use its configured BLE producer again.
    later_ble = await recovery.async_begin_attempt(
        hass, entry, VIN_A, _route("bridge_a", "switch.car_a_charger", "switch.fallback"),
        "start", "switch.car_a_charger",
    )
    assert later_ble is not None


@pytest.mark.asyncio
async def test_native_session_route_carries_across_operation_entities(harness):
    hass, entry, registrations, _ = harness
    for entity_id in ("number.fallback_current", "number.fallback_limit"):
        registrations[entity_id] = _Entity(
            entity_id,
            device="native-device",
            entry="native-entry",
            platform="teslemetry",
        )
    start_route = {"kind": "ha", "entity_id": "switch.fallback"}
    start = await recovery.async_begin_attempt(
        hass, entry, VIN_A, start_route, "start", "switch.fallback"
    )
    assert start is not None
    assert await recovery.async_finish_attempt(start, "accepted_unconfirmed")
    current = recovery.get_vehicle_route(hass, entry, VIN_A)
    assert current["session_anchor"]["entity_id"] == "switch.fallback"

    rate = await recovery.async_begin_attempt(
        hass,
        entry,
        VIN_A,
        {"kind": "ha", "entity_id": "number.fallback_current"},
        "rate",
        "number.fallback_current",
    )
    assert rate is not None
    assert rate.route["entity_id"] == "number.fallback_current"
    assert rate.route["session_anchor"]["entity_id"] == "switch.fallback"
    assert await recovery.async_finish_attempt(rate, "accepted_unconfirmed")

    limit = await recovery.async_begin_attempt(
        hass,
        entry,
        VIN_A,
        {"kind": "ha", "entity_id": "number.fallback_limit"},
        "limit",
        "number.fallback_limit",
    )
    assert limit is not None
    assert await recovery.async_finish_attempt(limit, "accepted_unconfirmed")

    stop = await recovery.async_begin_attempt(
        hass,
        entry,
        VIN_A,
        {"kind": "ha", "entity_id": "switch.fallback"},
        "stop",
        "switch.fallback",
    )
    assert stop is not None
    assert await recovery.async_finish_attempt(stop, "confirmed")
    assert recovery.get_vehicle_route(hass, entry, VIN_A) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("binding_change", ["none", "rate_identity", "anchor_identity", "device", "pairing"])
async def test_old_native_session_stop_keeps_exact_binding_and_unknown_rate(harness, binding_change):
    hass, entry, registrations, owners = harness
    current_entity = "number.fallback_current"
    registrations[current_entity] = _Entity(
        current_entity, device="native-device", entry="native-entry", platform="teslemetry",
    )
    start = await recovery.async_begin_attempt(
        hass, entry, VIN_A, {"kind": "ha", "entity_id": "switch.fallback"},
        "start", "switch.fallback",
    )
    assert start is not None and await recovery.async_finish_attempt(start, "accepted_unconfirmed")
    rate = await recovery.async_begin_attempt(
        hass, entry, VIN_A, {"kind": "ha", "entity_id": current_entity},
        "rate", current_entity,
    )
    assert rate is not None and await recovery.async_finish_attempt(rate, "sent_unknown")
    original = deepcopy(hass.data["power_sync"][entry.entry_id][recovery.RECOVERY_KEY]["vehicles"][VIN_A])
    owners[VIN_A] = {"owner": "powersync", "owner_mode": "manual", "session_id": "new-session"}
    if binding_change == "rate_identity":
        registrations[current_entity].unique_id = "replacement-current"
    elif binding_change == "anchor_identity":
        registrations["switch.fallback"].unique_id = "replacement-switch"
    elif binding_change == "device":
        registrations["switch.fallback"].device_id = "another-device"
    elif binding_change == "pairing":
        entry.options = {"ev_provider": "tesla_ble"}
    stop = await recovery.async_begin_attempt(
        hass, entry, VIN_A, {"kind": "ha", "entity_id": "switch.fallback"},
        "stop", "switch.fallback",
    )
    if binding_change == "none":
        assert stop is not None and await recovery.async_finish_attempt(stop, "accepted_unconfirmed")
        ledger = hass.data["power_sync"][entry.entry_id][recovery.RECOVERY_KEY]["vehicles"][VIN_A]
        assert ledger["attempts"] == original["attempts"]
        assert recovery.get_vehicle_route(hass, entry, VIN_A)["kind"] == "blocked"
    else:
        assert stop is None
        assert hass.data["power_sync"][entry.entry_id][recovery.RECOVERY_KEY]["vehicles"][VIN_A] == original


@pytest.mark.asyncio
async def test_native_session_route_rejects_other_provider_operation_entity(harness):
    hass, entry, registrations, _ = harness
    registrations["number.other_current"] = _Entity(
        "number.other_current",
        device="other-native-device",
        entry="other-native-entry",
        platform="tesla_fleet",
    )
    start = await recovery.async_begin_attempt(
        hass,
        entry,
        VIN_A,
        {"kind": "ha", "entity_id": "switch.fallback"},
        "start",
        "switch.fallback",
    )
    assert start is not None
    assert await recovery.async_finish_attempt(start, "accepted_unconfirmed")
    assert await recovery.async_begin_attempt(
        hass,
        entry,
        VIN_A,
        {"kind": "ha", "entity_id": "number.other_current"},
        "rate",
        "number.other_current",
    ) is None
    route = recovery.get_vehicle_route(hass, entry, VIN_A)
    assert route["entity_id"] == "switch.fallback"
    assert route["session_anchor"]["entity_id"] == "switch.fallback"


@pytest.mark.asyncio
async def test_native_session_route_survives_reload_but_rebind_or_lease_change_blocks(harness):
    hass, entry, registrations, owners = harness
    native = {"kind": "ha", "entity_id": "switch.fallback"}
    start = await recovery.async_begin_attempt(
        hass, entry, VIN_A, native, "start", native["entity_id"]
    )
    assert start is not None
    assert await recovery.async_finish_attempt(start, "accepted_unconfirmed")

    restored = _Hass({})
    restored.data["power_sync"][entry.entry_id] = deepcopy(
        hass.data["power_sync"][entry.entry_id]
    )
    restored.entity_registry = hass.entity_registry
    assert recovery.get_vehicle_route(restored, entry, VIN_A)["kind"] == "ha"

    owners[VIN_A]["ownership_generation"] = 2
    assert recovery.get_vehicle_route(restored, entry, VIN_A)["kind"] == "blocked"
    owners[VIN_A]["ownership_generation"] = 1
    registrations["switch.fallback"].device_id = "rebound-native-device"
    assert recovery.get_vehicle_route(restored, entry, VIN_A)["kind"] == "blocked"


@pytest.mark.asyncio
async def test_native_rate_without_active_start_does_not_pin_future_ble(harness):
    hass, entry, _, _ = harness
    native = {"kind": "ha", "entity_id": "switch.fallback"}
    rate = await recovery.async_begin_attempt(
        hass, entry, VIN_A, native, "rate", native["entity_id"]
    )
    assert rate is not None
    assert await recovery.async_finish_attempt(rate, "accepted_unconfirmed")
    assert recovery.get_vehicle_route(hass, entry, VIN_A) is None


@pytest.mark.asyncio
async def test_native_session_handoff_binds_after_start_lease_is_claimed(harness):
    hass, entry, _, owners = harness
    native = {"kind": "ha", "entity_id": "switch.fallback"}
    owners.pop(VIN_A)
    start = await recovery.async_begin_attempt(
        hass, entry, VIN_A, native, "start", native["entity_id"]
    )
    assert start is not None
    assert await recovery.async_finish_attempt(start, "accepted_unconfirmed")
    assert recovery.get_vehicle_route(hass, entry, VIN_A) is None

    owners[VIN_A] = {
        "owner": "powersync",
        "owner_mode": "manual",
        "session_id": "claimed-after-start",
        "ownership_generation": 3,
    }
    registration = hass.entity_registry.async_get(native["entity_id"])
    expected_identity = {
        key: getattr(registration, key)
        for key in ("entity_id", "platform", "config_entry_id", "device_id", "unique_id")
    }
    assert await recovery.async_bind_native_session_route(
        hass,
        entry,
        VIN_A,
        native["entity_id"],
        expected_identity=expected_identity,
    )
    assert recovery.get_vehicle_route(hass, entry, VIN_A)["kind"] == "ha"
    assert (
        recovery.get_vehicle_route(hass, entry, VIN_A)["session_scope"]["session_id"]
        == "claimed-after-start"
    )


@pytest.mark.asyncio
async def test_native_restore_handoff_reuses_exact_producer_for_rate_limit_stop(harness):
    hass, entry, registrations, owners = harness
    for entity_id in ("number.fallback_current", "number.fallback_limit"):
        registrations[entity_id] = _Entity(
            entity_id,
            device="native-device",
            entry="native-entry",
            platform="teslemetry",
        )
    start = await recovery.async_begin_attempt(
        hass,
        entry,
        VIN_A,
        {"kind": "ha", "entity_id": "switch.fallback"},
        "start",
        "switch.fallback",
    )
    assert start is not None
    assert await recovery.async_finish_attempt(start, "accepted_unconfirmed")
    persisted_route = deepcopy(recovery.get_vehicle_route(hass, entry, VIN_A))
    assert persisted_route["kind"] == "ha"

    # A restored manual session has a new ownership lease, while the registry
    # and pairing remain the exact producer captured by the old Start.
    restored = _Hass({})
    restored.data["power_sync"][entry.entry_id] = deepcopy(
        hass.data["power_sync"][entry.entry_id]
    )
    restored.entity_registry = hass.entity_registry
    owners[VIN_A] = {
        "owner": "powersync",
        "owner_mode": "manual",
        "session_id": "restored-session",
        "ownership_generation": 2,
    }
    assert recovery.get_vehicle_route(restored, entry, VIN_A)["kind"] == "blocked"
    assert await recovery.async_rebind_native_session_route(
        restored,
        entry,
        VIN_A,
        entity_id="switch.fallback",
        expected_identity=persisted_route["entity_identity"],
        mode="positive_resume",
    )
    rebound = recovery.get_vehicle_route(restored, entry, VIN_A)
    assert rebound["kind"] == "ha"
    assert rebound["entity_id"] == persisted_route["entity_id"]
    assert rebound["entity_identity"] == persisted_route["entity_identity"]
    assert rebound["vehicle_id"] == VIN_A
    assert rebound["producer_identity"] == persisted_route["producer_identity"]
    assert rebound["session_scope"]["session_id"] == "restored-session"

    # Operation-specific native entities inherit only the recovered session
    # route; the handoff itself does not create another Start attempt.
    rate = await recovery.async_begin_attempt(
        restored,
        entry,
        VIN_A,
        {"kind": "ha", "entity_id": "number.fallback_current"},
        "rate",
        "number.fallback_current",
    )
    assert rate is not None
    assert rate.route["session_anchor"]["entity_id"] == "switch.fallback"
    assert await recovery.async_finish_attempt(rate, "accepted_unconfirmed")
    limit = await recovery.async_begin_attempt(
        restored,
        entry,
        VIN_A,
        {"kind": "ha", "entity_id": "number.fallback_limit"},
        "limit",
        "number.fallback_limit",
    )
    assert limit is not None
    assert await recovery.async_finish_attempt(limit, "accepted_unconfirmed")
    stop = await recovery.async_begin_attempt(
        restored,
        entry,
        VIN_A,
        {"kind": "ha", "entity_id": "switch.fallback"},
        "stop",
        "switch.fallback",
    )
    assert stop is not None
    assert await recovery.async_finish_attempt(stop, "confirmed")
    assert recovery.get_vehicle_route(restored, entry, VIN_A) is None
    records = restored.data["power_sync"][entry.entry_id][recovery.RECOVERY_KEY]["vehicles"]
    assert records[VIN_A]["attempts"] == {}


@pytest.mark.asyncio
async def test_native_restore_resume_allows_isolated_ble_hazard(harness, monkeypatch):
    hass, entry, registrations, owners = harness
    registrations["number.fallback_current"] = _Entity(
        "number.fallback_current",
        device="native-device",
        entry="native-entry",
        platform="teslemetry",
    )
    ble_route = _route("bridge_a", "switch.car_a_charger", "switch.fallback")
    ble = await recovery.async_begin_attempt(
        hass, entry, VIN_A, ble_route, "start", ble_route["entity_id"]
    )
    assert ble is not None
    assert await recovery.async_finish_attempt(ble, "sent_unknown")
    _old_bridge_states(hass)
    hass.services.update_states = True
    monkeypatch.setattr(recovery, "BLE_ISOLATION_TIMEOUT", 0.2)
    assert await recovery.async_isolate_ble(ble, authorize=True)

    native = {"kind": "ha", "entity_id": "switch.fallback"}
    native_start = await recovery.async_begin_attempt(
        hass, entry, VIN_A, native, "start", native["entity_id"]
    )
    assert native_start is not None
    assert await recovery.async_finish_attempt(native_start, "accepted_unconfirmed")
    old_route = deepcopy(recovery.get_vehicle_route(hass, entry, VIN_A))
    assert old_route["kind"] == "ha"

    restored = _Hass({})
    restored.data["power_sync"][entry.entry_id] = deepcopy(
        hass.data["power_sync"][entry.entry_id]
    )
    restored.entity_registry = hass.entity_registry
    restored.states_by_id = hass.states_by_id
    restored.states = types.SimpleNamespace(get=restored.states_by_id.get)
    owners[VIN_A] = {
        "owner": "powersync",
        "owner_mode": "manual",
        "session_id": "restored-session",
        "ownership_generation": 2,
    }
    assert await recovery.async_rebind_native_session_route(
        restored,
        entry,
        VIN_A,
        entity_id=native["entity_id"],
        expected_identity=old_route["entity_identity"],
        mode="positive_resume",
    )
    rate = await recovery.async_begin_attempt(
        restored,
        entry,
        VIN_A,
        {"kind": "ha", "entity_id": "number.fallback_current"},
        "rate",
        "number.fallback_current",
    )
    assert rate is not None
    assert await recovery.async_finish_attempt(rate, "accepted_unconfirmed")
    ledger = restored.data["power_sync"][entry.entry_id][recovery.RECOVERY_KEY]["vehicles"][VIN_A]
    assert ble.attempt_id in ledger["attempts"]
    assert ledger["attempts"][ble.attempt_id]["outcome"] == "sent_unknown"


@pytest.mark.asyncio
async def test_native_restore_handoff_rejects_provider_rebind(harness):
    hass, entry, _, owners = harness
    native = {"kind": "ha", "entity_id": "switch.fallback"}
    start = await recovery.async_begin_attempt(
        hass, entry, VIN_A, native, "start", native["entity_id"]
    )
    assert start is not None
    assert await recovery.async_finish_attempt(start, "accepted_unconfirmed")
    old_route = deepcopy(recovery.get_vehicle_route(hass, entry, VIN_A))
    restored = _Hass({})
    restored.data["power_sync"][entry.entry_id] = deepcopy(
        hass.data["power_sync"][entry.entry_id]
    )
    restored.entity_registry = hass.entity_registry
    owners[VIN_A] = {
        "owner": "powersync",
        "owner_mode": "manual",
        "session_id": "restored-session",
        "ownership_generation": 2,
    }
    entry.options["ev_provider"] = "another-native-provider"
    assert not await recovery.async_rebind_native_session_route(
        restored,
        entry,
        VIN_A,
        entity_id=native["entity_id"],
        expected_identity=old_route["entity_identity"],
        mode="positive_resume",
    )
    assert restored.data["power_sync"][entry.entry_id][recovery.RECOVERY_KEY]["vehicles"][VIN_A]["route"] == old_route


@pytest.mark.asyncio
async def test_native_restore_handoff_rolls_back_pairing_change_during_persist(
    harness, monkeypatch
):
    hass, entry, _, owners = harness
    native = {"kind": "ha", "entity_id": "switch.fallback"}
    start = await recovery.async_begin_attempt(
        hass, entry, VIN_A, native, "start", native["entity_id"]
    )
    assert start is not None
    assert await recovery.async_finish_attempt(start, "accepted_unconfirmed")
    old_route = deepcopy(recovery.get_vehicle_route(hass, entry, VIN_A))
    restored = _Hass({})
    restored.data["power_sync"][entry.entry_id] = deepcopy(
        hass.data["power_sync"][entry.entry_id]
    )
    restored.entity_registry = hass.entity_registry
    owners[VIN_A] = {
        "owner": "powersync",
        "owner_mode": "manual",
        "session_id": "restored-session",
        "ownership_generation": 2,
    }

    fake = sys.modules["power_sync.automations.ev_ownership"]
    original_persist = fake.persist_ev_runtime_state
    changed = False

    async def persist_then_rebind(*args, **kwargs):
        nonlocal changed
        result = await original_persist(*args, **kwargs)
        if not changed:
            changed = True
            entry.options["ev_provider"] = "replacement-provider"
        return result

    monkeypatch.setattr(fake, "persist_ev_runtime_state", persist_then_rebind)
    assert not await recovery.async_rebind_native_session_route(
        restored,
        entry,
        VIN_A,
        entity_id=native["entity_id"],
        expected_identity=old_route["entity_identity"],
        mode="positive_resume",
    )
    ledger = restored.data["power_sync"][entry.entry_id][recovery.RECOVERY_KEY]["vehicles"][VIN_A]
    assert ledger["route"] == old_route


@pytest.mark.asyncio
async def test_native_cleanup_handoff_preserves_unknown_hazard(harness):
    hass, entry, _, owners = harness
    native = {"kind": "ha", "entity_id": "switch.fallback"}
    start = await recovery.async_begin_attempt(
        hass, entry, VIN_A, native, "start", native["entity_id"]
    )
    assert start is not None
    assert await recovery.async_finish_attempt(start, "accepted_unconfirmed")
    unknown = await recovery.async_begin_attempt(
        hass, entry, VIN_A, native, "rate", native["entity_id"]
    )
    assert unknown is not None
    assert await recovery.async_finish_attempt(unknown, "sent_unknown")
    before = deepcopy(
        hass.data["power_sync"][entry.entry_id][recovery.RECOVERY_KEY]["vehicles"][VIN_A]
    )

    restored = _Hass({})
    restored.data["power_sync"][entry.entry_id] = deepcopy(
        hass.data["power_sync"][entry.entry_id]
    )
    restored.entity_registry = hass.entity_registry
    owners[VIN_A] = {
        "owner": "powersync",
        "owner_mode": "manual",
        "session_id": "cleanup-session",
        "ownership_generation": 3,
    }
    expected_identity = before["route"]["entity_identity"]
    assert not await recovery.async_rebind_native_session_route(
        restored,
        entry,
        VIN_A,
        entity_id=native["entity_id"],
        expected_identity=expected_identity,
        mode="positive_resume",
    )
    assert await recovery.async_rebind_native_session_route(
        restored,
        entry,
        VIN_A,
        entity_id=native["entity_id"],
        expected_identity=expected_identity,
        mode="cleanup_stop",
    )
    ledger = restored.data["power_sync"][entry.entry_id][recovery.RECOVERY_KEY]["vehicles"][VIN_A]
    assert ledger["attempts"][unknown.attempt_id]["outcome"] == "sent_unknown"
    assert ledger["route"]["session_scope"]["session_id"] == "cleanup-session"

    stop = await recovery.async_begin_attempt(
        restored, entry, VIN_A, native, "stop", native["entity_id"]
    )
    assert stop is not None
    assert await recovery.async_finish_attempt(stop, "confirmed")
    ledger = restored.data["power_sync"][entry.entry_id][recovery.RECOVERY_KEY]["vehicles"][VIN_A]
    assert unknown.attempt_id in ledger["attempts"]
    assert ledger["attempts"][unknown.attempt_id]["outcome"] == "sent_unknown"


@pytest.mark.asyncio
async def test_cleanup_handoff_is_stop_only_until_cleanup_finishes(harness):
    hass, entry, registrations, owners = harness
    for entity_id in ("number.fallback_current", "number.fallback_limit"):
        registrations[entity_id] = _Entity(
            entity_id,
            device="native-device",
            entry="native-entry",
            platform="teslemetry",
        )
    native = {"kind": "ha", "entity_id": "switch.fallback"}
    start = await recovery.async_begin_attempt(
        hass, entry, VIN_A, native, "start", native["entity_id"]
    )
    assert start is not None
    assert await recovery.async_finish_attempt(start, "accepted_unconfirmed")
    old_route = deepcopy(recovery.get_vehicle_route(hass, entry, VIN_A))

    restored = _Hass({})
    restored.data["power_sync"][entry.entry_id] = deepcopy(
        hass.data["power_sync"][entry.entry_id]
    )
    restored.entity_registry = hass.entity_registry
    owners[VIN_A] = {
        "owner": "powersync",
        "owner_mode": "boost",
        "session_id": "cleanup-session",
        "ownership_generation": 4,
    }
    assert await recovery.async_rebind_native_session_route(
        restored,
        entry,
        VIN_A,
        entity_id=native["entity_id"],
        expected_identity=old_route["entity_identity"],
        mode="cleanup_stop",
    )
    rebound = recovery.get_vehicle_route(restored, entry, VIN_A)
    assert rebound["cleanup_only"] is True

    for operation, entity_id in (
        ("start", "switch.fallback"),
        ("rate", "number.fallback_current"),
        ("limit", "number.fallback_limit"),
    ):
        assert await recovery.async_begin_attempt(
            restored,
            entry,
            VIN_A,
            {"kind": "ha", "entity_id": entity_id},
            operation,
            entity_id,
        ) is None

    stop = await recovery.async_begin_attempt(
        restored, entry, VIN_A, native, "stop", native["entity_id"]
    )
    assert stop is not None
    assert await recovery.async_finish_attempt(stop, "confirmed")
    assert recovery.get_vehicle_route(restored, entry, VIN_A) is None


class _SidecarStore:
    def __init__(self, value=None):
        self.value = deepcopy(value)
        self.saves = []
        self.fail = False

    async def async_load(self):
        return deepcopy(self.value)

    async def async_save(self, value):
        if self.fail:
            return False
        self.value = deepcopy(value)
        self.saves.append(deepcopy(value))
        return None


def _sidecar_harness(harness):
    hass, entry, registrations, owners = harness
    store = _SidecarStore()
    hass.data["power_sync"][entry.entry_id]["_tesla_ev_recovery_sidecar_store"] = store
    return hass, entry, store, registrations, owners


@pytest.mark.asyncio
@pytest.mark.parametrize("malformed", [[], {"journal": "without-vehicles"}])
async def test_malformed_legacy_projection_is_held_without_empty_import(harness, malformed):
    hass, entry, store, _, _ = _sidecar_harness(harness)
    runtime = hass.data["power_sync"][entry.entry_id]
    raw = deepcopy(malformed)
    runtime[recovery.RECOVERY_KEY] = raw

    assert not await recovery.async_load_recovery_state(hass, entry)
    assert runtime[recovery.RECOVERY_KEY] == raw
    assert runtime["_tesla_ev_recovery_sidecar_hold"] == "legacy_journal_malformed"
    assert "_tesla_ev_recovery_sidecar" not in runtime
    assert store.saves == []


@pytest.mark.asyncio
async def test_reload_reholds_interrupted_recovery_and_rotates_workflow_token(harness):
    hass, entry, store, _, _ = _sidecar_harness(harness)
    state = recovery._new_sidecar()
    old_token = "workflow-before-host-crash"
    state["incidents"]["incident-1"] = {
        "incident_id": "incident-1", "phase": "reset_pending",
        "phase_token": old_token, "scope_closed": True,
        "current_candidate_vehicles": [VIN_A],
    }
    state["obligations"]["incident-1"] = {
        "phase": "reconciling", "phase_token": old_token,
        "expires_at": time.time() + 100,
    }
    store.value = deepcopy(state)

    assert await recovery.async_load_recovery_state(hass, entry, enrolled=True)
    runtime = hass.data["power_sync"][entry.entry_id]
    loaded = runtime["_tesla_ev_recovery_sidecar"]
    incident = loaded["incidents"]["incident-1"]
    obligation = loaded["obligations"]["incident-1"]
    assert incident["phase"] == "held"
    assert incident["retryable"] is True
    assert incident["phase_token"] != old_token
    assert obligation["phase"] == "held"
    assert obligation["phase_token"] == incident["phase_token"]
    assert obligation["expires_at"] > time.time()
    assert loaded["revision"] == state["revision"] + 1
    assert store.value["incidents"]["incident-1"]["phase"] == "held"
    generation = recovery.current_vehicle_generation(hass, entry, VIN_A)
    assert not recovery.recovery_control_allowed(hass, entry, VIN_A, generation, "rate")


@pytest.mark.asyncio
async def test_shutdown_drains_store_and_fences_old_runtime_after_reload(harness):
    hass, entry, store, _, _ = _sidecar_harness(harness)
    assert await recovery.async_load_recovery_state(hass, entry)
    runtime = hass.data["power_sync"][entry.entry_id]
    started = asyncio.Event()
    release = asyncio.Event()
    original_save = store.async_save

    async def blocked_save(value):
        started.set()
        await release.wait()
        return await original_save(value)

    store.async_save = blocked_save
    pending = asyncio.create_task(recovery.async_persist_recovery_state(hass, entry))
    await asyncio.wait_for(started.wait(), timeout=0.2)
    shutdown = asyncio.create_task(recovery.async_shutdown_recovery_state(hass, entry, runtime))
    await asyncio.sleep(0)
    assert not shutdown.done()
    release.set()
    assert await pending
    assert await shutdown
    assert runtime["_tesla_ev_recovery_stopping"] is True
    assert not recovery.recovery_control_allowed(
        hass, entry, VIN_A, recovery.current_vehicle_generation(hass, entry, VIN_A), "rate",
    )

    writes_after_shutdown = len(store.saves)
    assert not await recovery.async_persist_recovery_state(hass, entry)
    assert len(store.saves) == writes_after_shutdown

    replacement = _Hass({})
    replacement.data["power_sync"][entry.entry_id]["_tesla_ev_recovery_sidecar_store"] = store
    hass.data["power_sync"][entry.entry_id] = replacement.data["power_sync"][entry.entry_id]
    assert await recovery.async_load_recovery_state(hass, entry, enrolled=True)
    assert replacement.data["power_sync"][entry.entry_id]["_tesla_ev_recovery_stopping"] is False
    assert store.saves[-1]["schema_version"] == recovery.SIDECAR_SCHEMA_VERSION
    writes_after_reload = len(store.saves)
    assert not await recovery._save_sidecar(
        hass, entry, runtime, runtime["_tesla_ev_recovery_sidecar"], allow_stopping=True,
    )
    assert len(store.saves) == writes_after_reload


@pytest.mark.asyncio
async def test_sidecar_survives_projection_loss_and_holds_evidence_loss(harness):
    hass, entry, store, _, _ = _sidecar_harness(harness)
    assert await recovery.async_load_recovery_state(hass, entry)
    incident = await recovery.async_record_evidence_loss(
        hass,
        entry,
        surviving_facts=[{"kind": "rate", "reported_at": "redacted"}],
        provenance=["operator_summary"],
        missing_fields=["attempt_id", "binding", "target", "deadline"],
        candidate_producers=[{"device_id": "bridge-a"}, {"device_id": "bridge-b"}],
        candidate_vehicles=[VIN_A, VIN_B],
        scope_basis="operator_inventory_pending",
    )
    assert incident and incident["kind"] == "evidence_lost"
    assert incident["liability_refs"] == []
    assert recovery.current_vehicle_generation(hass, entry, VIN_A) == 1
    assert not recovery.recovery_control_allowed(hass, entry, VIN_A, 1, "rate")
    assert recovery.recovery_control_allowed(hass, entry, VIN_A, 1, "stop")

    # The old automation projection can be overwritten; the sidecar remains
    # authoritative and rehydrates the incident without inventing an attempt.
    runtime = hass.data["power_sync"][entry.entry_id]
    runtime.pop("ev_transport_recovery", None)
    runtime.pop("_tesla_ev_recovery_sidecar_loaded", None)
    assert await recovery.async_load_recovery_state(hass, entry)
    restored = runtime["_tesla_ev_recovery_sidecar"]["incidents"][incident["incident_id"]]
    assert restored["phase"] == "held"
    assert restored["missing_fields"] == ["attempt_id", "binding", "target", "deadline"]
    assert not restored.get("liability_refs")
    assert store.saves


@pytest.mark.asyncio
async def test_recovery_scope_requires_complete_explicit_coverage(harness):
    hass, entry, _, _, _ = _sidecar_harness(harness)
    assert await recovery.async_load_recovery_state(hass, entry)
    incident = await recovery.async_record_evidence_loss(
        hass, entry, candidate_producers=["bridge-a"], candidate_vehicles=[VIN_A]
    )
    state = hass.data["power_sync"][entry.entry_id]["_tesla_ev_recovery_sidecar"]
    assert not await recovery.async_confirm_recovery_scope(
        hass, entry, incident["incident_id"], expected_revision=state["revision"],
        operator_confirmation=True, candidate_producers=[], candidate_vehicles=[]
    )
    state = hass.data["power_sync"][entry.entry_id]["_tesla_ev_recovery_sidecar"]
    assert await recovery.async_confirm_recovery_scope(
        hass, entry, incident["incident_id"], expected_revision=state["revision"],
        candidate_producers=["bridge-a", "bridge-b"], candidate_vehicles=[VIN_A, VIN_B],
        scope_basis="confirmed_inventory", operator_confirmation=True,
    )
    assert not recovery.recovery_control_allowed(
        hass, entry, VIN_A, recovery.current_vehicle_generation(hass, entry, VIN_A), "start"
    )


@pytest.mark.asyncio
async def test_stock_recovery_releases_only_after_fresh_requested_observations(harness):
    hass, entry, _, _, _ = _sidecar_harness(harness)
    assert await recovery.async_load_recovery_state(hass, entry)
    submission_started = asyncio.Event()
    submission_release = asyncio.Event()

    async def _pending_submission():
        submission_started.set()
        await submission_release.wait()

    submission_task = asyncio.create_task(recovery.async_track_submission(
        hass, entry, _pending_submission, 0, vehicle_id=VIN_A, operation="rate",
    ))
    await asyncio.wait_for(submission_started.wait(), timeout=1)
    incident = await recovery.async_record_evidence_loss(
        hass, entry, candidate_producers=["bridge-a"], candidate_vehicles=[VIN_A]
    )
    state = hass.data["power_sync"][entry.entry_id]["_tesla_ev_recovery_sidecar"]
    assert await recovery.async_confirm_recovery_scope(
        hass, entry, incident["incident_id"], expected_revision=state["revision"],
        candidate_producers=["bridge-a"], candidate_vehicles=[VIN_A],
        operator_confirmation=True,
    )
    identity = {
        "config_entry_id": "ble-entry", "device_id": "bridge-a",
        "unique_id": "bridge-a", "platform": "esphome", "vin": VIN_A,
    }
    uptime = iter((100.0, 1.0, 2.0))
    now = time.time()
    async def _restart():
        return True
    async def _identity():
        return identity
    async def _uptime():
        value = next(uptime)
        return {"uptime": value, "observed_at": now + value + 1000, "fresh": True}
    async def _observe(vin):
        assert vin == VIN_A
        return {
            "requested_current": 10,
            "charge_limit": 80,
            "charging": True,
            "power": 2.0,
            "fresh": True,
            "observed_at": time.time() + 1,
            "provenance": {
                "requested_current": "native_service_read",
                "charge_limit": "native_service_read",
                "charging": "native_vehicle_read",
                "power": "native_vehicle_read",
            },
        }
    async def _release_submission():
        await asyncio.sleep(0.01)
        submission_release.set()
    asyncio.create_task(_release_submission())
    result = await recovery.async_recover_stock(
        hass, entry, incident_id=incident["incident_id"],
        profile={"queue_persistence": "ram_only", "host_replay": "none", "exercised": True},
        identity=identity, restart=_restart, wait_disconnected=lambda: True,
        wait_connected=lambda: True, read_identity=_identity, read_uptime=_uptime,
        observe=_observe,
    )
    assert result is True
    state = hass.data["power_sync"][entry.entry_id]["_tesla_ev_recovery_sidecar"]
    assert state["incidents"][incident["incident_id"]]["phase"] == "released"
    assert state["liabilities"][incident["incident_id"]]["state"] == "retained_reconciled"
    generation = recovery.current_vehicle_generation(hass, entry, VIN_A)
    assert recovery.recovery_control_allowed(hass, entry, VIN_A, generation, "rate")
    assert await submission_task is None


@pytest.mark.asyncio
async def test_stock_recovery_rejects_actual_only_observation(harness):
    hass, entry, _, _, _ = _sidecar_harness(harness)
    assert await recovery.async_load_recovery_state(hass, entry)
    incident = await recovery.async_record_evidence_loss(
        hass, entry, candidate_producers=["bridge-a"], candidate_vehicles=[VIN_A]
    )
    state = hass.data["power_sync"][entry.entry_id]["_tesla_ev_recovery_sidecar"]
    assert await recovery.async_confirm_recovery_scope(
        hass, entry, incident["incident_id"], expected_revision=state["revision"],
        candidate_producers=["bridge-a"], candidate_vehicles=[VIN_A],
        operator_confirmation=True,
    )
    identity = {"config_entry_id": "ble-entry", "device_id": "bridge-a",
                "unique_id": "bridge-a", "platform": "esphome", "vin": VIN_A}
    uptime = iter((100.0, 1.0, 2.0))
    now = time.time()
    async def _uptime():
        value = next(uptime)
        return {"uptime": value, "observed_at": now + value + 1000, "fresh": True}
    async def _observe(_vin):
        return {"charge_limit": 80, "charging": True, "power": 2.0,
                "fresh": True, "observed_at": time.time() + 1,
                "provenance": {"charge_limit": "read", "charging": "read", "power": "read"}}
    assert not await recovery.async_recover_stock(
        hass, entry, incident_id=incident["incident_id"],
        profile={"queue_persistence": "ram_only", "host_replay": "none", "exercised": True},
        identity=identity, restart=lambda: True, wait_disconnected=lambda: True,
        wait_connected=lambda: True, read_identity=lambda: identity, read_uptime=_uptime,
        observe=_observe,
    )
    state = hass.data["power_sync"][entry.entry_id]["_tesla_ev_recovery_sidecar"]
    assert state["incidents"][incident["incident_id"]]["phase"] == "held"
    assert state["incidents"][incident["incident_id"]]["retryable"] is True


@pytest.mark.asyncio
async def test_sidecar_persist_failure_restores_projection_and_revision(harness):
    hass, entry, store, _, _ = _sidecar_harness(harness)
    assert await recovery.async_load_recovery_state(hass, entry)
    runtime = hass.data["power_sync"][entry.entry_id]
    before = deepcopy(runtime["_tesla_ev_recovery_sidecar"])
    # The projection is deliberately changed before the save.  A failed
    # Store write must not leave the in-memory authoritative state half
    # advanced, because a later callback could otherwise observe a revision
    # that never became durable.
    runtime[recovery.RECOVERY_KEY] = {"vehicles": {
        VIN_A: {"route": None, "attempts": {}, "hazards": {"new": {
            "state": "pending", "binding": {"vin": VIN_A},
        }}}
    }}
    store.fail = True
    assert not await recovery.async_persist_recovery_state(hass, entry)
    assert runtime["_tesla_ev_recovery_sidecar"] == before


@pytest.mark.asyncio
async def test_live_projection_updates_sidecar_and_reload_keeps_terminal_retirement(harness):
    hass, entry, store, _, _ = _sidecar_harness(harness)
    assert await recovery.async_load_recovery_state(hass, entry)
    route = _route("bridge_a", "switch.car_a_charger", "switch.fallback")
    ticket = await recovery.async_begin_attempt(
        hass, entry, VIN_A, route, "start", route["entity_id"]
    )
    assert ticket is not None
    runtime = hass.data["power_sync"][entry.entry_id]
    legacy_attempt = runtime[recovery.RECOVERY_KEY]["vehicles"][VIN_A]["attempts"][ticket.attempt_id]
    legacy_attempt["outcome"] = "confirmed"
    legacy_attempt["recovery_retired"] = {"state": "producer_retired_reconciled"}
    assert await recovery.async_persist_recovery_state(hass, entry)
    durable = store.value["recovery"]["vehicles"][VIN_A]["attempts"][ticket.attempt_id]
    assert durable["outcome"] == "confirmed"
    assert durable["recovery_retired"]["state"] == "producer_retired_reconciled"

    # A restored legacy projection can still be stale/pending.  Reload must
    # preserve the sidecar's terminal outcome and retirement marker.
    legacy_attempt["outcome"] = "pending"
    legacy_attempt.pop("recovery_retired", None)
    runtime.pop(recovery.RECOVERY_KEY, None)
    runtime.pop("_tesla_ev_recovery_sidecar", None)
    runtime.pop("_tesla_ev_recovery_sidecar_loaded", None)
    assert await recovery.async_load_recovery_state(hass, entry)
    restored = runtime["_tesla_ev_recovery_sidecar"]["recovery"]["vehicles"][VIN_A]
    assert restored["attempts"][ticket.attempt_id]["outcome"] == "confirmed"
    assert restored["attempts"][ticket.attempt_id]["recovery_retired"]["state"] == "producer_retired_reconciled"


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ("accepted_unconfirmed", "not_sent", "confirmed"))
async def test_terminal_finish_removal_survives_stale_projection_and_reload(
    harness, outcome,
):
    hass, entry, store, registrations, owners = _sidecar_harness(harness)
    registrations["number.fallback_limit"] = _Entity(
        "number.fallback_limit", device="native-device", entry="native-entry",
        platform="teslemetry",
    )
    owners.pop(VIN_A)
    assert await recovery.async_load_recovery_state(hass, entry)
    route = {"kind": "ha", "entity_id": "number.fallback_limit"}
    ticket = await recovery.async_begin_attempt(
        hass, entry, VIN_A, route, "limit", route["entity_id"],
    )
    assert ticket is not None
    pending = deepcopy(
        store.value["recovery"]["vehicles"][VIN_A]["attempts"][ticket.attempt_id]
    )

    # Finish the ticket only after loading its persisted pending record into a
    # replacement runtime, matching the real reload path.
    restored = _Hass({})
    restored.entity_registry = hass.entity_registry
    restored_runtime = {
        "automation_store": object(),
        recovery.RECOVERY_KEY: deepcopy(
            hass.data["power_sync"][entry.entry_id][recovery.RECOVERY_KEY]
        ),
        "_tesla_ev_recovery_enrolled": True,
        "_tesla_ev_recovery_sidecar_established": True,
        recovery._SIDECAR_STORE_KEY: store,
    }
    restored.data["power_sync"][entry.entry_id] = restored_runtime
    assert await recovery.async_load_recovery_state(restored, entry)
    loaded_record = restored_runtime[recovery.RECOVERY_KEY]["vehicles"][VIN_A]["attempts"][ticket.attempt_id]
    loaded_ticket = recovery.RecoveryTicket(
        restored, entry, VIN_A, ticket.attempt_id,
        deepcopy(loaded_record["route"]), restored_runtime,
    )
    assert await recovery.async_finish_attempt(loaded_ticket, outcome)
    durable = store.value["recovery"]["vehicles"][VIN_A]
    assert durable["attempts"] == {}
    proof = store.value["recovery"]["terminal_removals"][VIN_A][loaded_ticket.attempt_id]
    assert proof["outcome"] == outcome

    # A late automation projection may still contain the pre-finish pending
    # record.  The exact proof removes only that matching record.
    live = restored_runtime[recovery.RECOVERY_KEY]
    live["vehicles"][VIN_A]["attempts"][ticket.attempt_id] = pending
    live["vehicles"][VIN_A]["route"] = None
    assert await recovery.async_persist_recovery_state(restored, entry)
    assert store.value["recovery"]["vehicles"][VIN_A]["attempts"] == {}

    reloaded = _Hass({})
    reloaded.entity_registry = hass.entity_registry
    reloaded_runtime = {
        "automation_store": object(),
        recovery.RECOVERY_KEY: deepcopy(live),
        "_tesla_ev_recovery_enrolled": True,
        "_tesla_ev_recovery_sidecar_established": True,
        recovery._SIDECAR_STORE_KEY: store,
    }
    reloaded.data["power_sync"][entry.entry_id] = reloaded_runtime
    assert await recovery.async_load_recovery_state(reloaded, entry)
    restored_ledger = reloaded_runtime[recovery.RECOVERY_KEY]["vehicles"][VIN_A]
    assert restored_ledger["attempts"] == {}

    # A later standalone native Limit is allowed; the completed ticket does
    # not become a permanent route or unresolved hazard.
    fresh = await recovery.async_begin_attempt(
        reloaded, entry, VIN_A, route, "limit", route["entity_id"],
    )
    assert fresh is not None


@pytest.mark.asyncio
@pytest.mark.parametrize('kind,outcome,retired_history', [
    ('ha', 'accepted_unconfirmed', False),
    ('ble', 'confirmed', False),
    ('ble', 'confirmed', True),
])
async def test_terminal_stop_proof_clears_retained_cleanup_route_and_allows_ble_retry(
    harness, kind, outcome, retired_history,
):
    """An exact terminal Stop releases only its stale producer cleanup route."""
    hass, entry, store, _, _ = _sidecar_harness(harness)
    assert await recovery.async_load_recovery_state(hass, entry)
    runtime = hass.data['power_sync'][entry.entry_id]
    history = None
    if retired_history:
        old_route = _route('bridge_a', 'switch.car_a_charger', 'switch.fallback')
        old = await recovery.async_begin_attempt(
            hass, entry, VIN_A, old_route, 'start', old_route['entity_id'])
        assert old and await recovery.async_finish_attempt(old, 'sent_unknown')
        # Create a real pending quarantine, then retire it and its unknown
        # attempt through the normal stock-recovery evidence pipeline.
        assert not await recovery.async_isolate_ble(old, authorize=lambda: False)
        producer = dict(old.route['entity_identity'], vin=VIN_A, prefix='bridge_a',
                        producer_identity=old.route['producer_identity'])
        incident = await recovery.async_record_evidence_loss(
            hass, entry, kind='command_uncertainty', candidate_producers=[producer],
            candidate_vehicles=[VIN_A])
        state = runtime['_tesla_ev_recovery_sidecar']
        assert incident and await recovery.async_confirm_recovery_scope(
            hass, entry, incident['incident_id'], expected_revision=state['revision'],
            candidate_producers=[producer], candidate_vehicles=[VIN_A],
            operator_confirmation=True)
        uptimes = iter((100., 1., 2.))
        async def uptime():
            return {'uptime':next(uptimes), 'observed_at':time.time()+1, 'fresh':True}
        async def observe(_vin):
            return {'requested_current':10, 'charge_limit':80, 'charging':False, 'power':0,
                    'observed_at':time.time()+1, 'fresh':True,
                    'provenance':{'requested_current':'native', 'charge_limit':'native',
                                  'charging':'vehicle', 'power':'vehicle'}}
        assert await recovery.async_recover_stock(
            hass, entry, incident_id=incident['incident_id'], profile={
                'queue_persistence':'ram_only', 'host_replay':'none', 'exercised':True},
            identity=producer, restart=lambda:True, wait_disconnected=lambda:True,
            wait_connected=lambda:True, read_identity=lambda:producer,
            read_uptime=uptime, observe=observe)
        history = deepcopy(runtime[recovery.RECOVERY_KEY]['vehicles'][VIN_A])
        assert history['attempts'][old.attempt_id]['outcome']=='sent_unknown'
        assert history['attempts'][old.attempt_id]['recovery_retired']['historical_outcome']=='unknown'
        assert history['hazards']['ble:bridge_a']['recovery_retired']['historical_outcome']=='unknown'
    route = {
        "kind": kind,
        "entity_id": "switch.car_a_charger" if kind=='ble' else "switch.fallback",
        "cleanup_only": True,
        "cleanup_obligation_id": "old-obligation",
    }
    if kind=='ble':
        route['prefix']='bridge_a'
    ticket = await recovery.async_begin_attempt(
        hass, entry, VIN_A, route, "stop", route["entity_id"],
    )
    assert ticket is not None
    pending = deepcopy(
        store.value["recovery"]["vehicles"][VIN_A]["attempts"][ticket.attempt_id]
    )

    # Finish on a replacement runtime so the durable sidecar contains the
    # exact terminal proof while a late legacy projection still has the old
    # cleanup-only route and pending attempt.
    restored = _Hass({})
    restored.entity_registry = hass.entity_registry
    restored_runtime = {
        "automation_store": object(),
        recovery.RECOVERY_KEY: deepcopy(
            hass.data["power_sync"][entry.entry_id][recovery.RECOVERY_KEY]
        ),
        "_tesla_ev_recovery_enrolled": True,
        "_tesla_ev_recovery_sidecar_established": True,
        recovery._SIDECAR_STORE_KEY: store,
    }
    restored.data["power_sync"][entry.entry_id] = restored_runtime
    assert await recovery.async_load_recovery_state(restored, entry)
    loaded_record = restored_runtime[recovery.RECOVERY_KEY]["vehicles"][VIN_A]["attempts"][ticket.attempt_id]
    loaded_ticket = recovery.RecoveryTicket(
        restored, entry, VIN_A, ticket.attempt_id,
        deepcopy(loaded_record["route"]), restored_runtime,
    )
    assert await recovery.async_finish_attempt(loaded_ticket, outcome)
    assert store.value["recovery"]["terminal_removals"][VIN_A][ticket.attempt_id]
    assert restored_runtime[recovery.RECOVERY_KEY]["vehicles"][VIN_A]["route"] is None

    stale = deepcopy(restored_runtime[recovery.RECOVERY_KEY])
    stale_ledger = stale["vehicles"][VIN_A]
    stale_ledger["route"] = deepcopy(pending["route"])
    stale_ledger["attempts"][ticket.attempt_id] = pending
    reloaded = _Hass({})
    reloaded.entity_registry = hass.entity_registry
    reloaded_runtime = {
        "automation_store": object(),
        recovery.RECOVERY_KEY: stale,
        "_tesla_ev_recovery_enrolled": True,
        "_tesla_ev_recovery_sidecar_established": True,
        recovery._SIDECAR_STORE_KEY: store,
    }
    reloaded.data["power_sync"][entry.entry_id] = reloaded_runtime
    assert await recovery.async_load_recovery_state(reloaded, entry)
    ledger = reloaded_runtime[recovery.RECOVERY_KEY]["vehicles"][VIN_A]
    assert ledger["attempts"] == (history['attempts'] if history else {})
    if history:
        assert ledger['hazards']==history['hazards']
    assert ledger["route"] is None

    # A later startup can see the terminal proof again after an earlier merge
    # already removed the attempt, while a stale cleanup-only route remains in
    # the projected root.  The exact proof must clear that route idempotently;
    # an absent attempt alone must never do so.
    post_proof = deepcopy(stale)
    post_proof["vehicles"][VIN_A]["attempts"].pop(ticket.attempt_id, None)
    post_proof_runtime_hass = _Hass({})
    post_proof_runtime_hass.entity_registry = hass.entity_registry
    post_proof_runtime = {
        "automation_store": object(),
        recovery.RECOVERY_KEY: post_proof,
        "_tesla_ev_recovery_enrolled": True,
        "_tesla_ev_recovery_sidecar_established": True,
        recovery._SIDECAR_STORE_KEY: store,
    }
    post_proof_runtime_hass.data["power_sync"][entry.entry_id] = post_proof_runtime
    assert await recovery.async_load_recovery_state(
        post_proof_runtime_hass, entry
    )
    assert (
        post_proof_runtime[recovery.RECOVERY_KEY]["vehicles"][VIN_A]["route"]
        is None
    )

    # A sidecar-only terminal reconciliation can run while the legacy runtime
    # projection still carries the old cleanup route.  The next ordinary
    # persistence pass must copy the already-validated clear into that live
    # projection instead of resurrecting the stop-only route.
    post_proof_runtime[recovery.RECOVERY_KEY]["vehicles"][VIN_A]["route"] = deepcopy(
        stale["vehicles"][VIN_A]["route"]
    )
    projection_ref=post_proof_runtime[recovery.RECOVERY_KEY]
    ledger_ref=projection_ref['vehicles'][VIN_A]
    attempts_ref,hazards_ref=ledger_ref['attempts'],ledger_ref['hazards']
    assert await recovery.async_persist_recovery_state(
        post_proof_runtime_hass, entry
    )
    assert post_proof_runtime[recovery.RECOVERY_KEY] is projection_ref
    assert projection_ref['vehicles'][VIN_A] is ledger_ref
    assert ledger_ref['attempts'] is attempts_ref and ledger_ref['hazards'] is hazards_ref
    assert (
        post_proof_runtime[recovery.RECOVERY_KEY]["vehicles"][VIN_A]["route"]
        is None
    )

    fresh = await recovery.async_begin_attempt(
        post_proof_runtime_hass,
        entry,
        VIN_A,
        {"kind": "ble", "prefix": "bridge_a", "entity_id": "switch.car_a_charger"},
        "start",
        "switch.car_a_charger",
    )
    assert fresh is not None
    if history:
        live_history=post_proof_runtime[recovery.RECOVERY_KEY]['vehicles'][VIN_A]
        for attempt_id,record in history['attempts'].items():
            assert live_history['attempts'][attempt_id]==record
        assert live_history['hazards']==history['hazards']


@pytest.mark.asyncio
@pytest.mark.parametrize('case',['failed','cancelled','pending','hazard','other_vin'])
async def test_terminal_cleanup_projection_clear_requires_saved_proof_and_no_new_liability(harness,case):
    hass,entry,store,_,_=_sidecar_harness(harness)
    assert await recovery.async_load_recovery_state(hass,entry)
    route={'kind':'ble','prefix':'bridge_a','entity_id':'switch.car_a_charger',
           'cleanup_only':True,'cleanup_obligation_id':'old-cleanup'}
    ticket=await recovery.async_begin_attempt(hass,entry,VIN_A,route,'stop',route['entity_id'])
    assert ticket and await recovery.async_finish_attempt(ticket,'confirmed')
    runtime=hass.data['power_sync'][entry.entry_id]
    if case in ('pending','hazard'):
        new=await recovery.async_begin_attempt(hass,entry,VIN_A,route,'stop',route['entity_id'])
        assert new
        if case=='hazard':
            assert not await recovery.async_isolate_ble(new,authorize=lambda:False)
            assert await recovery.async_finish_attempt(new,'not_sent')
    if case=='other_vin':
        other_route=_route('bridge_b','switch.car_b_charger','switch.fallback_b')
        other=await recovery.async_begin_attempt(hass,entry,VIN_B,other_route,'start',other_route['entity_id'])
        assert other and await recovery.async_finish_attempt(other,'sent_unknown')
    projection=runtime[recovery.RECOVERY_KEY]
    ledger=projection['vehicles'][VIN_A]
    ledger['route']=deepcopy(ticket.route)
    other_ledger=projection['vehicles'].get(VIN_B)
    other_before=deepcopy(other_ledger)
    retained_route=ledger['route']
    if case=='failed':
        store.fail=True
        assert not await recovery.async_persist_recovery_state(hass,entry)
    elif case=='cancelled':
        entered,release=asyncio.Event(),asyncio.Event()
        save=store.async_save
        async def paused(value):
            entered.set()
            await release.wait()
            return await save(value)
        store.async_save=paused
        task=asyncio.create_task(recovery.async_persist_recovery_state(hass,entry))
        await asyncio.wait_for(entered.wait(),1)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        store.async_save=save
    else:
        assert await recovery.async_persist_recovery_state(hass,entry)
    assert runtime[recovery.RECOVERY_KEY] is projection
    assert projection['vehicles'][VIN_A] is ledger
    if case=='other_vin':
        assert ledger['route'] is None
        assert projection['vehicles'][VIN_B] is other_ledger
        assert other_ledger==other_before
    else:
        assert ledger['route'] is retained_route
        assert await recovery.async_begin_attempt(
            hass,entry,VIN_A,route,'start',route['entity_id']) is None
    if case in ('failed','cancelled'):
        store.fail=False
        assert await recovery.async_persist_recovery_state(hass,entry)
        assert ledger['route'] is None


@pytest.mark.asyncio
async def test_expired_scoped_not_sent_finish_survives_owner_revocation_and_reload(harness):
    hass, entry, store, _, owners = _sidecar_harness(harness)
    assert await recovery.async_load_recovery_state(hass, entry)
    route = {"kind": "ha", "entity_id": "switch.fallback"}
    ticket = await recovery.async_begin_attempt(
        hass,
        entry,
        VIN_A,
        route,
        "start",
        route["entity_id"],
        deadline=time.time() - 1,
    )
    assert ticket is not None
    assert ticket.route.get("session_scope")
    pending = deepcopy(
        store.value["recovery"]["vehicles"][VIN_A]["attempts"][ticket.attempt_id]
    )
    assert pending["original_deadline"] < time.time()

    # The provider admission was never handed over.  The owner can disappear
    # before the caller records that proven not_sent result; finish proof must
    # use the exact ticket/provider binding rather than minting new authority.
    owners.pop(VIN_A)
    assert await recovery.async_finish_attempt(ticket, "not_sent")
    live = hass.data["power_sync"][entry.entry_id][recovery.RECOVERY_KEY]
    live["vehicles"][VIN_A]["attempts"][ticket.attempt_id] = pending
    live["vehicles"][VIN_A]["route"] = None
    assert await recovery.async_persist_recovery_state(hass, entry)
    assert store.value["recovery"]["vehicles"][VIN_A]["attempts"] == {}

    restored = _Hass({})
    restored.entity_registry = hass.entity_registry
    restored_runtime = {
        "automation_store": object(),
        recovery.RECOVERY_KEY: deepcopy(live),
        "_tesla_ev_recovery_enrolled": True,
        "_tesla_ev_recovery_sidecar_established": True,
        recovery._SIDECAR_STORE_KEY: store,
    }
    restored.data["power_sync"][entry.entry_id] = restored_runtime
    assert await recovery.async_load_recovery_state(restored, entry)
    ledger = restored_runtime[recovery.RECOVERY_KEY]["vehicles"][VIN_A]
    assert ledger["attempts"] == {}
    assert recovery.get_vehicle_route(restored, entry, VIN_A) is None


@pytest.mark.asyncio
async def test_terminal_finish_store_failure_keeps_pending_and_retryable(harness):
    hass, entry, store, registrations, owners = _sidecar_harness(harness)
    registrations["number.fallback_limit"] = _Entity(
        "number.fallback_limit", device="native-device", entry="native-entry",
        platform="teslemetry",
    )
    owners.pop(VIN_A)
    assert await recovery.async_load_recovery_state(hass, entry)
    route = {"kind": "ha", "entity_id": "number.fallback_limit"}
    ticket = await recovery.async_begin_attempt(
        hass, entry, VIN_A, route, "limit", route["entity_id"],
    )
    assert ticket is not None
    before = deepcopy(store.value)
    store.fail = True
    assert not await recovery.async_finish_attempt(ticket, "confirmed")
    assert store.value == before
    runtime_ledger = hass.data["power_sync"][entry.entry_id][recovery.RECOVERY_KEY]["vehicles"][VIN_A]
    assert ticket.attempt_id in runtime_ledger["attempts"]
    assert "terminal_removals" not in recovery._sidecar_recovery(
        hass.data["power_sync"][entry.entry_id], before
    )
    store.fail = False
    assert await recovery.async_finish_attempt(ticket, "confirmed")


@pytest.mark.asyncio
async def test_terminal_proof_does_not_remove_rebound_attempt_record(harness):
    hass, entry, store, registrations, owners = _sidecar_harness(harness)
    registrations["number.fallback_limit"] = _Entity(
        "number.fallback_limit", device="native-device", entry="native-entry",
        platform="teslemetry",
    )
    owners.pop(VIN_A)
    assert await recovery.async_load_recovery_state(hass, entry)
    route = {"kind": "ha", "entity_id": "number.fallback_limit"}
    ticket = await recovery.async_begin_attempt(
        hass, entry, VIN_A, route, "limit", route["entity_id"],
    )
    assert ticket is not None
    pending = deepcopy(
        store.value["recovery"]["vehicles"][VIN_A]["attempts"][ticket.attempt_id]
    )
    assert await recovery.async_finish_attempt(ticket, "confirmed")

    # Reusing the ticket ID with a changed target is a new, unproven record;
    # the old finish proof must not remove it or clear its route fence.
    pending["target"] = {"percent": 99}
    live = hass.data["power_sync"][entry.entry_id][recovery.RECOVERY_KEY]
    live["vehicles"][VIN_A]["attempts"][ticket.attempt_id] = pending
    assert await recovery.async_persist_recovery_state(hass, entry)
    rebound = store.value["recovery"]["vehicles"][VIN_A]["attempts"]
    assert rebound[ticket.attempt_id]["target"] == {"percent": 99}


@pytest.mark.asyncio
async def test_automation_store_failure_preserves_durable_unknown_finish(harness):
    hass, entry, store, registrations, owners = _sidecar_harness(harness)
    registrations["number.fallback_limit"] = _Entity(
        "number.fallback_limit", device="native-device", entry="native-entry",
        platform="teslemetry",
    )
    owners.pop(VIN_A)
    assert await recovery.async_load_recovery_state(hass, entry)
    route = {"kind": "ha", "entity_id": "number.fallback_limit"}
    ticket = await recovery.async_begin_attempt(
        hass, entry, VIN_A, route, "limit", route["entity_id"],
    )
    assert ticket is not None
    hass.persist_ok = False
    assert not await recovery.async_finish_attempt(ticket, "sent_unknown")
    durable_record = store.value["recovery"]["vehicles"][VIN_A]["attempts"][ticket.attempt_id]
    assert durable_record["outcome"] == "sent_unknown"
    live_record = hass.data["power_sync"][entry.entry_id][recovery.RECOVERY_KEY]["vehicles"][VIN_A]["attempts"][ticket.attempt_id]
    assert live_record["outcome"] == "pending"

    restored = _Hass({})
    restored.entity_registry = hass.entity_registry
    restored_runtime = {
        "automation_store": object(),
        recovery.RECOVERY_KEY: deepcopy(
            hass.data["power_sync"][entry.entry_id][recovery.RECOVERY_KEY]
        ),
        "_tesla_ev_recovery_enrolled": True,
        "_tesla_ev_recovery_sidecar_established": True,
        recovery._SIDECAR_STORE_KEY: store,
    }
    restored.data["power_sync"][entry.entry_id] = restored_runtime
    assert await recovery.async_load_recovery_state(restored, entry)
    restored_record = restored_runtime[recovery.RECOVERY_KEY]["vehicles"][VIN_A]["attempts"][ticket.attempt_id]
    assert restored_record["outcome"] == "sent_unknown"
    assert await recovery.async_begin_attempt(
        restored, entry, VIN_A, route, "limit", route["entity_id"],
    ) is None


@pytest.mark.asyncio
async def test_stock_recovery_cancellation_returns_retryable_held_phase(harness):
    hass, entry, _, _, _ = _sidecar_harness(harness)
    assert await recovery.async_load_recovery_state(hass, entry)
    incident = await recovery.async_record_evidence_loss(
        hass, entry, candidate_producers=["bridge-a"], candidate_vehicles=[VIN_A]
    )
    state = hass.data["power_sync"][entry.entry_id]["_tesla_ev_recovery_sidecar"]
    assert await recovery.async_confirm_recovery_scope(
        hass, entry, incident["incident_id"], expected_revision=state["revision"],
        candidate_producers=["bridge-a"], candidate_vehicles=[VIN_A],
        operator_confirmation=True,
    )
    identity = {"config_entry_id": "ble-entry", "device_id": "bridge-a",
                "unique_id": "bridge-a", "platform": "esphome", "vin": VIN_A}

    async def _quiesce(*_args):
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await recovery.async_recover_stock(
            hass, entry, incident_id=incident["incident_id"],
            profile={"queue_persistence": "ram_only", "host_replay": "none", "exercised": True},
            identity=identity, restart=lambda: True, quiesce=_quiesce,
        )
    state = hass.data["power_sync"][entry.entry_id]["_tesla_ev_recovery_sidecar"]
    assert state["incidents"][incident["incident_id"]]["phase"] == "held"
    assert state["incidents"][incident["incident_id"]]["retryable"] is True


@pytest.mark.asyncio
async def test_malformed_established_sidecar_holds_positive_control(harness):
    hass, entry, store, _, _ = _sidecar_harness(harness)
    store.value = {
        "schema_version": recovery.SIDECAR_SCHEMA_VERSION,
        "revision": 2,
        "incidents": {}, "generations": {}, "liabilities": {},
        "obligations": {}, "submissions": {},
        "recovery": {"vehicles": {
            VIN_A: {"route": None, "attempts": {"broken": {}}, "hazards": {}}
        }},
    }
    assert not await recovery.async_load_recovery_state(hass, entry)
    assert not recovery.recovery_control_allowed(hass, entry, VIN_A, 0, "rate")
    assert recovery.recovery_control_allowed(hass, entry, VIN_A, 0, "stop")


@pytest.mark.asyncio
@pytest.mark.parametrize("field, value", [
    ("incidents", {"incident-1": {"phase": "not-a-recovery-phase"}}),
    ("generations", {VIN_A: "generation-1"}),
])
async def test_malformed_recovery_control_metadata_holds_positive_control(harness, field, value):
    hass, entry, store, _, _ = _sidecar_harness(harness)
    state = recovery._new_sidecar()
    state[field] = value
    store.value = state

    assert not await recovery.async_load_recovery_state(hass, entry)
    runtime = hass.data["power_sync"][entry.entry_id]
    assert runtime["_tesla_ev_recovery_sidecar_hold"] == "sidecar_malformed"
    assert not recovery.recovery_control_allowed(hass, entry, VIN_A, 0, "rate")
    assert recovery.recovery_control_allowed(hass, entry, VIN_A, 0, "stop")


@pytest.mark.asyncio
async def test_empty_incident_inventory_keeps_positive_control_held(harness):
    hass, entry, store, _, _ = _sidecar_harness(harness)
    state = recovery._new_sidecar()
    state["incidents"]["incident-unknown"] = {
        "incident_id": "incident-unknown",
        "phase": "held",
        "current_candidate_vehicles": [],
    }
    store.value = state

    assert await recovery.async_load_recovery_state(hass, entry)
    generation = recovery.current_vehicle_generation(hass, entry, VIN_A)
    assert not recovery.recovery_control_allowed(
        hass, entry, VIN_A, generation, "rate",
    )
    assert recovery.recovery_control_allowed(
        hass, entry, VIN_A, generation, "stop",
    )


@pytest.mark.asyncio
async def test_submission_ticket_survives_outer_cancellation_and_drains_child(harness):
    hass, entry, store, _, _ = _sidecar_harness(harness)
    assert await recovery.async_load_recovery_state(hass, entry)
    started = asyncio.Event()
    release = asyncio.Event()
    called = []

    async def _submit():
        called.append("submitted")
        started.set()
        await release.wait()
        return "accepted"

    state = hass.data["power_sync"][entry.entry_id]["_tesla_ev_recovery_sidecar"]
    task = asyncio.create_task(recovery.async_track_submission(
        hass, entry, _submit, 0, vehicle_id=VIN_A, operation="rate",
    ))
    await asyncio.wait_for(started.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert called == ["submitted"]
    assert any(item["status"] == "pending" for item in state["submissions"].values())
    release.set()
    assert await asyncio.wait_for(recovery.async_drain_submissions(hass, entry), timeout=1)
    for _ in range(10):
        if any(item["status"] == "completed" for item in state["submissions"].values()):
            break
        await asyncio.sleep(0)
    assert any(item["status"] == "completed" for item in state["submissions"].values())
    assert store.saves


@pytest.mark.asyncio
async def test_submission_drain_timeout_keeps_real_child_pending(harness):
    hass, entry, _, _, _ = _sidecar_harness(harness)
    assert await recovery.async_load_recovery_state(hass, entry)
    started = asyncio.Event()
    release = asyncio.Event()

    async def _submit():
        started.set()
        await release.wait()
        return "accepted"

    task = asyncio.create_task(recovery.async_track_submission(
        hass, entry, _submit, 0, vehicle_id=VIN_A, operation="rate",
    ))
    await asyncio.wait_for(started.wait(), timeout=1)
    assert not await recovery.async_drain_submissions(hass, entry, timeout=0.01)
    assert not task.done()
    release.set()
    assert await asyncio.wait_for(recovery.async_drain_submissions(hass, entry), timeout=1)
    assert await task == "accepted"


@pytest.mark.asyncio
async def test_submission_drain_vehicle_scope_does_not_wait_for_other_car(harness):
    hass, entry, _, _, _ = _sidecar_harness(harness)
    assert await recovery.async_load_recovery_state(hass, entry)
    started_a, started_b = asyncio.Event(), asyncio.Event()
    release_a, release_b = asyncio.Event(), asyncio.Event()

    async def _submit(started, release):
        started.set()
        await release.wait()
        return "accepted"

    task_a = asyncio.create_task(recovery.async_track_submission(
        hass, entry, lambda: _submit(started_a, release_a), 0,
        vehicle_id=VIN_A, operation="rate",
    ))
    task_b = asyncio.create_task(recovery.async_track_submission(
        hass, entry, lambda: _submit(started_b, release_b), 0,
        vehicle_id=VIN_B, operation="rate",
    ))
    await asyncio.wait_for(asyncio.gather(started_a.wait(), started_b.wait()), timeout=1)
    release_a.set()
    assert await asyncio.wait_for(
        recovery.async_drain_submissions(hass, entry, vehicle_ids=[VIN_A]), timeout=1
    )
    assert not task_b.done()
    release_b.set()
    assert await asyncio.wait_for(recovery.async_drain_submissions(hass, entry), timeout=1)
    assert await task_a == "accepted"
    assert await task_b == "accepted"


@pytest.mark.asyncio
async def test_command_uncertainty_covers_only_real_pending_attempt(harness):
    hass, entry, _, _, _ = _sidecar_harness(harness)
    assert await recovery.async_load_recovery_state(hass, entry)
    route = _route("bridge_a", "switch.car_a_charger", "switch.fallback")
    ticket = await recovery.async_begin_attempt(
        hass, entry, VIN_A, route, "start", route["entity_id"]
    )
    assert ticket is not None
    assert await recovery.async_finish_attempt(ticket, "sent_unknown")
    incident = await recovery.async_record_evidence_loss(
        hass, entry, kind="command_uncertainty",
        covered_attempts=[{"vehicle_id": VIN_A, "attempt_id": ticket.attempt_id}],
        candidate_producers=["bridge-a"], candidate_vehicles=[VIN_A],
    )
    assert incident and incident["kind"] == "command_uncertainty"
    assert incident["covered_attempts"][0]["attempt_id"] == ticket.attempt_id
    assert await recovery.async_record_evidence_loss(
        hass, entry, kind="command_uncertainty", covered_attempts=["missing"],
        candidate_producers=["bridge-a"], candidate_vehicles=[VIN_A],
    ) is None


@pytest.mark.asyncio
async def test_known_command_uncertainty_can_close_from_exact_recorded_binding(harness):
    hass, entry, _, _, _ = _sidecar_harness(harness)
    assert await recovery.async_load_recovery_state(hass, entry)
    route = _route("bridge_a", "switch.car_a_charger", "switch.fallback")
    ticket = await recovery.async_begin_attempt(
        hass, entry, VIN_A, route, "start", route["entity_id"]
    )
    assert ticket is not None
    assert await recovery.async_finish_attempt(ticket, "sent_unknown")
    identity = ticket.route["entity_identity"]
    producer = dict(identity, vin=VIN_A, prefix="bridge_a",
                    producer_identity=ticket.route["producer_identity"])
    incident = await recovery.async_record_evidence_loss(
        hass, entry, kind="command_uncertainty",
        covered_attempts=[{"vehicle_id": VIN_A, "attempt_id": ticket.attempt_id}],
        candidate_producers=[producer], candidate_vehicles=[VIN_A],
    )
    assert incident is not None
    state = hass.data["power_sync"][entry.entry_id]["_tesla_ev_recovery_sidecar"]
    assert await recovery.async_confirm_recovery_scope(
        hass, entry, incident["incident_id"], expected_revision=state["revision"],
        candidate_producers=[producer], candidate_vehicles=[VIN_A],
        operator_confirmation=False, automatic=True,
    )
    state = hass.data["power_sync"][entry.entry_id]["_tesla_ev_recovery_sidecar"]
    selected = state["incidents"][incident["incident_id"]]
    assert selected["scope_closed"] is True
    assert selected["scope_basis"] == "known_journal_and_registry"
    assert selected["operator_scope_confirmation"]["confirmed_by"] == "recorded_binding"


@pytest.mark.asyncio
async def test_recovery_gate_is_scoped_to_incident_vehicles(harness):
    hass, entry, _, _, _ = _sidecar_harness(harness)
    assert await recovery.async_load_recovery_state(hass, entry)
    incident = await recovery.async_record_evidence_loss(
        hass, entry, candidate_producers=["bridge-a"], candidate_vehicles=[VIN_A]
    )
    assert incident
    assert not recovery.recovery_control_allowed(hass, entry, VIN_A, 1, "rate")
    assert recovery.recovery_control_allowed(hass, entry, VIN_B, 0, "rate")


@pytest.mark.asyncio
async def test_submission_final_gate_denies_after_ticket_save(harness):
    hass, entry, store, _, _ = _sidecar_harness(harness)
    assert await recovery.async_load_recovery_state(hass, entry)
    runtime = hass.data["power_sync"][entry.entry_id]
    original_save = store.async_save
    save_count = 0

    async def save_and_revoke(value):
        nonlocal save_count
        save_count += 1
        result = await original_save(value)
        if save_count == 1:
            runtime["_tesla_ev_recovery_sidecar"]["incidents"]["revoked"] = {
                "phase": "held", "current_candidate_vehicles": [VIN_A],
            }
        return result

    store.async_save = save_and_revoke
    called = []

    async def _submit():
        called.append(True)

    with pytest.raises(recovery.RecoverySubmissionDenied) as error:
        await recovery.async_track_submission(
            hass, entry, _submit, 0, vehicle_id=VIN_A, operation="rate",
        )
    assert error.value.ev_recovery_not_submitted is True
    assert not called


@pytest.mark.asyncio
async def test_stock_recovery_rejects_multi_producer_scope(harness):
    hass, entry, _, _, _ = _sidecar_harness(harness)
    assert await recovery.async_load_recovery_state(hass, entry)
    incident = await recovery.async_record_evidence_loss(
        hass, entry, candidate_producers=["bridge-a", "bridge-b"], candidate_vehicles=[VIN_A]
    )
    state = hass.data["power_sync"][entry.entry_id]["_tesla_ev_recovery_sidecar"]
    assert await recovery.async_confirm_recovery_scope(
        hass, entry, incident["incident_id"], expected_revision=state["revision"],
        candidate_producers=["bridge-a", "bridge-b"], candidate_vehicles=[VIN_A],
        operator_confirmation=True,
    )
    assert not await recovery.async_recover_stock(
        hass, entry, incident_id=incident["incident_id"],
        profile={"queue_persistence": "ram_only", "host_replay": "none", "exercised": True},
        identity={"config_entry_id": "ble-entry", "device_id": "bridge-a",
                  "unique_id": "bridge-a", "platform": "esphome", "vin": VIN_A},
    )


def test_stock_scope_structured_producer_requires_exact_route_identity():
    identity = {
        "platform": "esphome", "config_entry_id": "ble-entry",
        "device_id": "ble-device", "unique_id": "switch.car_a_charger",
    }
    ref = {"route": {"kind": "ble", "prefix": "bridge_a",
                      "entity_identity": identity}}
    producer = dict(identity, producer_identity="esphome:ble-entry:ble-device:switch.car_a_charger")
    assert recovery._liability_matches_stock_scope(ref, producer)
    assert not recovery._liability_matches_stock_scope(
        ref, dict(producer, device_id="another-device")
    )
    assert not recovery._producer_scope_matches(
        [{"device_id": "ble-device"}], dict(identity, vin=VIN_A)
    )


@pytest.mark.asyncio
async def test_stock_recovery_retires_covered_route_but_keeps_unknown_outcome(harness):
    hass, entry, store, _, _ = _sidecar_harness(harness)
    assert await recovery.async_load_recovery_state(hass, entry)
    route = _route("bridge_a", "switch.car_a_charger", "switch.fallback")
    ticket = await recovery.async_begin_attempt(
        hass, entry, VIN_A, route, "start", route["entity_id"]
    )
    assert ticket and await recovery.async_finish_attempt(ticket, "sent_unknown")
    incident = await recovery.async_record_evidence_loss(
        hass, entry, kind="command_uncertainty",
        covered_attempts=[{"vehicle_id": VIN_A, "attempt_id": ticket.attempt_id}],
        candidate_producers=["bridge-a"], candidate_vehicles=[VIN_A],
    )
    state = hass.data["power_sync"][entry.entry_id]["_tesla_ev_recovery_sidecar"]
    assert await recovery.async_confirm_recovery_scope(
        hass, entry, incident["incident_id"], expected_revision=state["revision"],
        candidate_producers=["bridge-a"], candidate_vehicles=[VIN_A],
        operator_confirmation=True,
    )
    identity = {"config_entry_id": "ble-entry", "device_id": "bridge-a",
                "unique_id": "bridge-a", "platform": "esphome", "vin": VIN_A}
    uptime = iter((100.0, 1.0, 2.0))
    async def _uptime():
        value = next(uptime)
        return {"uptime": value, "observed_at": time.time() + 1, "fresh": True}
    async def _observe(_vin):
        return {"requested_current": 10, "charge_limit": 80, "charging": True, "power": 2,
                "observed_at": time.time() + 1, "fresh": True,
                "provenance": {"requested_current": "native", "charge_limit": "native",
                                "charging": "vehicle", "power": "vehicle"}}
    assert await recovery.async_recover_stock(
        hass, entry, incident_id=incident["incident_id"], profile={
            "queue_persistence": "ram_only", "host_replay": "none", "exercised": True,
        }, identity=identity, restart=lambda: True,
        wait_disconnected=lambda: True, wait_connected=lambda: True,
        read_identity=lambda: identity, read_uptime=_uptime, observe=_observe,
    )
    record = hass.data["power_sync"][entry.entry_id][recovery.RECOVERY_KEY]
    attempt = record["vehicles"][VIN_A]["attempts"][ticket.attempt_id]
    assert attempt["outcome"] == "sent_unknown"
    assert attempt["recovery_retired"]["historical_outcome"] == "unknown"
    durable_attempt = store.value["recovery"]["vehicles"][VIN_A]["attempts"][ticket.attempt_id]
    assert durable_attempt["outcome"] == "sent_unknown"
    assert durable_attempt["recovery_retired"]["historical_outcome"] == "unknown"
    assert recovery.get_vehicle_route(hass, entry, VIN_A) is None


def test_hazard_projection_conflict_keeps_new_same_prefix_identity_unretired():
    """A newer hazard must not inherit retirement from a stable-key predecessor."""
    binding = {
        "entities": {"charger": {
            "platform": "esphome", "config_entry_id": "ble-entry",
            "device_id": "bridge-a", "unique_id": "charger-a",
        }}
    }
    old = {
        "kind": "ble_quarantine", "state": "retired", "prefix": "bridge-a",
        "attempt_id": "old-attempt", "binding": binding,
        "producer_identity": "esphome:ble-entry:bridge-a:charger-a",
        "recovery_retired": {"incident_id": "old-incident"},
    }
    new = {
        "kind": "ble_quarantine", "state": "pending", "prefix": "bridge-a",
        "attempt_id": "new-attempt", "binding": binding,
        "producer_identity": "esphome:ble-entry:bridge-a:charger-a",
    }
    merged = recovery._merge_recovery_roots(
        {"vehicles": {VIN_A: {"route": None, "attempts": {},
                               "hazards": {"ble:bridge-a": old}}}},
        {"vehicles": {VIN_A: {"route": None, "attempts": {},
                               "hazards": {"ble:bridge-a": new}}}},
        prefer_legacy_mutable=True,
    )
    hazards = merged["vehicles"][VIN_A]["hazards"]
    assert hazards["ble:bridge-a"]["attempt_id"] == "new-attempt"
    assert "recovery_retired" not in hazards["ble:bridge-a"]
    assert any(
        item.get("attempt_id") == "old-attempt"
        and item.get("recovery_retired", {}).get("incident_id") == "old-incident"
        for item in hazards.values()
    )
    repeated = recovery._merge_recovery_roots(merged, {
        "vehicles": {VIN_A: {"route": None, "attempts": {},
                               "hazards": {"ble:bridge-a": new}}}},
        prefer_legacy_mutable=True,
    )
    assert sorted(repeated["vehicles"][VIN_A]["hazards"]) == sorted(hazards)


def test_missing_hazard_attempt_identity_cannot_transfer_retirement_marker():
    old = {
        "kind": "ble_quarantine", "state": "retired", "prefix": "bridge-a",
        "binding": {"entities": {"charger": {
            "platform": "esphome", "config_entry_id": "ble-entry",
            "device_id": "bridge-a", "unique_id": "charger-a",
        }}}, "recovery_retired": {"incident_id": "old-incident"},
    }
    new = {**old, "state": "pending", "attempt_id": "new-attempt"}
    new.pop("recovery_retired")
    merged = recovery._merge_recovery_roots(
        {"vehicles": {VIN_A: {"route": None, "attempts": {},
                               "hazards": {"ble:bridge-a": old}}}},
        {"vehicles": {VIN_A: {"route": None, "attempts": {},
                               "hazards": {"ble:bridge-a": new}}}},
        prefer_legacy_mutable=True,
    )
    assert "recovery_retired" not in merged["vehicles"][VIN_A]["hazards"]["ble:bridge-a"]
    repeated = recovery._merge_recovery_roots(
        merged,
        {"vehicles": {VIN_A: {"route": None, "attempts": {},
                               "hazards": {"ble:bridge-a": new}}}},
        prefer_legacy_mutable=True,
    )
    assert sorted(repeated["vehicles"][VIN_A]["hazards"]) == sorted(
        merged["vehicles"][VIN_A]["hazards"]
    )
    assert any(item.get("recovery_retired", {}).get("incident_id") == "old-incident"
               for item in repeated["vehicles"][VIN_A]["hazards"].values())


def test_late_retirement_for_old_hazard_cannot_retire_reused_prefix():
    runtime = {recovery.RECOVERY_KEY: {"vehicles": {VIN_A: {
        "route": None, "attempts": {"new-attempt": {"outcome": "sent_unknown"}}, "hazards": {
            "ble:bridge-a": {
                "kind": "ble_quarantine", "state": "pending",
                "prefix": "bridge-a", "attempt_id": "new-attempt",
                "producer_identity": "esphome:ble-entry:bridge-a:charger-a",
            }
        },
    }}}}
    recovery._retire_recovery_liabilities(
        runtime, [{"vehicle_id": VIN_A, "hazard_id": "ble:bridge-a",
                   "attempt_id": "old-attempt", "prefix": "bridge-a",
                   "producer_identity": "esphome:ble-entry:bridge-a:charger-a"}],
        "old-incident", {"operation": "stop"},
    )
    hazard = runtime[recovery.RECOVERY_KEY]["vehicles"][VIN_A]["hazards"]["ble:bridge-a"]
    assert "recovery_retired" not in hazard
    assert "recovery_retired" not in runtime[recovery.RECOVERY_KEY]["vehicles"][VIN_A]["attempts"]["new-attempt"]

    legacy_ref_runtime = deepcopy(runtime)
    recovery._retire_recovery_liabilities(
        legacy_ref_runtime, [{"vehicle_id": VIN_A, "hazard_id": "ble:bridge-a",
                              "prefix": "bridge-a",
                              "producer_identity": "esphome:ble-entry:bridge-a:charger-a"}],
        "legacy-incident", {"operation": "stop"},
    )
    assert "recovery_retired" not in legacy_ref_runtime[recovery.RECOVERY_KEY]["vehicles"][VIN_A]["hazards"]["ble:bridge-a"]
