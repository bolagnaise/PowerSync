"""Whole-inventory Stop recovery, using the real durable recovery manager."""
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import types
import time

import pytest
import pytest_asyncio

from test_tesla_ev_recovery import (harness, _sidecar_harness, _Entity, _State,
    VIN_A, VIN_B, recovery)
from power_sync import tesla_ble_stop_recovery as stop
from test_tesla_ev_recovery import _seed_legacy_quarantine


@pytest_asyncio.fixture
async def inventory(harness, monkeypatch):
    hass, entry, store, registrations, owners = _sidecar_harness(harness)
    # Standalone tests replace packages during collection. Pin the real module
    # and mapping dependency used by this test's runtime, not another fake.
    monkeypatch.setitem(sys.modules, "power_sync.tesla_ev_recovery", recovery)
    monkeypatch.setattr(sys.modules["power_sync"], "tesla_ev_recovery", recovery, raising=False)
    from power_sync import tesla_ble_mapping as mapping
    monkeypatch.setattr(mapping, "resolve_ble_prefixes", lambda *_: ["bridge_a", "bridge_b"])
    monkeypatch.setattr(stop, "POLL_SECONDS", 0.001)
    monkeypatch.setattr(stop, "TIMEOUT", 0.04)
    # Protocol deadlines advance deterministically with polling rather than
    # the host's CPU load. The native sample timestamps and all assertions
    # still exercise the production verifier; a slow full-suite worker cannot
    # consume a 40ms fixture budget during exact inventory validation.
    ticks = [0.0]
    def monotonic():
        ticks[0] += 0.001
        return ticks[0]
    monkeypatch.setattr(stop, "time", types.SimpleNamespace(
        time=time.time, monotonic=monotonic))
    entry.data.update(ev_provider="tesla_ble", tesla_ble_entity_prefix="bridge_a,bridge_b")
    config_entries = {}
    receipts, aliases = {}, {"bridge_a": "ble_bridge_a", "bridge_b": "ble_bridge_b"}
    declared = [{"prefix": "bridge_a", "vin": VIN_A}, {"prefix": "bridge_b", "vin": VIN_B}]
    for index, item in enumerate(declared):
        prefix = item["prefix"]
        for entity_id in (f"switch.{prefix}_charger", f"sensor.{prefix}_powersync_recovery_proof",
                          f"text.{prefix}_powersync_recovery_request", f"switch.{prefix}_ble_connection",
                          f"binary_sensor.{prefix}_ble_status"):
            registrations[entity_id] = _Entity(entity_id, device=prefix, entry=prefix)
        registrations.pop(f"sensor.{prefix}_transport_fence", None)
        registrations.pop(f"text.{prefix}_transport_isolation_request", None)
        config_entries[prefix] = types.SimpleNamespace(runtime_data=types.SimpleNamespace(
            client=types.SimpleNamespace(is_connected=True)))
        receipts[prefix] = dict(v=2,b=str(index+1)*16,o="",n="",g=0,m="i",i=item["vin"],s=0,t=0)
        owners[aliases[prefix]] = {"owner": "scheduler", "owner_mode": "scheduled", "session_id": prefix,
                                   "ownership_generation": 1}
    hass.config_entries = types.SimpleNamespace(async_get_entry=config_entries.get)
    assert await recovery.async_load_recovery_state(hass, entry)
    for item in declared:
        prefix = item["prefix"]
        route = {"kind": "ble", "prefix": prefix, "entity_id": f"switch.{prefix}_charger"}
        # Sanitized ticket-150 shape: 14 + 4 unknown Stops, 14 + 3 pending
        # quarantines, synthetic identities and no old producer fence.
        count = 14 if prefix == "bridge_a" else 4
        for index in range(count):
            ticket = await recovery.async_begin_attempt(hass, entry, aliases[prefix], route, "stop", route["entity_id"])
            assert ticket
            assert await recovery.async_finish_attempt(ticket, "sent_unknown")
            if prefix == "bridge_a" or index < 3:
                await _seed_legacy_quarantine(ticket)
                ledger = hass.data["power_sync"][entry.entry_id][recovery.RECOVERY_KEY]["vehicles"][aliases[prefix]]
                ledger["hazards"][f"retained-{index}"] = ledger["hazards"].pop(f"ble:{prefix}")
                # Assemble the sanitized historical fixture with distinct
                # retained IDs in both stores, rather than simulate a live
                # stable-key rename that intentionally preserves both rows.
                hass.data["power_sync"][entry.entry_id][recovery._SIDECAR_STATE_KEY]["recovery"]["vehicles"][aliases[prefix]]["hazards"] = deepcopy(ledger["hazards"])
                assert await recovery._persist(hass,entry,hass.data["power_sync"][entry.entry_id])
    runtime = hass.data["power_sync"][entry.entry_id]
    calls, frozen = [], set()
    hook = {"call": None, "read": None}

    def read(entity_id):
        if entity_id.endswith("_powersync_recovery_proof"):
            prefix = entity_id[7:-len("_powersync_recovery_proof")]
            receipt = receipts[prefix]
            if hook["read"]:
                hook["read"](prefix, receipt)
            if receipt["m"] in {"q", "v"} and prefix not in frozen:
                receipt.update(s=receipt["s"]+1,t=receipt["t"]+1500)
                if receipt["s"] >= 2:
                    receipt.update(m="v",a=0,w=0,c="S")
            return _State(entity_id, json.dumps(receipt, separators=(",", ":")), datetime.now(timezone.utc))
        return hass.states_by_id.get(entity_id)

    async def call(domain, service, data, *, blocking):
        assert domain == "text" and service == "set_value"
        prefix = data["entity_id"][5:-len("_powersync_recovery_request")]
        action, nonce = data["value"].split(":")
        calls.append((prefix, action, nonce))
        if hook["call"]:
            await hook["call"](prefix, action)
        receipt = receipts[prefix]
        if action == "prepare":
            receipt.update(n=nonce,o=receipt["b"],b="f"*15+str(len(calls)%10),g=receipt["g"]+1,m="q",s=0,t=0)
        elif action == "commit":
            # The real saved sidecar must release ALL records before a bridge
            # can be unlocked. A final-save failure cannot unlock even one.
            assert all(x["phase"] == "released" for x in store.value["incidents"].values())
            assert not stop._live_refs(runtime)
            receipt["m"] = "r"
        elif action == "hold":
            receipt["m"] = "h"
    hass.states = types.SimpleNamespace(get=read)
    hass.services = types.SimpleNamespace(async_call=call)
    return types.SimpleNamespace(hass=hass,entry=entry,store=store,registrations=registrations,owners=owners,
        runtime=runtime,receipts=receipts,aliases=aliases,declared=declared,calls=calls,frozen=frozen,
        hook=hook,config_entries=config_entries)


async def recover(case):
    return await stop.async_recover_ble_stops(case.hass,case.entry,case.declared,confirm_complete_history=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["exact_proof", "wrong_producer", "recovery_started", "write_failure"])
