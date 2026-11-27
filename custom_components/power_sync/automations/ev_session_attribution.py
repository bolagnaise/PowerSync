"""Attribute charger-metered EV sessions to a vehicle.

An OCPP charger meters the energy, but it does not know which car is plugged
in.  The only identifier OCPP carries is the authorisation tag (an RFID card
or app token) presented when the transaction starts.  This module resolves a
session's vehicle, in order:

1. ``rfid``: the session's tag is mapped to a vehicle.
2. ``selected``: the user selected the connected EV for this charger.
3. ``only_profile``: exactly one vehicle profile uses this charger.
4. ``unassigned``: nothing identifies the car.

Tags are user credentials for the charger, so raw tags are never persisted or
returned.  Mappings are keyed by a SHA-256 of the normalised tag and keep only
the last four characters for display.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any

from .ev_loadpoint_profiles import (
    effective_profile,
    profile_id,
    profiles_for_loadpoint,
)

SOURCE_RFID = "rfid"
SOURCE_SELECTED = "selected"
SOURCE_ONLY_PROFILE = "only_profile"
SOURCE_MANUAL = "manual"
SOURCE_UNASSIGNED = "unassigned"

# Attribution that a later, better source may still replace on an active
# session.  ``rfid`` and ``manual`` are final.
PROVISIONAL_SOURCES = frozenset({SOURCE_UNASSIGNED, SOURCE_ONLY_PROFILE, SOURCE_SELECTED, None})

def _now():
    try:
        from homeassistant.util import dt as dt_util

        return dt_util.now()
    except ImportError:  # pragma: no cover - only outside Home Assistant
        from datetime import datetime, timezone

        return datetime.now(timezone.utc)


RFID_TAGS_KEY = "rfid_tags"
RFID_RECENT_KEY = "rfid_recent_unmapped"
RECENT_UNMAPPED_DAYS = 30
MAX_RECENT_UNMAPPED = 20
UNASSIGNED_VEHICLE_KEY = "unassigned"


def normalize_tag(tag: Any) -> str:
    """Return the comparable form of an OCPP idTag, or ``""`` when absent."""
    text = str(tag or "").strip()
    if text.lower() in ("", "unknown", "unavailable", "none", "null"):
        return ""
    return text.upper()


def tag_ref(tag: Any) -> str:
    """Return the stable, non-reversible reference for a tag."""
    normalized = normalize_tag(tag)
    if not normalized:
        return ""
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def tag_last4(tag: Any) -> str:
    return normalize_tag(tag)[-4:]


def _profiles(stored_data: Mapping[str, Any] | None) -> list[Mapping[str, Any]]:
    configs = (stored_data or {}).get("vehicle_charging_configs", [])
    return [item for item in configs if isinstance(item, Mapping)]


def _tag_map(stored_data: Mapping[str, Any] | None) -> Mapping[str, Any]:
    tags = (stored_data or {}).get(RFID_TAGS_KEY)
    return tags if isinstance(tags, Mapping) else {}


def known_vehicle_ids(stored_data: Mapping[str, Any] | None) -> set[str]:
    return {profile_id(profile) for profile in _profiles(stored_data) if profile_id(profile)}


def vehicle_for_tag(stored_data: Mapping[str, Any] | None, tag: Any) -> str | None:
    """Return the configured vehicle mapped to ``tag``, if it still exists."""
    ref = tag_ref(tag)
    if not ref:
        return None
    entry = _tag_map(stored_data).get(ref)
    if not isinstance(entry, Mapping):
        return None
    vehicle_id = str(entry.get("vehicle_id") or "").strip()
    if not vehicle_id or vehicle_id not in known_vehicle_ids(stored_data):
        return None
    return vehicle_id


def resolve_session_vehicle(
    stored_data: Mapping[str, Any] | None,
    loadpoint_id: str,
    id_tag: Any = None,
) -> tuple[str | None, str]:
    """Return ``(vehicle_id, source)`` for a session on ``loadpoint_id``."""
    vehicle_id = vehicle_for_tag(stored_data, id_tag)
    if vehicle_id:
        return vehicle_id, SOURCE_RFID

    profiles = _profiles(stored_data)
    candidates = profiles_for_loadpoint(profiles, loadpoint_id)
    if len(candidates) > 1:
        profile = effective_profile(profiles, stored_data, loadpoint_id)
        if profile is not None and profile_id(profile):
            return profile_id(profile), SOURCE_SELECTED
    elif len(candidates) == 1 and profile_id(candidates[0]):
        return profile_id(candidates[0]), SOURCE_ONLY_PROFILE
    return None, SOURCE_UNASSIGNED


def record_unmapped_tag(
    stored_data: dict[str, Any],
    tag: Any,
    charger_id: str,
    *,
    now: Any = None,
) -> bool:
    """Remember a recently presented tag with no mapping so it can be assigned.

    Returns True when storage changed.
    """
    ref = tag_ref(tag)
    if not ref or ref in _tag_map(stored_data):
        return False
    recent = stored_data.get(RFID_RECENT_KEY)
    if not isinstance(recent, dict):
        recent = {}
        stored_data[RFID_RECENT_KEY] = recent
    seen = (now or _now()).isoformat()
    recent[ref] = {"last4": tag_last4(tag), "charger_id": charger_id, "last_seen": seen}
    prune_recent_unmapped(stored_data, now=now)
    return True


def prune_recent_unmapped(stored_data: dict[str, Any], *, now: Any = None) -> None:
    recent = stored_data.get(RFID_RECENT_KEY)
    if not isinstance(recent, dict):
        return
    from datetime import timedelta

    cutoff = ((now or _now()) - timedelta(days=RECENT_UNMAPPED_DAYS)).isoformat()
    mapped = _tag_map(stored_data)
    for ref, entry in list(recent.items()):
        if ref in mapped or not isinstance(entry, Mapping) or str(entry.get("last_seen", "")) < cutoff:
            recent.pop(ref, None)
    if len(recent) > MAX_RECENT_UNMAPPED:
        ordered = sorted(recent.items(), key=lambda item: str(item[1].get("last_seen", "")))
        for ref, _entry in ordered[: len(recent) - MAX_RECENT_UNMAPPED]:
            recent.pop(ref, None)
    if not recent:
        stored_data.pop(RFID_RECENT_KEY, None)


def assign_tag(
    stored_data: dict[str, Any],
    ref: str,
    vehicle_id: str,
    label: str | None = None,
) -> dict[str, Any]:
    """Map a recently seen tag reference to a configured vehicle."""
    ref = str(ref or "").strip().lower()
    vehicle_id = str(vehicle_id or "").strip()
    if vehicle_id not in known_vehicle_ids(stored_data):
        raise ValueError("vehicle_id is not a configured vehicle")
    tags = stored_data.get(RFID_TAGS_KEY)
    if not isinstance(tags, dict):
        tags = {}
    recent = stored_data.get(RFID_RECENT_KEY)
    recent = recent if isinstance(recent, dict) else {}
    existing = tags.get(ref) if isinstance(tags.get(ref), Mapping) else None
    seen = recent.get(ref) if isinstance(recent.get(ref), Mapping) else None
    if existing is None and seen is None:
        raise ValueError("tag_ref is not a known or recently seen tag")
    source = existing or seen or {}
    entry = {
        "vehicle_id": vehicle_id,
        "label": str(label).strip()[:40] if label else (source.get("label") or ""),
        "last4": str(source.get("last4") or ""),
        "last_seen": source.get("last_seen"),
    }
    tags[ref] = entry
    stored_data[RFID_TAGS_KEY] = tags
    recent.pop(ref, None)
    if not recent:
        stored_data.pop(RFID_RECENT_KEY, None)
    return entry


def remove_tag(stored_data: dict[str, Any], ref: str) -> bool:
    tags = stored_data.get(RFID_TAGS_KEY)
    if not isinstance(tags, dict) or ref not in tags:
        return False
    tags.pop(ref, None)
    if not tags:
        stored_data.pop(RFID_TAGS_KEY, None)
    return True


def remove_vehicle_tags(stored_data: dict[str, Any], vehicle_id: str) -> bool:
    """Drop tag mappings for a deleted vehicle."""
    tags = stored_data.get(RFID_TAGS_KEY)
    if not isinstance(tags, dict):
        return False
    stale = [ref for ref, entry in tags.items()
             if isinstance(entry, Mapping) and entry.get("vehicle_id") == vehicle_id]
    for ref in stale:
        tags.pop(ref, None)
    if not tags:
        stored_data.pop(RFID_TAGS_KEY, None)
    return bool(stale)


def touch_mapped_tag(stored_data: dict[str, Any], tag: Any, *, now: Any = None) -> bool:
    ref = tag_ref(tag)
    tags = stored_data.get(RFID_TAGS_KEY)
    if not ref or not isinstance(tags, dict) or not isinstance(tags.get(ref), dict):
        return False
    tags[ref]["last_seen"] = (now or _now()).isoformat()
    return True


def public_tag_listing(stored_data: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return mappings and recent unmapped tags without raw tag values."""
    names = {
        profile_id(profile): str(profile.get("display_name") or profile_id(profile))
        for profile in _profiles(stored_data)
    }
    tags = [
        {
            "tag_ref": ref,
            "last4": entry.get("last4", ""),
            "label": entry.get("label", ""),
            "vehicle_id": entry.get("vehicle_id"),
            "vehicle_name": names.get(entry.get("vehicle_id")),
            "last_seen": entry.get("last_seen"),
        }
        for ref, entry in _tag_map(stored_data).items()
        if isinstance(entry, Mapping)
    ]
    recent_raw = (stored_data or {}).get(RFID_RECENT_KEY)
    recent = [
        {
            "tag_ref": ref,
            "last4": entry.get("last4", ""),
            "charger_id": entry.get("charger_id"),
            "last_seen": entry.get("last_seen"),
        }
        for ref, entry in (recent_raw.items() if isinstance(recent_raw, Mapping) else [])
        if isinstance(entry, Mapping)
    ]
    tags.sort(key=lambda item: (str(item.get("vehicle_name") or ""), item["last4"]))
    recent.sort(key=lambda item: str(item.get("last_seen") or ""), reverse=True)
    return {"tags": tags, "recent_unmapped": recent}


