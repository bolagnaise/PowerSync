"""Runtime EV ownership helpers for coordinated charging decisions."""

from __future__ import annotations

import asyncio
import math
from collections.abc import Mapping
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4


DEFAULT_VEHICLE_ID = "_default"
EXTERNAL_OWNER_MODE = "external"
EXTERNAL_CONTROL_POLICY_OVERRIDE = "override"
EXTERNAL_CONTROL_POLICY_YIELD = "yield"
EXTERNAL_CONTROL_POLICIES = frozenset(
    {EXTERNAL_CONTROL_POLICY_OVERRIDE, EXTERNAL_CONTROL_POLICY_YIELD}
)
EXTERNAL_SESSION_POLICY_ADOPT = "adopt"
EXTERNAL_SESSION_POLICY_ABORT = "abort"
EXTERNAL_SESSION_POLICY_LEAVE_ALONE = "leave_alone"
EXTERNAL_SESSION_POLICIES = frozenset(
    {
        EXTERNAL_SESSION_POLICY_ADOPT,
        EXTERNAL_SESSION_POLICY_ABORT,
        EXTERNAL_SESSION_POLICY_LEAVE_ALONE,
    }
)
EXTERNAL_CONTROL_DEFAULT_LOADPOINT_TYPES = frozenset(
    {"generic", "ocpp", "sigenergy", "zaptec"}
)
MANUAL_STOP_HOLD_SECONDS = 15 * 60
RECOVERED_OWNERSHIP_MAX_AGE_SECONDS = 15 * 60
EXTERNAL_COMMAND_SETTLE_SECONDS = 90
POWERSYNC_STOP_CONFIRM_SECONDS = 15 * 60
EV_QUICK_OBLIGATIONS_KEY = "ev_quick_control_obligations"
_EV_QUICK_TIMERS_KEY = "_ev_quick_control_timers"
_EV_QUICK_PARAM_KEYS = frozenset(
    {
        "charger_type",
        "charger_switch_entity",
        "charger_amps_entity",
        "charger_status_entity",
        "charger_power_entity",
        "ocpp_charger_id",
        "sigenergy_charger_host",
        "sigenergy_charger_port",
        "sigenergy_charger_slave_id",
        "sigenergy_charger_type",
        "vehicle_id",
        "vehicle_vin",
        "source_mode",
        "dynamic_mode",
        "owner_mode",
        "quick_control",
        "duration_minutes",
        "expires_at",
        "_boost_start_deadline",
        "_quick_control_obligation_id",
        "_tesla_command_entity_id",
        "_tesla_native_command_identity",
        "_quick_control_binding",
        "prefer_vin_scoped_current_control",
    }
)


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


def normalize_external_control_policy(value: Any) -> str:
    """Return the safe persisted policy for externally started charging."""
    policy = str(value or "").strip().lower()
    if policy == EXTERNAL_CONTROL_POLICY_YIELD:
        return EXTERNAL_CONTROL_POLICY_YIELD
    return EXTERNAL_CONTROL_POLICY_OVERRIDE


def validate_external_control_policy(value: Any) -> str:
    """Validate an explicit API policy value without silently mutating it."""
    policy = str(value or "").strip().lower()
    if policy not in EXTERNAL_CONTROL_POLICIES:
        raise ValueError("external_control_policy must be 'override' or 'yield'")
    return policy


def normalize_external_session_policy(value: Any) -> str | None:
    """Normalize an optional per-mode external-session action.

    ``None`` retains the pre-selector mode behavior.  This is important for
    existing installations: adding the field must never change their current
    adoption/stop path.
    """
    if value is None or str(value).strip() == "":
        return None
    policy = str(value).strip().lower().replace("-", "_")
    return policy if policy in EXTERNAL_SESSION_POLICIES else None


def validate_external_session_policy(value: Any) -> str:
    """Validate an explicitly supplied external-session action."""
    policy = normalize_external_session_policy(value)
    if policy is None:
        raise ValueError(
            "external_session_policy must be 'adopt', 'abort', or 'leave_alone'"
        )
    return policy


def external_control_policy_supported(
    vehicle_id: Any,
    charger_type: Any,
) -> bool:
    """Return whether a profile has an identity safe enough to opt into yield."""
    if normalize_vehicle_id(vehicle_id) != DEFAULT_VEHICLE_ID:
        return True
    return str(charger_type or "").strip().lower() in (
        EXTERNAL_CONTROL_DEFAULT_LOADPOINT_TYPES
    )


def _stored_vehicle_charging_configs(
    hass: Any,
    config_entry: Any,
) -> list[Mapping[str, Any]]:
    entry = _entry_data(hass, config_entry.entry_id)
    store = entry.get("automation_store")
    stored_data = getattr(store, "_data", {}) if store is not None else {}
    configs = (
        stored_data.get("vehicle_charging_configs", [])
        if isinstance(stored_data, Mapping)
        else []
    )
    if not isinstance(configs, list):
        return []
    return [config for config in configs if isinstance(config, Mapping)]


def _stored_automation_data(hass: Any, config_entry: Any) -> Mapping[str, Any]:
    entry = _entry_data(hass, config_entry.entry_id)
    store = entry.get("automation_store")
    data = getattr(store, "_data", {}) if store is not None else {}
    return data if isinstance(data, Mapping) else {}


def _ownership_loadpoint_id(hass: Any, config_entry: Any, vehicle_id: Any) -> str:
    """Return the physical key for new shared profiles, preserving legacy IDs."""
    vid = normalize_vehicle_id(vehicle_id)
    try:
        from .ev_loadpoint_profiles import physical_loadpoint_id, profile_id, profiles_for_loadpoint

        configs = _stored_vehicle_charging_configs(hass, config_entry)
        matching = [config for config in configs if profile_id(config) == vid]
        if len(matching) == 1:
            profile = matching[0]
            loadpoint = physical_loadpoint_id(profile)
            peers = profiles_for_loadpoint(configs, loadpoint)
            # Preserve the byte-for-byte legacy runtime namespace until a
            # physical mapping is actually shared (or explicitly introduced).
            if len(peers) > 1 or profile.get("loadpoint_id"):
                return loadpoint
        # A physical ID received from a mode/controller is already canonical
        # when it maps to a shared or explicitly named loadpoint.
        peers = profiles_for_loadpoint(configs, vid)
        if len(peers) > 1 or (len(peers) == 1 and peers[0].get("loadpoint_id")):
            return vid
    except Exception:
        pass
    return vid


