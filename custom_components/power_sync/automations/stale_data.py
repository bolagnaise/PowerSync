"""Grace period for data that goes missing while an EV is already charging.

Phase 2 of the EV charging refactor: missing or stale data may hold a new
start, but must not end a running charge on its first gap. A running session
keeps its current behaviour for ``STALE_DATA_GRACE_SECONDS`` after a value
goes missing, then stops if it is still missing. The clock resets as soon as
the value returns.
"""

from __future__ import annotations

from collections.abc import MutableMapping
import time
from typing import Any

STALE_DATA_GRACE_SECONDS = 15 * 60
_MISSING_SINCE_KEY = "_missing_data_since"


def missing_data_grace_expired(
    holder: MutableMapping[str, Any],
    key: str,
    *,
    missing: bool,
    now: float | None = None,
) -> bool:
    """Track how long ``key`` has been missing in ``holder``.

    Returns True once it has been missing for the whole grace period, so the
    caller should act on the gap (stop). Returns False while the value is
    present or still inside the grace period. A present value clears the clock.
    """
    stamps = holder.get(_MISSING_SINCE_KEY)
    if not isinstance(stamps, dict):
        stamps = {}
        holder[_MISSING_SINCE_KEY] = stamps
    if not missing:
        stamps.pop(key, None)
        return False
    now = time.time() if now is None else now
    since = stamps.get(key)
    if not isinstance(since, (int, float)) or since > now:
        stamps[key] = since = now
    return now - since >= STALE_DATA_GRACE_SECONDS


def missing_data_grace_remaining(
    holder: MutableMapping[str, Any], key: str, *, now: float | None = None,
) -> int | None:
    """Return whole seconds left in ``key``'s grace period, or None if not missing."""
    stamps = holder.get(_MISSING_SINCE_KEY)
    since = stamps.get(key) if isinstance(stamps, dict) else None
    if not isinstance(since, (int, float)):
        return None
    now = time.time() if now is None else now
    return max(0, int(STALE_DATA_GRACE_SECONDS - (now - since)))


def missing_data_grace_minutes_left(
    holder: MutableMapping[str, Any], key: str, *, now: float | None = None,
) -> int:
    """Return the whole minutes left in ``key``'s grace period, rounded up (>= 1)."""
    seconds = missing_data_grace_remaining(holder, key, now=now) or 0
    return max(1, -(-seconds // 60))