async def test_unstarted_inventory_for_unsent_start_does_not_block_other_bridge(
    inventory, case,
):
    c = inventory
    assert (await recover(c))["success"]
    c.calls.clear()

    first, second = c.declared
    first_alias, second_alias = c.aliases[first["prefix"]], c.aliases[second["prefix"]]
    first_charger = f"switch.{first['prefix']}_charger"
    first_route = {"kind": "ble", "prefix": first["prefix"], "entity_id": first_charger}
    pending = await recovery.async_begin_attempt(
        c.hass, c.entry, first_alias, first_route, "start", first_charger,
    )
    assert pending
    bridge = stop.Bridge(c.hass, c.entry, first["prefix"], first["vin"])
    incident = await recovery.async_record_evidence_loss(
        c.hass, c.entry,
        covered_attempts=stop._live_refs(c.runtime),
        candidate_producers=[bridge.identity],
        candidate_vehicles=[first_alias],
        scope_basis={
            "kind": "ble_command_inventory_v2",
            "inventory_digest": recovery._digest([first]),
            "historical_identity_reconstructed": False,
            "complete_affected_inventory": [first],
        },
    )
    assert incident and await recovery.async_finish_attempt(pending, "not_sent")
    sidecar = c.runtime[recovery._SIDECAR_STATE_KEY]
    if case == "wrong_producer":
        removals = sidecar["recovery"]["terminal_removals"]
        removals[first_alias][pending.attempt_id]["route"]["prefix"] = second["prefix"]
    elif case == "recovery_started":
        sidecar["incidents"][incident["incident_id"]]["phase_token"] = "interrupted-prepare"

    second_charger = f"switch.{second['prefix']}_charger"
    second_route = {"kind": "ble", "prefix": second["prefix"], "entity_id": second_charger}
    retained = await recovery.async_begin_attempt(
        c.hass, c.entry, second_alias, second_route, "stop", second_charger,
    )
    assert retained and await recovery.async_finish_attempt(retained, "sent_unknown")

    async def recover_second():
        return await stop.async_recover_ble_stops(
            c.hass, c.entry, [second], confirm_complete_history=True,
        )

    if case in {"wrong_producer", "recovery_started"}:
        with pytest.raises(stop.StopRecoveryRefused, match="another_recovery_scope_is_held"):
            await recover_second()
        assert c.calls == []
        assert c.store.value["incidents"][incident["incident_id"]]["phase"] == "held"
        return

    if case == "write_failure":
        original_save = c.store.async_save

        async def fail_settlement(value):
            if value["incidents"][incident["incident_id"]]["phase"] == "released":
                return False
            return await original_save(value)

        c.store.async_save = fail_settlement
        with pytest.raises(stop.StopRecoveryRefused, match="recovery_scope_not_persisted"):
            await recover_second()
        assert c.calls == []
        assert sidecar["incidents"][incident["incident_id"]]["phase"] == "held"
        assert c.store.value["incidents"][incident["incident_id"]]["phase"] == "held"
        c.store.async_save = original_save

    assert (await recover_second())["success"]
    assert [(prefix, action) for prefix, action, _ in c.calls] == [
        (second["prefix"], "prepare"), (second["prefix"], "commit"),
    ]
    settled = c.store.value["incidents"][incident["incident_id"]]
    assert settled["phase"] == "released"
    assert settled["historical_outcome"] == "not_sent"
    assert settled["liability_refs"] == incident["liability_refs"]
    assert not stop._live_refs(c.runtime)


@pytest.mark.asyncio
async def test_full_two_alias_inventory_retires_exact_unknown_stops_and_survives_reload(inventory):
    c = inventory
    before = deepcopy(c.runtime[recovery.RECOVERY_KEY])
    assert sum(len(x["attempts"]) for x in before["vehicles"].values()) == 18
    assert sum(len(x["hazards"]) for x in before["vehicles"].values()) == 17
    result = await recover(c)
    assert result["success"]
    assert [x[1] for x in c.calls] == ["prepare", "prepare", "commit", "commit"]
    incident = c.store.value["incidents"][result["incident_id"]]
    assert incident["historical_outcome"] == "unknown"
    assert incident["scope_basis"]["historical_identity_reconstructed"] is False
    assert incident["scope_basis"]["complete_affected_inventory"] == c.declared
    assert incident["operator_scope_confirmation"]["confirmed_by"] == "operator"
    assert set(c.store.value["recovery"]["vehicles"]) == set(c.aliases.values())
    for vid, old in before["vehicles"].items():
        ledger = c.store.value["recovery"]["vehicles"][vid]
        assert ledger["route"] is None
        for aid, record in ledger["attempts"].items():
            assert record["outcome"] == old["attempts"][aid]["outcome"] == "sent_unknown"
            assert record["recovery_retired"]["incident_id"] == result["incident_id"]
    assert not stop._live_refs(c.runtime)
    assert stop.ACTIVE_KEY not in c.runtime
    c.runtime.pop(recovery.RECOVERY_KEY)
    c.runtime.pop("_tesla_ev_recovery_sidecar_loaded")
    assert await recovery.async_load_recovery_state(c.hass,c.entry)
    assert not stop._live_refs(c.runtime)
    # Repeating the service never resets a completed bridge or fabricates a
    # second historical incident.
    assert (await recover(c))["incident_id"] == result["incident_id"]
    assert len(c.calls) == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("exact", [True, False])
async def test_existing_stock_hold_can_change_method_only_for_its_recorded_producer(inventory, monkeypatch, exact):
    c = inventory
    bridge = stop.Bridge(c.hass, c.entry, "bridge_a", VIN_A)
    route = {"kind": "ble", "prefix": "bridge_a", "entity_identity": bridge.identity}
    refs = [{"operation": "rate", "attempt_id": "old-rate", "vehicle_id": VIN_A,
             "outcome": "sent_unknown", "route": route}]
    producer = {**bridge.identity, "prefix": "bridge_a", "vin": VIN_A}
    if not exact:
        producer["device_id"] = "replaced-device"
    candidate = {
        "incident_id": "old-stock", "kind": "command_uncertainty", "phase": "held",
        "scope_basis": "known_journal_and_registry", "scope_closed": True,
        "current_candidate_producers": [producer], "current_candidate_vehicles": [VIN_A],
        "liability_refs": refs,
    }
    state = c.runtime[recovery._SIDECAR_STATE_KEY]
    state["incidents"]["old-stock"] = deepcopy(candidate)
    monkeypatch.setattr(stop, "_live_refs", lambda runtime: refs)
    scope = {"kind": "ble_command_inventory_v2"}
    async def adopt():
        await stop._adopt_held_stock_scope(
            c.hass, c.entry, c.runtime, candidate, state["revision"], scope,
            {"bridge_a": bridge}, {"bridge_a": VIN_A}, refs,
        )

    if not exact:
        with pytest.raises(stop.StopRecoveryRefused, match="another_recovery_scope"):
            await adopt()
        assert state["incidents"]["old-stock"] == candidate
    else:
        await adopt()
        updated = state["incidents"]["old-stock"]
        assert updated["scope_basis"] == scope
        assert updated["prior_stock_scope"]["scope_basis"] == "known_journal_and_registry"
        assert updated["liability_refs"] == refs
        assert updated["phase"] == "held" and updated["scope_closed"] is False
    assert c.calls == []


@pytest.mark.asyncio
async def test_operator_confirmation_required_before_any_write(inventory):
    c = inventory
    with pytest.raises(stop.StopRecoveryRefused,match="confirmation_required"):
        await stop.async_recover_ble_stops(c.hass,c.entry,c.declared)
    assert c.calls == []
    assert c.store.value["incidents"] == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("variant", ["missing_vehicle", "duplicate_vin", "wrong_vin", "missing_entity", "rebound_entity", "positive", "orphan_hazard"])