def get_external_session_policy(
    hass: Any,
    config_entry: Any,
    vehicle_id: Any,
    mode: Any,
) -> str | None:
    """Return the selected profile's optional action for one mode family.

    It is intentionally ignored under ``yield``.  Callers use ``None`` to
    retain their existing mode-specific path, avoiding a compatibility change
    for profiles that do not opt in.
    """
    if get_external_control_policy(hass, config_entry, vehicle_id) == EXTERNAL_CONTROL_POLICY_YIELD:
        return None
    try:
        from .ev_loadpoint_profiles import effective_profile_for_id

        data = _stored_automation_data(hass, config_entry)
        profile = effective_profile_for_id(
            _stored_vehicle_charging_configs(hass, config_entry), data, vehicle_id
        )
        if profile is None:
            return None
        configured = profile.get("external_session_policy")
        if isinstance(configured, Mapping):
            configured = configured.get(owner_family(mode), configured.get(str(mode)))
        return normalize_external_session_policy(configured)
    except Exception:
        return None


def _canonical_external_policy_id(
    hass: Any,
    config_entry: Any,
    vehicle_id: Any,
    configs: list[Mapping[str, Any]],
    *extra_ids: Any,
) -> str:
    value = normalize_vehicle_id(vehicle_id)
    if not value.startswith("ble_"):
        return value
    try:
        from ..tesla_ble_mapping import (
            canonical_tesla_vehicle_id,
            resolve_ble_prefixes,
        )

        options = {
            **getattr(config_entry, "data", {}),
            **getattr(config_entry, "options", {}),
        }
        candidate_ids = [config.get("vehicle_id") for config in configs]
        candidate_ids.extend(extra_ids)
        fleet_vins = [
            str(candidate)
            for candidate in candidate_ids
            if len(str(candidate or "")) == 17
            and str(candidate or "").isalnum()
            and not str(candidate or "").isdigit()
        ]
        return canonical_tesla_vehicle_id(
            options,
            value,
            fleet_vins,
            resolve_ble_prefixes(hass, options),
        )
    except Exception:
        return value


def external_control_vehicle_ids_match(
    hass: Any,
    config_entry: Any,
    first: Any,
    second: Any,
) -> bool:
    """Match exact loadpoint identities, including configured BLE/VIN pairs."""
    from .ev_vehicle_capacity import vehicle_ids_match

    configs = _stored_vehicle_charging_configs(hass, config_entry)
    left = _canonical_external_policy_id(
        hass, config_entry, first, configs, first, second
    )
    right = _canonical_external_policy_id(
        hass, config_entry, second, configs, first, second
    )
    return vehicle_ids_match(left, right)


def get_external_control_policy(
    hass: Any,
    config_entry: Any,
    vehicle_id: Any,
) -> str:
    """Resolve one exact vehicle's external-control policy.

    Missing, invalid, or ambiguous identities fail safe to PowerSync control.
    A unique configured non-Tesla default loadpoint can opt in; Tesla BLE
    aliases are mapped only when the configured pairing is exact and
    unambiguous.
    """
    vid = normalize_vehicle_id(vehicle_id)
    configs = _stored_vehicle_charging_configs(hass, config_entry)
    # A shared non-Tesla loadpoint has no anonymous policy.  Only an explicit
    # selected profile may opt into yield; an unselected/ambiguous charger
    # fails safe to override.
    try:
        from .ev_loadpoint_profiles import (
            effective_profile_for_id,
            physical_loadpoint_id,
            profile_id,
            profiles_for_loadpoint,
        )

        matching = [config for config in configs if profile_id(config) == vid]
        physical_id = (
            physical_loadpoint_id(matching[0]) if len(matching) == 1 else vid
        )
        peers = profiles_for_loadpoint(configs, physical_id)
        if len(peers) > 1 or (len(peers) == 1 and peers[0].get("loadpoint_id")):
            selected = effective_profile_for_id(configs, _stored_automation_data(hass, config_entry), vid)
            if selected is None:
                return EXTERNAL_CONTROL_POLICY_OVERRIDE
            return normalize_external_control_policy(
                selected.get("external_control_policy")
            )
    except Exception:
        # The legacy resolution below remains the fail-safe fallback for a
        # partial upgrade or a malformed persisted profile.
        pass
    if vid == DEFAULT_VEHICLE_ID:
        matches = [
            config
            for config in configs
            if external_control_policy_supported(
                config.get("vehicle_id"),
                config.get("charger_type"),
            )
        ]
    else:
        matches = [
            config
            for config in configs
            if external_control_vehicle_ids_match(
                hass,
                config_entry,
                config.get("vehicle_id"),
                vid,
            )
        ]

        # Older non-Tesla profiles can store the anonymous id while Price-Level
        # uses the configured charger's synthetic id. Preserve an explicit
        # policy only when one profile and the exact configured backend agree.
        if not matches and len(configs) == 1:
            config = configs[0]
            options = {
                **getattr(config_entry, "data", {}),
                **getattr(config_entry, "options", {}),
            }
            configured_type = None
            if vid == "generic_ev" and options.get("generic_charger_enabled"):
                configured_type = "generic"
            elif (
                options.get("ocpp_enabled")
                and vid == f"ocpp_{options.get('ocpp_charger_id') or 'ocpp_charger'}"
            ):
                configured_type = "ocpp"
            entity_keys = (
                ("charger_switch_entity", "generic_charger_switch_entity"),
                ("charger_amps_entity", "generic_charger_amps_entity"),
                ("charger_status_entity", "generic_charger_status_entity"),
                ("charger_power_entity", "generic_charger_power_entity"),
            ) if configured_type == "generic" else (("ocpp_charger_id", "ocpp_charger_id"),)
            mappings_agree = all(
                not config.get(profile_key)
                or not options.get(option_key)
                or str(config[profile_key]).strip() == str(options[option_key]).strip()
                for profile_key, option_key in entity_keys
            )
            if (
                configured_type is not None
                and mappings_agree
                and normalize_vehicle_id(config.get("vehicle_id")) == DEFAULT_VEHICLE_ID
                and str(config.get("charger_type") or "").lower() == configured_type
            ):
                matches = [config]

    if len(matches) != 1:
        return EXTERNAL_CONTROL_POLICY_OVERRIDE
    return normalize_external_control_policy(
        matches[0].get("external_control_policy")
    )


