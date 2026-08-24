"""Runtime EV ownership helpers for coordinated charging decisions."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from typing import Any


DEFAULT_VEHICLE_ID = "_default"
EXTERNAL_OWNER_MODE = "external"
MANUAL_STOP_HOLD_SECONDS = 15 * 60
RECOVERED_OWNERSHIP_MAX_AGE_SECONDS = 15 * 60
EXTERNAL_COMMAND_SETTLE_SECONDS = 90
POWERSYNC_STOP_CONFIRM_SECONDS = 15 * 60


def owner_family(owner_mode: Any = None) -> str:
    """Return the arbitration family for an EV owner mode."""
    mode = str(owner_mode or "dynamic")
    if mode.startswith("price_level"):
        return "price_level"
    if mode.startswith("smart_schedule"):
        return "smart_schedule"
    if mode.startswith("solar_surplus"):
        return "solar_surplus"
    if mode.startswith("scheduled"):
        return "scheduled"
    if mode.startswith("manual"):
        return "manual"
    return mode


def is_solar_surplus_owner_mode(owner_mode: Any = None) -> bool:
    """Return whether an owner mode represents a solar-surplus session."""
    mode = str(owner_mode or "dynamic")
    return mode.startswith("solar_surplus") or mode.endswith("_solar_surplus")


def can_take_over_ev_ownership(
    existing_mode: Any,
    requested_mode: Any,
    *,
    allow_takeover: bool = False,
) -> bool:
    """Return whether one EV owner mode may replace another."""
    requested_family = owner_family(requested_mode)
    existing_family = owner_family(existing_mode)

    if str(existing_mode) == str(requested_mode) or existing_family == requested_family:
        return True

    if requested_family == "manual":
        return True

    # Boost is an explicit user command and historically superseded automated
    # charging owners. Keep that priority while the dynamic controller retains
    # ownership and phase-load enforcement for the boost window.
    if requested_family == "boost":
        return True

    if existing_family == "manual":
        return False

    return bool(allow_takeover and is_solar_surplus_owner_mode(existing_mode))


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _as_utc_datetime(value: Any) -> datetime | None:
    """Return one timezone-aware timestamp, or ``None`` when unusable."""
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value))
        except (TypeError, ValueError):
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def normalize_vehicle_id(vehicle_id: Any = None) -> str:
    """Return the canonical id used by PowerSync for a loadpoint."""
    return str(vehicle_id or DEFAULT_VEHICLE_ID)


def record_manual_stop_hold(
    hass: Any,
    config_entry: Any,
    vehicle_id: Any = None,
    *,
    seconds: int = MANUAL_STOP_HOLD_SECONDS,
    reason: str | None = None,
) -> dict[str, Any]:
    """Temporarily suppress automated EV restarts after a user stop."""
    vid = normalize_vehicle_id(vehicle_id)
    now = datetime.now(timezone.utc)
    expires_at = now.timestamp() + max(1, int(seconds))
    entry = _entry_data(hass, config_entry.entry_id)
    _discard_recovered_ev_ownership(entry, vid)
    holds = entry.setdefault("ev_manual_stop_holds", {})
    hold = {
        "vehicle_id": vid,
        "created_at": now.isoformat(),
        "expires_at": datetime.fromtimestamp(expires_at, timezone.utc).isoformat(),
        "reason": reason or "manual stop",
    }
    holds[vid] = hold
    _schedule_runtime_save(hass, config_entry)
    return hold


def manual_stop_hold_reason(
    hass: Any,
    config_entry: Any,
    vehicle_id: Any = None,
) -> str | None:
    """Return a restart-blocking reason while a manual stop hold is active."""
    now = datetime.now(timezone.utc)
    entry = _entry_data(hass, config_entry.entry_id)
    holds = entry.setdefault("ev_manual_stop_holds", {})
    candidates = [normalize_vehicle_id(vehicle_id)]
    if candidates[0] != DEFAULT_VEHICLE_ID:
        candidates.append(DEFAULT_VEHICLE_ID)

    for vid in candidates:
        hold = holds.get(vid)
        if not isinstance(hold, dict):
            continue
        try:
            expires_at = datetime.fromisoformat(str(hold.get("expires_at")))
        except (TypeError, ValueError):
            holds.pop(vid, None)
            continue
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at <= now:
            holds.pop(vid, None)
            continue
        return "Manual stop hold active until " + expires_at.astimezone().strftime("%H:%M")

    return None


def _entry_data(hass: Any, entry_id: str) -> dict[str, Any]:
    from ..const import DOMAIN

    return hass.data.setdefault(DOMAIN, {}).setdefault(entry_id, {})


def _discard_recovered_ev_ownership(entry: dict[str, Any], vehicle_id: Any) -> None:
    """Discard restart evidence superseded by a newer user or runtime owner."""
    recovered = entry.get("ev_recovered_ownership")
    if not isinstance(recovered, dict):
        return
    vid = normalize_vehicle_id(vehicle_id)
    recovered.pop(vid, None)
    if vid != DEFAULT_VEHICLE_ID:
        recovered.pop(DEFAULT_VEHICLE_ID, None)
    if not recovered:
        entry.pop("ev_recovered_ownership_saved_at", None)


def _runtime_snapshot(hass: Any, config_entry: Any) -> dict[str, Any]:
    """Return the EV runtime state safe to persist across restarts."""
    return {
        "active_ownership": dict(get_ev_ownerships(hass, config_entry)),
        "last_commands": dict(get_ev_last_commands(hass, config_entry)),
        "manual_stop_holds": dict(
            _entry_data(hass, config_entry.entry_id).get("ev_manual_stop_holds", {})
        ),
        "saved_at": _now_iso(),
    }


async def persist_ev_runtime_state(
    hass: Any,
    config_entry: Any,
    store: Any = None,
) -> None:
    """Persist EV runtime diagnostics without relying on active leases later."""
    entry = _entry_data(hass, config_entry.entry_id)
    runtime_store = store or entry.get("automation_store")
    if runtime_store is None or not hasattr(runtime_store, "_data"):
        return

    runtime_store._data["ev_runtime_state"] = _runtime_snapshot(hass, config_entry)
    save = getattr(runtime_store, "async_save", None)
    if save is None:
        return

    result = save()
    if hasattr(result, "__await__"):
        await result


def _schedule_runtime_save(hass: Any, config_entry: Any) -> None:
    """Schedule a best-effort runtime persistence save."""
    entry = _entry_data(hass, config_entry.entry_id)
    if not entry.get("automation_store"):
        return
    create_task = getattr(hass, "async_create_task", None)
    if create_task is None:
        return
    create_task(persist_ev_runtime_state(hass, config_entry, entry.get("automation_store")))


def restore_ev_runtime_state(
    hass: Any,
    config_entry: Any,
    store: Any = None,
) -> dict[str, Any]:
    """Restore last EV command diagnostics and clear stale active ownership."""
    entry = _entry_data(hass, config_entry.entry_id)
    runtime_store = store or entry.get("automation_store")
    if runtime_store is None or not hasattr(runtime_store, "_data"):
        return {"restored_ownership": 0, "restored_commands": 0}

    runtime_state = runtime_store._data.get("ev_runtime_state") or {}
    stored_commands = runtime_state.get("last_commands") or {}
    stored_ownership = runtime_state.get("active_ownership") or {}
    stored_manual_stop_holds = runtime_state.get("manual_stop_holds") or {}

    if isinstance(stored_commands, dict):
        get_ev_last_commands(hass, config_entry).update(
            {
                normalize_vehicle_id(vehicle_id): dict(command)
                for vehicle_id, command in stored_commands.items()
                if isinstance(command, Mapping)
            }
        )

    if isinstance(stored_manual_stop_holds, dict):
        entry["ev_manual_stop_holds"] = {
            normalize_vehicle_id(vehicle_id): dict(hold)
            for vehicle_id, hold in stored_manual_stop_holds.items()
            if isinstance(hold, Mapping)
        }

    recovered: dict[str, dict[str, Any]] = {}
    resumable_manual_sessions: dict[str, dict[str, Any]] = {}
    expired_manual_sessions: dict[str, dict[str, Any]] = {}
    now = datetime.now(timezone.utc)
    if isinstance(stored_ownership, dict):
        for vehicle_id, lease in stored_ownership.items():
            if not isinstance(lease, Mapping):
                continue
            vid = normalize_vehicle_id(vehicle_id)
            owner_mode = str(lease.get("owner_mode") or "dynamic")
            recovered[vid] = dict(lease)
            if owner_mode == "manual" and lease.get("quick_control"):
                try:
                    expires_at = datetime.fromisoformat(str(lease.get("expires_at")))
                    if expires_at.tzinfo is None:
                        expires_at = expires_at.replace(tzinfo=timezone.utc)
                    target = (
                        resumable_manual_sessions
                        if expires_at > now
                        else expired_manual_sessions
                    )
                    target[vid] = dict(lease)
                except (TypeError, ValueError):
                    pass
            get_ev_last_commands(hass, config_entry)[vid] = {
                "command": "ha_restart_recovery",
                "at": _now_iso(),
                "source": "powersync",
                "success": True,
                "reason": f"Cleared stale {owner_mode} ownership after HA restart",
            }

    # Active leases are intentionally not restored: timers and control loops do
    # not survive HA restarts, so restored ownership would become a ghost lock.
    get_ev_ownerships(hass, config_entry).clear()
    entry["ev_recovered_ownership"] = recovered
    entry["ev_recovered_ownership_saved_at"] = runtime_state.get("saved_at")

    if stored_commands or recovered:
        _schedule_runtime_save(hass, config_entry)

    return {
        "restored_ownership": len(recovered),
        "restored_commands": len(stored_commands) if isinstance(stored_commands, dict) else 0,
        "resumable_manual_sessions": resumable_manual_sessions,
        "expired_manual_sessions": expired_manual_sessions,
    }


def consume_recovered_ev_ownership(
    hass: Any,
    config_entry: Any,
    vehicle_id: Any,
    *,
    expected_owner_family: str,
) -> dict[str, Any] | None:
    """Consume one fresh, exact-match ownership snapshot after HA restart.

    Recovered leases are evidence for rebuilding a new runtime controller, not
    live ownership.  Keep this deliberately VIN-scoped and one-shot so a stale
    snapshot cannot be reused for a later external charging session.
    """
    vid = normalize_vehicle_id(vehicle_id)
    entry = _entry_data(hass, config_entry.entry_id)
    recovered = entry.get("ev_recovered_ownership")
    if not isinstance(recovered, dict):
        return None

    saved_at_raw = entry.get("ev_recovered_ownership_saved_at")
    lease = recovered.pop(vid, None)
    if not recovered:
        entry.pop("ev_recovered_ownership_saved_at", None)
    if not isinstance(lease, Mapping):
        return None

    try:
        saved_at = datetime.fromisoformat(str(saved_at_raw))
    except (TypeError, ValueError):
        return None
    if saved_at.tzinfo is None:
        saved_at = saved_at.replace(tzinfo=timezone.utc)

    age_seconds = (datetime.now(timezone.utc) - saved_at).total_seconds()
    if age_seconds < -60 or age_seconds > RECOVERED_OWNERSHIP_MAX_AGE_SECONDS:
        return None
    if lease.get("owner") != "powersync":
        return None
    if owner_family(lease.get("owner_mode")) != expected_owner_family:
        return None

    result = dict(lease)
    result["recovered_saved_at"] = saved_at.isoformat()
    return result


def update_ev_ownership_details(
    hass: Any,
    config_entry: Any,
    vehicle_id: Any,
    **details: Any,
) -> dict[str, Any] | None:
    """Update a live ownership lease with command state worth persisting."""
    _lease_id, lease = get_ev_ownership(hass, config_entry, vehicle_id)
    if lease is None:
        return None
    lease.update(details)
    lease["updated_at"] = _now_iso()
    _schedule_runtime_save(hass, config_entry)
    return lease


def _candidate_vehicle_ids(vehicle_id: Any, leases: Mapping[str, Any]) -> list[str]:
    """Return lease ids that may refer to the same physical loadpoint."""
    vid = normalize_vehicle_id(vehicle_id)
    candidate_ids = [vid]
    if vid != DEFAULT_VEHICLE_ID:
        candidate_ids.append(DEFAULT_VEHICLE_ID)
    else:
        candidate_ids.extend(key for key in leases if key != DEFAULT_VEHICLE_ID)
    return candidate_ids


def get_ev_ownerships(hass: Any, config_entry: Any) -> dict[str, dict[str, Any]]:
    """Return all active ownership leases for a config entry."""
    entry = _entry_data(hass, config_entry.entry_id)
    leases = entry.setdefault("ev_ownership", {})
    return leases if isinstance(leases, dict) else {}


def get_ev_last_commands(hass: Any, config_entry: Any) -> dict[str, dict[str, Any]]:
    """Return last EV command records for a config entry."""
    entry = _entry_data(hass, config_entry.entry_id)
    commands = entry.setdefault("ev_last_command", {})
    return commands if isinstance(commands, dict) else {}


def get_ev_ownership(
    hass: Any,
    config_entry: Any,
    vehicle_id: Any = None,
) -> tuple[str | None, dict[str, Any] | None]:
    """Return the active ownership lease for a loadpoint, considering overlap."""
    leases = get_ev_ownerships(hass, config_entry)
    for candidate_id in _candidate_vehicle_ids(vehicle_id, leases):
        lease = leases.get(candidate_id)
        if isinstance(lease, dict) and lease.get("owner"):
            return candidate_id, lease
    return None, None


def _has_fresh_recovered_powersync_ownership(
    hass: Any,
    config_entry: Any,
    vehicle_id: Any,
) -> bool:
    """Return whether restart evidence still attributes this session to PowerSync."""
    entry = _entry_data(hass, config_entry.entry_id)
    recovered = entry.get("ev_recovered_ownership")
    if not isinstance(recovered, Mapping):
        return False

    saved_at = _as_utc_datetime(entry.get("ev_recovered_ownership_saved_at"))
    if saved_at is None:
        return False
    age_seconds = (datetime.now(timezone.utc) - saved_at).total_seconds()
    if age_seconds < -60 or age_seconds > RECOVERED_OWNERSHIP_MAX_AGE_SECONDS:
        return False

    for candidate_id in _candidate_vehicle_ids(vehicle_id, recovered):
        lease = recovered.get(candidate_id)
        if isinstance(lease, Mapping) and lease.get("owner") == "powersync":
            return True
    return False


def ensure_external_ev_ownership(
    hass: Any,
    config_entry: Any,
    vehicle_id: Any,
    *,
    session_id: str | None = None,
    reason: str = "Observed external charging",
    observed_at: Any = None,
) -> bool:
    """Latch an unowned physical charging session as externally controlled.

    Fresh restart evidence and a still-settling PowerSync command take priority,
    so delayed Tesla telemetry cannot turn PowerSync's own session into an
    external lease.  Once external ownership is established it remains until
    an explicit unplug or a direct manual/Boost takeover.
    """
    _lease_id, lease = get_ev_ownership(hass, config_entry, vehicle_id)
    if lease is not None:
        if lease.get("owner") != EXTERNAL_OWNER_MODE:
            return False
        if lease.get("stop_settling"):
            existing_session_id = lease.get("session_id")
            if (
                session_id
                and existing_session_id
                and session_id != existing_session_id
            ):
                lease.pop("stop_settling", None)
                lease.pop("stop_command_at", None)
                lease.pop("stop_settling_expires_at", None)
                lease["reason"] = reason
            expires_at = _as_utc_datetime(lease.get("stop_settling_expires_at"))
            if expires_at is not None and datetime.now(timezone.utc) >= expires_at:
                lease.pop("stop_settling", None)
                lease.pop("stop_command_at", None)
                lease.pop("stop_settling_expires_at", None)
                lease["reason"] = reason
                lease["updated_at"] = _now_iso()
                _schedule_runtime_save(hass, config_entry)
        if session_id and lease.get("session_id") != session_id:
            lease["session_id"] = session_id
            lease["updated_at"] = _now_iso()
            _schedule_runtime_save(hass, config_entry)
        return True

    if _has_fresh_recovered_powersync_ownership(hass, config_entry, vehicle_id):
        return False

    observed_time = _as_utc_datetime(observed_at)
    commands = get_ev_last_commands(hass, config_entry)
    for candidate_id in _candidate_vehicle_ids(vehicle_id, commands):
        command = commands.get(candidate_id)
        if not isinstance(command, Mapping) or command.get("source") != "powersync":
            continue
        command_time = _as_utc_datetime(command.get("at"))
        if command_time is None:
            continue
        command_age = (datetime.now(timezone.utc) - command_time).total_seconds()
        command_name = str(command.get("command") or "")
        is_successful_stop = bool(
            command.get("success")
            and (command_name == "stop" or command_name.startswith("stop_"))
            and not command.get("settled_at")
        )
        if (
            -60 <= command_age <= POWERSYNC_STOP_CONFIRM_SECONDS
            and is_successful_stop
            and (observed_time is None or observed_time > command_time)
        ):
            claim_ev_ownership(
                hass,
                config_entry,
                vehicle_id,
                owner=EXTERNAL_OWNER_MODE,
                owner_mode=EXTERNAL_OWNER_MODE,
                session_id=session_id,
                reason="Awaiting telemetry confirmation of PowerSync stop",
                extra={
                    "observed": True,
                    "stop_settling": True,
                    "stop_command_at": command_time.isoformat(),
                    "stop_settling_expires_at": (
                        command_time
                        + timedelta(seconds=POWERSYNC_STOP_CONFIRM_SECONDS)
                    ).isoformat(),
                },
            )
            return True
        if (
            -60 <= command_age <= EXTERNAL_COMMAND_SETTLE_SECONDS
            and (observed_time is None or observed_time <= command_time)
        ):
            return False

    claim_ev_ownership(
        hass,
        config_entry,
        vehicle_id,
        owner=EXTERNAL_OWNER_MODE,
        owner_mode=EXTERNAL_OWNER_MODE,
        session_id=session_id,
        reason=reason,
        extra={"observed": True},
    )
    return True


def confirm_powersync_ev_stop(
    hass: Any,
    config_entry: Any,
    vehicle_id: Any,
    *,
    observed_at: Any = None,
) -> bool:
    """Clear only the provisional lease once stopped telemetry is observed."""
    observed_time = _as_utc_datetime(observed_at)
    lease_id, lease = get_ev_ownership(hass, config_entry, vehicle_id)
    if (
        lease_id is not None
        and lease is not None
        and lease.get("owner") == EXTERNAL_OWNER_MODE
        and lease.get("stop_settling")
    ):
        stop_time = _as_utc_datetime(lease.get("stop_command_at"))
        if observed_time is None or (
            stop_time is not None and observed_time <= stop_time
        ):
            return False
        release_ev_ownership(
            hass,
            config_entry,
            lease_id,
            reason="PowerSync stop confirmed by vehicle telemetry",
            command="stop_observed",
            source="powersync",
        )
        return True
    if lease is not None and lease.get("owner") == EXTERNAL_OWNER_MODE:
        return False

    commands = get_ev_last_commands(hass, config_entry)
    for candidate_id in _candidate_vehicle_ids(vehicle_id, commands):
        command = commands.get(candidate_id)
        if not isinstance(command, dict) or command.get("source") != "powersync":
            continue
        command_name = str(command.get("command") or "")
        if not (
            command.get("success")
            and (command_name == "stop" or command_name.startswith("stop_"))
            and not command.get("settled_at")
        ):
            continue
        command_time = _as_utc_datetime(command.get("at"))
        if observed_time is None or (
            command_time is not None and observed_time <= command_time
        ):
            continue
        command["settled_at"] = _now_iso()
        _schedule_runtime_save(hass, config_entry)
        return True
    return False


def release_external_ev_ownership(
    hass: Any,
    config_entry: Any,
    vehicle_id: Any,
    *,
    reason: str = "External vehicle unplugged",
) -> bool:
    """Release only a matching externally-owned loadpoint lease."""
    lease_id, lease = get_ev_ownership(hass, config_entry, vehicle_id)
    if lease_id is None or lease is None or lease.get("owner") != EXTERNAL_OWNER_MODE:
        return False
    release_ev_ownership(
        hass,
        config_entry,
        lease_id,
        reason=reason,
        command="external_unplug",
        source=EXTERNAL_OWNER_MODE,
    )
    return True


def record_ev_command(
    hass: Any,
    config_entry: Any,
    vehicle_id: Any = None,
    *,
    command: str,
    success: bool,
    reason: str | None = None,
    source: str = "powersync",
) -> dict[str, Any]:
    """Remember an EV command attempt without changing ownership."""
    vid = normalize_vehicle_id(vehicle_id)
    now = _now_iso()
    last_command = {
        "command": command,
        "at": now,
        "source": source,
        "success": bool(success),
        "reason": reason,
    }
    get_ev_last_commands(hass, config_entry)[vid] = last_command

    lease_id, lease = get_ev_ownership(hass, config_entry, vid)
    if lease_id is not None and lease is not None:
        lease["last_command"] = last_command
        lease["updated_at"] = now

    _schedule_runtime_save(hass, config_entry)
    return last_command


def can_claim_ev_ownership(
    hass: Any,
    config_entry: Any,
    vehicle_id: Any = None,
    *,
    owner_mode: str,
    allow_takeover: bool = False,
) -> tuple[bool, str | None, dict[str, Any] | None, str | None]:
    """Return whether an owner mode may claim a loadpoint right now."""
    lease_id, lease = get_ev_ownership(hass, config_entry, vehicle_id)
    if lease is None:
        return True, None, None, None

    existing_mode = str(lease.get("owner_mode") or "dynamic")
    if can_take_over_ev_ownership(
        existing_mode,
        owner_mode,
        allow_takeover=allow_takeover,
    ):
        return True, lease_id, lease, None

    reason = f"{existing_mode} already owns this loadpoint"
    return False, lease_id, lease, reason


def claim_ev_ownership(
    hass: Any,
    config_entry: Any,
    vehicle_id: Any = None,
    *,
    owner_mode: str,
    owner: str = "powersync",
    session_id: str | None = None,
    reason: str | None = None,
    command: str | None = None,
    success: bool = True,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Claim ownership of an EV/loadpoint for the current config entry."""
    vid = normalize_vehicle_id(vehicle_id)
    _discard_recovered_ev_ownership(
        _entry_data(hass, config_entry.entry_id),
        vid,
    )
    now = _now_iso()
    leases = get_ev_ownerships(hass, config_entry)
    if vid != DEFAULT_VEHICLE_ID:
        leases.pop(DEFAULT_VEHICLE_ID, None)
    previous = leases.get(vid, {}) if isinstance(leases.get(vid), dict) else {}
    lease: dict[str, Any] = {
        **previous,
        "vehicle_id": vid,
        "owner": owner,
        "owner_mode": owner_mode,
        "started_at": previous.get("started_at") or now,
        "updated_at": now,
        "session_id": session_id if session_id is not None else previous.get("session_id"),
        "reason": reason,
    }
    if extra:
        lease.update(dict(extra))
    if command:
        lease["last_command"] = {
            "command": command,
            "at": now,
            "source": "powersync",
            "success": bool(success),
            "reason": reason,
        }
        get_ev_last_commands(hass, config_entry)[vid] = lease["last_command"]

    leases[vid] = lease
    _schedule_runtime_save(hass, config_entry)
    return lease


