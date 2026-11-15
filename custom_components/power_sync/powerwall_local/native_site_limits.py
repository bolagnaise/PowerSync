"""Native site limits: authenticated adjustment, exact CAS and persistent receipts.

Public values are integer watts. Native fields remain signed kilowatts. This
module does not infer connection approval from a mutable gateway setting.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import date
from decimal import Decimal
import hashlib
import json
import math
import time
from uuid import uuid4

from .battery_output import digest, request_identity

FIELDS = {"import_limit_w": ("max_site_meter_power_ac", 1),
          "export_limit_w": ("min_site_meter_power_ac", -1)}
MAX_LIMIT_W = 1_000_000


class SiteLimitError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code.replace("_", " "))


def parse_config_json(blob):
    """Reject lossy/ambiguous JSON before normal parsing can conceal it."""
    def number(token):
        exact = Decimal(token)
        value = float(exact)
        if (not math.isfinite(value) or Decimal(str(value)) != exact
                or exact == exact.to_integral() and abs(exact) > 2**53 - 1):
            raise SiteLimitError("configuration_numeric_precision_unsupported")
        return value if any(char in token for char in ".eE") else int(exact)

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise SiteLimitError("configuration_duplicate_key")
            result[key] = value
        return result

    def invalid(_):
        raise SiteLimitError("configuration_non_finite_number")

    result = json.loads(blob, parse_float=number, parse_int=number,
                        parse_constant=invalid, object_pairs_hook=pairs)
    if not isinstance(result, dict) or not isinstance(result.get("site_info"), dict):
        raise SiteLimitError("native_configuration_unavailable")
    return result


def native_fields(config):
    info = config.get("site_info") if isinstance(config, dict) else None
    if not isinstance(info, dict):
        raise SiteLimitError("native_configuration_unavailable")
    result = {}
    for public, (key, sign) in FIELDS.items():
        raw = info.get(key)
        watts = Decimal(str(raw)) * sign * 1000 if type(raw) in (int, float) and math.isfinite(raw) else None
        supported = watts is not None and watts == watts.to_integral() and 0 <= watts <= MAX_LIMIT_W
        result[public] = {"present": key in info, "raw": raw,
                          "value_w": int(watts) if supported else None,
                          "supported": supported}
    return result


def configuration_revision(document, identity):
    native_hash = document.get("hash")
    if not isinstance(native_hash, str) or not native_hash:
        raise SiteLimitError("native_configuration_hash_unavailable")
    return digest({"hash": native_hash, "identity": identity})


def approval_revision(state):
    return digest(state.get("approval", {}))


def identity_for_entry(entry):
    data = getattr(entry, "data", {})
    return {"site_id": str(data.get("tesla_energy_site_id") or data.get("powerwall_local_energy_site_id")),
            "gateway": data.get("powerwall_local_din"),
            "pairing": digest(data.get("powerwall_local_public_key_der"))}


def recorded_limits_w(state, identity):
    """Restrictive approved/operational/possibly-applied bounds, including zero."""
    if not state:
        return None, None
    if not isinstance(state, dict) or state.get("identity") != identity:
        return 0, 0
    bounds = []
    for public, (native, sign) in FIELDS.items():
        values = []
        approval = state.get("approval", {}).get(public, {})
        if approval.get("active"):
            maximum = approval.get("max_w")
            if approval.get("identity") != identity or type(maximum) is not int or not 0 <= maximum <= MAX_LIMIT_W:
                return 0, 0
            values.append(maximum)
        selected = state.get("operational", {}).get(public)
        if selected is not None:
            values.append(selected if type(selected) is int and 0 <= selected <= MAX_LIMIT_W else 0)
        transaction = state.get("transaction") or {}
        if transaction.get("phase") in ("prepared", "submitted", "outcome_unknown", "recovery_required"):
            original = transaction.get("baseline", {}).get(public)
            if original is not None:
                values.append(original.get("value_w") if original.get("supported") else 0)
            desired = transaction.get("updates", {}).get(f"site_info.{native}")
            if desired is not None:
                values.append(abs(desired) * 1000 if type(desired) in (int, float) and math.isfinite(desired) else 0)
        bounds.append(min(values) if values else None)
    return tuple(bounds)


def approved_envelope(data, state, identity, actor, now):
    if actor is None or data.get("confirm_installation_limits") is not True:
        raise SiteLimitError("owner_approval_required")
    if data.get("expected_approval_revision") != approval_revision(state):
        raise SiteLimitError("approval_revision_conflict")
    reference = data.get("document_reference")
    try:
        approved_on = date.fromisoformat(data.get("approval_date", ""))
    except (ValueError, TypeError):
        raise SiteLimitError("invalid_approval_date") from None
    if not isinstance(reference, str) or not 1 <= len(reference.strip()) <= 200 or approved_on > date.fromtimestamp(now):
        raise SiteLimitError("invalid_approval_reference")
    changes = {key: data[key] for key in FIELDS if key in data}
    if not changes:
        raise SiteLimitError("no_approved_limits_supplied")
    result = deepcopy(state.get("approval", {}))
    for key, value in changes.items():
        if value is not None and (type(value) is not int or not 0 <= value <= MAX_LIMIT_W):
            raise SiteLimitError("invalid_approved_limit")
        result[key] = {"max_w": value, "active": value is not None,
                       "provenance": "owner_attested_installation_network_documentation",
                       "reference": reference.strip(), "approval_date": approved_on.isoformat(),
                       "confirmed_by": actor, "confirmed_at": now, "identity": identity}
    return result


def plan_changes(config, requested, approval, identity):
    current = native_fields(config)
    updates, baseline = {}, {}
    for public, watts in requested.items():
        if public not in FIELDS or type(watts) is not int or not 0 <= watts <= MAX_LIMIT_W:
            raise SiteLimitError("invalid_site_limit")
        held = current[public]
        # A deliberate authenticated edit is authority for this limit. Legacy
        # approval records remain enforced until the new value is read back.
        if held["supported"] and held["value_w"] == watts:
            continue
        key, sign = FIELDS[public]
        baseline[public] = held
        updates[f"site_info.{key}"] = sign * watts / 1000
    return updates, baseline


class NativeSiteLimits:
    """One persistent limits journal behind the existing site route lock."""

    def __init__(self, *, identity, load, save, read, write, guard, lock, changed, clock=time.time, approval_guard=lambda: True, automatic_guard=lambda: True):
        self.approval_guard = approval_guard
        self.automatic_guard = automatic_guard
        self.identity, self.load, self._save = identity, load, save
        self.read_document, self.write_document = read, write
        self.guard, self.lock, self.changed, self.clock = guard, lock, changed, clock

    async def save(self, state, pending):
        transaction = state.get("transaction")
        if transaction:
            state.setdefault("requests", {})[transaction["request_id"]] = {
                key: transaction[key] for key in ("payload_hash", "operation_id", "phase")}
        await self._save(state, pending)

    async def read(self):
        state = await self.load() or {}
        try:
            document = await self.read_document()
        except Exception:
            document = None
        fields = native_fields(document["config"] if document else {"site_info": {}})
        same_identity = not state or state.get("identity", self.identity()) == self.identity()
        return {"schema_version": 1, "configuration_revision": configuration_revision(document, self.identity()) if document else "",
                "approval_revision": approval_revision(state), "read_at": self.clock(), "source": "local" if document else "stored_authority",
                "limits": fields, "approval": state.get("approval", {}),
                "transaction": state.get("transaction"), "snapshots": state.get("snapshots", []),
                "capabilities": {"readable": document is not None, "writable": document is not None and same_identity and self.guard(False),
                    "zero_is_a_limit": True, "unlimited_supported": False,
                    "absent_field_restore_supported": False, "physical_enforcement_verified": False,
                    "identity_rebaseline_required": not same_identity}}

    async def execute(self, data, *, actor=None, automatic=False):
        # Recheck at every write boundary, including after asynchronous reads.
        guard = lambda own: self.guard(own) and (not automatic or self.automatic_guard())
        if not isinstance(data, dict) or set(data) - {
            "operation", "request_id", "expected_config_revision", "expected_approval_revision",
            "import_limit_w", "export_limit_w", "snapshot_id", "confirm_installation_limits",
            "document_reference", "approval_date", "entry_id", "confirm_pairing_change",
        }:
            raise SiteLimitError("invalid_site_limit_request")
        request_identity(data.get("request_id"))
        operation = data.get("operation", "set")
        if operation not in ("set", "restore", "approve", "reconcile", "rebaseline"):
            raise SiteLimitError("invalid_site_limit_operation")
        async with self.lock:
            state = await self.load() or {}
            identity = self.identity()
            seen = state.get("requests", {}).get(data["request_id"])
            if seen:
                if seen["payload_hash"] != digest(data):
                    raise SiteLimitError("request_id_conflict")
                return {"success": seen["phase"] == "readback_confirmed", "replayed": True,
                        "operation_id": seen["operation_id"]}
            if operation == "rebaseline":
                if actor is None or data.get("confirm_pairing_change") is not True or not self.approval_guard() or not guard(True):
                    raise SiteLimitError("administrator_pairing_confirmation_required")
                prior = state.get("transaction") or {}
                if prior.get("phase") in ("submitted", "outcome_unknown", "recovery_required") and (
                        not prior.get("acceptance_until") or self.clock() <= prior["acceptance_until"] + 30):
                    raise SiteLimitError("site_limit_acceptance_pending")
                document = await self.read_document()
                if data.get("expected_config_revision") != configuration_revision(document, identity):
                    raise SiteLimitError("configuration_revision_conflict")
                checked = await self.read_document()
                if checked["hash"] != document["hash"]:
                    raise SiteLimitError("configuration_revision_conflict")
                if data.get("expected_approval_revision") != approval_revision(state):
                    raise SiteLimitError("approval_revision_conflict")
                archived = {key: value for key, value in state.items() if key != "archives"}
                fields = native_fields(document["config"])
                replacement = {"identity": identity, "approval": {},
                    "operational": {key: item["value_w"] if item["supported"] else 0 for key, item in fields.items()},
                    "archives": (state.get("archives", []) + [archived])[-8:],
                    "requests": state.get("requests", {}),
                    "rebaseline_previous_operation_id": prior.get("operation_id"),
                    "rebaseline": {"confirmed_by": actor, "confirmed_at": self.clock(), "readback_hash": document["hash"]}}
                replacement["requests"][data["request_id"]] = {"payload_hash": digest(data), "operation_id": uuid4().hex, "phase": "readback_confirmed"}
                await self.save(deepcopy(replacement), None)
                await self.changed()
                return {"success": True, "hardware_command_sent": False, **await self.read()}
            if state.get("identity", identity) != identity:
                raise SiteLimitError("site_limit_pairing_changed")
            previous = state.get("transaction")
            payload_hash = digest(data)
            seen = state.get("requests", {}).get(data["request_id"])
            if seen:
                if seen["payload_hash"] != payload_hash:
                    raise SiteLimitError("request_id_conflict")
                return {"success": seen["phase"] == "readback_confirmed", "replayed": True,
                        "operation_id": seen["operation_id"], **await self.read()}
            if previous and previous.get("request_id") == data["request_id"]:
                if previous["payload_hash"] != payload_hash:
                    raise SiteLimitError("request_id_conflict")
                return {"success": previous.get("phase") == "readback_confirmed", "replayed": True,
                        **await self.read()}
            if (operation in ("approve", "rebaseline") or "expected_approval_revision" in data) and data.get("expected_approval_revision") != approval_revision(state):
                raise SiteLimitError("approval_revision_conflict")
            state["identity"] = identity
            if operation == "approve":
                # Software authority can be revoked even when a gateway is
                # offline or a previous physical write remains unresolved.
                if not self.approval_guard():
                    raise SiteLimitError("site_limit_approval_unavailable")
                state["approval"] = approved_envelope(data, state, identity, actor, self.clock())
                state.setdefault("requests", {})[data["request_id"]] = {
                    "payload_hash": payload_hash, "operation_id": uuid4().hex, "phase": "readback_confirmed"}
                await self.save(deepcopy(state), "preserve")
                await self.changed()
                return {"success": True, "hardware_command_sent": False,
                        "approval": state["approval"], "approval_revision": approval_revision(state)}
            if not guard(True):
                raise SiteLimitError("site_limit_control_owned_or_unavailable")
            if operation == "reconcile":
                if not previous or previous.get("phase") not in ("prepared", "submitted", "outcome_unknown", "recovery_required"):
                    return {"success": True, "no_op": True, **await self.read()}
                acceptance = previous.get("acceptance_until")
                if previous["phase"] != "prepared" and (not acceptance or self.clock() <= acceptance + 30):
                    raise SiteLimitError("site_limit_acceptance_pending")
                observed = await self.read_document()
                checked = await self.read_document()
                if checked["hash"] != observed["hash"]:
                    raise SiteLimitError("configuration_revision_conflict")
                fields = native_fields(observed["config"])
                keys = previous["baseline"]
                desired = all(fields[key]["present"] and fields[key]["raw"] == previous["updates"][f"site_info.{FIELDS[key][0]}"] for key in keys)
                original = all(fields[key]["present"] == before["present"] and fields[key]["raw"] == before["raw"] for key, before in keys.items())
                if not desired and not original:
                    raise SiteLimitError("site_limit_externally_modified")
                previous["phase"] = "readback_confirmed" if desired else "rejected"
                previous["readback_hash"] = observed["hash"]
                for key in keys:
                    if desired:
                        state.get("approval", {}).pop(key, None)
                    watts = fields[key]["value_w"]
                    state.setdefault("operational", {})[key] = int(watts) if fields[key]["supported"] and float(watts).is_integer() else 0
                if desired and not any(item["snapshot_id"] == previous["operation_id"] for item in state.get("snapshots", [])):
                    state["snapshots"] = (state.get("snapshots", []) + [{"snapshot_id": previous["operation_id"], "identity": identity,
                        "baseline": previous["baseline"], "desired": {key: fields[key]["raw"] for key in keys}}])[-16:]
                await self.save(deepcopy(state), None)
                await self.changed()
                return {"success": True, "reconciled": True, "verification": "gateway_configuration_readback", **await self.read()}
            if previous and previous.get("phase") in ("prepared", "submitted", "outcome_unknown", "recovery_required"):
                raise SiteLimitError("site_limit_reconciliation_required")
            document = await self.read_document()
            if data.get("expected_config_revision") != configuration_revision(document, identity):
                raise SiteLimitError("configuration_revision_conflict")
            requested = {key: data[key] for key in FIELDS if data.get(key) is not None}
            if operation == "restore":
                saved = next((item for item in state.get("snapshots", []) if item["snapshot_id"] == data.get("snapshot_id")), None)
                if saved is None or saved.get("identity") != identity:
                    raise SiteLimitError("saved_limits_snapshot_unavailable")
                fields = native_fields(document["config"])
                requested = {}
                for public, held in saved["baseline"].items():
                    if fields[public]["raw"] != saved["desired"][public] or not fields[public]["present"]:
                        raise SiteLimitError("saved_limit_externally_modified")
                    if not held["present"] or not held["supported"] or not float(held["value_w"]).is_integer():
                        raise SiteLimitError("saved_limit_restoration_unsupported")
                    requested[public] = int(held["value_w"])
            if not requested:
                raise SiteLimitError("no_site_limit_change_supplied")
            updates, baseline = plan_changes(document["config"], requested, state.get("approval", {}), identity)
            if not updates:
                for key in requested:
                    state.get("approval", {}).pop(key, None)
                state.setdefault("operational", {}).update(requested)
                state.setdefault("requests", {})[data["request_id"]] = {
                    "payload_hash": payload_hash, "operation_id": uuid4().hex, "phase": "readback_confirmed"}
                await self.save(deepcopy(state), None)
                await self.changed()
                return {"success": True, "no_op": True, **await self.read()}
            transaction = {"operation_id": uuid4().hex, "request_id": data["request_id"],
                "payload_hash": payload_hash, "identity": identity, "phase": "prepared",
                "native_hash": document["hash"], "baseline": baseline, "updates": updates,
                "approval_revision": approval_revision(state), "created_at": self.clock()}
            state["transaction"] = transaction
            pending = {"id": transaction["operation_id"], "kind": "native_site_limits",
                       "site_id": identity["site_id"], "updates": updates}
            await self.save(deepcopy(state), pending)
            submitted = False

            def validate(config, native_hash):
                if not guard(True) or self.identity() != identity:
                    raise SiteLimitError("site_limit_authority_changed")
                if native_hash.hex() != document["hash"] or configuration_revision(
                        {"hash": native_hash.hex()}, identity) != data["expected_config_revision"]:
                    raise SiteLimitError("configuration_revision_conflict")
                checked, old = plan_changes(config, requested, state.get("approval", {}), identity)
                if checked != updates or old != baseline:
                    raise SiteLimitError("configuration_revision_conflict")
                return True

            async def stage(expires_at):
                nonlocal pending
                transaction.update(phase="submitted", acceptance_until=expires_at)
                pending = {**pending, "expires_at": expires_at}
                await self.save(deepcopy(state), pending)

            def before_write():
                nonlocal submitted
                if not guard(True) or self.identity() != identity:
                    raise SiteLimitError("site_limit_authority_changed")
                submitted = True

            try:
                acknowledged = await self.write_document(updates, validate, stage, before_write)
                transaction["transport_outcome"] = "acknowledged" if acknowledged else "unknown"
                if not acknowledged:
                    raise SiteLimitError("site_limit_outcome_unknown")
                observed = await self.read_document()
                fields = native_fields(observed["config"])
                if any(fields[key]["value_w"] != value for key, value in requested.items()):
                    raise SiteLimitError("site_limit_readback_unconfirmed")
                transaction["phase"] = "readback_confirmed"
                transaction["readback_hash"] = observed["hash"]
                snapshot = {"snapshot_id": transaction["operation_id"], "identity": identity,
                            "baseline": baseline, "desired": {key: fields[key]["raw"] for key in baseline}}
                state["snapshots"] = (state.get("snapshots", []) + [snapshot])[-16:]
                state.setdefault("operational", {}).update(requested)
                for key in requested:
                    state.get("approval", {}).pop(key, None)
                await self.save(deepcopy(state), None)
                await self.changed()
                return {"success": True, "verification": "gateway_configuration_readback",
                        "physical_enforcement_verified": False, **await self.read()}
            except BaseException:
                if transaction.get("phase") == "readback_confirmed":
                    raise  # Replanning failure cannot undo a proven write receipt.
                transaction["phase"] = "recovery_required" if submitted else "rejected"
                await self.save(deepcopy(state), pending if submitted else None)
                await self.changed()  # Invalidate plans even when the outcome is unknown.
                raise