def record_manual_stop_hold(
    hass: Any,
    config_entry: Any,
    vehicle_id: Any = None,
    *,
    seconds: int = MANUAL_STOP_HOLD_SECONDS,
    reason: str | None = None,
) -> dict[str, Any]:
    """Temporarily suppress automated EV restarts after a user stop."""
    vid = _ownership_loadpoint_id(hass, config_entry, vehicle_id)
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
    candidates = [_ownership_loadpoint_id(hass, config_entry, vehicle_id)]
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
    entry = _entry_data(hass, config_entry.entry_id)
    if isinstance(entry.get("ev_boost_sessions"), dict):
        for vehicle_id in entry["ev_boost_sessions"]:
            reconcile_boost_supersession(hass, config_entry, vehicle_id)
    missing_transport_recovery = object()
    transport_recovery = entry.get("ev_transport_recovery", missing_transport_recovery)
    # The recovery journal's empty schema was introduced after runtime state
    # persistence.  Migrate absent/empty legacy footprints while preserving
    # any nonempty malformed record for the recovery layer to reject safely.
    if transport_recovery is missing_transport_recovery or transport_recovery == {}:
        transport_recovery = {"vehicles": {}}
    quick_control_obligations = entry.get(EV_QUICK_OBLIGATIONS_KEY, {})
    return {
        "active_ownership": dict(get_ev_ownerships(hass, config_entry)),
        "last_commands": dict(get_ev_last_commands(hass, config_entry)),
        "manual_stop_holds": dict(
            entry.get("ev_manual_stop_holds", {})
        ),
        "capability_refresh_records": dict(
            entry.get("ev_capability_refresh_records", {})
        ),
        # Stop obligations outlive the live lease/controller after a failed
        # stop, and must not disappear when restart clears active ownership.
        "boost_sessions": deepcopy(entry.get("ev_boost_sessions", {})),
        # Provider uncertainty survives a session/lease replacement. Reload
        # restores evidence for observation and cleanup, never Start authority.
        "transport_recovery": deepcopy(transport_recovery),
        # Finite manual starts persist their absolute cleanup obligation before
        # the positive command is allowed to reach a charger.
        EV_QUICK_OBLIGATIONS_KEY: deepcopy(quick_control_obligations),
        "saved_at": _now_iso(),
    }


_LIFECYCLE_SAVE_HOLDS = (
    "lifecycle_sidecar_save_failed", "lifecycle_projection_save_failed",
    "lifecycle_save_incomplete", "lifecycle_projection_save_incomplete",
)


def _mark_lifecycle_save_hold(entry: dict, reason: str) -> None:
    current = entry.get("_tesla_ev_recovery_sidecar_hold")
    if current is None or current in _LIFECYCLE_SAVE_HOLDS:
        entry["_tesla_ev_recovery_sidecar_hold"] = reason


def _clear_recovered_lifecycle_save_hold(hass: Any, config_entry: Any, entry: dict) -> None:
    """Clear only this module's transient save hold after full readiness."""
    if (entry.get("_tesla_ev_recovery_sidecar_hold") not in _LIFECYCLE_SAVE_HOLDS
            or entry.get("_tesla_ev_recovery_sidecar_loaded") is not True):
        return
    from ..tesla_ev_recovery import recovery_sidecar_snapshot

    state = recovery_sidecar_snapshot(hass, config_entry)
    obligations = state.get("obligations") if isinstance(state, Mapping) else None
    if not isinstance(obligations, Mapping):
        return
    current = _runtime_snapshot(hass, config_entry)
    if (obligations.get("ev_boost_sessions") == current["boost_sessions"]
            and obligations.get(EV_QUICK_OBLIGATIONS_KEY) == current[EV_QUICK_OBLIGATIONS_KEY]):
        # Incidents, liabilities and malformed/missing-store holds are owned
        # by the recovery manager. This does not acknowledge or clear them.
        entry.pop("_tesla_ev_recovery_sidecar_hold", None)


async def persist_ev_runtime_state(
    hass: Any,
    config_entry: Any,
    store: Any = None,
    *,
    _recovery_already_saved: bool = False,
) -> bool:
    """Persist EV runtime diagnostics without relying on active leases later."""
    from ..const import DOMAIN
    entry = hass.data.get(DOMAIN, {}).get(config_entry.entry_id)
    if not isinstance(entry, dict):
        return False
    runtime_store = store or entry.get("automation_store")
    if (runtime_store is None or not hasattr(runtime_store, "_data")
            or entry.get("automation_store") is not runtime_store):
        return False

    async def save_projection() -> bool:
        async with entry.setdefault("ev_runtime_save_lock", asyncio.Lock()):
            if (hass.data.get(DOMAIN, {}).get(config_entry.entry_id) is not entry
                    or entry.get("automation_store") is not runtime_store):
                return False
            runtime_store._data["ev_runtime_state"] = _runtime_snapshot(hass, config_entry)
            save = getattr(runtime_store, "async_save", None)
            if save is None:
                return False
            result = save()
            if hasattr(result, "__await__"):
                result = await result
            return (result is not False
                    and hass.data.get(DOMAIN, {}).get(config_entry.entry_id) is entry
                    and entry.get("automation_store") is runtime_store)

    lifecycle = entry.get("_ev_recovery_lifecycle_prepared") is True
    if not lifecycle:
        return await save_projection()
    if _recovery_already_saved:
        try:
            saved = await save_projection()
        except BaseException:
            _mark_lifecycle_save_hold(entry, "lifecycle_projection_save_incomplete")
            raise
        if not saved:
            _mark_lifecycle_save_hold(entry, "lifecycle_projection_save_failed")
        elif hass.data.get(DOMAIN, {}).get(config_entry.entry_id) is entry:
            _clear_recovered_lifecycle_save_hold(hass, config_entry, entry)
        return saved

    # The recovery manager already owns its journal lock when saving a
    # projection. External lifecycle saves must take that lock first, before
    # the legacy runtime lock, so neither path can invert the other.
    from ..tesla_ev_recovery import (
        async_persist_recovery_state,
        async_update_recovery_obligations,
    )

    async with entry.setdefault("_ev_recovery_lifecycle_save_lock", asyncio.Lock()):
        if (hass.data.get(DOMAIN, {}).get(config_entry.entry_id) is not entry
                or entry.get("automation_store") is not runtime_store):
            return False
        if entry.get("_tesla_ev_recovery_sidecar_loaded") is not True:
            entry.setdefault("_tesla_ev_recovery_sidecar_hold", "sidecar_not_hydrated")
            return False
        # Reconcile a rolled-back provisional Boost takeover before copying
        # the authoritative family. Doing this only in the later legacy save
        # would leave the sidecar with the cancelled old expiry authority.
        snapshot = _runtime_snapshot(hass, config_entry)
        obligations = {
            "ev_boost_sessions": snapshot["boost_sessions"],
            EV_QUICK_OBLIGATIONS_KEY: snapshot[EV_QUICK_OBLIGATIONS_KEY],
        }
        try:
            saved = await async_update_recovery_obligations(hass, config_entry, obligations)
            if saved:
                saved = await async_persist_recovery_state(hass, config_entry)
            if not saved:
                _mark_lifecycle_save_hold(entry, "lifecycle_sidecar_save_failed")
                return False
            if (hass.data.get(DOMAIN, {}).get(config_entry.entry_id) is not entry
                    or entry.get("automation_store") is not runtime_store):
                return False
            saved = await save_projection()
            if not saved:
                _mark_lifecycle_save_hold(entry, "lifecycle_projection_save_failed")
            else:
                _clear_recovered_lifecycle_save_hold(hass, config_entry, entry)
            return saved
        except BaseException:
            _mark_lifecycle_save_hold(entry, "lifecycle_save_incomplete")
            raise