async def test_incomplete_or_contradictory_inventory_never_prepares(inventory, variant):
    c = inventory
    if variant == "missing_vehicle": c.declared.pop()
    elif variant == "duplicate_vin": c.declared[1]["vin"] = VIN_A
    elif variant == "wrong_vin": c.declared[0]["vin"] = "5YJ3E1EA7NF0000C3"
    elif variant == "missing_entity": c.registrations.pop("text.bridge_b_powersync_recovery_request")
    elif variant == "rebound_entity": c.registrations["switch.bridge_a_charger"].unique_id = "other"
    else:
        ledger = c.runtime[recovery.RECOVERY_KEY]["vehicles"][c.aliases["bridge_a"]]
        if variant == "positive": next(iter(ledger["attempts"].values()))["operation"] = "start"
        else: next(iter(ledger["hazards"].values()))["attempt_id"] = "missing"
    with pytest.raises(stop.StopRecoveryRefused): await recover(c)
    assert c.calls == []
    assert any(ledger["attempts"] for ledger in c.runtime[recovery.RECOVERY_KEY]["vehicles"].values())


@pytest.mark.asyncio
@pytest.mark.parametrize("variant", ["old_boot", "old_nonce", "wrong_vin", "not_zero", "missing_power", "duplicate_sequence", "second_car_stale"])
async def test_replayed_or_incomplete_native_proof_retires_nothing(inventory, variant):
    c = inventory
    c.frozen.add("bridge_b" if variant == "second_car_stale" else "bridge_a")
    def poison(prefix, receipt):
        if prefix != "bridge_a" or receipt["m"] not in {"q", "v"}: return
        receipt.update(m="v",s=2,t=1500,a=0,w=0,c="S")
        if variant == "old_boot": receipt["b"] = receipt["o"]
        elif variant == "old_nonce": receipt["n"] = "0"*32
        elif variant == "wrong_vin": receipt["i"] = VIN_B
        elif variant == "not_zero": receipt["a"] = 1
        elif variant == "missing_power": receipt.pop("w",None)
    if variant != "second_car_stale": c.hook["read"] = poison
    with pytest.raises(stop.StopRecoveryRefused): await recover(c)
    assert not any(x[1] == "commit" for x in c.calls)
    assert all(x["phase"] == "held" for x in c.store.value["incidents"].values())
    assert stop._live_refs(c.runtime)
    assert all(not row.get("recovery_retired") for ledger in c.runtime[recovery.RECOVERY_KEY]["vehicles"].values() for row in ledger["attempts"].values())


@pytest.mark.asyncio
@pytest.mark.parametrize("variant", ["owner", "config", "registry", "runtime", "generation", "phase_token", "new_liability", "stop"])
async def test_scope_change_during_prepare_aborts_all_retirement(inventory, variant):
    c = inventory
    async def change(prefix,action):
        if action != "prepare": return
        if variant == "owner": c.owners[c.aliases[prefix]]["ownership_generation"] += 1
        elif variant == "config": c.entry.options = {"new": True}
        elif variant == "registry": c.registrations[f"switch.{prefix}_charger"].unique_id = "rebound"
        elif variant == "runtime": c.config_entries[prefix].runtime_data = types.SimpleNamespace(client=object())
        elif variant == "generation": c.runtime[recovery._SIDECAR_STATE_KEY]["generations"][c.aliases[prefix]] += 1
        elif variant == "phase_token": next(iter(c.runtime[recovery._SIDECAR_STATE_KEY]["incidents"].values()))["phase_token"] = "foreign"
        elif variant == "stop": c.runtime[stop.ACTIVE_KEY]["interrupted"] = True
        else:
            ledger = c.runtime[recovery.RECOVERY_KEY]["vehicles"][c.aliases[prefix]]
            ledger["attempts"]["new"] = {**deepcopy(next(iter(ledger["attempts"].values()))),"attempt_id":"new"}
    c.hook["call"] = change
    with pytest.raises(stop.StopRecoveryRefused): await recover(c)
    assert not any(x[1] == "commit" for x in c.calls)
    assert stop._live_refs(c.runtime)
    if variant == "phase_token":
        assert next(iter(c.runtime[recovery._SIDECAR_STATE_KEY]["incidents"].values()))["phase_token"] == "foreign"


async def _interrupt_with_added_stop(c):
    added = None

    async def stop_during_prepare(prefix, action):
        nonlocal added
        if action != "prepare" or added is not None:
            return
        route = {"kind": "ble", "prefix": prefix,
                 "entity_id": f"switch.{prefix}_charger"}
        added = await recovery.async_begin_attempt(
            c.hass, c.entry, c.aliases[prefix], route, "stop", route["entity_id"])
        assert added
        assert await recovery.async_finish_attempt(added, "sent_unknown")
        c.runtime[stop.ACTIVE_KEY]["interrupted"] = True

    c.hook["call"] = stop_during_prepare
    with pytest.raises(stop.StopRecoveryRefused, match="recovery_scope_changed"):
        await recover(c)
    assert added is not None
    incident = next(iter(c.store.value["incidents"].values()))
    incident_id = incident["incident_id"]
    original_refs = deepcopy(incident["liability_refs"])
    assert len(stop._live_refs(c.runtime)) == len(original_refs) + 1
    assert incident["phase"] == "held"
    assert not any(action == "commit" for _, action, _ in c.calls)
    c.hook["call"] = None
    return added, incident_id, original_refs


@pytest.mark.asyncio
@pytest.mark.parametrize("reload", [False, True])
async def test_new_unknown_stop_during_recovery_can_retry_complete_inventory(inventory, reload):
    c = inventory
    added, incident_id, original_refs = await _interrupt_with_added_stop(c)
    if reload:
        c.runtime.pop(recovery.RECOVERY_KEY)
        c.runtime.pop("_tesla_ev_recovery_sidecar_loaded")
        assert await recovery.async_load_recovery_state(c.hass, c.entry)
    c.hook["call"] = None
    result = await recover(c)
    assert result == {"success": True, "incident_id": incident_id}
    saved = c.store.value["incidents"][incident_id]
    assert len(saved["liability_refs"]) == len(original_refs) + 1
    assert c.store.value["liabilities"][incident_id]["references"] == saved["liability_refs"]
    assert saved["scope_refreshes"][0]["previous_liability_refs"] == original_refs
    assert not stop._live_refs(c.runtime)
    assert all(receipt["m"] == "r" for receipt in c.receipts.values())
    new_record = c.store.value["recovery"]["vehicles"][added.vehicle_id]["attempts"][added.attempt_id]
    assert new_record["outcome"] == "sent_unknown"
    assert new_record["recovery_retired"]["incident_id"] == incident_id