def hacs_remote_id_tags(hass: Any) -> set[str]:
    """Return HACS OCPP's internal remote-start tags (not user cards).

    HACS starts remote transactions with a random per-charge-point tag.  It
    says nothing about which car is plugged in, so it is ignored for
    attribution and never offered as an unassigned card.
    """
    tags: set[str] = set()
    for central_system in ((getattr(hass, "data", {}) or {}).get("ocpp") or {}).values():
        charge_points = getattr(central_system, "charge_points", None)
        if not isinstance(charge_points, Mapping):
            continue
        for charge_point in charge_points.values():
            normalized = normalize_tag(getattr(charge_point, "_remote_id_tag", None))
            if normalized:
                tags.add(normalized)
    return tags


def session_tag(hass: Any, raw_tag: Any) -> str:
    """Return a user-presented tag, ignoring HACS remote-start tags."""
    normalized = normalize_tag(raw_tag)
    if not normalized or normalized in hacs_remote_id_tags(hass):
        return ""
    return normalized


def vehicle_display_names(stored_data: Mapping[str, Any] | None) -> dict[str, str]:
    return {
        profile_id(profile): str(profile.get("display_name") or "").strip() or profile_id(profile)
        for profile in _profiles(stored_data)
        if profile_id(profile)
    }


# A tag sensor keeps its last value between transactions.  Only trust a tag
# reported shortly before or during this session, so a card used for an
# earlier session cannot be credited to a different car on a free-vend start.
TAG_FRESHNESS_BEFORE_START_SECONDS = 15 * 60