def _schedule_runtime_save(hass: Any, config_entry: Any) -> None:
    """Schedule a best-effort runtime persistence save."""
    entry = _entry_data(hass, config_entry.entry_id)
    if not entry.get("automation_store"):
        return
    create_task = getattr(hass, "async_create_task", None)
    if create_task is None:
        return
    create_task(persist_ev_runtime_state(hass, config_entry, entry.get("automation_store")))


def _quick_control_params(params: Mapping[str, Any]) -> dict[str, Any]:
    """Keep only identity and cleanup-safe fields in a durable obligation."""
    result: dict[str, Any] = {}
    for key in _EV_QUICK_PARAM_KEYS:
        value = params.get(key)
        if value is None:
            continue
        try:
            result[key] = deepcopy(value)
        except Exception:
            continue
    return result


def _quick_control_timer_entry(hass: Any, config_entry: Any) -> dict[str, Any]:
    entry = _entry_data(hass, config_entry.entry_id)
    timers = entry.get(_EV_QUICK_TIMERS_KEY)
    if not isinstance(timers, dict):
        timers = entry[_EV_QUICK_TIMERS_KEY] = {}
    return timers


def get_ev_quick_control_obligation(
    hass: Any,
    config_entry: Any,
    vehicle_id: Any,
) -> dict[str, Any] | None:
    """Return one finite EV cleanup obligation without exposing its timer."""
    entry = _entry_data(hass, config_entry.entry_id)
    obligations = entry.get(EV_QUICK_OBLIGATIONS_KEY)
    if not isinstance(obligations, dict):
        return None
    vid = _ownership_loadpoint_id(hass, config_entry, vehicle_id)
    record = obligations.get(vid)
    return deepcopy(record) if isinstance(record, Mapping) else None


def update_ev_quick_control_obligation(
    hass: Any,
    config_entry: Any,
    vehicle_id: Any,
    params: Mapping[str, Any],
) -> bool:
    """Refresh cleanup identity after a native Start selects its producer."""
    entry = _entry_data(hass, config_entry.entry_id)
    obligations = entry.get(EV_QUICK_OBLIGATIONS_KEY)
    if not isinstance(obligations, dict):
        return False
    vid = _ownership_loadpoint_id(hass, config_entry, vehicle_id)
    record = obligations.get(vid)
    if not isinstance(record, dict):
        return False
    updated_params = _quick_control_params(params)
    # Native Start may add producer identity, but must preserve the exact
    # finite-session generation used by an already armed expiry callback.
    updated_params["_quick_control_obligation_id"] = record.get(
        "obligation_id"
    )
    record["params"] = updated_params
    record["updated_at"] = _now_iso()
    _schedule_runtime_save(hass, config_entry)
    return True


def _cancel_ev_quick_control_timer(
    hass: Any,
    config_entry: Any,
    vehicle_id: Any,
) -> None:
    timers = _quick_control_timer_entry(hass, config_entry)
    vid = _ownership_loadpoint_id(hass, config_entry, vehicle_id)
    timer = timers.pop(vid, None)
    if callable(timer):
        try:
            timer()
        except Exception:
            pass


def clear_ev_quick_control_obligation(
    hass: Any,
    config_entry: Any,
    vehicle_id: Any,
    *,
    expected_token: str | None = None,
) -> bool:
    """Clear a finite cleanup obligation after terminal stop handling."""
    entry = _entry_data(hass, config_entry.entry_id)
    obligations = entry.get(EV_QUICK_OBLIGATIONS_KEY)
    if not isinstance(obligations, dict):
        return False
    vid = _ownership_loadpoint_id(hass, config_entry, vehicle_id)
    current = obligations.get(vid)
    if (
        expected_token is not None
        and (
            not isinstance(current, Mapping)
            or current.get("obligation_id") != expected_token
        )
    ):
        return False
    _cancel_ev_quick_control_timer(hass, config_entry, vehicle_id)
    removed = obligations.pop(vid, None)
    if removed is not None:
        _schedule_runtime_save(hass, config_entry)
    return removed is not None


