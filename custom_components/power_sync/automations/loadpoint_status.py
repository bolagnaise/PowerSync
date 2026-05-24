"""Shared helpers for normalized EV loadpoint status."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from typing import Any


ACTIVE_POWER_THRESHOLD_KW = 0.05
DEFAULT_LOADPOINT_KEYS = {"default", "genericev", "ev"}
BRIDGE_LOADPOINT_KEYS = {"wallconnector", "teslaev"}
BRIDGE_WIDGET_KEYS = BRIDGE_LOADPOINT_KEYS | {
    "teslawallconnector",
    "teslable",
    "teslableteslable",
}
GENERIC_CHARGING_STATES = {"charging"}
GENERIC_CONNECTED_STATES = {
    "connected",
    "plugged",
    "pluggedin",
    "preparing",
    "charging",
    "suspendedevse",
    "suspendedev",
    "finishing",
    "paused",
    "stopped",
    "complete",
}


def _float_value(value: Any, default: float = 0.0) -> float:
    """Return a float for loosely typed integration state."""
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _int_value(value: Any, default: int = 0) -> int:
    """Return an int for loosely typed integration state."""
    if value is None:
        return default
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _optional_int(value: Any) -> int | None:
    """Return an optional int for values such as EV SOC."""
    if value is None:
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _normal_key(value: Any) -> str:
    """Normalize a name/id for best-effort loadpoint matching."""
    if value is None:
        return ""
    return re.sub(r"[^a-z0-9]+", "", str(value).lower())


def _is_default_loadpoint(vehicle_id: str, vehicle_name: str) -> bool:
    return (
        _normal_key(vehicle_id) in DEFAULT_LOADPOINT_KEYS
        or _normal_key(vehicle_name) in DEFAULT_LOADPOINT_KEYS
    )


def _is_generic_observation(observation: Mapping[str, Any]) -> bool:
    return (
        observation.get("charger_type") == "generic"
        or _normal_key(observation.get("vehicle_id")) in DEFAULT_LOADPOINT_KEYS
        or _normal_key(observation.get("charger_id")) in DEFAULT_LOADPOINT_KEYS
    )


def _is_bridge_loadpoint(observation: Mapping[str, Any]) -> bool:
    return (
        _normal_key(observation.get("vehicle_id")) in BRIDGE_LOADPOINT_KEYS
        or _normal_key(observation.get("charger_id")) in BRIDGE_LOADPOINT_KEYS
        or _normal_key(observation.get("vehicle_name")) in BRIDGE_LOADPOINT_KEYS
    )


def _is_bridge_widget(widget: Mapping[str, Any]) -> bool:
    name_key = _normal_key(widget.get("vehicle_name"))
    id_key = _normal_key(widget.get("vehicle_id"))
    return name_key in BRIDGE_WIDGET_KEYS or id_key in BRIDGE_WIDGET_KEYS


def _widget_power_kw(widget: Mapping[str, Any]) -> float:
    return _float_value(
        widget.get("current_power_kw", widget.get("ev_power_kw")),
        0.0,
    )


def _widget_is_active(widget: Mapping[str, Any]) -> bool:
    return (
        bool(widget.get("is_charging"))
        or _widget_power_kw(widget) > ACTIVE_POWER_THRESHOLD_KW
    )


def _is_specific_ev_widget(widget: Mapping[str, Any]) -> bool:
    if _is_bridge_widget(widget):
        return False
    name_key = _normal_key(widget.get("vehicle_name"))
    id_key = _normal_key(widget.get("vehicle_id"))
    return bool(name_key or id_key) and name_key not in DEFAULT_LOADPOINT_KEYS


def coalesce_ev_widget_data(
    widgets: Iterable[Mapping[str, Any]] | None,
) -> list[Mapping[str, Any]]:
    """Hide bridge-only telemetry already represented by a named EV widget."""
    rows = list(widgets or [])
    active_specific = [
        row for row in rows
        if _is_specific_ev_widget(row) and _widget_is_active(row)
    ]

    if len(active_specific) != 1:
        return rows

    return [
        row for row in rows
        if not (_is_bridge_widget(row) and _widget_is_active(row))
    ]


def _is_tesla_ble_observation(observation: Mapping[str, Any]) -> bool:
    vehicle_id = str(observation.get("vehicle_id") or "")
    charger_id = str(observation.get("charger_id") or "")
    name = str(observation.get("vehicle_name") or observation.get("name") or "")
    return (
        vehicle_id.startswith("ble_")
        or charger_id.startswith("ble_")
        or _normal_key(name).startswith("teslable")
    )


def _looks_like_tesla_vin(value: Any) -> bool:
    text = str(value or "").strip()
    return len(text) == 17 and text.isalnum() and not text.isdigit()


def _is_physical_tesla_observation(observation: Mapping[str, Any]) -> bool:
    if _is_tesla_ble_observation(observation):
        return False
    return (
        observation.get("charger_type") == "tesla"
        or _looks_like_tesla_vin(observation.get("vehicle_id"))
        or _looks_like_tesla_vin(observation.get("vin"))
    )


def _merge_observation_status(target: dict[str, Any], source: Mapping[str, Any]) -> None:
    source_power = _float_value(
        source.get("ev_power_kw", source.get("current_power_kw")),
        0.0,
    )
    target_power = _float_value(
        target.get("ev_power_kw", target.get("current_power_kw")),
        0.0,
    )
    if source_power > target_power:
        target["ev_power_kw"] = source_power
        target["current_power_kw"] = source_power

    source_soc = source.get("ev_soc", source.get("current_soc"))
    if source_soc is not None:
        target["ev_soc"] = source_soc
        target["current_soc"] = source_soc

    target["is_connected"] = bool(target.get("is_connected")) or bool(source.get("is_connected"))
    target["is_charging"] = bool(target.get("is_charging")) or bool(source.get("is_charging"))


def _physical_tesla_observation_key(observation: Mapping[str, Any]) -> str:
    """Return a stable key for duplicate Fleet/Tesla observations."""
    if not _is_physical_tesla_observation(observation):
        return ""

    for field in ("vehicle_id", "vin"):
        value = observation.get(field)
        if _looks_like_tesla_vin(value):
            return f"vin:{str(value).strip().lower()}"

    name = _normal_key(observation.get("vehicle_name") or observation.get("name"))
    return f"name:{name}" if name else ""


def _merge_duplicate_physical_tesla_observations(
    observations: list[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    """Collapse duplicate Tesla devices for the same physical car."""
    merged: list[Mapping[str, Any]] = []
    by_key: dict[str, int] = {}

    for observation in observations:
        key = _physical_tesla_observation_key(observation)
        if key and key in by_key:
            existing_index = by_key[key]
            target = dict(merged[existing_index])
            _merge_observation_status(target, observation)
            for field in ("vehicle_id", "vin", "vehicle_name", "name", "charger_type"):
                if not target.get(field) and observation.get(field):
                    target[field] = observation[field]
            merged[existing_index] = target
            continue

        if key:
            by_key[key] = len(merged)
        merged.append(observation)

    return merged


def coalesce_vehicle_observations(
    observed_vehicles: Iterable[Mapping[str, Any]] | None,
) -> list[Mapping[str, Any]]:
    """Merge bridge telemetry, such as Tesla BLE, into a single physical EV.

    Tesla BLE exposes local car telemetry through an ESPHome bridge. When the
    same account also has a named Tesla vehicle from Fleet/Teslemetry, the BLE
    prefix is a source for that vehicle rather than a second loadpoint.
    """
    observations = _merge_duplicate_physical_tesla_observations(
        list(observed_vehicles or [])
    )
    ble_indexes = [
        index for index, observation in enumerate(observations)
        if _is_tesla_ble_observation(observation)
    ]
    candidates = [
        index for index, observation in enumerate(observations)
        if (
            index not in ble_indexes
            and not _is_generic_observation(observation)
            and not _is_bridge_loadpoint(observation)
            and _is_physical_tesla_observation(observation)
        )
    ]

    if len(ble_indexes) == 1 and len(candidates) == 1:
        merged = [
            dict(observation) if index == candidates[0] else observation
            for index, observation in enumerate(observations)
        ]
        _merge_observation_status(merged[candidates[0]], observations[ble_indexes[0]])
        return [
            observation for index, observation in enumerate(merged)
            if index != ble_indexes[0]
        ]

    return observations


def _display_name(vehicle_id: str, state: Mapping[str, Any]) -> str:
    params = state.get("params") or {}
    return (
        state.get("vehicle_name")
        or params.get("vehicle_name")
        or params.get("display_name")
        or vehicle_id
    )


def _status_source(
    power_kw: float,
    surplus_kw: float,
    allocated_surplus_kw: float | None = None,
) -> str:
    if power_kw <= ACTIVE_POWER_THRESHOLD_KW:
        return "idle"
    solar_kw = surplus_kw
    if allocated_surplus_kw is not None:
        solar_kw = max(solar_kw, allocated_surplus_kw)
    return "solar" if solar_kw >= power_kw * 0.8 else "grid"


def charging_state_plugged_status(value: Any) -> bool | None:
    """Infer plug state from Tesla charging-state text when available."""
    if value is None:
        return None

    normalized = str(value).strip().lower().replace(" ", "_")
    if normalized in ("", "unknown", "unavailable"):
        return None

    plugged_states = {
        "charging",
        "connected",
        "stopped",
        "complete",
        "starting",
        "no_power",
    }
    if normalized in plugged_states:
        return True
    if normalized in ("disconnected", "none"):
        return False
    return None


def _loadpoint_status(
    connected: bool,
    actually_charging: bool,
    commanded_amps: int,
    paused: bool,
) -> str:
    if paused:
        return "paused"
    if commanded_amps > 0 and not actually_charging:
        return "commanded_no_power"
    if actually_charging:
        return "charging"
    if connected:
        return "connected_idle"
    return "idle"


def _coerce_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _timer_snapshot(
    *,
    phase: str,
    started_at: datetime,
    duration_minutes: Any,
    label: str,
    reason: str,
) -> dict[str, Any]:
    duration_seconds = max(0, int(_float_value(duration_minutes, 0) * 60))
    now = (
        datetime.now(timezone.utc)
        if started_at.tzinfo is not None
        else datetime.now()
    )
    elapsed_seconds = max(0, int((now - started_at).total_seconds()))
    return {
        "phase": phase,
        "label": label,
        "reason": reason,
        "started_at": started_at.isoformat(),
        "duration_seconds": duration_seconds,
        "elapsed_seconds": elapsed_seconds,
        "remaining_seconds": max(0, duration_seconds - elapsed_seconds),
    }


def _solar_surplus_delay_timer(state: Mapping[str, Any]) -> dict[str, Any] | None:
    params = state.get("params") or {}
    mode = str(params.get("dynamic_mode") or state.get("mode") or "").lower()
    owner_mode = str(params.get("owner_mode") or "").lower()
    if "solar_surplus" not in mode and "solar_surplus" not in owner_mode:
        return None

    current_amps = _int_value(state.get("current_amps"), 0)
    low_surplus_start = _coerce_datetime(state.get("low_surplus_start"))
    if low_surplus_start is not None and current_amps > 0:
        return _timer_snapshot(
            phase="stop",
            started_at=low_surplus_start,
            duration_minutes=params.get("stop_delay_minutes", 5),
            label="Stop delay",
            reason="Waiting before stopping because surplus is below the charging minimum",
        )

    high_surplus_start = _coerce_datetime(state.get("high_surplus_start"))
    if (
        high_surplus_start is not None
        and current_amps <= 0
        and not bool(state.get("charging_started"))
    ):
        return _timer_snapshot(
            phase="start",
            started_at=high_surplus_start,
            duration_minutes=params.get("sustained_surplus_minutes", 2),
            label="Start delay",
            reason="Waiting for sustained surplus before starting",
        )

    return None


def _find_observation(
    vehicle_id: str,
    vehicle_name: str,
    observations: list[Mapping[str, Any]],
    used_indexes: set[int],
) -> tuple[int, Mapping[str, Any]] | tuple[None, None]:
    if _is_default_loadpoint(vehicle_id, vehicle_name):
        charging_matches: list[tuple[int, Mapping[str, Any]]] = []
        connected_matches: list[tuple[int, Mapping[str, Any]]] = []
        for index, observation in enumerate(observations):
            if index in used_indexes or _is_generic_observation(observation):
                continue
            power_kw = _float_value(
                observation.get("ev_power_kw", observation.get("current_power_kw")),
                0.0,
            )
            charging = bool(observation.get("is_charging")) or power_kw > ACTIVE_POWER_THRESHOLD_KW
            connected = bool(observation.get("is_connected")) or charging
            if charging:
                charging_matches.append((index, observation))
            elif connected:
                connected_matches.append((index, observation))

        if len(charging_matches) == 1:
            return charging_matches[0]
        if not charging_matches and len(connected_matches) == 1:
            return connected_matches[0]

    vehicle_keys = {
        _normal_key(vehicle_id),
        _normal_key(vehicle_name),
    }
    vehicle_keys.discard("")

    for index, observation in enumerate(observations):
        if index in used_indexes:
            continue
        observed_keys = {
            _normal_key(observation.get("vehicle_id")),
            _normal_key(observation.get("charger_id")),
            _normal_key(observation.get("vin")),
            _normal_key(observation.get("vehicle_name")),
            _normal_key(observation.get("name")),
        }
        observed_keys.discard("")
        if vehicle_keys & observed_keys:
            return index, observation
        if any(a and b and (a in b or b in a) for a in vehicle_keys for b in observed_keys):
            return index, observation

    return None, None


def _lookup_alias(
    values: Mapping[str, Mapping[str, Any]] | None,
    *keys: Any,
) -> Mapping[str, Any] | None:
    """Return a mapping entry by exact key or common loadpoint aliases."""
    if not values:
        return None

    aliases: list[str] = []
    for key in keys:
        if key is None:
            continue
        text = str(key)
        if not text:
            continue
        aliases.append(text)
        if text.startswith("ocpp_"):
            aliases.append(text[5:])
        else:
            aliases.append(f"ocpp_{text}")

    for alias in aliases:
        match = values.get(alias)
        if isinstance(match, Mapping):
            return match

    normalized = {_normal_key(alias) for alias in aliases}
    normalized.discard("")
    for key, value in values.items():
        if _normal_key(key) in normalized and isinstance(value, Mapping):
            return value

    return None


def _dynamic_loadpoint(
    vehicle_id: str,
    state: Mapping[str, Any],
    observation: Mapping[str, Any] | None,
    site_surplus_kw: float,
    ownership: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    params = state.get("params") or {}
    vehicle_name = _display_name(vehicle_id, state)
    loadpoint_id = vehicle_id
    observation_vehicle_id = None
    if observation is not None and _is_default_loadpoint(vehicle_id, vehicle_name):
        observation_vehicle_id = (
            observation.get("vehicle_id")
            or observation.get("charger_id")
            or observation.get("vin")
        )
        observed_name = (
            observation.get("vehicle_name")
            or observation.get("name")
            or observation_vehicle_id
        )
        if observed_name:
            vehicle_name = observed_name
        if observation_vehicle_id:
            loadpoint_id = observation_vehicle_id

    current_amps = _int_value(state.get("current_amps"), 0)
    target_amps = _int_value(state.get("target_amps"), current_amps)
    voltage = _float_value(params.get("voltage"), 240.0)
    phases = _float_value(params.get("phases"), 1.0)
    commanded_power_kw = current_amps * voltage * phases / 1000

    observed_power_kw = None
    if observation is not None:
        observed_power_kw = _float_value(
            observation.get("ev_power_kw", observation.get("current_power_kw")),
            0.0,
        )
    power_kw = observed_power_kw if observed_power_kw is not None else commanded_power_kw

    if observation is not None and "is_charging" in observation:
        actually_charging = bool(observation.get("is_charging"))
    elif observed_power_kw is not None:
        actually_charging = observed_power_kw > ACTIVE_POWER_THRESHOLD_KW
    else:
        actually_charging = current_amps > 0 and bool(state.get("charging_started", False))

    if observation is not None and "is_connected" in observation:
        connected = bool(observation.get("is_connected"))
    else:
        connected = actually_charging or bool(state.get("active", False))

    paused = bool(state.get("paused", False))
    status = _loadpoint_status(connected, actually_charging, current_amps, paused)
    blocking_reason = state.get("paused_reason") or state.get("reason") or None
    if status == "commanded_no_power" and not blocking_reason:
        blocking_reason = f"Commanded {current_amps}A but no measured charge power"

    allocated_surplus_kw = _float_value(state.get("allocated_surplus_kw"), 0.0)
    soc = _optional_int(
        (observation or {}).get("ev_soc", (observation or {}).get("current_soc"))
    )
    if soc is None:
        soc = _optional_int(state.get("current_soc") or params.get("current_soc"))

    owner_mode = (
        (ownership or {}).get("owner_mode")
        or params.get("owner_mode")
        or params.get("dynamic_mode")
        or state.get("mode")
        or "dynamic"
    )
    charger_type = params.get("charger_type") or state.get("charger_type") or "tesla"
    owner = (ownership or {}).get("owner")
    if owner is None and state.get("active", False):
        owner = "powersync"
    session_id = (ownership or {}).get("session_id") or state.get("session_id")
    source_mode = (ownership or {}).get("source_mode") or params.get("source_mode")
    duration_minutes = (
        (ownership or {}).get("duration_minutes")
        or params.get("duration_minutes")
        or state.get("duration_minutes")
    )
    expires_at = (
        (ownership or {}).get("expires_at")
        or params.get("expires_at")
        or state.get("expires_at")
    )

    return {
        "loadpoint_id": loadpoint_id,
        "vehicle_id": observation_vehicle_id or vehicle_id,
        "vehicle_name": vehicle_name,
        "charger_type": charger_type,
        "connected": connected,
        "actual_charging": actually_charging,
        "status": status,
        "owner": owner,
        "owner_mode": owner_mode,
        "source": _status_source(power_kw, site_surplus_kw, allocated_surplus_kw),
        "current_power_kw": round(power_kw, 2),
        "commanded_power_kw": round(commanded_power_kw, 2),
        "current_amps": current_amps,
        "target_amps": target_amps,
        "soc": soc,
        "target_soc": _optional_int(params.get("target_soc")),
        "allocated_surplus_kw": round(allocated_surplus_kw, 2),
        "delay_timer": _solar_surplus_delay_timer(state),
        "blocking_reason": blocking_reason,
        "session_id": session_id,
        "last_command": (ownership or {}).get("last_command"),
        "confidence": "observed" if observation is not None else "commanded",
        "source_mode": source_mode,
        "duration_minutes": duration_minutes,
        "expires_at": expires_at,
        "quick_control": bool((ownership or {}).get("quick_control") or params.get("quick_control")),
    }


def _observed_loadpoint(
    observation: Mapping[str, Any],
    site_surplus_kw: float,
    ownership: Mapping[str, Any] | None = None,
    last_command: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    vehicle_name = (
        observation.get("vehicle_name")
        or observation.get("name")
        or observation.get("vehicle_id")
        or observation.get("charger_id")
        or "EV Charger"
    )
    loadpoint_id = (
        observation.get("vehicle_id")
        or observation.get("charger_id")
        or observation.get("vin")
        or _normal_key(vehicle_name)
        or "external_ev"
    )
    power_kw = _float_value(
        observation.get("ev_power_kw", observation.get("current_power_kw")),
        0.0,
    )
    actually_charging = bool(observation.get("is_charging")) or power_kw > ACTIVE_POWER_THRESHOLD_KW
    connected = bool(observation.get("is_connected")) or actually_charging
    current_amps = _int_value(observation.get("current_amps"), 0)
    status = _loadpoint_status(connected, actually_charging, current_amps, False)

    return {
        "loadpoint_id": loadpoint_id,
        "vehicle_id": observation.get("vehicle_id"),
        "vehicle_name": vehicle_name,
        "charger_type": observation.get("charger_type") or "ev",
        "connected": connected,
        "actual_charging": actually_charging,
        "status": status,
        "owner": (ownership or {}).get("owner") or "external",
        "owner_mode": (ownership or {}).get("owner_mode") or observation.get("owner_mode"),
        "source": _status_source(power_kw, site_surplus_kw),
        "current_power_kw": round(power_kw, 2),
        "commanded_power_kw": None,
        "current_amps": current_amps,
        "target_amps": _int_value(observation.get("target_amps"), current_amps),
        "soc": _optional_int(observation.get("ev_soc", observation.get("current_soc"))),
        "target_soc": _optional_int(observation.get("target_soc")),
        "allocated_surplus_kw": 0.0,
        "blocking_reason": observation.get("blocking_reason"),
        "session_id": (ownership or {}).get("session_id") or observation.get("session_id"),
        "last_command": (
            observation.get("last_command")
            or (ownership or {}).get("last_command")
            or last_command
        ),
        "confidence": "observed",
        "source_mode": (ownership or {}).get("source_mode") or observation.get("source_mode"),
        "duration_minutes": (ownership or {}).get("duration_minutes") or observation.get("duration_minutes"),
        "expires_at": (ownership or {}).get("expires_at") or observation.get("expires_at"),
        "quick_control": bool((ownership or {}).get("quick_control") or observation.get("quick_control")),
    }


def build_generic_charger_observation(
    *,
    vehicle_id: str = "generic_ev",
    vehicle_name: str = "EV",
    switch_state: Any = None,
    amps_value: Any = None,
    status_state: Any = None,
    soc_value: Any = None,
) -> dict[str, Any]:
    """Build an observed loadpoint record for a generic HA charger."""
    normalized_status = _normal_key(status_state)
    switch_on = str(switch_state).lower() in ("on", "true", "1", "charging")
    current_amps = _int_value(amps_value, 0)

    status_says_connected = normalized_status in GENERIC_CONNECTED_STATES
    status_says_charging = normalized_status in GENERIC_CHARGING_STATES

    is_connected = switch_on or status_says_connected
    is_charging = status_says_charging
    blocking_reason = None
    if normalized_status in {"paused", "suspendedevse", "suspendedev", "finishing", "faulted"}:
        blocking_reason = str(status_state)
    elif current_amps > 0 and not is_charging:
        blocking_reason = f"Commanded {current_amps}A but no measured charge power"

    return {
        "charger_id": vehicle_id,
        "vehicle_id": vehicle_id,
        "vehicle_name": vehicle_name,
        "charger_type": "generic",
        "current_amps": current_amps,
        "target_amps": current_amps,
        "ev_power_kw": 0.0,
        "ev_soc": _optional_int(soc_value),
        "is_connected": is_connected,
        "is_charging": is_charging,
        "blocking_reason": blocking_reason,
        "include_idle": True,
    }


def build_loadpoint_status(
    dynamic_states: Mapping[str, Mapping[str, Any]] | None,
    observed_vehicles: Iterable[Mapping[str, Any]] | None = None,
    site: Mapping[str, Any] | None = None,
    ownerships: Mapping[str, Mapping[str, Any]] | None = None,
    last_commands: Mapping[str, Mapping[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Merge PowerSync-owned EV state with observed charger/vehicle telemetry."""
    observations = coalesce_vehicle_observations(observed_vehicles)
    used_indexes: set[int] = set()
    site_surplus_kw = _float_value((site or {}).get("surplus_kw"), 0.0)
    loadpoints: list[dict[str, Any]] = []

    for vehicle_id, state in (dynamic_states or {}).items():
        if not isinstance(state, Mapping):
            continue
        vehicle_name = _display_name(vehicle_id, state)
        index, observation = _find_observation(
            vehicle_id,
            vehicle_name,
            observations,
            used_indexes,
        )
        if index is not None:
            used_indexes.add(index)
            if (
                _is_default_loadpoint(vehicle_id, vehicle_name)
                and observation is not None
                and not _is_generic_observation(observation)
            ):
                for obs_index, obs in enumerate(observations):
                    if _is_generic_observation(obs):
                        used_indexes.add(obs_index)
        ownership = _lookup_alias(ownerships, vehicle_id)
        loadpoints.append(
            _dynamic_loadpoint(vehicle_id, state, observation, site_surplus_kw, ownership)
        )

    for index, observation in enumerate(observations):
        if index in used_indexes:
            continue
        power_kw = _float_value(
            observation.get("ev_power_kw", observation.get("current_power_kw")),
            0.0,
        )
        should_include = (
            observation.get("include_idle")
            or observation.get("is_connected")
            or observation.get("is_charging")
            or power_kw > ACTIVE_POWER_THRESHOLD_KW
            or observation.get("ev_soc") is not None
            or observation.get("current_soc") is not None
        )
        if should_include:
            loadpoint_id = (
                observation.get("vehicle_id")
                or observation.get("charger_id")
                or observation.get("vin")
            )
            ownership = _lookup_alias(
                ownerships,
                loadpoint_id,
                observation.get("vehicle_id"),
                observation.get("charger_id"),
                observation.get("vin"),
            )
            loadpoints.append(
                _observed_loadpoint(
                    observation,
                    site_surplus_kw,
                    ownership,
                    _lookup_alias(
                        last_commands,
                        loadpoint_id,
                        observation.get("vehicle_id"),
                        observation.get("charger_id"),
                        observation.get("vin"),
                    ),
                )
            )

    return loadpoints
