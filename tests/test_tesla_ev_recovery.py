"""Focused durability and fail-closed tests for Tesla transport recovery."""
from __future__ import annotations

import asyncio
from contextlib import suppress
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
import sys
import types

import pytest


ROOT = Path(__file__).resolve().parents[1] / "custom_components" / "power_sync"
package = sys.modules.setdefault("power_sync", types.ModuleType("power_sync"))
package.__path__ = [str(ROOT)]

from power_sync import tesla_ev_recovery as recovery  # noqa: E402


VIN_A = "5YJ3E1EA7NF0000A1"
VIN_B = "5YJ3E1EA7NF0000B2"


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
            stamp = datetime.now(timezone.utc)
            for entity_id in ("switch.bridge_a_ble_connection", "binary_sensor.bridge_a_ble_status"):
                self.hass.states_by_id[entity_id] = _State(entity_id, "off", stamp)


class _Hass:
    __slots__ = ("data", "states_by_id", "states", "entity_registry", "services", "persist_ok", "persisted")

    def __init__(self, registrations):
        self.data = {"power_sync": {"entry-1": {"automation_store": object()}}}
        self.states_by_id = {}
        self.states = types.SimpleNamespace(get=self.states_by_id.get)
        self.entity_registry = types.SimpleNamespace(async_get=registrations.get)
        self.persist_ok = True
        self.persisted = []
        self.services = _Services(self)


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
        "switch.bridge_b_ble_connection": _Entity("switch.bridge_b_ble_connection", device="ble-b", entry="ble-entry-b"),
        "binary_sensor.bridge_b_ble_status": _Entity("binary_sensor.bridge_b_ble_status", device="ble-b", entry="ble-entry-b"),
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
    assert hass.services.calls == [("switch", "turn_off", {"entity_id": "switch.bridge_a_ble_connection"}, True)]
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
    monkeypatch.setattr(recovery, "BLE_ISOLATION_TIMEOUT", 0.2)
    owners.pop(VIN_A)
    pending = recovery.pending_ble_attempt(restored, entry, VIN_A)
    assert pending is not None
    assert pending.runtime_ref is restored.data["power_sync"][entry.entry_id]
    assert await recovery.async_isolate_ble(pending, authorize=True)
    assert recovery.get_vehicle_route(restored, entry, VIN_A)["kind"] == "ha"
    assert recovery.ble_quarantined(restored, "bridge_a")
    assert all(call[1] == "turn_off" for call in restored.services.calls)
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