def arm_ev_quick_control_obligation(
    hass: Any,
    config_entry: Any,
    vehicle_id: Any,
    record: Mapping[str, Any],
    *,
    schedule_at: datetime | None = None,
) -> Any:
    """Arm one absolute expiry callback for a persisted cleanup obligation."""
    if not isinstance(record, Mapping):
        return None
    # Preserve malformed evidence for diagnosis, but never turn a missing or
    # non-mapping parameter payload into an executable cleanup request.
    params = record.get("params")
    if not isinstance(params, Mapping) or not params:
        return None
    token = record.get("obligation_id")
    if token is not None and not isinstance(token, str):
        return None
    expiry = _as_utc_datetime(record.get("expires_at"))
    if expiry is None:
        return None
    if not record.get("obligation_id"):
        # Obligations were introduced with a generation token.  Give a
        # hand-written/partially migrated record one before arming so a stale
        # callback can never target a later session by VIN alone.
        if not isinstance(record, dict):
            return None
        record["obligation_id"] = uuid4().hex
        params = record.setdefault("params", {})
        params["_quick_control_obligation_id"] = record["obligation_id"]
        _schedule_runtime_save(hass, config_entry)
    from homeassistant.helpers.event import async_track_point_in_utc_time

    vid = _ownership_loadpoint_id(hass, config_entry, vehicle_id)
    runtime_entry = _entry_data(hass, config_entry.entry_id)
    runtime_store = runtime_entry.get("automation_store")

    def _runtime_scope_current() -> bool:
        current_entry = _entry_data(hass, config_entry.entry_id)
        return (
            current_entry is runtime_entry
            and current_entry.get("automation_store") is runtime_store
        )

    _cancel_ev_quick_control_timer(hass, config_entry, vid)

    async def _expire(_now: Any) -> None:
        # Import lazily so ownership restore does not create an actions cycle.
        from .actions import _action_stop_ev_charging_dynamic

        # Revoke positive ownership before entering the cleanup-only guard.
        # A superseded callback must not hold or stop the replacement session.
        if not _runtime_scope_current():
            return
        current = get_ev_quick_control_obligation(hass, config_entry, vid)
        token = record.get("obligation_id")
        if (
            current is None
            or token is None
            or current.get("obligation_id") != token
        ):
            return
        try:
            reconciled = await async_reconcile_quick_control_stopped(
                hass, config_entry, vid, record)
        except asyncio.CancelledError:
            current = get_ev_quick_control_obligation(hass, config_entry, vid)
            if (_runtime_scope_current() and current is not None
                    and current.get("obligation_id") == token):
                arm_ev_quick_control_obligation(hass, config_entry, vid, record,
                    schedule_at=datetime.now(timezone.utc)+timedelta(seconds=30))
            raise
        if reconciled is None:
            current = get_ev_quick_control_obligation(hass, config_entry, vid)
            if (_runtime_scope_current() and current is not None
                    and current.get("obligation_id") == token):
                arm_ev_quick_control_obligation(hass, config_entry, vid, record,
                    schedule_at=datetime.now(timezone.utc)+timedelta(seconds=30))
            return
        if reconciled is not False:
            return
        current = get_ev_quick_control_obligation(hass, config_entry, vid)
        if (not _runtime_scope_current() or current is None
                or current.get("obligation_id") != token):
            return
        record_manual_stop_hold(
            hass,
            config_entry,
            vid,
            reason="Quick EV charge duration elapsed",
        )

        cleanup_params = dict(record.get("params") or {})
        charger_type = cleanup_params.get("charger_type", "tesla")
        cleanup_params.update(
            {
                "vehicle_id": vid,
                "vehicle_vin": vid if charger_type == "tesla" else None,
                "stop_charging": True,
                "stop_untracked": True,
                "manual_stop": True,
                "quick_control": True,
                "_force_tesla_stop_request": charger_type == "tesla",
                "stop_reason": "Quick EV charge duration elapsed",
                "_quick_control_obligation_id": record.get("obligation_id"),
                "_ev_cleanup_only": True,
            }
        )
        try:
            result = await _action_stop_ev_charging_dynamic(
                hass,
                config_entry,
                cleanup_params,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            result = False
        if not _runtime_scope_current():
            return
        if result is True:
            clear_ev_quick_control_obligation(
                hass,
                config_entry,
                vid,
                expected_token=record.get("obligation_id"),
            )
            return

        # A failed/unknown stop keeps the persisted obligation. Retry a small
        # bounded number of times without moving the original expiry; reload
        # can retry the retained obligation after this in-memory budget.
        current = get_ev_quick_control_obligation(hass, config_entry, vid)
        if not _runtime_scope_current():
            return
        token = record.get("obligation_id")
        if (
            current is None
            or token is None
            or current.get("obligation_id") != token
        ):
            return
        retry_count = int(record.get("retry_count", 0) or 0)
        if retry_count >= 3:
            return
        record["retry_count"] = retry_count + 1
        record["updated_at"] = _now_iso()
        _schedule_runtime_save(hass, config_entry)
        arm_ev_quick_control_obligation(
            hass,
            config_entry,
            vid,
            record,
            schedule_at=datetime.now(timezone.utc)
            + timedelta(seconds=min(60, 15 * (2**retry_count))),
        )

    timer = async_track_point_in_utc_time(
        hass,
        _expire,
        schedule_at if schedule_at is not None else expiry,
    )
    _quick_control_timer_entry(hass, config_entry)[vid] = timer
    return timer


def get_ev_quick_control_timer(
    hass: Any,
    config_entry: Any,
    vehicle_id: Any,
) -> Any:
    """Return the transient timer already armed for one obligation."""
    timers = _quick_control_timer_entry(hass, config_entry)
    vid = _ownership_loadpoint_id(hass, config_entry, vehicle_id)
    return timers.get(vid)


async def async_reconcile_quick_control_stopped(hass, config_entry, vehicle_id, saved_record) -> bool | None:
    """True retires, False permits normal Stop, None retains changed scope."""
    try:
        from ..const import DOMAIN
        from .actions import _dynamic_ev_state, _tesla_quick_control_binding_current
        from .. import _passive_ev_cleanup_observation
        from ..tesla_ev_observation import async_read_stopped_tesla_cleanup
        from ..tesla_ev_recovery import get_vehicle_route
    except ImportError:
        return False
    runtime = _entry_data(hass, config_entry.entry_id)
    records = runtime.get(EV_QUICK_OBLIGATIONS_KEY)
    record = records.get(vehicle_id) if isinstance(records, dict) else None
    if (not isinstance(record, dict) or not isinstance(saved_record, Mapping)
            or record.get("obligation_id") != saved_record.get("obligation_id")
            or not record.get("obligation_id") or record.get("vehicle_id") != vehicle_id):
        return False
    snapshot = deepcopy(record)
    params = deepcopy(record.get("params") or {})
    deadline = _as_utc_datetime(record.get("expires_at"))
    if (deadline is None or datetime.now(timezone.utc) < deadline
            or type(record.get("_boost_start_deadline")) not in (int, float)
            or record["_boost_start_deadline"] != deadline.timestamp()
            or params.get("charger_type", "tesla") != "tesla"):
        return False
    store = runtime.get("automation_store")
    state = _dynamic_ev_state.get(config_entry.entry_id, {}).get(vehicle_id)
    owner = get_ev_ownership(hass, config_entry, vehicle_id)[1]
    token = record["obligation_id"]
    timer = get_ev_quick_control_timer(hass, config_entry, vehicle_id)
    if (state is not None and (state.get("params") or {}).get("_quick_control_obligation_id") != token
            or owner is not None and (state is None or state.get("ownership") is not owner)):
        return None

    def current():
        return (hass.data.get(DOMAIN, {}).get(config_entry.entry_id) is runtime
                and runtime.get("automation_store") is store
                and not runtime.get("powerwall_local_dispatch_stopping")
                and runtime.get(EV_QUICK_OBLIGATIONS_KEY) is records
                and records.get(vehicle_id) is record and record == snapshot
                and get_ev_quick_control_timer(hass, config_entry, vehicle_id) is timer
                and _dynamic_ev_state.get(config_entry.entry_id, {}).get(vehicle_id) is state
                and get_ev_ownership(hass, config_entry, vehicle_id)[1] is owner
                and _tesla_quick_control_binding_current(hass, config_entry, params))

    if not current():
        return None
    if (get_vehicle_route(hass, config_entry, vehicle_id) or {}).get("kind") == "blocked":
        return False
    after = deadline.isoformat()
    if _passive_ev_cleanup_observation(hass, config_entry, vehicle_id) == "charging":
        return False
    passive = _passive_ev_cleanup_observation(hass, config_entry, vehicle_id, after)
    if passive == "stopped":
        evidence_current = lambda: _passive_ev_cleanup_observation(
            hass, config_entry, vehicle_id, after) == "stopped"
    else:
        native = (params.get("_quick_control_binding") or {}).get("native_entity")
        if not isinstance(native, Mapping) or not native.get("entity_id"):
            return False
        evidence_current = await async_read_stopped_tesla_cleanup(
            hass, config_entry, vehicle_id, after, authorize=current,
            preferred_entity_id=native["entity_id"])
    if not current():
        return None
    if evidence_current is None:
        return False
    # Keep the obligation durable until the proof survives the awaited checkpoint.
    try:
        if await persist_ev_runtime_state(hass, config_entry, store) is not True:
            return None
    except Exception:
        return None
    if (not current() or not evidence_current()
            or (get_vehicle_route(hass, config_entry, vehicle_id) or {}).get("kind") == "blocked"):
        return None
    clear_ev_quick_control_obligation(hass, config_entry, vehicle_id, expected_token=token)
    save_task = asyncio.create_task(persist_ev_runtime_state(hass, config_entry, store))
    cancelled = False
    while not save_task.done():
        try:
            await asyncio.shield(save_task)
        except asyncio.CancelledError:
            cancelled = True
    try:
        saved = save_task.result() is True
    except asyncio.CancelledError:
        saved, cancelled = False, True
    except Exception:
        saved = False
    if not saved:
        if (hass.data.get(DOMAIN, {}).get(config_entry.entry_id) is runtime
                and runtime.get(EV_QUICK_OBLIGATIONS_KEY) is records
                and vehicle_id not in records
                and get_ev_quick_control_timer(hass, config_entry, vehicle_id) is None):
            records[vehicle_id] = record
            arm_ev_quick_control_obligation(hass, config_entry, vehicle_id, record,
                schedule_at=datetime.now(timezone.utc)+timedelta(seconds=30))
    elif get_ev_ownership(hass, config_entry, vehicle_id)[1] is owner and owner is not None:
        release_ev_ownership(hass, config_entry, vehicle_id, reason="Fresh stopped quick EV observation")
    if cancelled:
        raise asyncio.CancelledError
    return True if saved else None


async def async_begin_ev_quick_control_obligation(
    hass: Any,
    config_entry: Any,
    vehicle_id: Any,
    params: Mapping[str, Any],
    *,
    reason: str = "Manual EV charging",
) -> dict[str, Any] | None:
    """Persist and arm a finite cleanup before positive EV dispatch."""
    expires_at = _as_utc_datetime(params.get("expires_at"))
    deadline = params.get("_boost_start_deadline")
    if expires_at is None or type(deadline) not in (int, float) or not math.isfinite(deadline):
        return None
    entry = _entry_data(hass, config_entry.entry_id)
    obligations = entry.setdefault(EV_QUICK_OBLIGATIONS_KEY, {})
    if not isinstance(obligations, dict):
        return None
    vid = _ownership_loadpoint_id(hass, config_entry, vehicle_id)
    record = {
        "vehicle_id": vid,
        "obligation_id": uuid4().hex,
        "expires_at": expires_at.isoformat(),
        "_boost_start_deadline": float(deadline),
        "created_at": _now_iso(),
        "updated_at": _now_iso(),
        "reason": reason,
        "status": "pending",
        "retry_count": 0,
        "params": _quick_control_params(params),
    }
    record["params"]["_quick_control_obligation_id"] = record["obligation_id"]
    previous = obligations.get(vid)
    previous_snapshot = deepcopy(previous) if isinstance(previous, Mapping) else previous
    obligations[vid] = record
    async def _restore_previous() -> None:
        if previous_snapshot is None:
            obligations.pop(vid, None)
        else:
            obligations[vid] = deepcopy(previous_snapshot)
            # A failed replacement arm may have cancelled the old transient
            # timer. Restore it before returning the persistence error.
            if arm_ev_quick_control_obligation(
                hass,
                config_entry,
                vid,
                obligations[vid],
            ) is None:
                _cancel_ev_quick_control_timer(hass, config_entry, vid)
        try:
            await persist_ev_runtime_state(hass, config_entry)
        except BaseException:
            # The original durable record was already persisted before this
            # replacement attempt. Keep the in-memory old obligation and let
            # the caller retain the original failure/cancellation outcome.
            pass

    try:
        persisted = await persist_ev_runtime_state(hass, config_entry)
    except BaseException:
        await _restore_previous()
        raise
    if not persisted:
        await _restore_previous()
        return None
    try:
        timer = arm_ev_quick_control_obligation(hass, config_entry, vid, record)
    except BaseException:
        await _restore_previous()
        raise
    if timer is None:
        await _restore_previous()
        return None
    return deepcopy(record)


def hydrate_ev_recovery_projection(
    hass: Any,
    config_entry: Any,
    store: Any,
) -> None:
    """Load surviving legacy recovery facts without scheduling any callback."""
    runtime = _entry_data(hass, config_entry.entry_id)
    runtime.setdefault("ev_boost_sessions", {})
    runtime.setdefault(EV_QUICK_OBLIGATIONS_KEY, {})
    stored = getattr(store, "_data", {}).get("ev_runtime_state", {})
    if not isinstance(stored, Mapping):
        runtime["_tesla_ev_recovery_sidecar_hold"] = "legacy_runtime_malformed"
        return
    transport = stored.get("transport_recovery", {"vehicles": {}})
    if transport == {}:
        transport = {"vehicles": {}}
    runtime["ev_transport_recovery"] = deepcopy(transport)
    runtime["ev_boost_sessions"] = deepcopy(stored.get("boost_sessions", {}))
    runtime[EV_QUICK_OBLIGATIONS_KEY] = deepcopy(stored.get(EV_QUICK_OBLIGATIONS_KEY, {}))


def restore_ev_recovery_obligations(hass: Any, config_entry: Any) -> bool:
    """Restore finite cleanup only from the already-hydrated sidecar."""
    from ..tesla_ev_recovery import recovery_sidecar_snapshot, restore_recovery_projection

    runtime = _entry_data(hass, config_entry.entry_id)
    snapshot = recovery_sidecar_snapshot(hass, config_entry)
    if not isinstance(snapshot, Mapping) or not restore_recovery_projection(hass, config_entry):
        return False
    obligations = snapshot.get("obligations")
    if not isinstance(obligations, Mapping):
        return False
    valid = True
    for key in ("ev_boost_sessions", EV_QUICK_OBLIGATIONS_KEY):
        # Empty is an authoritative absence, not a request to re-import an
        # older automation-store record. Malformed evidence remains retained.
        value = deepcopy(obligations.get(key, {}))
        runtime[key] = value
        if not isinstance(value, dict):
            valid = False
    if not valid:
        runtime["_tesla_ev_recovery_sidecar_hold"] = "finite_obligations_malformed"
    return valid


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
    stored_capability_refresh = (
        runtime_state.get("capability_refresh_records") or {}
    )
    stored_boost_sessions = runtime_state.get("boost_sessions", {})
    missing_transport_recovery = object()
    stored_transport_recovery = runtime_state.get(
        "transport_recovery", missing_transport_recovery
    )
    stored_quick_control_obligations = runtime_state.get(
        EV_QUICK_OBLIGATIONS_KEY, {}
    )
    # Older persisted runtime state had no recovery journal, or serialized an
    # empty dict.  Both are valid empty journals under the current schema;
    # retain every nonempty footprint unchanged so recovery can fail closed.
    if (
        stored_transport_recovery is missing_transport_recovery
        or stored_transport_recovery == {}
    ):
        stored_transport_recovery = {"vehicles": {}}
    if entry.get("_ev_recovery_lifecycle_prepared") is True:
        # Setup has already imported/hydrated this state before native-rate
        # admission. A late whole-store serializer must not replace newer
        # transport liabilities or finite obligations with its old projection.
        if entry.get("_tesla_ev_recovery_sidecar_loaded") is True:
            restore_ev_recovery_obligations(hass, config_entry)
    else:
        entry["ev_transport_recovery"] = deepcopy(stored_transport_recovery)
        entry[EV_QUICK_OBLIGATIONS_KEY] = (
            deepcopy(stored_quick_control_obligations)
            if isinstance(stored_quick_control_obligations, dict)
            else {}
        )
        # The Boost restore path validates retained identity/deadline evidence.
        entry["ev_boost_sessions"] = deepcopy(stored_boost_sessions)

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

    if isinstance(stored_capability_refresh, dict):
        entry["ev_capability_refresh_records"] = {
            str(episode_key): dict(record)
            for episode_key, record in stored_capability_refresh.items()
            if isinstance(record, Mapping)
        }

    recovered: dict[str, dict[str, Any]] = {}
    resumable_manual_sessions: dict[str, dict[str, Any]] = {}
    expired_manual_sessions: dict[str, dict[str, Any]] = {}
    authoritative_obligations = (
        entry.get("_ev_recovery_lifecycle_prepared") is True
        and entry.get("_tesla_ev_recovery_sidecar_loaded") is True
    )
    now = datetime.now(timezone.utc)
    if isinstance(stored_ownership, dict):
        for vehicle_id, lease in stored_ownership.items():
            if not isinstance(lease, Mapping):
                continue
            vid = normalize_vehicle_id(vehicle_id)
            owner_mode = str(lease.get("owner_mode") or "dynamic")
            recovered[vid] = dict(lease)
            if (not authoritative_obligations
                    and owner_mode == "boost" and isinstance(entry["ev_boost_sessions"], dict)
                    and vid not in entry["ev_boost_sessions"]):
                # Older Boost leases did not record their absolute deadline or
                # binding. Keep the obligation visible; never infer either.
                entry["ev_boost_sessions"][vid] = {
                    "entry_id": config_entry.entry_id,
                    "vehicle_id": vid,
                    "expires_at": lease.get("expires_at"),
                    "duration_minutes": lease.get("duration_minutes"),
                    "status": "blocked_legacy_recovery_identity",
                }
            quick_obligations = entry.get(EV_QUICK_OBLIGATIONS_KEY)
            finite_lease_survives = (
                not authoritative_obligations
                or (isinstance(quick_obligations, Mapping) and vid in quick_obligations)
            )
            if owner_mode == "manual" and lease.get("quick_control") and finite_lease_survives:
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
        "boost_sessions": entry["ev_boost_sessions"],
        EV_QUICK_OBLIGATIONS_KEY: deepcopy(entry[EV_QUICK_OBLIGATIONS_KEY]),
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
    vehicle_id = _ownership_loadpoint_id(hass, config_entry, vehicle_id)
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
    yield_external_control = (
        get_external_control_policy(hass, config_entry, vehicle_id)
        == EXTERNAL_CONTROL_POLICY_YIELD
    )
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
                if not yield_external_control:
                    release_external_ev_ownership(
                        hass,
                        config_entry,
                        vehicle_id,
                        reason="New session observed under PowerSync control policy",
                        command="external_policy_override",
                        source="powersync",
                    )
                    return False
                lease.pop("stop_settling", None)
                lease.pop("stop_command_at", None)
                lease.pop("stop_settling_expires_at", None)
                lease["reason"] = reason
            expires_at = _as_utc_datetime(lease.get("stop_settling_expires_at"))
            if expires_at is not None and datetime.now(timezone.utc) >= expires_at:
                if not yield_external_control:
                    release_external_ev_ownership(
                        hass,
                        config_entry,
                        vehicle_id,
                        reason="PowerSync stop-settling window expired",
                        command="external_policy_override",
                        source="powersync",
                    )
                    return False
                lease.pop("stop_settling", None)
                lease.pop("stop_command_at", None)
                lease.pop("stop_settling_expires_at", None)
                lease["reason"] = reason
                lease["updated_at"] = _now_iso()
                _schedule_runtime_save(hass, config_entry)
            if not yield_external_control:
                return True
        elif not yield_external_control:
            release_external_ev_ownership(
                hass,
                config_entry,
                vehicle_id,
                reason="PowerSync control policy is active",
                command="external_policy_override",
                source="powersync",
            )
            return False
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

    if not yield_external_control:
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


def observe_recorded_ev_stop(
    hass: Any, config_entry: Any, vehicle_id: Any, *,
    observed_at: Any, charging: bool,
) -> None:
    """Observe an existing stop obligation without issuing or authorizing controls."""
    if not charging:
        confirm_powersync_ev_stop(hass, config_entry, vehicle_id, observed_at=observed_at)
        return
    from ..action_events import observe_ev_stop
    vid = _ownership_loadpoint_id(hass, config_entry, vehicle_id)
    commands = get_ev_last_commands(hass, config_entry)
    for candidate_id in _candidate_vehicle_ids(vid, commands):
        command = commands.get(candidate_id)
        if isinstance(command, dict):
            observe_ev_stop(hass, config_entry, candidate_id, command,
                            _as_utc_datetime(observed_at), charging=True)
            break


def confirm_powersync_ev_stop(
    hass: Any,
    config_entry: Any,
    vehicle_id: Any,
    *,
    observed_at: Any = None,
) -> bool:
    """Clear only the provisional lease once stopped telemetry is observed."""
    vehicle_id = _ownership_loadpoint_id(hass, config_entry, vehicle_id)
    observed_time = _as_utc_datetime(observed_at)
    if observed_time is None or (observed_time - datetime.now(timezone.utc)).total_seconds() > 60:
        return False
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
            observed_at=observed_time.isoformat(),
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
        if not command.get("success"):
            from ..action_events import observe_ev_stop
            previously_settled = command.get("settled_at")
            observe_ev_stop(hass, config_entry, candidate_id, command, observed_time,
                            charging=False)
            if not previously_settled and command.get("settled_at"):
                _schedule_runtime_save(hass, config_entry)
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
        from ..action_events import observe_ev_stop
        observe_ev_stop(hass, config_entry, candidate_id, command, observed_time,
                        charging=False)
        _schedule_runtime_save(hass, config_entry)
        return True
    return False


def release_external_ev_ownership(
    hass: Any,
    config_entry: Any,
    vehicle_id: Any,
    *,
    reason: str = "External vehicle unplugged",
    command: str = "external_unplug",
    source: str = EXTERNAL_OWNER_MODE,
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
        command=command,
        source=source,
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
    vid = _ownership_loadpoint_id(hass, config_entry, vehicle_id)
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
    from ..action_events import publish_ev_command
    publish_ev_command(hass, config_entry, vid, last_command)
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

    if (
        lease.get("owner") == EXTERNAL_OWNER_MODE
        and get_external_control_policy(hass, config_entry, vehicle_id)
        != EXTERNAL_CONTROL_POLICY_YIELD
    ):
        if lease.get("stop_settling"):
            return (
                False,
                lease_id,
                lease,
                "Awaiting telemetry confirmation of PowerSync stop",
            )
        release_ev_ownership(
            hass,
            config_entry,
            lease_id,
            reason="PowerSync control policy is active",
            command="external_policy_override",
            source="powersync",
        )
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
    vid = _ownership_loadpoint_id(hass, config_entry, vehicle_id)
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
    boost_sessions = _entry_data(hass, config_entry.entry_id).get("ev_boost_sessions")
    record = boost_sessions.get(vid) if isinstance(boost_sessions, dict) else None
    if owner_mode == "boost" or isinstance(record, dict):
        lease["ownership_generation"] = uuid4().hex
    if owner_mode != "boost" and isinstance(record, dict):
        record.setdefault("status_before_supersession", record.get("status", "active"))
        record["superseded"] = True
        record["status"] = "superseded"
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
    if command:
        from ..action_events import publish_ev_command
        publish_ev_command(hass, config_entry, vid, lease["last_command"], record_type="ownership")
    return lease


def boost_recovery_observation_after(record: dict) -> str | None:
    """Latest strict readback fence for a finite start/stop, including a replacement.

    An unknown submitted start can outlive a stopped sample in another queue.
    Its obligation therefore survives until a sample newer than its original
    finite deadline, as well as its last submission/stop attempt.
    """
    fences = []
    for item in (record, record.get("pending_intent")):
        if item is None:
            continue
        if not isinstance(item, dict):
            raise ValueError("Invalid Boost pending intent")
        if item is not record and any(item.get(key) != record.get(key) for key in
                                     ("entry_id", "vehicle_id", "pairing", "stop_params")):
            raise ValueError("Changed Boost pending identity")
        keys = ["stop_attempted_at"]
        if item.get("dispatch_started"):
            keys.append("attempted_at")
        if item.get("start_in_flight") or item.get("unknown_start_outcome"):
            keys.append("expires_at")
        for key in keys:
            if item.get(key) is None and key != "stop_attempted_at":
                raise ValueError("Missing Boost recovery timestamp")
            if item.get(key) is not None:
                timestamp = datetime.fromisoformat(item[key])
                if timestamp.tzinfo is None:
                    raise ValueError("Naive Boost recovery timestamp")
                fences.append(timestamp)
    return max(fences).isoformat() if fences else None


def reconcile_boost_supersession(hass: Any, config_entry: Any, vehicle_id: Any) -> None:
    """A rolled-back provisional claim cannot cancel an accepted Boost expiry."""
    entry = _entry_data(hass, config_entry.entry_id)
    records = entry.get("ev_boost_sessions")
    record = records.get(vehicle_id) if isinstance(records, dict) else None
    _, lease = get_ev_ownership(hass, config_entry, vehicle_id)
    if (isinstance(record, dict) and record.get("superseded") and lease is not None
            and lease.get("owner_mode") == "boost"
            and record.get("ownership_generation") is not None
            and lease.get("ownership_generation") == record["ownership_generation"]):
        record.pop("superseded", None)
        record["status"] = record.pop("status_before_supersession", "active")


def release_ev_ownership(
    hass: Any,
    config_entry: Any,
    vehicle_id: Any = None,
    *,
    reason: str | None = None,
    command: str = "release",
    success: bool = True,
    source: str = "powersync",
    observed_at: str | None = None,
) -> dict[str, Any] | None:
    """Release ownership and remember the final command for diagnostics."""
    vid = _ownership_loadpoint_id(hass, config_entry, vehicle_id)
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
    if command == "stop_observed":
        last_command["settled_at"] = observed_at or now
    get_ev_last_commands(hass, config_entry)[vid] = last_command
    _schedule_runtime_save(hass, config_entry)
    from ..action_events import publish_ev_command
    publish_ev_command(hass, config_entry, vid, last_command,
                       physical_confirmation=command == "stop_observed", record_type="ownership")
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
        vid = _ownership_loadpoint_id(hass, config_entry, vehicle_id)
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
    lease_id, lease = get_ev_ownership(hass, config_entry, vid)
    if lease is not None:
        if (
            lease.get("owner") == EXTERNAL_OWNER_MODE
            and not lease.get("stop_settling")
            and get_external_control_policy(hass, config_entry, vid)
            != EXTERNAL_CONTROL_POLICY_YIELD
        ):
            release_ev_ownership(
                hass,
                config_entry,
                lease_id,
                reason="PowerSync control policy is active",
                command="external_policy_override",
                source="powersync",
            )
            return None
        return str(lease.get("owner_mode") or "dynamic")

    return None
