"""Read-only exact-entry inspection of legacy native uncertainty."""
import ast
from copy import deepcopy
import importlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from test_tesla_native_command_journal import _native_hass, _native_identity, _Entry, VIN, ENTITY


@pytest.mark.parametrize("legacy", [False, True])
def test_status_preserves_six_legacy_unknowns_and_other_vehicle(monkeypatch, legacy):
    recovery = importlib.import_module("power_sync.tesla_ev_recovery")
    hass = _native_hass(monkeypatch, "tesla_fleet")
    entry = _Entry()
    route = {"kind": "ha", "entity_id": ENTITY, "entity_identity": _native_identity(hass)}
    attempts = {
        str(index): {"attempt_id": str(index), "operation": operation,
            "entity_id": ENTITY, "route": deepcopy(route), "target": None,
            "original_deadline": None, "outcome": "sent_unknown"}
        for index, operation in enumerate(["rate", *(["stop"] * 5)])
    }
    root = {"vehicles": {
        VIN: {"route": deepcopy(route), "attempts": attempts, "hazards": {}},
        "5YJ3E1EA7NF0000B2": {"route": None, "attempts": {}, "hazards": {}},
    }, "terminal_removals": {VIN: {f"other-{i}": {"attempt_id": f"other-{i}"} for i in range(86)}}}
    runtime = hass.data["power_sync"][entry.entry_id]
    key = recovery.LEGACY_RECOVERY_KEY if legacy else recovery.RECOVERY_KEY
    runtime[key] = root
    runtime[recovery._SIDECAR_STATE_KEY] = {"revision": 1765, "generations": {VIN: 7}}
    before = deepcopy(runtime[key])
    keys = set(runtime)
    status = recovery.ev_recovery_status(hass, entry, VIN)
    assert status["success"] and status["reason"] == "native_recovery"
    assert status["generation"] == 7 and status["journal_revision"] == 1765
    assert len(status["retained_attempts"]) == 6
    assert all(row["target"] is None and row["original_deadline"] is None
        and row["command_request"] is None for row in status["retained_attempts"])
    assert not status["native_retirement_available"] and not status["controls_issued"]
    assert status["missing_evidence"] == "original_attempt_terminal_or_no_late_effect_proof"
    status["retained_attempts"][0]["producer_identity"]["unique_id"] = "changed-response"
    assert runtime[key] == before and set(runtime) == keys
    assert not hass.services.calls


def test_status_preserves_malformed_history(monkeypatch):
    recovery = importlib.import_module("power_sync.tesla_ev_recovery")
    hass = _native_hass(monkeypatch, "tesla_fleet")
    entry = _Entry()
    runtime = hass.data["power_sync"][entry.entry_id]
    runtime[recovery.RECOVERY_KEY] = "malformed"
    result = recovery.ev_recovery_status(hass, entry, VIN)
    assert not result["success"] and result["reason"] == "malformed_recovery"
    assert runtime[recovery.RECOVERY_KEY] == "malformed" and not hass.services.calls
    with pytest.raises(ValueError, match="invalid_vehicle_vin"):
        recovery.ev_recovery_status(hass, entry, "ble_alias")


def test_status_does_not_report_unloaded_history_as_empty(monkeypatch):
    recovery = importlib.import_module("power_sync.tesla_ev_recovery")
    hass = _native_hass(monkeypatch, "tesla_fleet")
    result = recovery.ev_recovery_status(hass, _Entry(), VIN)
    assert not result["success"] and result["reason"] == "recovery_history_unavailable"
    assert result["recovery_required"] is None
    assert recovery.RECOVERY_KEY not in hass.data["power_sync"][_Entry.entry_id]


@pytest.mark.asyncio
@pytest.mark.parametrize("requested", [None, "entry-1", "other-entry", "missing", 12, ""])
async def test_status_service_requires_exact_entry_and_never_finalizes_control(monkeypatch, requested):
    from conftest import load_source_node

    root = Path(__file__).resolve().parents[1]
    hass = _native_hass(monkeypatch, "tesla_fleet")
    entry = _Entry()
    node = load_source_node(root / "custom_components/power_sync/__init__.py", "handle_get_ev_recovery_status")
    scope = {"__name__": "power_sync", "__package__": "power_sync", "hass": hass,
        "entry": entry, "DOMAIN": "power_sync", "ServiceCall": object, "Any": Any}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[])), "status-handler", "exec"), scope)
    handler = scope["handle_get_ev_recovery_status"]
    calls = []

    async def other(call):
        calls.append(call.data)
        return {"other_entry": True}

    hass.data["power_sync"][entry.entry_id]["licensing_control_handlers"] = {"get_ev_recovery_status": handler}
    hass.data["power_sync"]["other-entry"] = {"licensing_control_handlers": {"get_ev_recovery_status": other}}
    call = SimpleNamespace(data={"entry_id": requested, "vin": VIN})
    if requested in (None, "missing", 12, ""):
        with pytest.raises(ValueError):
            await handler(call)
        assert not calls
    else:
        result = await handler(call)
        assert result.get("other_entry", False) is (requested == "other-entry")
    # With one loaded handler, omission is unambiguous.
    hass.data["power_sync"].pop("other-entry")
    assert (await handler(SimpleNamespace(data={"vin": VIN})))["vehicle_id"] == VIN
    source = ast.parse((root / "custom_components/power_sync/__init__.py").read_text())
    registration = next(n for n in ast.walk(source) if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute) and n.func.attr == "async_register"
        and len(n.args) > 2 and isinstance(n.args[1], ast.Constant)
        and n.args[1].value == "get_ev_recovery_status")
    assert isinstance(registration.args[2], ast.Name)
    assert registration.args[2].id == "handle_get_ev_recovery_status"
    assert not hass.services.calls