async def _interrupt_with_rekeyed_quarantine(c):
    prefix = "bridge_a"
    route = {"kind": "ble", "prefix": prefix,
             "entity_id": f"switch.{prefix}_charger"}
    original = await recovery.async_begin_attempt(
        c.hass, c.entry, c.aliases[prefix], route, "stop", route["entity_id"])
    assert await recovery.async_finish_attempt(original, "sent_unknown")
    await _seed_legacy_quarantine(original)
    added, incident_id, original_refs = await _interrupt_with_added_stop(c)
    # Normal containment reuses ble:<prefix>; the durable merge preserves
    # the original quarantine at ble:<prefix>@<original attempt>.
    await _seed_legacy_quarantine(added)
    current = stop._live_refs(c.runtime)
    old_hazard = next(ref for ref in original_refs
                      if ref.get("hazard_id") == f"ble:{prefix}")
    moved = [ref for ref in current if ref.get("hazard_id")
             and ref.get("attempt_id") == original.attempt_id]
    assert len(moved) == 1
    assert moved[0]["hazard_id"] != old_hazard["hazard_id"]
    assert {k: v for k, v in moved[0].items() if k != "hazard_id"} == {
        k: v for k, v in old_hazard.items() if k != "hazard_id"}
    return incident_id, original_refs, current, moved[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("reload", [False, True])
@pytest.mark.parametrize("history_suffix", ["", "#2"])
async def test_retry_recovers_quarantine_preserved_under_history_key(inventory, reload, history_suffix):
    c = inventory
    incident_id, original_refs, current, moved = await _interrupt_with_rekeyed_quarantine(c)
    if history_suffix:
        c.runtime[recovery.RECOVERY_KEY] = deepcopy(stop._retained_root(c.runtime))
        for root in (c.runtime[recovery.RECOVERY_KEY], c.runtime[recovery._SIDECAR_STATE_KEY]["recovery"]):
            hazards = root["vehicles"][moved["vehicle_id"]]["hazards"]
            hazards[moved["hazard_id"] + history_suffix] = hazards.pop(moved["hazard_id"])
    if reload:
        c.runtime.pop(recovery.RECOVERY_KEY)
        c.runtime.pop("_tesla_ev_recovery_sidecar_loaded")
        assert await recovery.async_load_recovery_state(c.hass, c.entry)
    result = await recover(c)
    assert result == {"success": True, "incident_id": incident_id}
    saved = c.store.value["incidents"][incident_id]
    assert saved["scope_refreshes"][0]["previous_liability_refs"] == original_refs
    assert len(saved["liability_refs"]) == len(current)
    assert not stop._live_refs(c.runtime)
    assert all(receipt["m"] == "r" for receipt in c.receipts.values())


@pytest.mark.asyncio
@pytest.mark.parametrize("variant", ["missing", "binding", "attempt", "ambiguous", "arbitrary_key"])
async def test_rekeyed_quarantine_requires_unique_exact_original_evidence(inventory, variant):
    c = inventory
    incident_id, _, _, moved = await _interrupt_with_rekeyed_quarantine(c)
    c.runtime[recovery.RECOVERY_KEY] = deepcopy(stop._retained_root(c.runtime))
    for root in (c.runtime[recovery.RECOVERY_KEY], c.runtime[recovery._SIDECAR_STATE_KEY]["recovery"]):
        hazards = root["vehicles"][moved["vehicle_id"]]["hazards"]
        record = hazards[moved["hazard_id"]]
        if variant == "missing":
            hazards.pop(moved["hazard_id"])
        elif variant == "binding":
            record["binding"]["entities"]["charger"]["unique_id"] = "replacement"
        elif variant == "attempt":
            record["attempt_id"] = "missing-old-stop"
        elif variant == "arbitrary_key":
            hazards["unproven-rename"] = hazards.pop(moved["hazard_id"])
        else:
            hazards[moved["hazard_id"] + "#2"] = deepcopy(record)
    before = deepcopy(c.runtime[recovery._SIDECAR_STATE_KEY]["incidents"][incident_id])
    calls_before = len(c.calls)
    with pytest.raises(stop.StopRecoveryRefused):
        await recover(c)
    assert len(c.calls) == calls_before
    assert c.runtime[recovery._SIDECAR_STATE_KEY]["incidents"][incident_id] == before
    assert stop._live_refs(c.runtime)


@pytest.mark.asyncio
async def test_scope_refresh_save_failure_preserves_old_scope_and_can_retry(inventory):
    c = inventory
    _, incident_id, original_refs = await _interrupt_with_added_stop(c)
    calls_before = len(c.calls)
    original_save = c.store.async_save

    async def fail_refresh(value):
        if value["incidents"][incident_id].get("scope_refreshes"):
            return False
        return await original_save(value)

    c.store.async_save = fail_refresh
    with pytest.raises(stop.StopRecoveryRefused, match="scope_not_persisted"):
        await recover(c)
    assert len(c.calls) == calls_before
    assert c.runtime[recovery._SIDECAR_STATE_KEY]["incidents"][incident_id]["liability_refs"] == original_refs
    assert c.store.value["incidents"][incident_id]["liability_refs"] == original_refs
    assert c.store.value["liabilities"][incident_id]["references"] == original_refs
    assert stop._live_refs(c.runtime)
    c.store.async_save = original_save
    assert (await recover(c))["success"]


@pytest.mark.asyncio
@pytest.mark.parametrize("variant", ["positive", "old_binding", "missing_old", "producer", "inventory", "missing_liability"])
async def test_added_stop_retry_cannot_adopt_unsafe_scope(inventory, variant):
    c = inventory
    added, incident_id, _ = await _interrupt_with_added_stop(c)
    state = c.runtime[recovery._SIDECAR_STATE_KEY]
    incident = state["incidents"][incident_id]
    if variant == "positive":
        for root in (c.runtime[recovery.RECOVERY_KEY], state["recovery"]):
            root["vehicles"][added.vehicle_id]["attempts"][added.attempt_id]["operation"] = "start"
    elif variant == "old_binding":
        incident["liability_refs"][0]["route"]["entity_identity"]["unique_id"] = "foreign"
    elif variant == "missing_old":
        incident["liability_refs"][0]["attempt_id"] = "missing-old-stop"
    elif variant == "producer":
        c.registrations["switch.bridge_a_charger"].unique_id = "replacement"
    elif variant == "missing_liability":
        state["liabilities"].pop(incident_id)
    else:
        c.declared.pop()
    before = deepcopy(incident)
    calls_before = len(c.calls)
    with pytest.raises(stop.StopRecoveryRefused):
        await recover(c)
    assert len(c.calls) == calls_before
    assert state["incidents"][incident_id] == before
    assert stop._live_refs(c.runtime)


@pytest.mark.asyncio
async def test_refreshed_scope_still_requires_fresh_proof_from_every_bridge(inventory):
    c = inventory
    _, incident_id, _ = await _interrupt_with_added_stop(c)
    c.frozen.add("bridge_b")
    with pytest.raises(stop.StopRecoveryRefused, match="proof_unavailable"):
        await recover(c)
    assert c.store.value["incidents"][incident_id]["phase"] == "held"
    assert not any(action == "commit" for _, action, _ in c.calls)
    assert stop._live_refs(c.runtime)
    c.runtime.pop(recovery.RECOVERY_KEY)
    c.runtime.pop("_tesla_ev_recovery_sidecar_loaded")
    assert await recovery.async_load_recovery_state(c.hass, c.entry)
    loaded = c.runtime[recovery._SIDECAR_STATE_KEY]
    assert loaded["incidents"][incident_id]["scope_refreshes"]
    assert loaded["liabilities"][incident_id]["references"] == loaded["incidents"][incident_id]["liability_refs"]
    c.frozen.clear()
    assert (await recover(c))["success"]


@pytest.mark.asyncio
async def test_failed_release_save_never_unlocks_and_retry_preserves_unknown_outcomes(inventory):
    c = inventory
    original_save = c.store.async_save
    async def fail_release(value):
        if any(i["phase"] == "released" for i in value["incidents"].values()): return False
        return await original_save(value)
    c.store.async_save = fail_release
    with pytest.raises(stop.StopRecoveryRefused,match="release_not_persisted"): await recover(c)
    assert not any(x[1] == "commit" for x in c.calls)
    assert stop._live_refs(c.runtime)
    c.store.async_save = original_save
    assert (await recover(c))["success"]


@pytest.mark.asyncio
async def test_interrupted_unlock_retries_exact_completed_generation_without_prepare(inventory):
    c = inventory
    async def fail(prefix,action):
        if action == "commit": raise stop.StopRecoveryRefused("connection_lost")
    c.hook["call"] = fail
    with pytest.raises(stop.StopRecoveryRefused,match="connection_lost"): await recover(c)
    assert not stop._live_refs(c.runtime)
    assert all(i["phase"] == "released" for i in c.store.value["incidents"].values())
    calls_before = len(c.calls)
    c.hook["call"] = None
    assert (await recover(c))["success"]
    assert all(x[1] == "commit" for x in c.calls[calls_before:])


@pytest.mark.parametrize("value", ["{}", "null", '{"v":2,"v":2}', json.dumps(dict(v=2,b="1"*16,i=VIN_A,m="i",g=0,s=0,t=0)),
    json.dumps(dict(v=2,b="1"*16,i=VIN_A,m="i",g=True,s=0,t=0,n="",o=""))])
def test_malformed_proof_refused(value):
    with pytest.raises(stop.StopRecoveryRefused): stop.decode_proof(value)


@pytest.mark.asyncio
async def test_durable_only_positive_history_is_not_omitted(inventory):
    c = inventory
    durable = c.runtime[recovery._SIDECAR_STATE_KEY]["recovery"]["vehicles"][c.aliases["bridge_a"]]
    row = deepcopy(next(iter(durable["attempts"].values())))
    row.update(attempt_id="hidden-start",operation="start")
    durable["attempts"]["hidden-start"] = row
    with pytest.raises(stop.StopRecoveryRefused,match="non_stop"): await recover(c)
    assert c.calls == []


@pytest.mark.asyncio
async def test_durable_only_stop_is_retired_without_resurrection(inventory):
    c = inventory
    durable = c.runtime[recovery._SIDECAR_STATE_KEY]["recovery"]["vehicles"][c.aliases["bridge_a"]]
    row = deepcopy(next(iter(durable["attempts"].values())))
    row.update(attempt_id="hidden-stop")
    durable["attempts"]["hidden-stop"] = row
    assert (await recover(c))["success"]
    assert c.store.value["recovery"]["vehicles"][c.aliases["bridge_a"]]["attempts"]["hidden-stop"]["recovery_retired"]


@pytest.mark.asyncio
@pytest.mark.parametrize("physical_vin", [False, True])
async def test_recovery_waits_for_real_submission_to_drain(inventory, physical_vin):
    import asyncio
    c = inventory
    started, finish = asyncio.Event(), asyncio.Event()
    async def submit():
        started.set()
        await finish.wait()
    submission = asyncio.create_task(recovery.async_track_submission(c.hass,c.entry,submit,0,
        vehicle_id=VIN_A if physical_vin else c.aliases["bridge_a"],operation="stop"))
    await started.wait()
    task = asyncio.create_task(recover(c))
    for _ in range(50):
        if c.runtime.get(stop.ACTIVE_KEY): break
        await asyncio.sleep(0.001)
    await asyncio.sleep(0.002)
    assert c.calls == []
    finish.set()
    await submission
    assert (await task)["success"]


@pytest.mark.asyncio
async def test_cancellation_after_prepare_preserves_exact_scope_and_radio(inventory):
    import asyncio
    c = inventory
    async def cancel(prefix,action):
        if action == "prepare": raise asyncio.CancelledError
    c.hook["call"] = cancel
    with pytest.raises(asyncio.CancelledError): await recover(c)
    assert stop._live_refs(c.runtime)
    assert all(i["phase"] == "held" for i in c.store.value["incidents"].values())
    assert [action for _, action, _ in c.calls] == ["prepare"]
    assert stop.ACTIVE_KEY not in c.runtime


@pytest.mark.asyncio
async def test_old_ha_receipt_time_does_not_renew_native_proof(inventory, monkeypatch):
    c = inventory
    monkeypatch.setattr(stop,"_fresh_state_at",lambda _: 1.0)
    with pytest.raises(stop.StopRecoveryRefused,match="proof_unavailable"): await recover(c)
    assert not any(x[1] == "commit" for x in c.calls)


@pytest.mark.asyncio
async def test_shared_producer_liability_in_another_entry_refuses(inventory):
    c = inventory
    c.hass.data["power_sync"]["foreign-entry"] = {
        recovery.RECOVERY_KEY: deepcopy(c.runtime[recovery.RECOVERY_KEY])}
    with pytest.raises(stop.StopRecoveryRefused,match="another_entry"): await recover(c)
    assert c.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["stop", "start", "rate", "limit"])
