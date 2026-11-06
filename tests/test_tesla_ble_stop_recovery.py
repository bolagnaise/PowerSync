"""Whole-inventory Stop recovery, using the real durable recovery manager."""
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import types

import pytest
import pytest_asyncio

from test_tesla_ev_recovery import (harness, _sidecar_harness, _Entity, _State,
    VIN_A, VIN_B, recovery)
from power_sync import tesla_ble_stop_recovery as stop


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
                assert not await recovery.async_isolate_ble(ticket, authorize=True)
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
@pytest.mark.parametrize("variant", ["owner", "config", "registry", "runtime", "generation", "phase_token", "new_liability"])
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
        else:
            ledger = c.runtime[recovery.RECOVERY_KEY]["vehicles"][c.aliases[prefix]]
            ledger["attempts"]["new"] = {**deepcopy(next(iter(ledger["attempts"].values()))),"attempt_id":"new"}
    c.hook["call"] = change
    with pytest.raises(stop.StopRecoveryRefused): await recover(c)
    assert not any(x[1] == "commit" for x in c.calls)
    assert stop._live_refs(c.runtime)
    if variant == "phase_token":
        assert next(iter(c.runtime[recovery._SIDECAR_STATE_KEY]["incidents"].values()))["phase_token"] == "foreign"


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
async def test_recovery_waits_for_real_submission_to_drain(inventory):
    import asyncio
    c = inventory
    started, finish = asyncio.Event(), asyncio.Event()
    async def submit():
        started.set()
        await finish.wait()
    submission = asyncio.create_task(recovery.async_track_submission(c.hass,c.entry,submit,0,
        vehicle_id=c.aliases["bridge_a"],operation="stop"))
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
async def test_cancellation_after_prepare_preserves_exact_scope_and_contains(inventory):
    import asyncio
    c = inventory
    async def cancel(prefix,action):
        if action == "prepare": raise asyncio.CancelledError
    c.hook["call"] = cancel
    with pytest.raises(asyncio.CancelledError): await recover(c)
    assert stop._live_refs(c.runtime)
    assert all(i["phase"] == "held" for i in c.store.value["incidents"].values())
    assert c.calls[1][1] == "hold"
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
