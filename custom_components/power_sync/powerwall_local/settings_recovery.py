"""Explicit, incident-bound adoption of current settings under residual risk.

This is an administrator policy exception, never evidence that an old signed
full-document write completed. There is deliberately no device-write callback.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
import json
import math
import time

from .battery_output import digest
from .native_site_limits import native_fields, parse_config_json

POLICY = "supervised_local_settings_v1"
MAX_HISTORY = 8


def identity_for_entry(entry):
    from .native_site_limits import identity_for_entry as native_identity
    return {"entry_id": entry.entry_id, **native_identity(entry)}


def store_state(data, identity):
    """Reconstruct admission; a changed pairing cannot inherit an exception."""
    history = data.get("tesla_settings_history", [])
    if not isinstance(history, list) or any(not isinstance(item, dict) for item in history):
        raise SettingsRecoveryError("settings_recovery_history_invalid")
    pending = data.get("tesla_settings_pending")
    if not pending:
        for item in history:
            recovery = item.get("supervised_local_recovery")
            if (not isinstance(recovery, dict) or recovery.get("policy") != POLICY
                    or recovery.get("identity") != identity):
                pending = {key: deepcopy(value) for key, value in item.items()
                           if key != "supervised_local_recovery"}
                pending["recovery_binding_changed"] = True
                break
    return {"pending": pending, "history": deepcopy(history)}


def recovery_status(history):
    """Expose retained uncertainty without exporting configuration or actor IDs."""
    return [{"incident_id": item.get("id"), "site_id": item.get("site_id"),
             "old_outcome": "terminal" if item.get("terminal") is True else "unknown",
             "policy": item["supervised_local_recovery"]["policy"],
             "accepted_at": item["supervised_local_recovery"].get("confirmed_at")}
            for item in history if isinstance(item.get("supervised_local_recovery"), dict)]


class SettingsRecoveryError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code.replace("_", " "))


class LocalSettingsRecovery:
    """Keep the original unknown incident and atomically accept a new baseline."""

    def __init__(self, *, identity, load, save, read, guard, gate, clock=time.time):
        self.identity, self.load, self.save = identity, load, save
        self.read, self.guard, self.gate, self.clock = read, guard, gate, clock

    def _check(self, identity, incident, *, owner=None):
        if self.identity() != identity or not self.guard(incident, owner=owner):
            raise SettingsRecoveryError("settings_recovery_authority_changed")

    async def _proposal(self):
        state = await self.load()
        incident = state.get("pending")
        identity = deepcopy(self.identity())
        if (not isinstance(incident, dict) or not incident.get("id")
                or incident.get("kind") is not None
                or incident.get("recovery_binding_changed")
                or incident.get("site_id") != identity.get("site_id")
                or not identity.get("gateway") or not identity.get("pairing")):
            raise SettingsRecoveryError("legacy_settings_incident_required")
        expiry = incident.get("expires_at")
        if (type(expiry) not in (int, float) or not math.isfinite(expiry)
                or self.clock() <= expiry + 60):
            # Quiescence precondition only: this does NOT prove remote expiry.
            raise SettingsRecoveryError("settings_acceptance_window_open")
        self._check(identity, incident)
        documents = []
        for _ in range(2):
            document = await self.read()
            self._check(identity, incident)
            if (not isinstance(document, dict) or not isinstance(document.get("hash"), str)
                    or not document["hash"] or not isinstance(document.get("config"), dict)):
                raise SettingsRecoveryError("complete_native_configuration_required")
            # Reject lossy numbers/non-finite data even from an injected reader.
            parse_config_json(json.dumps(document["config"], allow_nan=False))
            documents.append(deepcopy(document))
        if documents[0] != documents[1]:
            raise SettingsRecoveryError("settings_configuration_changed")
        latest = await self.load()
        self._check(identity, incident)
        if latest != state:
            raise SettingsRecoveryError("settings_incident_changed")
        binding = {"policy": POLICY, "identity": identity, "incident": incident,
                   "history": state.get("history", []), "document": documents[-1]}
        return state, binding, digest(binding)

    async def execute(self, data, *, actor):
        if actor is None:
            raise SettingsRecoveryError("administrator_required")
        if not isinstance(data, dict) or set(data) - {
            "operation", "entry_id", "site_id", "incident_id", "expected_recovery_revision",
            "acknowledge", "accept_unknown_full_configuration_write",
        }:
            raise SettingsRecoveryError("invalid_settings_recovery_request")
        operation = data.get("operation", "inspect")
        if operation not in ("inspect", "resume"):
            raise SettingsRecoveryError("invalid_settings_recovery_operation")
        if operation == "resume" and (data.get("acknowledge") is not True
                or data.get("accept_unknown_full_configuration_write") is not True):
            raise SettingsRecoveryError("unknown_full_configuration_risk_acknowledgement_required")
        async with self.gate():
            existing = await self.load()
            if not existing.get("pending") and existing.get("history"):
                return {"success": True, "local_settings_resumed": True,
                        "hardware_command_sent": False, "already_resumed": True,
                        "retained_incidents": recovery_status(existing["history"])}
            state, binding, revision = await self._proposal()
            incident = binding["incident"]
            result = {"success": True, "policy": POLICY, "recovery_revision": revision,
                "entry_id": binding["identity"]["entry_id"], "site_id": incident["site_id"],
                "incident_id": incident["id"], "old_outcome": "terminal" if incident.get("terminal") is True else "unknown",
                "old_write_may_apply_later": incident.get("terminal") is not True,
                "hardware_command_sent": False, "local_settings_resumed": False,
                "limits": native_fields(binding["document"]["config"])}
            if operation == "inspect":
                return result
            if any(data.get(key) != result[key] for key in ("entry_id", "site_id", "incident_id")):
                raise SettingsRecoveryError("settings_recovery_identity_mismatch")
            if data.get("expected_recovery_revision") != revision:
                raise SettingsRecoveryError("settings_recovery_revision_conflict")
            history = deepcopy(state.get("history", []))
            if not isinstance(history, list) or len(history) >= MAX_HISTORY:
                raise SettingsRecoveryError("settings_recovery_history_full")
            accepted = {**deepcopy(incident), "supervised_local_recovery": {
                "policy": POLICY, "confirmed_by": actor, "confirmed_at": self.clock(),
                "identity": binding["identity"], "recovery_revision": revision,
                "baseline": binding["document"], "old_outcome": result["old_outcome"],
                "accepted_unknown_full_configuration_write": True,
            }}
            history.append(accepted)
            self._check(binding["identity"], incident)
            owner = asyncio.current_task()
            await self.save(state, history, lambda: self._check(binding["identity"], incident, owner=owner))
            return {**result, "local_settings_resumed": True,
                    "verification": "current_configuration_adopted_under_supervision"}
