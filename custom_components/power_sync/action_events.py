"""Public HA action events. Command results never imply physical confirmation."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

_LOGGER = logging.getLogger(__name__)
EVENT_OPTIMIZER_ACTION = "power_sync_optimizer_action"
EVENT_EV_COMMAND = "power_sync_ev_command"
EVENT_EV_STOP_FAILED = "power_sync_ev_stop_failed"


def fire_action_event(hass: Any, event_type: str, payload: dict[str, Any]) -> None:
    """Publish without allowing an observer failure to interrupt control cleanup."""
    fire = getattr(getattr(hass, "bus", None), "async_fire", None)
    if fire is None:
        return
    try:
        fire(event_type, {"schema_version": 1, **payload})
    except Exception:
        _LOGGER.exception("Could not publish %s", event_type)


def publish_optimizer_action(coordinator: Any, action: Any, status: str,
                             effective_action: str | None = None) -> None:
    """Describe evaluation and runtime targets, without claiming an actuator ACK."""
    fire_action_event(coordinator.hass, EVENT_OPTIMIZER_ACTION, {
        "entry_id": coordinator.entry_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "planned_action": getattr(action, "action", None),
        "effective_action": effective_action,
        "power_w": getattr(action, "power_w", None),
        "status": status,
        "physical_confirmation": False,
    })


def _notification(hass: Any, entry_id: str, vehicle_id: str, *, reason: str | None,
                  clear: bool = False) -> None:
    # Import lazily so the event contract stays usable in pure logic consumers.
    try:
        from homeassistant.components import persistent_notification
        notification_id = f"power_sync_ev_stop_{entry_id}_{vehicle_id}"
        if clear:
            persistent_notification.async_dismiss(hass, notification_id)
        else:
            persistent_notification.async_create(
                hass,
                f"PowerSync could not confirm that EV charging stopped for {vehicle_id}. "
                f"{reason or 'The stop request failed.'} Check the vehicle/charger and its "
                "actual charging power. Continued charging may drain the home battery.",
                title="PowerSync: EV stop needs attention",
                notification_id=notification_id,
            )
    except ImportError:
        _LOGGER.debug("Persistent notifications are unavailable")
    except Exception:
        _LOGGER.exception("Could not update EV stop notification")


def publish_ev_command(hass: Any, config_entry: Any, vehicle_id: str,
                       command: dict[str, Any], *, physical_confirmation: bool = False,
                       record_type: str = "command") -> None:
    """Publish a recorded result; latch one safety warning per unresolved loadpoint."""
    payload = {
        "entry_id": config_entry.entry_id,
        "vehicle_id": vehicle_id,
        "command": command["command"],
        "timestamp": command["at"],
        "source": command["source"],
        "success": command["success"],
        "reason": command.get("reason"),
        "physical_confirmation": physical_confirmation,
        "record_type": "observation" if physical_confirmation else record_type,
        "observed_at": command.get("settled_at") if physical_confirmation else command.get("observed_at"),
    }
    fire_action_event(hass, EVENT_EV_COMMAND, payload)
    if command["source"] != "powersync":
        return
    name = str(command["command"])
    is_stop = name == "stop" or name.startswith("stop_")
    if not is_stop:
        return
    entry = hass.data.setdefault("power_sync", {}).setdefault(config_entry.entry_id, {})
    alerts = entry.setdefault("ev_stop_alerts", {})
    if physical_confirmation and command["success"]:
        if alerts.pop(vehicle_id, None) is not None:
            _notification(hass, config_entry.entry_id, vehicle_id, reason=None, clear=True)
    elif not command["success"] and vehicle_id not in alerts:
        alerts[vehicle_id] = payload
        fire_action_event(hass, EVENT_EV_STOP_FAILED, payload)
        _notification(hass, config_entry.entry_id, vehicle_id, reason=payload["reason"])


def observe_ev_stop(hass: Any, config_entry: Any, vehicle_id: str,
                    command: dict[str, Any], observed_at: datetime | None,
                    *, charging: bool) -> None:
    """Use only telemetry newer than a stop; ignore delayed or future samples."""
    if command.get("source") != "powersync":
        return
    name = str(command.get("command", ""))
    if not (name == "stop" or name.startswith("stop_")) or command.get("settled_at"):
        return
    try:
        stopped_at = datetime.fromisoformat(command["at"])
        if observed_at is None or stopped_at.tzinfo is None or observed_at.tzinfo is None:
            return
        age = (observed_at - stopped_at).total_seconds()
        if age <= 0 or (observed_at - datetime.now(timezone.utc)).total_seconds() > 60:
            return
    except (ValueError, TypeError, KeyError):
        return
    if charging:
        if age < 90 or (datetime.now(timezone.utc) - observed_at).total_seconds() > 120:
            return
        publish_ev_command(hass, config_entry, vehicle_id, {
            **command, "success": False, "observed_at": observed_at.isoformat(),
            "reason": "Vehicle telemetry still reports charging at least 90 seconds after the stop request",
        })
    else:
        command["settled_at"] = observed_at.isoformat()
        publish_ev_command(hass, config_entry, vehicle_id, {
            **command, "command": "stop_observed", "success": True,
            "reason": "Stopped state confirmed by newer vehicle telemetry",
        }, physical_confirmation=True)