async def test_read_only_recovery_status_preserves_every_retained_record(inventory, operation):
    from power_sync.tesla_ble_recovery_status import ble_recovery_status
    c = inventory
    for root in (c.runtime[recovery.RECOVERY_KEY], c.runtime[recovery._SIDECAR_STATE_KEY]["recovery"]):
        next(iter(root["vehicles"][c.aliases["bridge_a"]]["attempts"].values()))["operation"] = operation
    before_runtime, before_store = deepcopy(c.runtime[recovery.RECOVERY_KEY]), deepcopy(c.store.value)
    status = ble_recovery_status(c.hass, c.entry)
    assert status["success"] and status["controls_issued"] is False
    assert len(status["retained_history"]) == 35
    assert status["recovery"]["general_ble_recovery_available"] is True
    assert status["recovery"]["candidate_service"] == (
        "power_sync.recover_ble_stop_history" if operation == "stop" else "power_sync.recover_ble_command_history")
    assert status["recovery"]["reason"] == "complete_inventory_and_native_stop_proof_required"
    assert c.calls == [] and c.store.value == before_store
    assert c.runtime[recovery.RECOVERY_KEY] == before_runtime


@pytest.mark.asyncio
@pytest.mark.parametrize("requested", [None, "other-entry", 12, "missing-entry"])
async def test_status_service_routes_exact_entry_without_manual_control_cleanup(inventory, requested):
    import ast
    from typing import Any
    from conftest import load_source_node
    c = inventory
    root = Path(__file__).resolve().parents[1]
    node = load_source_node(root / "custom_components/power_sync/__init__.py", "handle_get_ble_recovery_status")
    scope = {"__name__": "power_sync", "__package__": "power_sync",
             "hass": c.hass, "entry": c.entry, "DOMAIN": "power_sync", "ServiceCall": object, "Any": Any}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[])), "status-handler", "exec"), scope)
    other_calls = []
    async def other(call):
        other_calls.append(call.data)
        return {"other_entry": True}
    c.hass.data["power_sync"]["other-entry"] = {"licensing_control_handlers": {"get_ble_recovery_status": other}}
    call = types.SimpleNamespace(data={"entry_id": requested})
    if requested in (12, "missing-entry"):
        with pytest.raises(ValueError):
            await scope["handle_get_ble_recovery_status"](call)
        assert not other_calls
    else:
        result = await scope["handle_get_ble_recovery_status"](call)
        assert result.get("other_entry", False) is (requested == "other-entry")
    # The registration must use this read-only handler directly. The normal
    # control router finalises a manual command and must not wrap an inspection.
    source = ast.parse((root / "custom_components/power_sync/__init__.py").read_text())
    registration = next(n for n in ast.walk(source) if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute) and n.func.attr == "async_register"
        and len(n.args) > 2 and isinstance(n.args[1], ast.Constant)
        and n.args[1].value == "get_ble_recovery_status")
    assert isinstance(registration.args[2], ast.Name)
    assert registration.args[2].id == "handle_get_ble_recovery_status"
    assert c.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["start", "rate", "limit"])
