"""The automation scheduler journals exact versioned requests, never Force."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock
import time

import pytest
from conftest import load_power_sync_submodule

m = load_power_sync_submodule("powerwall_local.automation_controls", "advanced_automation_test")
OUTPUT = {"action_type": "battery_output", "parameters": {"schema_version": 1, "power_w": 1800, "duration_seconds": 60}}
CAP = {"action_type": "native_site_limits", "parameters": {"schema_version": 1, "export_limit_w": 0, "approval_revision": "b" * 64}}


@pytest.mark.parametrize("action", [OUTPUT, CAP])
def test_versioned_action_validation_and_sequence_ownership(action):
    assert m.validate_actions([action]) is None
    assert m.validate_actions([action, {"action_type": "send_notification"}]) is None
    assert m.validate_actions([action, action])
    assert m.validate_actions([action, {"action_type": "restore_normal"}])
    assert m.validate_actions([{**action, "parameters": {**action["parameters"], "schema_version": 0}}])


@pytest.mark.asyncio
@pytest.mark.parametrize("action", [OUTPUT, CAP])
async def test_exact_request_is_durable_before_submission_and_not_replayed(action):
    journal, persisted, receipts, calls = {}, [], {}, []

    async def save():
        persisted.append(deepcopy(journal))

    async def submit(data):
        assert persisted[-1][data["request_id"]] == data
        calls.append(deepcopy(data))
        receipts[data["request_id"]] = {"phase": "active" if action is OUTPUT else "readback_confirmed"}
        return {"success": True}

    async def load():
        return {"requests": deepcopy(receipts)}

    control = SimpleNamespace(load=load, read=AsyncMock(return_value={"control_revision": "a" * 64,
        "configuration_revision": "c" * 64, "approval_revision": "b" * 64}), dispatch=submit, execute=submit)
    runtime = {"battery_output_control": control, "native_site_limits_control": control}
    hass, entry = SimpleNamespace(data={"power_sync": {"entry": runtime}}), SimpleNamespace(entry_id="entry")
    context = {"battery_run_id": "run", "battery_run_started": time.time(), "battery_request_journal": journal,
               "save_battery_request_journal": save, "action_index": 0}
    assert await m.execute(hass, entry, action["action_type"], action["parameters"], context)
    assert await m.execute(hass, entry, action["action_type"], action["parameters"], context)
    assert len(calls) == 1
    if action is OUTPUT:
        assert calls[0]["power_w"] == 1800
        assert 1 <= calls[0]["duration_seconds"] <= 60
        assert "takeover" not in calls[0]
    else:
        assert calls[0]["export_limit_w"] == 0
        assert "import_limit_w" not in calls[0]


@pytest.mark.asyncio
async def test_unknown_receipt_never_replays_and_expired_unsent_request_never_starts(monkeypatch):
    control = SimpleNamespace(load=AsyncMock(return_value={"requests": {}}), read=AsyncMock(return_value={"control_revision": "a" * 64}), dispatch=AsyncMock())
    hass, entry = SimpleNamespace(data={"power_sync": {"entry": {"battery_output_control": control}}}), SimpleNamespace(entry_id="entry")
    context = {"battery_run_id": "run", "battery_run_started": 1000, "battery_request_journal": {},
               "save_battery_request_journal": AsyncMock(), "action_index": 0}
    monkeypatch.setattr(m.time, "time", lambda: 1100)
    assert await m.execute(hass, entry, "battery_output", OUTPUT["parameters"], context) is False
    control.dispatch.assert_not_awaited()
    request_id = "auto:" + m.digest({"run": "run", "index": 0, "action": OUTPUT})
    control.load.return_value = {"requests": {request_id: {"phase": "recovery_required"}}}
    control.read.reset_mock()
    assert await m.execute(hass, entry, "battery_output", OUTPUT["parameters"], context) is False
    control.read.assert_not_awaited()
    control.dispatch.assert_not_awaited()


@pytest.mark.asyncio
async def test_approval_revision_change_prevents_scheduled_limits():
    control = SimpleNamespace(load=AsyncMock(return_value={}), read=AsyncMock(return_value={"approval_revision": "d" * 64}), execute=AsyncMock())
    hass, entry = SimpleNamespace(data={"power_sync": {"entry": {"native_site_limits_control": control}}}), SimpleNamespace(entry_id="entry")
    context = {"battery_run_id": "run", "battery_request_journal": {}, "save_battery_request_journal": AsyncMock()}
    assert await m.execute(hass, entry, "native_site_limits", CAP["parameters"], context) is False
    control.execute.assert_not_awaited()
    context["save_battery_request_journal"].assert_not_awaited()
