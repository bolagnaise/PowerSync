"""Operator-scoped charging-history recovery using producer protocol 2.

This is separate from stock/native-cloud recovery. A current VIN never becomes
a reconstructed historical identity. Legacy aliases require an explicit closed
historical inventory, followed by proof for every declared producer/vehicle.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
import json
import re
import time
from typing import Any, Mapping
from uuid import uuid4

DOMAIN = "power_sync"
TIMEOUT = 300.0
POLL_SECONDS = 0.2
ACTIVE_KEY = "_ble_stop_recovery_active"


class StopRecoveryRefused(ValueError):
    """A missing proof leaves the retained command history held."""


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate proof field")
        result[key] = value
    return result


def decode_proof(value: Any) -> dict[str, Any]:
    """Validate a compact producer receipt, never a timer/HA-state substitute."""
    try:
        if not isinstance(value, str) or len(value.encode()) > 255:
            raise ValueError
        proof = json.loads(value, object_pairs_hook=_unique_object)
        if not isinstance(proof, dict) or type(proof.get("v")) is not int or proof["v"] != 2:
            raise ValueError
        if not re.fullmatch(r"[0-9a-f]{16}", proof.get("b", "")):
            raise ValueError
        if not re.fullmatch(r"[A-HJ-NPR-Z0-9]{17}", proof.get("i", "")):
            raise ValueError
        if proof.get("m") not in {"i", "p", "h", "q", "v", "r"}:
            raise ValueError
        for key in ("g", "s", "t"):
            if type(proof.get(key)) is not int or not 0 <= proof[key] < 2**32:
                raise ValueError
        for key, length in (("n", 32), ("o", 16)):
            if key not in proof or (proof[key] != "" and not re.fullmatch(r"[0-9a-f]{%d}" % length, proof[key])):
                raise ValueError
        return proof
    except (ValueError, TypeError, OverflowError) as error:
        raise StopRecoveryRefused("invalid_bridge_proof") from error


def verified_sample(proof, baseline, nonce, vin, *, released=False):
    """A new boot, persisted nonce and producer-validated native zero sample."""
    return bool(
        proof.get("m") == ("r" if released else "v")
        and proof.get("n") == nonce and proof.get("o") == baseline["b"]
        and proof.get("b") != baseline["b"]
        and proof.get("g") == baseline["g"] + 1 and proof.get("i") == vin
        and proof.get("s", 0) >= 2 and proof.get("t", 0) > 0
        and proof.get("c") in {"S", "C", "D", "N"}
        and type(proof.get("a")) in (int, float) and proof["a"] == 0
        and type(proof.get("w")) in (int, float) and proof["w"] == 0
    )


def _retained_root(runtime):
    from . import tesla_ev_recovery as manager

    root = manager._recovery(runtime, create=False)
    if not isinstance(root, Mapping) or manager._journal_malformed(runtime):
        raise StopRecoveryRefused("invalid_retained_history")
    durable = manager._sidecar_recovery(runtime)
    if isinstance(durable, Mapping):
        for vid, ledger in root.get("vehicles", {}).items():
            recorded = durable.get("vehicles", {}).get(vid, {}).get("attempts", {})
            for aid, attempt in ledger.get("attempts", {}).items():
                if aid in recorded and manager._attempt_record_digest(attempt) != manager._attempt_record_digest(recorded[aid]):
                    raise StopRecoveryRefused("contradictory_retained_history")
        root = manager._merge_recovery_roots(durable, root, prefer_legacy_mutable=True)
    return root


def _live_refs(runtime):
    from . import tesla_ev_recovery as manager

    result = []
    root = _retained_root(runtime)
    for ref in manager._unresolved_liabilities({manager.RECOVERY_KEY: root}):
        ledger = root["vehicles"][ref["vehicle_id"]]
        family, key = ("hazards", ref["hazard_id"]) if "hazard_id" in ref else ("attempts", ref["attempt_id"])
        if not ledger[family][key].get("recovery_retired"):
            result.append(ref)
    return result


def _configuration(entry):
    return {**entry.data, **entry.options}


def _fresh_state_at(state):
    stamp = getattr(state, "last_reported", None) or getattr(state, "last_updated", None)
    try:
        return stamp.timestamp()
    except (AttributeError, ValueError, OverflowError):
        return 0.0


class Bridge:
    """Pin one actual ESPHome producer and its new proof/control entities."""

    def __init__(self, hass, entry, prefix, vin):
        from . import tesla_ev_recovery as manager

        self.hass, self.entry, self.prefix, self.vin = hass, entry, prefix, vin
        self.ids = {
            "charger": f"switch.{prefix}_charger",
            "proof": f"sensor.{prefix}_powersync_recovery_proof",
            "request": f"text.{prefix}_powersync_recovery_request",
        }
        self.identities = {key: manager._identity(hass, value) for key, value in self.ids.items()}
        if any(value is None for value in self.identities.values()):
            raise StopRecoveryRefused("bridge_recovery_firmware_required")
        self.identity = self.identities["charger"]
        if any(value.get("platform") != "esphome" or any(
            value.get(field) != self.identity.get(field) for field in ("device_id", "config_entry_id")
        ) for value in self.identities.values()):
            raise StopRecoveryRefused("ambiguous_bridge_binding")
        self.config_entry = hass.config_entries.async_get_entry(self.identity["config_entry_id"])
        self.runtime = getattr(self.config_entry, "runtime_data", None)
        self.client = getattr(self.runtime, "client", None)
        if self.client is None or not getattr(self.client, "is_connected", False):
            raise StopRecoveryRefused("bridge_api_unavailable")
        self.baseline = self.proof()
        if self.baseline["i"] != vin or self.baseline["g"] >= 2**32 - 1:
            raise StopRecoveryRefused("vehicle_identity_or_generation_mismatch")
        self.nonce = uuid4().hex
        self.started = 0.0
        self.prepared = False

    def current(self):
        from . import tesla_ev_recovery as manager

        current = self.hass.config_entries.async_get_entry(self.identity["config_entry_id"])
        return bool(
            current is self.config_entry and getattr(current, "runtime_data", None) is self.runtime
            and getattr(self.runtime, "client", None) is self.client
            and all(manager._identity(self.hass, self.ids[key]) == value for key, value in self.identities.items())
        )

    def proof(self, *, fresh=False):
        state = self.hass.states.get(self.ids["proof"])
        if state is None or (fresh and not self.started < _fresh_state_at(state) <= time.time() + 5):
            raise StopRecoveryRefused("stale_bridge_proof")
        return decode_proof(state.state)

    async def send(self, action, authorize):
        if not authorize() or not self.current():
            raise StopRecoveryRefused("recovery_scope_changed")
        await self.hass.services.async_call("text", "set_value", {
            "entity_id": self.ids["request"], "value": f"{action}:{self.nonce}",
        }, blocking=True)
        if not authorize() or not self.current():
            raise StopRecoveryRefused("recovery_scope_changed")

    async def prepare(self, authorize):
        self.started = time.time()
        # Mark before the await: an ambiguous service result must be contained.
        self.prepared = True
        await self.send("prepare", authorize)

    async def wait_verified(self, authorize):
        deadline = time.monotonic() + TIMEOUT
        first = None
        while time.monotonic() < deadline:
            if not authorize() or not self.current():
                raise StopRecoveryRefused("recovery_scope_changed")
            try:
                proof = self.proof(fresh=True)
            except StopRecoveryRefused:
                proof = {}
            if verified_sample(proof, self.baseline, self.nonce, self.vin):
                if first is None:
                    first = proof
                elif proof["b"] == first["b"] and proof["s"] > first["s"] and proof["t"] - first["t"] >= 1000:
                    return {"first": first, "second": proof, "proof_entity": self.ids["proof"]}
            elif proof.get("n") == self.nonce and proof.get("m") == "h":
                raise StopRecoveryRefused("bridge_recovery_held")
            await asyncio.sleep(POLL_SECONDS)
        raise StopRecoveryRefused("fresh_stopped_vehicle_proof_unavailable")

    async def unlock(self, evidence, authorize):
        await self.send("commit", authorize)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if not authorize() or not self.current():
                raise StopRecoveryRefused("recovery_scope_changed")
            proof = self.proof(fresh=True)
            if (verified_sample(proof, self.baseline, self.nonce, self.vin, released=True)
                    and proof["b"] == evidence["second"]["b"]
                    and proof["s"] >= evidence["second"]["s"]):
                return proof
            await asyncio.sleep(POLL_SECONDS)
        raise StopRecoveryRefused("bridge_unlock_unconfirmed")

    async def contain(self):
        # No request may cross a rebound device. The nonce further confines a
        # late cleanup to this bridge's own still-current recovery generation.
        if self.prepared and self.current():
            await self.hass.services.async_call("text", "set_value", {
                "entity_id": self.ids["request"], "value": f"hold:{self.nonce}",
            }, blocking=True)


def _inventory_bridges(hass, entry, inventory):
    from . import tesla_ev_recovery as manager
    from .tesla_ble_mapping import resolve_ble_prefixes

    config = _configuration(entry)
    if config.get("ev_provider") not in {"tesla_ble", "both"}:
        raise StopRecoveryRefused("ble_provider_not_configured")
    configured = set(resolve_ble_prefixes(hass, config))
    if not isinstance(inventory, list) or not inventory:
        raise StopRecoveryRefused("complete_historical_inventory_required")
    bridges = {}
    vins = set()
    for item in inventory:
        if not isinstance(item, dict) or set(item) != {"prefix", "vin"}:
            raise StopRecoveryRefused("invalid_vehicle_inventory")
        prefix, vin = item["prefix"], item["vin"]
        if (not isinstance(prefix, str) or prefix not in configured or prefix in bridges
                or not isinstance(vin, str) or not re.fullmatch(r"[A-HJ-NPR-Z0-9]{17}", vin) or vin in vins):
            raise StopRecoveryRefused("ambiguous_vehicle_inventory")
        bridges[prefix] = Bridge(hass, entry, prefix, vin)
        vins.add(vin)
    identities = {(b.identity["config_entry_id"], b.identity["device_id"]) for b in bridges.values()}
    if len(identities) != len(bridges):
        raise StopRecoveryRefused("shared_bridge_producer")
    return bridges


def _resolve_inventory(hass, entry, inventory, bridges=None, *, allow_positive=False):
    from . import tesla_ev_recovery as manager

    bridges = bridges or _inventory_bridges(hass, entry, inventory)
    runtime = manager._runtime(hass, entry)
    root = _retained_root(runtime)
    refs = _live_refs(runtime)
    selected, affected = [], {}
    # The explicit inventory must cover every retained BLE liability in this
    # entry. A stale/removed prefix or a mixed producer cannot be omitted.
    for ref in refs:
        route = ref.get("route")
        prefix = route.get("prefix") if isinstance(route, Mapping) and route.get("kind") == "ble" else ref.get("prefix")
        if prefix not in bridges:
            raise StopRecoveryRefused("retained_inventory_incomplete")
        bridge = bridges[prefix]
        ledger = root["vehicles"][ref["vehicle_id"]]
        record = ledger.get("attempts", {}).get(ref.get("attempt_id"))
        if (not isinstance(record, Mapping) or record.get("operation") not in ({"start", "stop", "rate", "limit"} if allow_positive else {"stop"})
                or not manager._record_compatible(hass, entry, runtime, ref["vehicle_id"], record)
                or not manager._liability_matches_stock_scope(ref, bridge.identity)):
            raise StopRecoveryRefused("non_stop_or_unproven_history")
        # An authoritative old VIN is a hard boundary, never overridden by an
        # operator's current inventory. Unanchored aliases remain aliases.
        vid = ref["vehicle_id"]
        if re.fullmatch(r"[A-HJ-NPR-Z0-9]{17}", vid) and vid != bridge.vin:
            raise StopRecoveryRefused("historical_vehicle_mismatch")
        previous = affected.setdefault(prefix, vid)
        if previous != vid:
            raise StopRecoveryRefused("ambiguous_historical_aliases")
        selected.append(ref)
    if not selected or set(affected) != set(bridges):
        raise StopRecoveryRefused("inventory_has_no_exact_retained_scope")
    # A second integration entry must not hide another producer's liability.
    _check_other_entries(hass, runtime, bridges)
    return bridges, selected, affected


def _check_other_entries(hass, runtime, bridges):
    from . import tesla_ev_recovery as manager

    for other in hass.data.get(DOMAIN, {}).values():
        if not isinstance(other, dict) or other is runtime or manager.RECOVERY_KEY not in other:
            continue
        for ref in _live_refs(other):
            if any(manager._liability_matches_stock_scope(ref, b.identity) for b in bridges.values()):
                raise StopRecoveryRefused("producer_shared_with_another_entry")


async def async_recover_ble_history(hass, entry, inventory, *, confirm_complete_history=False):
    """Recover all stateful charging operations after a producer reset and native readback.

    Operational convergence does not reconstruct an old command's outcome.
    """
    return await async_recover_ble_stops(
        hass, entry, inventory, confirm_complete_history=confirm_complete_history,
        _allow_positive=True,
    )


async def async_recover_ble_stops(hass, entry, inventory, *, confirm_complete_history=False,
                                  _allow_positive=False):
    """Recover the whole operator-confirmed affected inventory, or retain it."""
    from . import tesla_ev_recovery as manager

    if confirm_complete_history is not True:
        raise StopRecoveryRefused("complete_historical_inventory_confirmation_required")
    runtime = manager._runtime(hass, entry)
    if runtime is None or not manager._sidecar_ready(runtime):
        raise StopRecoveryRefused("recovery_store_unavailable")
    lock = runtime.setdefault("_ble_stop_inventory_lock", asyncio.Lock())
    if lock.locked():
        raise StopRecoveryRefused("recovery_already_running")
    async with lock:
        async with manager._journal_lock(runtime):
            # Scope the authoritative union, not just the automation store's
            # possibly older projection. Never discard durable-only history.
            runtime[manager.RECOVERY_KEY] = deepcopy(_retained_root(runtime))
            manager._sync_sidecar_projection(runtime, runtime[manager._SIDECAR_STATE_KEY])
        bridges = _inventory_bridges(hass, entry, inventory)
        if not _live_refs(runtime):
            return await _resume_released(hass, entry, runtime, bridges, inventory)
        bridges, refs, affected = _resolve_inventory(hass, entry, inventory, bridges, allow_positive=_allow_positive)
        config = deepcopy(_configuration(entry))
        owners = {vid: manager._owner_snapshot(hass, entry, vid) for vid in affected.values()}
        scope_digest = manager._digest(refs)
        inventory_digest = manager._digest(inventory)
        incident = None
        snapshot = manager.recovery_sidecar_snapshot(hass, entry)
        for candidate in snapshot["incidents"].values():
            if candidate.get("phase") != "released":
                basis = candidate.get("scope_basis") or {}
                if basis.get("kind") not in {"ble_stop_inventory_v2", "ble_command_inventory_v2"} or basis.get("inventory_digest") != inventory_digest:
                    raise StopRecoveryRefused("another_recovery_scope_is_held")
                if incident is not None:
                    raise StopRecoveryRefused("ambiguous_recovery_incidents")
                incident = candidate
        if incident is None:
            incident = await manager.async_record_evidence_loss(
                hass, entry, kind="evidence_lost", covered_attempts=refs,
                candidate_producers=[b.identity for b in bridges.values()],
                candidate_vehicles=list(affected.values()),
                surviving_facts=[{"prefix": p, "journal_vehicle_id": affected[p], "declared_physical_vin": b.vin}
                                 for p, b in bridges.items()],
                scope_basis={"kind": "ble_command_inventory_v2" if _allow_positive else "ble_stop_inventory_v2", "inventory_digest": inventory_digest,
                             "historical_identity_reconstructed": False,
                             "complete_affected_inventory": deepcopy(inventory)},
            )
        if incident is None:
            raise StopRecoveryRefused("recovery_scope_not_persisted")
        await _refresh_held_scope(hass, entry, runtime, incident["incident_id"],
                                  inventory, bridges, refs, affected, allow_positive=_allow_positive)
        snapshot = manager.recovery_sidecar_snapshot(hass, entry)
        incident = await manager.async_confirm_recovery_scope(
            hass, entry, incident["incident_id"], expected_revision=snapshot["revision"],
            operator_confirmation=True,
        )
        if not incident:
            raise StopRecoveryRefused("historical_inventory_not_confirmed")
        return await _run_inventory(hass, entry, runtime, bridges, refs, affected, config, owners,
                                    scope_digest, incident["incident_id"])


async def _refresh_held_scope(hass, entry, runtime, incident_id, inventory, bridges, refs, affected, *, allow_positive=False):
    """Reconfirm added Stops on an explicit retry, never alter a running scope."""
    from . import tesla_ev_recovery as manager

    async with manager._journal_lock(runtime):
        state = runtime[manager._SIDECAR_STATE_KEY]
        selected = state["incidents"][incident_id]
        if selected.get("phase") != "held":
            raise StopRecoveryRefused("recovery_already_running")
        basis = selected.get("scope_basis") or {}
        if (basis.get("kind") not in {"ble_stop_inventory_v2", "ble_command_inventory_v2"}
                or basis.get("inventory_digest") != manager._digest(inventory)
                or basis.get("complete_affected_inventory") != inventory
                or selected.get("current_candidate_producers") != [b.identity for b in bridges.values()]
                or selected.get("current_candidate_vehicles") != list(affected.values())
                or not all(b.current() for b in bridges.values())):
            raise StopRecoveryRefused("recovery_scope_changed")
        # Repeat exact operation/binding validation against the authoritative
        # union while holding the persistence lock. A new Stop must not make an
        # old incident permanently unretryable, or permit an unvalidated writer.
        _, current_refs, current_affected = _resolve_inventory(hass, entry, inventory, bridges, allow_positive=allow_positive)
        if current_refs != refs or current_affected != affected:
            raise StopRecoveryRefused("recovery_scope_changed")
        previous = selected.get("liability_refs")
        if previous == refs:
            return
        if not isinstance(previous, list) or not previous:
            raise StopRecoveryRefused("invalid_retained_history")

        def identity(ref):
            return ref.get("vehicle_id"), ref.get("attempt_id"), ref.get("hazard_id")

        def binding(ref):
            return {key: value for key, value in ref.items() if key not in {"outcome", "state"}}

        def retained(ref):
            if not isinstance(ref, Mapping):
                return False
            exact = current.get(identity(ref))
            if exact is not None:
                return binding(ref) == binding(exact)
            # Containment reuses ble:<prefix>. The durable union moves its
            # previous quarantine to a history key. Require the exact original
            # attempt and complete binding, never adopt the new stable-key row.
            if (not isinstance(ref.get("hazard_id"), str) or not ref["hazard_id"]
                    or not isinstance(ref.get("attempt_id"), str) or not ref["attempt_id"]):
                return False
            historical = {key: value for key, value in binding(ref).items() if key != "hazard_id"}
            history_key = re.escape(f"{ref['hazard_id']}@{ref['attempt_id']}") + r"(?:#(?:[2-9]|[1-9][0-9]+))?"
            matches = [candidate for candidate in refs
                       if isinstance(candidate.get("hazard_id"), str)
                       and re.fullmatch(history_key, candidate["hazard_id"])
                       and {key: value for key, value in binding(candidate).items()
                            if key != "hazard_id"} == historical]
            return len(matches) == 1

        current = {identity(ref): ref for ref in refs}
        if len(current) != len(refs) or any(not retained(ref) for ref in previous):
            # Do not erase a missing old liability or adopt a rebound hazard.
            raise StopRecoveryRefused("retained_recovery_scope_changed")
        liability = state["liabilities"].get(incident_id)
        if not isinstance(liability, dict):
            raise StopRecoveryRefused("invalid_retained_history")
        before = deepcopy(state)
        selected.setdefault("scope_refreshes", []).append({
            "previous_liability_refs": deepcopy(previous),
            "previous_covered_attempts": deepcopy(selected.get("covered_attempts")),
            "previous_operator_confirmation": deepcopy(selected.get("operator_scope_confirmation")),
            "refreshed_at": time.time(),
        })
        selected["liability_refs"] = deepcopy(refs)
        selected["covered_attempts"] = deepcopy(refs)
        liability["references"] = deepcopy(refs)
        selected["scope_closed"] = False
        selected["operator_scope_confirmation"] = None
        manager._next_revision(state)
        try:
            if not await manager._save_sidecar(hass, entry, runtime, state):
                raise StopRecoveryRefused("recovery_scope_not_persisted")
        except BaseException:
            manager._restore_sidecar_state(state, before)
            raise


async def _run_inventory(hass, entry, runtime, bridges, refs, affected, config, owners, scope_digest, incident_id):
    from . import tesla_ev_recovery as manager

    token = uuid4().hex
    runtime[ACTIVE_KEY] = {"token": token, "vehicles": tuple(affected.values())}
    generations = {}

    def scope_current():
        try:
            state = runtime[manager._SIDECAR_STATE_KEY]
            incident = state["incidents"][incident_id]
            _check_other_entries(hass, runtime, bridges)
            return bool(
                manager._runtime(hass, entry) is runtime and manager._sidecar_ready(runtime)
                and not runtime.get(manager._RECOVERY_STOPPING_KEY)
                and runtime.get(ACTIVE_KEY, {}).get("token") == token
                and not runtime.get(ACTIVE_KEY, {}).get("interrupted")
                and _configuration(entry) == config
                and all(b.current() for b in bridges.values())
                and all(manager._owner_snapshot(hass, entry, vid) == owner for vid, owner in owners.items())
                and manager._digest(_live_refs(runtime)) == scope_digest
                and manager._digest(incident.get("liability_refs")) == scope_digest
                and incident.get("scope_closed") is True
                and incident.get("current_candidate_vehicles") == list(affected.values())
                and incident.get("current_candidate_producers") == [b.identity for b in bridges.values()]
            )
        except (KeyError, TypeError, StopRecoveryRefused):
            return False

    def current():
        if not scope_current():
            return False
        incident = runtime[manager._SIDECAR_STATE_KEY]["incidents"][incident_id]
        return bool(
            incident.get("phase") in {"quiescing", "reset_pending", "reconciling"}
            and incident.get("phase_token") == token and incident.get("generation") == generations
            and all(manager.current_vehicle_generation(hass, entry, vid) == gen for vid, gen in generations.items())
        )

    async def phase(name, evidence=None):
        async with manager._journal_lock(runtime):
            if not (current() if generations else scope_current()):
                raise StopRecoveryRefused("recovery_scope_changed")
            state = runtime[manager._SIDECAR_STATE_KEY]
            selected = state["incidents"][incident_id]
            if not generations and selected.get("phase") != "held":
                raise StopRecoveryRefused("recovery_already_running")
            if selected.get("scope_closed") is not True:
                raise StopRecoveryRefused("historical_inventory_not_confirmed")
            before = deepcopy(state)
            if not generations:
                for vid in affected.values():
                    gen = manager.current_vehicle_generation(hass, entry, vid) + 1
                    state["generations"][vid] = gen
                    generations[vid] = gen
                selected["generation"] = dict(generations)
            selected["phase"] = name
            selected["phase_token"] = token
            if evidence is not None:
                selected["retirement_evidence"] = [deepcopy(evidence)]
                selected["reconciliation_evidence"] = deepcopy(evidence)
            state["obligations"][incident_id] = {"phase": name, "phase_token": token,
                "incident_id": incident_id, "vehicles": list(affected.values()), "generations": dict(generations)}
            manager._next_revision(state)
            try:
                if not await manager._save_sidecar(hass, entry, runtime, state):
                    manager._restore_sidecar_state(state, before)
                    raise StopRecoveryRefused("recovery_phase_not_persisted")
            except BaseException:
                manager._restore_sidecar_state(state, before)
                raise

    succeeded = False
    try:
        await phase("quiescing")
        # This is a complete-entry inventory. Drain every actual submission,
        # including metadata keyed by a physical VIN rather than a legacy alias.
        if not current() or not await manager.async_drain_submissions(hass, entry) or not current():
            raise StopRecoveryRefused("submission_drain_or_scope_failed")
        await phase("reset_pending")
        for bridge in bridges.values():
            await bridge.prepare(current)
        evidence = {}
        for prefix, bridge in bridges.items():
            evidence[prefix] = await bridge.wait_verified(current)
        # All declared producers/vehicles must have independent new native
        # samples before ANY covered record is retired or bridge unlocked.
        await phase("reconciling", evidence)
        async with manager._journal_lock(runtime):
            if not current():
                raise StopRecoveryRefused("recovery_scope_changed")
            state = runtime[manager._SIDECAR_STATE_KEY]
            before, before_projection = deepcopy(state), manager._snapshot_recovery(runtime)
            try:
                # Include durable-only liabilities hidden by a stale legacy
                # projection. Every captured record must receive its marker.
                runtime[manager.RECOVERY_KEY] = _retained_root(runtime)
                selected = state["incidents"][incident_id]
                selected["reconciliation_evidence"] = deepcopy(evidence)
                selected["resume_binding"] = {"config_digest": manager._digest(config), "owners": deepcopy(owners)}
                manager._retire_recovery_liabilities(runtime, refs, incident_id, {"protocol": 2, "proofs": evidence})
                for vid in affected.values():
                    manager._ledger(runtime, vid)["route"] = None
                selected.update(phase="released", phase_token=uuid4().hex, released_at=time.time(),
                                historical_outcome="unknown", activation_pending=True)
                state["obligations"][incident_id].update(phase="released", phase_token=selected["phase_token"])
                state["liabilities"][incident_id].update(state="retained_reconciled", historical_outcome="unknown")
                if not manager._sync_sidecar_projection(runtime, state):
                    raise StopRecoveryRefused("recovery_projection_not_persisted")
                manager._next_revision(state)
                if not await manager._save_sidecar(hass, entry, runtime, state):
                    raise StopRecoveryRefused("recovery_release_not_persisted")
            except BaseException:
                manager._restore_sidecar_state(state, before)
                manager._restore_recovery(runtime, before_projection)
                raise
        # A failed HA save must never leave the firmware unlocked. The
        # retirement proof and exact release are durable before commit is
        # sent. An interrupted unlock can be retried against this same record.
        succeeded = True
        return await _resume_released(hass, entry, runtime, bridges,
            runtime[manager._SIDECAR_STATE_KEY]["incidents"][incident_id]["scope_basis"]["complete_affected_inventory"],
            expected_incident=incident_id)
    finally:
        if not succeeded:
            for bridge in bridges.values():
                try:
                    await asyncio.shield(bridge.contain())
                except BaseException:
                    pass # durable incident remains held even if containment fails
            incident = runtime.get(manager._SIDECAR_STATE_KEY, {}).get("incidents", {}).get(incident_id, {})
            if (manager._runtime(hass, entry) is runtime
                    and incident.get("phase_token") == token
                    and incident.get("generation") == generations
                    and all(manager.current_vehicle_generation(hass, entry, vid) == gen for vid, gen in generations.items())):
                await manager._mark_recovery_failure(hass, entry, runtime, incident_id, "ble_stop_inventory_incomplete")
        if runtime.get(ACTIVE_KEY, {}).get("token") == token:
            runtime.pop(ACTIVE_KEY, None)


async def _resume_released(hass, entry, runtime, bridges, inventory, *, expected_incident=None):
    """Idempotently finish a bridge unlock after an already durable release."""
    from . import tesla_ev_recovery as manager

    state = runtime[manager._SIDECAR_STATE_KEY]
    matches = [item for item in state["incidents"].values()
        if item.get("phase") == "released"
        and (item.get("scope_basis") or {}).get("kind") in {"ble_stop_inventory_v2", "ble_command_inventory_v2"}
        and item["scope_basis"].get("inventory_digest") == manager._digest(inventory)
        and (expected_incident is None or item.get("incident_id") == expected_incident)]
    if not matches or any(item.get("phase") != "released" for item in state["incidents"].values()):
        raise StopRecoveryRefused("no_matching_completed_recovery")
    matches = [item for item in matches
        if item.get("current_candidate_producers") == [b.identity for b in bridges.values()]
        and all(manager.current_vehicle_generation(hass, entry, vid) == gen for vid, gen in item.get("generation", {}).items())]
    if len(matches) != 1:
        raise StopRecoveryRefused("completed_recovery_generation_ambiguous")
    selected = matches[0]
    incident_id, phase_token = selected["incident_id"], selected["phase_token"]
    generations = deepcopy(selected["generation"])
    binding = selected.get("resume_binding", {})
    proofs = selected.get("reconciliation_evidence", {})
    if (set(proofs) != set(bridges) or binding.get("config_digest") != manager._digest(_configuration(entry))
            or selected.get("current_candidate_producers") != [b.identity for b in bridges.values()]):
        raise StopRecoveryRefused("completed_recovery_binding_changed")
    token = uuid4().hex
    previous_active = runtime.get(ACTIVE_KEY)
    if isinstance(previous_active, dict) and previous_active.get("interrupted"):
        raise StopRecoveryRefused("recovery_preempted_by_stop")
    runtime[ACTIVE_KEY] = {"token": token, "vehicles": tuple(generations)}

    def current():
        try:
            _check_other_entries(hass, runtime, bridges)
            incident = runtime[manager._SIDECAR_STATE_KEY]["incidents"][incident_id]
            return bool(manager._runtime(hass, entry) is runtime and manager._sidecar_ready(runtime)
                and not runtime.get(manager._RECOVERY_STOPPING_KEY)
                and runtime.get(ACTIVE_KEY, {}).get("token") == token
                and not runtime.get(ACTIVE_KEY, {}).get("interrupted")
                and not _live_refs(runtime) and incident.get("phase") == "released"
                and incident.get("phase_token") == phase_token
                and incident.get("generation") == generations
                and manager._digest(_configuration(entry)) == binding.get("config_digest")
                and all(b.current() for b in bridges.values())
                and all(manager.current_vehicle_generation(hass, entry, vid) == gen for vid, gen in generations.items())
                and all(manager._owner_snapshot(hass, entry, vid) == owner for vid, owner in binding.get("owners", {}).items()))
        except (KeyError, TypeError, StopRecoveryRefused):
            return False

    try:
        if not current():
            raise StopRecoveryRefused("completed_recovery_scope_changed")
        for prefix, bridge in bridges.items():
            old = proofs[prefix]["second"]
            proof = bridge.proof()
            if not (proof["n"] == old["n"] and proof["g"] == old["g"] and proof["i"] == bridge.vin):
                raise StopRecoveryRefused("completed_recovery_producer_changed")
            if proof["m"] in {"i", "r"}:
                continue # NVS says the exact completed generation is unlocked
            if proof["b"] != old["b"] or proof["o"] != old["o"]:
                raise StopRecoveryRefused("completed_recovery_boot_changed")
            bridge.baseline = {"b": old["o"], "g": old["g"] - 1}
            bridge.nonce, bridge.started, bridge.prepared = old["n"], time.time(), True
            fresh = await bridge.wait_verified(current)
            await bridge.unlock(fresh, current)
        # Keep normal admission closed across HA restarts until every
        # firmware commit is acknowledged and this installation is durable.
        async with manager._journal_lock(runtime):
            if not current():
                raise StopRecoveryRefused("completed_recovery_scope_changed")
            state = runtime[manager._SIDECAR_STATE_KEY]
            selected = state["incidents"][incident_id]
            before = deepcopy(state)
            selected["activation_pending"] = False
            selected["activated_at"] = time.time()
            manager._next_revision(state)
            try:
                if not await manager._save_sidecar(hass, entry, runtime, state):
                    raise StopRecoveryRefused("recovery_activation_not_persisted")
            except BaseException:
                manager._restore_sidecar_state(state, before)
                raise
        return {"success": True, "incident_id": incident_id}
    finally:
        if runtime.get(ACTIVE_KEY, {}).get("token") == token:
            if previous_active is None:
                runtime.pop(ACTIVE_KEY, None)
            else:
                runtime[ACTIVE_KEY] = previous_active