async def test_general_recovery_accepts_positive_history_only_after_all_native_proofs(inventory, operation):
    c = inventory
    for root in (c.runtime[recovery.RECOVERY_KEY], c.runtime[recovery._SIDECAR_STATE_KEY]["recovery"]):
        next(iter(root["vehicles"][c.aliases["bridge_a"]]["attempts"].values()))["operation"] = operation
    before = deepcopy(c.runtime[recovery.RECOVERY_KEY])
    result = await stop.async_recover_ble_history(c.hass, c.entry, c.declared, confirm_complete_history=True)
    assert result["success"]
    assert [x[1] for x in c.calls] == ["prepare", "prepare", "commit", "commit"]
    for vid, ledger in c.store.value["recovery"]["vehicles"].items():
        for aid, record in ledger["attempts"].items():
            assert record["outcome"] == before["vehicles"][vid]["attempts"][aid]["outcome"] == "sent_unknown"
            assert record["recovery_retired"]["incident_id"] == result["incident_id"]
    assert not stop._live_refs(c.runtime)
    c.runtime.pop(recovery.RECOVERY_KEY)
    c.runtime.pop(recovery._SIDECAR_LOADED_KEY)
    assert await recovery.async_load_recovery_state(c.hass, c.entry)
    assert not stop._live_refs(c.runtime)
    assert (await stop.async_recover_ble_history(c.hass, c.entry, c.declared,
                                              confirm_complete_history=True))["success"]
    assert len(c.calls) == 4


@pytest.mark.asyncio
async def test_general_positive_recovery_does_not_release_any_vehicle_on_missing_proof(inventory):
    c = inventory
    for root in (c.runtime[recovery.RECOVERY_KEY], c.runtime[recovery._SIDECAR_STATE_KEY]["recovery"]):
        next(iter(root["vehicles"][c.aliases["bridge_a"]]["attempts"].values()))["operation"] = "start"
    c.frozen.add("bridge_b")
    with pytest.raises(stop.StopRecoveryRefused, match="proof_unavailable"):
        await stop.async_recover_ble_history(c.hass, c.entry, c.declared, confirm_complete_history=True)
    assert not any(x[1] == "commit" for x in c.calls)
    assert stop._live_refs(c.runtime)


@pytest.mark.asyncio
async def test_restart_between_release_and_bridge_commit_keeps_positive_admission_closed(inventory):
    c = inventory
    async def interrupted(prefix, action):
        if action == "commit":
            raise stop.StopRecoveryRefused("connection_lost")
    c.hook["call"] = interrupted
    with pytest.raises(stop.StopRecoveryRefused, match="connection_lost"):
        await stop.async_recover_ble_history(c.hass, c.entry, c.declared, confirm_complete_history=True)
    assert not stop._live_refs(c.runtime)
    assert all(i["activation_pending"] for i in c.store.value["incidents"].values())
    c.runtime.pop(recovery.RECOVERY_KEY)
    c.runtime.pop(recovery._SIDECAR_LOADED_KEY)
    assert await recovery.async_load_recovery_state(c.hass, c.entry)
    for vid in c.aliases.values():
        generation = recovery.current_vehicle_generation(c.hass, c.entry, vid)
        assert not recovery.recovery_control_allowed(c.hass, c.entry, vid, generation, "start")
        assert recovery.get_vehicle_route(c.hass, c.entry, vid)["reason"] == "ble_recovery_commit_pending"
        assert recovery.recovery_control_allowed(c.hass, c.entry, vid, generation, "stop")
    from power_sync.tesla_ble_recovery_status import ble_recovery_status
    status = ble_recovery_status(c.hass, c.entry)
    assert status["recovery"]["required"] is True
    assert status["recovery"]["reason"] == "bridge_commit_pending"
    assert status["recovery"]["candidate_service"] == "power_sync.recover_ble_command_history"
    c.hook["call"] = None
    assert (await stop.async_recover_ble_history(c.hass, c.entry, c.declared, confirm_complete_history=True))["success"]
    assert all(i["activation_pending"] is False for i in c.store.value["incidents"].values())
    assert len([x for x in c.calls if x[1] == "prepare"]) == 2


@pytest.mark.asyncio
async def test_reproof_after_second_reboot_resumes_already_durable_release(inventory):
    c = inventory
    async def fail_second_commit(prefix, action):
        if prefix == "bridge_b" and action == "commit":
            raise stop.StopRecoveryRefused("connection_lost")

    c.hook["call"] = fail_second_commit
    with pytest.raises(stop.StopRecoveryRefused, match="connection_lost"):
        await recover(c)
    assert c.receipts["bridge_a"]["m"] == "r"
    assert c.receipts["bridge_b"]["m"] == "v"
    c.receipts["bridge_b"].update(b="a" * 16, m="q", s=0, t=0)
    c.hook["call"] = None
    calls = len(c.calls)
    assert (await recover(c))["success"]
    assert [(prefix, action) for prefix, action, _ in c.calls[calls:]] == [("bridge_b", "commit")]
    incident = next(iter(c.store.value["incidents"].values()))
    assert incident["activation_pending"] is False
    assert incident["reconciliation_evidence"]["bridge_b"]["second"]["b"] == "a" * 16


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome, proof_state", [
    ("not_sent", "valid"),
    ("not_sent", "reloaded"),
    ("not_sent", "missing"),
    ("not_sent", "rebound"),
    ("not_sent", "unconfirmed"),
    ("confirmed", "valid"),
    ("confirmed", "reloaded"),
    ("confirmed", "rebound"),
    ("confirmed", "stale_readback"),
])
async def test_retry_after_captured_pending_stop_finishes(inventory, outcome, proof_state):
    c = inventory
    # Both bridges are configured, but only bridge A has new unresolved history.
    assert (await recover(c))["success"]
    c.calls.clear()
    prefix = "bridge_a"

    async def recover_affected():
        return await stop.async_recover_ble_stops(
            c.hass, c.entry, c.declared[:1], confirm_complete_history=True)

    route = {"kind": "ble", "prefix": prefix, "entity_id": f"switch.{prefix}_charger"}
    pending = await recovery.async_begin_attempt(
        c.hass, c.entry, c.aliases[prefix], route, "stop", route["entity_id"])
    assert pending
    unknown_ids = set()
    for _ in range(6):
        unknown = await recovery.async_begin_attempt(
            c.hass, c.entry, c.aliases[prefix], route, "stop", route["entity_id"])
        assert unknown and await recovery.async_finish_attempt(unknown, "sent_unknown")
        unknown_ids.add(unknown.attempt_id)
    finished = False

    async def finish_before_release(prefix, action):
        nonlocal finished
        if action == "prepare" and not finished:
            assert await recovery.async_finish_attempt(pending, outcome)
            finished = True

    c.hook["call"] = finish_before_release
    with pytest.raises(stop.StopRecoveryRefused, match="recovery_scope_changed"):
        await recover_affected()
    assert finished
    assert not any(action == "commit" for _, action, _ in c.calls)
    assert stop._live_refs(c.runtime)
    c.hook["call"] = None
    proof = c.runtime[recovery._SIDECAR_STATE_KEY]["recovery"]["terminal_removals"][pending.vehicle_id]
    assert proof[pending.attempt_id]["outcome"] == outcome
    if proof_state == "reloaded":
        c.runtime.pop(recovery.RECOVERY_KEY)
        c.runtime.pop(recovery._SIDECAR_LOADED_KEY)
        assert await recovery.async_load_recovery_state(c.hass, c.entry)
    elif proof_state in {"missing", "rebound", "unconfirmed"}:
        if proof_state == "missing":
            proof.pop(pending.attempt_id)
        elif proof_state == "rebound":
            proof[pending.attempt_id]["route"]["entity_identity"]["device_id"] = "different-device"
        else:
            proof[pending.attempt_id]["outcome"] = "accepted_unconfirmed"
        calls_before = list(c.calls)
        with pytest.raises(stop.StopRecoveryRefused, match="retained_recovery_scope_changed"):
            await recover_affected()
        assert c.calls == calls_before
        assert stop._live_refs(c.runtime)
        return
    if proof_state == "stale_readback":
        c.frozen.add(prefix)
        c.calls.clear()
        with pytest.raises(stop.StopRecoveryRefused, match="proof_unavailable"):
            await recover_affected()
        assert not any(action == "commit" for _, action, _ in c.calls)
        assert {ref["attempt_id"] for ref in stop._live_refs(c.runtime)} == unknown_ids
        return

    result = await recover_affected()
    assert result["success"]
    assert not stop._live_refs(c.runtime)
    assert {bridge for bridge, _, _ in c.calls} == {prefix}
    refs = c.store.value["recovery"]["vehicles"][pending.vehicle_id]["attempts"]
    for attempt_id in unknown_ids:
        assert refs[attempt_id]["outcome"] == "sent_unknown"
        assert refs[attempt_id]["recovery_retired"]["incident_id"] == result["incident_id"]