def release_ev_ownership(
    hass: Any,
    config_entry: Any,
    vehicle_id: Any = None,
    *,
    reason: str | None = None,
    command: str = "release",
    success: bool = True,
    source: str = "powersync",
) -> dict[str, Any] | None:
    """Release ownership and remember the final command for diagnostics."""
    vid = normalize_vehicle_id(vehicle_id)
    leases = get_ev_ownerships(hass, config_entry)
    previous = leases.pop(vid, None)
    if vid != DEFAULT_VEHICLE_ID:
        default_previous = leases.pop(DEFAULT_VEHICLE_ID, None)
        previous = previous if previous is not None else default_previous
    now = _now_iso()
    last_command = {
        "command": command,
        "at": now,
        "source": source,
        "success": bool(success),
        "reason": reason,
    }
    get_ev_last_commands(hass, config_entry)[vid] = last_command
    _schedule_runtime_save(hass, config_entry)
    return previous if isinstance(previous, dict) else None


def clear_ev_ownerships(
    hass: Any,
    config_entry: Any,
    vehicle_ids: list[Any] | tuple[Any, ...] | set[Any] | None = None,
) -> None:
    """Clear EV ownership leases for selected vehicles, or all if omitted."""
    leases = get_ev_ownerships(hass, config_entry)
    if vehicle_ids is None:
        leases.clear()
        _schedule_runtime_save(hass, config_entry)
        return
    for vehicle_id in vehicle_ids:
        vid = normalize_vehicle_id(vehicle_id)
        leases.pop(vid, None)
        if vid != DEFAULT_VEHICLE_ID:
            leases.pop(DEFAULT_VEHICLE_ID, None)
    _schedule_runtime_save(hass, config_entry)


def get_active_ev_owner_mode(
    hass: Any,
    config_entry: Any,
    vehicle_id: Any = None,
) -> str | None:
    """Return the active owner mode for a loadpoint, considering default overlap."""
    vid = normalize_vehicle_id(vehicle_id)
    _lease_id, lease = get_ev_ownership(hass, config_entry, vid)
    if lease is not None:
        return str(lease.get("owner_mode") or "dynamic")

    return None
