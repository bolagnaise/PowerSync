"""Public action events and EV failed-stop alerts without hardware commands."""
from __future__ import annotations

import importlib.util
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

PATH = Path(__file__).resolve().parents[1] / "custom_components/power_sync/action_events.py"
SPEC = importlib.util.spec_from_file_location("_action_events_test", PATH)
events = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(events)


@pytest.fixture
def env(monkeypatch):
    notice = types.ModuleType("homeassistant.components.persistent_notification")
    notice.async_create, notice.async_dismiss = Mock(), Mock()
    components = types.ModuleType("homeassistant.components")
    components.persistent_notification = notice
    monkeypatch.setitem(sys.modules, "homeassistant.components", components)
    monkeypatch.setitem(sys.modules, "homeassistant.components.persistent_notification", notice)
    hass = SimpleNamespace(data={}, bus=SimpleNamespace(async_fire=Mock()))
    entry = SimpleNamespace(entry_id="entry-1")
    return hass, entry, notice


def command(*, success=True, age=120, name="stop", source="powersync"):
    return {"command": name, "source": source, "success": success, "reason": "window ended",
            "at": (datetime.now(timezone.utc) - timedelta(seconds=age)).isoformat()}


def fired(hass, event_type):
    return [c.args[1] for c in hass.bus.async_fire.call_args_list if c.args[0] == event_type]


def test_ack_is_not_a_physical_stop_confirmation(env):
    hass, entry, notice = env
    events.publish_ev_command(hass, entry, "car", command())
    assert fired(hass, events.EVENT_EV_COMMAND)[0]["physical_confirmation"] is False
    assert fired(hass, events.EVENT_EV_STOP_FAILED) == []
    notice.async_create.assert_not_called()


def test_failed_stop_alert_dedupes_per_entry_and_vehicle(env):
    hass, entry, notice = env
    for _ in range(2):
        events.publish_ev_command(hass, entry, "car-a", command(success=False))
    events.publish_ev_command(hass, entry, "car-b", command(success=False))
    events.publish_ev_command(hass, SimpleNamespace(entry_id="entry-2"), "car-a", command(success=False))
    assert len(fired(hass, events.EVENT_EV_COMMAND)) == 4
    assert len(fired(hass, events.EVENT_EV_STOP_FAILED)) == 3
    assert notice.async_create.call_count == 3
    assert len({c.kwargs["notification_id"] for c in notice.async_create.call_args_list}) == 3


def test_newer_stopped_sample_clears_warning_and_rearms_next_failure(env):
    hass, entry, notice = env
    last = command(success=False)
    events.publish_ev_command(hass, entry, "car", last)
    events.observe_ev_stop(hass, entry, "car", last, datetime.now(timezone.utc), charging=False)
    assert last.get("settled_at")
    assert fired(hass, events.EVENT_EV_COMMAND)[-1]["physical_confirmation"] is True
    notice.async_dismiss.assert_called_once()
    events.publish_ev_command(hass, entry, "car", command(success=False))
    assert len(fired(hass, events.EVENT_EV_STOP_FAILED)) == 2


def test_accepted_retry_does_not_dismiss_unresolved_warning(env):
    hass, entry, notice = env
    events.publish_ev_command(hass, entry, "car", command(success=False))
    events.publish_ev_command(hass, entry, "car", command())
    notice.async_dismiss.assert_not_called()


def test_fresh_charging_after_grace_warns_once_without_rewriting_ack(env):
    hass, entry, notice = env
    last = command()
    for _ in range(2):
        events.observe_ev_stop(hass, entry, "car", last, datetime.now(timezone.utc), charging=True)
    assert last["success"] is True
    assert len(fired(hass, events.EVENT_EV_STOP_FAILED)) == 1
    notice.async_create.assert_called_once()
    assert not last.get("settled_at")


@pytest.mark.parametrize("case", ["missing", "old", "future", "settling", "stale", "settled", "external", "new_start"])
def test_telemetry_must_match_a_current_stop_and_be_fresh(env, case):
    hass, entry, notice = env
    last = command()
    now = datetime.now(timezone.utc)
    observed = now
    if case == "missing": observed = None
    if case == "old": observed = now - timedelta(seconds=130)
    if case == "future": observed = now + timedelta(seconds=120)
    if case == "settling": last = command(age=30)
    if case == "stale":
        last = command(age=400)
        observed = now - timedelta(seconds=180)
    if case == "settled": last["settled_at"] = now.isoformat()
    if case == "external": last["source"] = "external"
    if case == "new_start": last["command"] = "start"
    events.observe_ev_stop(hass, entry, "car", last, observed, charging=True)
    assert not fired(hass, events.EVENT_EV_STOP_FAILED)
    notice.async_create.assert_not_called()


def test_optimizer_monitoring_event_does_not_claim_execution(env):
    hass, entry, _ = env
    coordinator = SimpleNamespace(hass=hass, entry_id=entry.entry_id)
    action = SimpleNamespace(action="idle", power_w=0)
    events.publish_optimizer_action(coordinator, action, "monitoring", "self_consumption")
    payload = fired(hass, events.EVENT_OPTIMIZER_ACTION)[0]
    assert payload["planned_action"] == "idle"
    assert payload["effective_action"] == "self_consumption"
    assert payload["status"] == "monitoring"
    assert payload["physical_confirmation"] is False


def test_event_and_notification_errors_do_not_interrupt_cleanup(env):
    hass, entry, notice = env
    hass.bus.async_fire.side_effect = RuntimeError("bus unavailable")
    notice.async_create.side_effect = RuntimeError("notification unavailable")
    events.publish_ev_command(hass, entry, "car", command(success=False))
    assert "car" in hass.data["power_sync"][entry.entry_id]["ev_stop_alerts"]


def test_first_fresh_charging_sample_after_long_poll_gap_still_warns(env):
    hass, entry, notice = env
    events.observe_ev_stop(hass, entry, "car", command(age=1800),
                           datetime.now(timezone.utc), charging=True)
    assert len(fired(hass, events.EVENT_EV_STOP_FAILED)) == 1
    assert fired(hass, events.EVENT_EV_COMMAND)[0]["observed_at"]


def test_stop_confirmation_carries_observation_time(env):
    hass, entry, _ = env
    now = datetime.now(timezone.utc)
    events.observe_ev_stop(hass, entry, "car", command(), now, charging=False)
    payload = fired(hass, events.EVENT_EV_COMMAND)[0]
    assert payload["observed_at"] == now.isoformat()
    assert payload["timestamp"] != payload["observed_at"]