@pytest.mark.asyncio
@pytest.mark.parametrize("proof_state", ["not_sent", "wrong_operation", "confirmed"])
async def test_stop_only_retry_after_general_inventory_proves_pending_start_was_not_sent(inventory, proof_state):
    c = inventory
    assert (await recover(c))["success"]
    c.calls.clear()
    prefix = "bridge_a"
    alias = c.aliases[prefix]
    route = {"kind": "ble", "prefix": prefix, "entity_id": f"switch.{prefix}_charger"}
    pending_start = await recovery.async_begin_attempt(
        c.hass, c.entry, alias, route, "start", route["entity_id"])
    assert pending_start
    unknown_stop = await recovery.async_begin_attempt(
        c.hass, c.entry, alias, route, "stop", route["entity_id"])
    assert unknown_stop and await recovery.async_finish_attempt(unknown_stop, "sent_unknown")

    async def finish_unsent_start(_, action):
        if action == "prepare":
            assert await recovery.async_finish_attempt(pending_start, "not_sent")

    c.hook["call"] = finish_unsent_start
    with pytest.raises(stop.StopRecoveryRefused, match="recovery_scope_changed"):
        await stop.async_recover_ble_history(c.hass, c.entry, c.declared[:1], confirm_complete_history=True)
    assert not any(action == "commit" for _, action, _ in c.calls)
    assert {ref["attempt_id"] for ref in stop._live_refs(c.runtime)} == {unknown_stop.attempt_id}
    c.hook["call"] = None
    retry_start = len(c.calls)

    if proof_state != "not_sent":
        proof = c.runtime[recovery._SIDECAR_STATE_KEY]["recovery"]["terminal_removals"][alias][pending_start.attempt_id]
        if proof_state == "wrong_operation":
            proof["operation"] = "rate"
        else:
            proof["outcome"] = "confirmed"
        with pytest.raises(stop.StopRecoveryRefused, match="retained_recovery_scope_changed"):
            await stop.async_recover_ble_stops(c.hass, c.entry, c.declared[:1], confirm_complete_history=True)
        assert len(c.calls) == retry_start
        assert {ref["attempt_id"] for ref in stop._live_refs(c.runtime)} == {unknown_stop.attempt_id}
        return

    result = await stop.async_recover_ble_stops(c.hass, c.entry, c.declared[:1], confirm_complete_history=True)

    assert result["success"]
    assert [action for _, action, _ in c.calls[retry_start:]] == ["prepare", "commit"]
    assert not stop._live_refs(c.runtime)
    durable = c.store.value["recovery"]
    assert durable["terminal_removals"][alias][pending_start.attempt_id]["outcome"] == "not_sent"
    saved_stop = durable["vehicles"][alias]["attempts"][unknown_stop.attempt_id]
    assert saved_stop["outcome"] == "sent_unknown"
    assert saved_stop["recovery_retired"]["incident_id"] == result["incident_id"]


@pytest_asyncio.fixture
async def readded_bridge(inventory, monkeypatch):
    import base64

    case = inventory
    assert (await recover(case))["success"]
    case.calls.clear()
    case.declared = [case.declared[0]]
    bridge = case.declared[0]
    prefix, vin = bridge["prefix"], bridge["vin"]
    case.receipts[prefix]["b"] = "d" * 16
    charger = f"switch.{prefix}_charger"
    case.mac = "aabbccddeeff"
    case.registrations[charger].unique_id = "AA:BB:CC:DD:EE:FF/0/switch/Charger"
    case.original_identity = recovery._identity(case.hass, charger)
    route = {"kind": "ble", "prefix": prefix, "entity_id": charger}
    ticket = await recovery.async_begin_attempt(case.hass, case.entry, vin, route, "stop", charger)
    assert ticket and await recovery.async_finish_attempt(ticket, "sent_unknown")
    await _seed_legacy_quarantine(ticket)
    assert len(stop._live_refs(case.runtime)) == 2

    case.active_key = base64.b64encode(bytes(range(32))).decode()
    active = case.config_entries.pop(prefix)
    active.domain = "esphome"
    active.entry_id = "readded-bridge"
    active.unique_id = case.mac
    active.created_at = datetime(2026, 11, 7, 6, tzinfo=timezone.utc)
    active.data = {"host": "bridge.local", "port": 6053, "noise_psk": case.active_key}
    case.active = active
    case.config_entries[active.entry_id] = active
    case.config_entries["bridge_b"].data = {}
    case.hass.config_entries.async_entries = lambda domain: list(case.config_entries.values())
    for registration in case.registrations.values():
        if registration.config_entry_id == prefix:
            registration.config_entry_id = active.entry_id

    case.auth_calls = []
    case.reported_mac = case.mac

    class BridgeApi:
        def __init__(self, host, port, password, *, noise_psk):
            assert (host, port, password) == ("bridge.local", 6053, None)
            assert noise_psk == case.active_key

        async def connect(self, *, login):
            assert login is True
            case.auth_calls.append("connect")

        async def device_info(self):
            case.auth_calls.append("device_info")
            return types.SimpleNamespace(mac_address=case.reported_mac)

        async def disconnect(self, *, force):
            assert force is True
            case.auth_calls.append("disconnect")

    monkeypatch.setitem(sys.modules, "aioesphomeapi", types.SimpleNamespace(
        APIClient=BridgeApi, APIConnectionError=type("APIConnectionError", (Exception,), {})
    ))
    return case


