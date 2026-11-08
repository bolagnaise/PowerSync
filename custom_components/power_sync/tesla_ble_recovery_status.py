"""Read-only BLE capabilities and retained-history recovery requirements.

Present telemetry cannot qualify an older positive command's execution fence.
This endpoint never refreshes, restarts, unlocks or retires a producer.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def ble_recovery_status(hass: Any, entry: Any) -> dict[str, Any]:
    from .tesla_ble import get_tesla_ble_charge_power_state, get_tesla_ble_observed_at
    from .tesla_ble_mapping import resolve_ble_prefixes
    from .tesla_ble_stop_recovery import (
        StopRecoveryRefused, _live_refs, _retained_root, decode_proof,
    )

    runtime = hass.data.get("power_sync", {}).get(entry.entry_id, {})
    try:
        root = _retained_root(runtime)
        refs = _live_refs(runtime)
    except (StopRecoveryRefused, KeyError, TypeError, ValueError):
        return {"success": False, "error": "invalid_retained_history", "controls_issued": False}
    rows = []
    for ref in refs:
        record = root.get("vehicles", {}).get(ref["vehicle_id"], {}).get("attempts", {}).get(ref.get("attempt_id"), {})
        operation = record.get("operation", "unknown")
        route = record.get("route") or {}
        rows.append({
            "vehicle_id": ref["vehicle_id"], "attempt_id": ref.get("attempt_id"),
            "hazard_id": ref.get("hazard_id"), "operation": operation,
            "transport": route.get("kind", ref.get("kind", "unknown")),
        })
    operations = sorted({row["operation"] if isinstance(row["operation"], str) else "unknown" for row in rows})
    # This is a candidate pathway only. The operator action still validates
    # complete history, exact identities, all producers and fresh native proof.
    stop_only = bool(rows) and all(row["operation"] == "stop" and row["transport"] == "ble" for row in rows)
    config = {**entry.data, **entry.options}
    bridges = []
    for prefix in resolve_ble_prefixes(hass, config):
        power = get_tesla_ble_charge_power_state(hass, prefix)
        observed = get_tesla_ble_observed_at(hass, power)
        age = (datetime.now(timezone.utc) - observed).total_seconds() if observed else None
        entity_id = getattr(power, "entity_id", "")
        proof_state = hass.states.get(f"sensor.{prefix}_powersync_recovery_proof")
        protocol = None
        if proof_state is not None:
            try:
                protocol = decode_proof(proof_state.state)["v"]
            except StopRecoveryRefused:
                pass
        bridges.append({
            "prefix": prefix,
            "power_receipt": (
                "paired_native_value" if entity_id and hass.states.get(f"{entity_id}_sample") is not None
                else "native_timestamp" if entity_id and hass.states.get(f"{entity_id}_received") is not None
                else "legacy_ha_timestamp"
            ),
            "power_sample_age_seconds": round(age, 1) if age is not None else None,
            "power_sample_fresh": age is not None and 0 <= age <= 90,
            "stop_recovery_protocol": protocol,
        })
    return {
        "success": True, "controls_issued": False, "bridges": bridges,
        "retained_history": rows,
        "recovery": {
            "required": bool(rows), "operations": operations,
            "candidate_service": "power_sync.recover_ble_stop_history" if stop_only else None,
            "general_ble_recovery_available": False,
            "reason": (
                "complete_inventory_and_native_stop_proof_required" if stop_only
                else "vehicle_execution_fence_unproven" if rows else "no_retained_history"
            ),
        },
    }
