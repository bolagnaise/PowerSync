"""Source timestamps for battery readings from Home Assistant entities."""

from datetime import datetime
import math
from typing import Any


MAX_BATTERY_SAMPLE_SKEW_SECONDS = 30


def oldest_paired_report(stamps: list[datetime | None]) -> datetime | None:
    """Require all components of a reading to come from one time window."""
    if not stamps or any(stamp is None for stamp in stamps):
        return None
    earliest = min(stamps)
    latest = max(stamps)
    if (latest - earliest).total_seconds() > MAX_BATTERY_SAMPLE_SKEW_SECONDS:
        return None
    return earliest


def oldest_entity_report(
    hass: Any,
    *entity_ids: str | None,
    numeric: bool = True,
) -> datetime | None:
    """Timestamp a reading only if every contributing entity reported it."""
    if not entity_ids:
        return None
    stamps = []
    for entity_id in entity_ids:
        state = hass.states.get(entity_id) if entity_id else None
        if state is None or state.state in (None, "", "unknown", "unavailable"):
            return None
        if numeric:
            try:
                if not math.isfinite(float(state.state)):
                    return None
            except (TypeError, ValueError):
                return None
        stamp = getattr(state, "last_reported", None) or getattr(
            state, "last_updated", None
        )
        if not isinstance(stamp, datetime) or stamp.utcoffset() is None:
            return None
        stamps.append(stamp)
    return oldest_paired_report(stamps)