def fresh_session_tag(hass: Any, tag_state: Any, session_start: Any) -> str:
    """Return the user tag from ``tag_state`` when it belongs to this session."""
    if tag_state is None:
        return ""
    tag = session_tag(hass, getattr(tag_state, "state", None))
    if not tag:
        return ""
    reported = (
        getattr(tag_state, "last_reported", None)
        or getattr(tag_state, "last_updated", None)
        or getattr(tag_state, "last_changed", None)
    )
    if reported is None or session_start is None:
        return ""
    try:
        if (session_start - reported).total_seconds() > TAG_FRESHNESS_BEFORE_START_SECONDS:
            return ""
    except TypeError:
        return ""
    return tag


async def async_attribute_charger_session(
    hass: Any,
    entry_id: str,
    session_manager: Any,
    session_key: str,
    charger_id: str,
    tag_state: Any = None,
) -> None:
    """Attribute the active charger-metered session ``session_key``.

    Runs on every poll while a session is active; it settles on the first
    final source (``rfid``/``manual``) and writes storage only when a newly
    presented tag needs recording.
    """
    session = session_manager.active_sessions.get(session_key)
    if session is None:
        return
    if not session.charger_id:
        session.charger_id = charger_id
    if session.attribution_source not in PROVISIONAL_SOURCES:
        return

    entry_data = (getattr(hass, "data", {}) or {}).get("power_sync", {}).get(entry_id, {})
    store = entry_data.get("automation_store") if isinstance(entry_data, Mapping) else None
    data = getattr(store, "_data", None)
    if not isinstance(data, dict):
        data = {}

    from .ev_charging_session import _parse_aware

    try:
        started = _parse_aware(session.start_time)
    except (TypeError, ValueError):
        started = None
    tag = fresh_session_tag(hass, tag_state, started)
    vehicle_id, source = resolve_session_vehicle(data, session_key, tag)

    if source != SOURCE_RFID:
        # A PowerSync-owned session for a profile on this charger identifies
        # the car better than "the only profile"; meter it here instead of
        # leaving a second, empty session in history.
        for profile in profiles_for_loadpoint(_profiles(data), session_key):
            owner_key = profile_id(profile)
            owner = session_manager.active_sessions.get(owner_key)
            if owner_key and owner_key != session_key and owner is not None:
                vehicle_id, source = owner_key, SOURCE_SELECTED
                mark = getattr(session_manager, "mark_metered_by", None)
                if mark is not None:
                    await mark(owner_key, session.id)
                break

    new_tag = bool(tag) and session.id_tag_last4 != tag_last4(tag)
    session.set_attribution(vehicle_id, source, tag_last4(tag) if tag else None)
    if not new_tag:
        return
    changed = (
        touch_mapped_tag(data, tag)
        if source == SOURCE_RFID
        else record_unmapped_tag(data, tag, charger_id)
    )
    save = getattr(store, "async_save", None)
    if changed and save is not None:
        await save()
