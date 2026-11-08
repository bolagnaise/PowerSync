"""Pure projections. No HA, clients, network, logging or dynamic getters here."""
from __future__ import annotations

import copy

from .protocol import ACTIONS, ROLES, UNITS, CONTRACT, encode, number, digest, validate_evidence, SupportError

NUMERIC = frozenset(CONTRACT["numeric_fields"]) - {"schema_version", "captured_at", "value", "source_time", "reported_at", "observed_at", "age_s"}
BOOLS = frozenset({"monitoring_mode", "enabled", "allow_grid_charge", "disable_idle", "active", "commitment_eligible"})


def project_fields(raw: dict) -> dict:
    """Select fields, never stringify unknown objects, errors or free text."""
    result = {}
    for key in NUMERIC:
        if key in raw:
            result[key] = number(raw[key])
    for key in BOOLS:
        if type(raw.get(key)) is bool:
            result[key] = raw[key]
    for key in ("action", "type", "planned_action", "effective_action"):
        if type(raw.get(key)) is str and raw[key] in ACTIONS:
            result[key] = raw[key]
    for key in ("reason", "solver_used"):
        if type(raw.get(key)) is str and raw[key] in CONTRACT["string_fields"][key]:
            result[key] = raw[key]
    if type(raw.get("feasible")) is bool:
        result["feasible"] = raw["feasible"]
    return result


def snapshot(raw: dict, *, captured_at: float, version: str) -> dict:
    """Produce an independent, bounded primitive snapshot from reviewed input."""
    entities = []
    for role, state in list(raw.get("entities", {}).items())[:64]:
        if role not in ROLES or type(state) is not dict:
            continue
        unit = state.get("unit") if type(state.get("unit")) is str and state["unit"] in UNITS else None
        value = number(state.get("value")) if unit else None
        source = number(state.get("source_time"))
        report = number(state.get("reported_at"))
        age = max(0, captured_at - source) if source is not None else None
        entities.append({"role": role, "ref": role, "value": value, "unit": unit,
            "source_time": source, "reported_at": report, "observed_at": captured_at,
            "age_s": age, "availability": "available" if value is not None else "unavailable",
            "freshness": "unknown" if age is None else ("stale" if age > 300 else "fresh"),
            "timestamp_quality": "provider_observation" if source is not None else "ha_cache_only"})
    schedule = [project_fields(slot) for slot in raw.get("schedule", [])[:576] if type(slot) is dict]
    optimizer = project_fields(raw.get("optimizer", {}))
    force = project_fields(raw.get("force", {}))
    # The envelope is deliberately explicit that cached state is not a command receipt.
    result = {"schema_version": 1, "captured_at": captured_at, "source": "ha_cache",
        "coherence": "mixed", "installation_assurance": "self_asserted",
        "system": [{"integration_version": version, "settings": project_fields(raw.get("settings", {}))}],
        "entities": entities, "optimizer": [{"state": optimizer, "schedule": schedule,
            "input_revision": raw.get("input_revision"), "source_time": number(raw.get("plan_time")),
            "price_unit": "unknown", "input_provenance": "cached_after_solve",
            "availability": "available" if schedule else "unavailable"}],
        "control_trace": [{"evidence_level": "cached_control_state", "state": force,
            "command_attempt": "not_recorded", "acknowledgement": "not_recorded",
            "hardware_readback": "not_recorded", "physical_effect": "not_recorded"}],
        "logs": [], "logs_availability": "not_recorded"}
    # An input revision must be a projection digest, never an arbitrary provider string.
    result["optimizer"][0]["input_revision"] = digest({"schedule": schedule, "state": optimizer, "settings": result["system"][0]["settings"]})
    if len(encode(result)) > 192 * 1024:
        result["optimizer"][0]["schedule"] = schedule[:96]
        result["truncated"] = True
    return copy.deepcopy(result)


def action_event(raw, kind, timestamp):
    """Project existing HA action events; never include VINs, reasons or free text."""
    result = {"event": kind, "timestamp": timestamp}
    if kind == "optimizer_action":
        if raw.get("status") not in CONTRACT["string_fields"]["status"]:
            raise SupportError("unsupported_event")
        result.update(project_fields(raw))
        result.update(timestamp=timestamp, status=raw["status"], physical_confirmation=False, evidence_level="decision" if raw["status"] in {"monitoring", "evaluating"} else "cached_control_state")
    elif kind == "ev_command":
        name = raw.get("command")
        result["command"] = name if name in CONTRACT["string_fields"]["command"] else "other"
        for field in ("success", "physical_confirmation"):
            if type(raw.get(field)) is bool:
                result[field] = raw[field]
        result["status"] = "observation" if raw.get("record_type") == "observation" else "command_result"
        result["evidence_level"] = "physical_observation" if raw.get("physical_confirmation") is True else "command_result"
    else:
        raise SupportError("unsupported_event")
    validate_evidence({"records": [result]})
    return result


def restore_frame(value):
    try:
        validate_evidence(value)
        if value.get("schema_version") != 1 or type(value.get("entities")) is not list:
            return None
        return copy.deepcopy(value)
    except (SupportError, TypeError, ValueError):
        return None


def select(frame: dict, category: str) -> dict:
    records = frame.get(category, [])
    return {"schema_version": 1, "category": category, "captured_at": frame["captured_at"],
        "source": frame.get("source", "support_recorder"), "coherence": "mixed",
        "installation_assurance": "self_asserted", "records": copy.deepcopy(records),
        "availability": "available" if records else "not_recorded",
        "truncated": frame.get("truncated", False)}