async def continuity_action(case, step, response=None):
    return await stop.async_recover_readded_ble_stop(
        case.hass, case.entry, case.declared, confirm_complete_history=True,
        step=step, response=response,
    )


def make_backup_response(case, tmp_path, challenge, *, historical_key=None, shared_key=False, after_readd=False):
    from io import BytesIO
    from runpy import run_path
    import tarfile

    historical_key = historical_key or case.active_key

    def add_file(archive, name, contents):
        header = tarfile.TarInfo(name)
        header.size = len(contents)
        archive.addfile(header, BytesIO(contents))

    def encode(payload):
        return json.dumps(payload).encode()

    entries = [{"domain": "esphome", "entry_id": case.original_identity["config_entry_id"],
                "unique_id": case.mac, "data": {"noise_psk": historical_key}}]
    if shared_key:
        entries.append({"domain": "esphome", "entry_id": "another-bridge",
                        "unique_id": "aabbccddee00", "data": {"noise_psk": historical_key}})

    core = BytesIO()
    with tarfile.open(fileobj=core, mode="w:gz") as archive:
        add_file(archive, "./data/.storage/core.config_entries", encode({"data": {"entries": entries}}))
        add_file(archive, "./data/.storage/core.entity_registry",
                 encode({"data": {"entities": [case.original_identity]}}))
    snapshot = tmp_path / "historical-ha-backup.tar"
    with tarfile.open(snapshot, "w") as archive:
        add_file(archive, "./backup.json", encode({"date": (
            "2026-11-08T04:00:00+00:00" if after_readd else "2026-11-07T04:00:00+00:00"
        )}))
        add_file(archive, "./homeassistant.tar.gz", core.getvalue())
    tool = run_path(str(Path(__file__).parents[1] / "scripts" / "tesla_ble_continuity_proof.py"))
    return tool["generate_response"](snapshot, challenge)


@pytest.mark.asyncio
async def test_readded_ble_stop_reuses_durable_recovery_after_authenticated_continuity(readded_bridge, tmp_path):
    case = readded_bridge
    with pytest.raises(stop.StopRecoveryRefused, match="non_stop_or_unproven_history"):
        await recover(case)
    before = deepcopy(case.store.value)
    prepared = await continuity_action(case, "prepare")
    assert prepared["success"] and prepared["controls_issued"] is False
    assert case.store.value == before and case.auth_calls == case.calls == []
    proof = make_backup_response(case, tmp_path, prepared["challenge"])
    result = await continuity_action(case, "recover", proof)
    assert result["success"]
    assert case.auth_calls == ["connect", "device_info", "disconnect"]
    assert [action for _, action, _ in case.calls] == ["prepare", "commit"]
    assert not stop._live_refs(case.runtime)
    recovered = next(incident for incident in case.store.value["incidents"].values()
                     if "producer_continuity" in incident["scope_basis"])
    assert recovered["historical_outcome"] == "unknown"
    assert recovered["phase"] == "released" and recovered["activation_pending"] is False
    assert proof not in repr(case.store.value) and case.active_key not in repr(case.store.value)
    assert recovered["scope_basis"]["producer_continuity"]["original"] == case.original_identity
    assert recovered["scope_basis"]["producer_continuity"]["current"]["config_entry_id"] == case.active.entry_id


@pytest.mark.asyncio
@pytest.mark.parametrize("variant", ("rotated", "wrong_mac", "shared_current", "shared_historical", "newer_backup", "stale_vehicle"))
async def test_readded_ble_stop_refuses_missing_or_conflicting_evidence(readded_bridge, tmp_path, variant):
    import base64

    case = readded_bridge
    before = deepcopy(case.store.value)
    if variant == "shared_current":
        case.config_entries["bridge_b"].data["noise_psk"] = case.active_key
        with pytest.raises(stop.StopRecoveryRefused, match="bridge_requires_unique_encryption_key"):
            await continuity_action(case, "prepare")
    else:
        prepared = await continuity_action(case, "prepare")
        if variant in {"shared_historical", "newer_backup"}:
            with pytest.raises(ValueError, match="shared with another bridge|from before the bridge was re-added"):
                make_backup_response(case, tmp_path, prepared["challenge"],
                                     shared_key=variant == "shared_historical", after_readd=variant == "newer_backup")
        else:
            wrong_key = base64.b64encode(bytes(reversed(range(32)))).decode()
            proof = make_backup_response(case, tmp_path, prepared["challenge"],
                                         historical_key=wrong_key if variant == "rotated" else None)
            if variant == "wrong_mac":
                case.reported_mac = "aabbccddee01"
            if variant == "stale_vehicle":
                case.frozen.add(case.declared[0]["prefix"])
            reason = {"rotated": "historical_bridge_key_proof_mismatch", "wrong_mac": "authenticated_bridge_mismatch",
                      "stale_vehicle": "fresh_stopped_vehicle_proof_unavailable"}[variant]
            with pytest.raises(stop.StopRecoveryRefused, match=reason):
                await continuity_action(case, "recover", proof)
    if variant == "stale_vehicle":
        assert [action for _, action, _ in case.calls] == ["prepare"]
    else:
        assert case.calls == [] and case.store.value == before
    assert len(stop._live_refs(case.runtime)) == 2


@pytest.mark.asyncio
async def test_readded_ble_stop_resume_requires_same_key_and_committed_scope(readded_bridge, tmp_path):
    import base64

    case = readded_bridge
    prepared = await continuity_action(case, "prepare")
    proof = make_backup_response(case, tmp_path, prepared["challenge"])

    async def interrupt_commit(prefix, action):
        if action == "commit":
            raise stop.StopRecoveryRefused("connection_lost")

    case.hook["call"] = interrupt_commit
    with pytest.raises(stop.StopRecoveryRefused, match="connection_lost"):
        await continuity_action(case, "recover", proof)
    assert not stop._live_refs(case.runtime)
    assert any(item["activation_pending"] for item in case.store.value["incidents"].values())
    case.runtime.pop(recovery.RECOVERY_KEY)
    case.runtime.pop(recovery._SIDECAR_LOADED_KEY)
    assert await recovery.async_load_recovery_state(case.hass, case.entry)
    case.hook["call"] = None
    case.active.data["noise_psk"] = base64.b64encode(bytes(reversed(range(32)))).decode()
    with pytest.raises(stop.StopRecoveryRefused, match="completed_recovery_scope_changed"):
        await recover(case)
    assert [action for _, action, _ in case.calls] == ["prepare", "commit"]
    case.active.data["noise_psk"] = case.active_key
    assert (await recover(case))["success"]
    assert [action for _, action, _ in case.calls] == ["prepare", "commit", "commit"]


@pytest.mark.asyncio
async def test_readded_ble_stop_rejects_previous_challenge_after_preparing_a_new_one(readded_bridge, tmp_path):
    case = readded_bridge
    first = await continuity_action(case, "prepare")
    response = make_backup_response(case, tmp_path, first["challenge"])
    second = await continuity_action(case, "prepare")
    assert second["challenge"] != first["challenge"]
    with pytest.raises(stop.StopRecoveryRefused, match="historical_bridge_key_proof_mismatch"):
        await continuity_action(case, "recover", response)
    assert case.auth_calls == case.calls == []
    assert len(stop._live_refs(case.runtime)) == 2
