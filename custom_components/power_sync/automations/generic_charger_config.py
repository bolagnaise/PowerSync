"""Generic Charger profile configuration helpers."""

from __future__ import annotations

from collections.abc import Mapping, MutableMapping
from typing import Any

_GENERIC_CHARGER_ENTITY_OPTIONS = (
    ("charger_switch_entity", "generic_charger_switch_entity"),
    ("charger_amps_entity", "generic_charger_amps_entity"),
    ("charger_status_entity", "generic_charger_status_entity"),
    ("charger_power_entity", "generic_charger_power_entity"),
)


def resolve_generic_charger_profile(
    profile: MutableMapping[str, Any],
    options: Mapping[str, Any],
) -> str | None:
    """Resolve and complete a Generic Charger profile from entry options.

    A persisted vehicle profile may already identify itself as ``generic`` while
    omitting one or more entities inherited from the Generic Charger setup.
    Complete only missing fields so profile-specific entities remain authoritative.
    """
    charger_type = profile.get("charger_type")
    if charger_type != "generic":
        if not charger_type and options.get("generic_charger_enabled"):
            charger_type = "generic"
        else:
            return charger_type

    # Keep an already-persisted generic profile's previous behavior when the
    # global Generic Charger integration is disabled: do not revive stale entry
    # entities, but leave type resolution to the caller unchanged.
    if not options.get("generic_charger_enabled"):
        return charger_type

    for profile_key, option_key in _GENERIC_CHARGER_ENTITY_OPTIONS:
        if not profile.get(profile_key):
            profile[profile_key] = options.get(option_key, "")
    return "generic"
