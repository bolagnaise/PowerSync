"""
Decision logging system for PowerSync optimizer.

Records every optimizer action with human-readable explanations,
persisted via HA Store with a 288-entry ring buffer (24h at 5-min intervals).
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import asdict, dataclass
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

_LOGGER = logging.getLogger(__name__)

DECISION_LOG_VERSION = 1
DECISION_LOG_SAVE_DELAY = 60  # Coalesce writes — flush at most every 60 seconds
MAX_ENTRIES = 288  # 24 hours at 5-minute intervals


@dataclass
class DecisionEntry:
    """Single decision record from the optimizer."""

    timestamp: str  # ISO 8601
    action: str  # "charge", "export", "self_consumption", "idle"
    original_action: str  # What LP chose before overrides
    reason: str  # Human-readable explanation
    override_reason: str | None  # If action was overridden, why
    import_price: float  # Current $/kWh
    export_price: float  # Current $/kWh
    soc: float  # Battery SOC as fraction (0-1)
    solar_power_kw: float
    load_kw: float
    savings_impact: float  # Estimated $ impact vs baseline for this interval


class DecisionLog:
    """Ring buffer of optimizer decisions with HA Store persistence."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        """Initialize the decision log."""
        self._hass = hass
        self._entries: deque[DecisionEntry] = deque(maxlen=MAX_ENTRIES)
        self._store = Store(
            hass,
            DECISION_LOG_VERSION,
            f"power_sync.decision_log.{entry_id}",
        )

    def log(self, entry: DecisionEntry) -> None:
        """Add a decision entry to the ring buffer and schedule persistence."""
        self._entries.append(entry)
        self._schedule_save()

    def get_entries(self, limit: int = MAX_ENTRIES) -> list[dict[str, Any]]:
        """Get decision log entries as dicts, newest first."""
        entries = list(self._entries)
        entries.reverse()
        return [asdict(e) for e in entries[:limit]]

    def get_latest(self) -> DecisionEntry | None:
        """Get the most recent decision entry."""
        if self._entries:
            return self._entries[-1]
        return None

    async def restore(self) -> None:
        """Load persisted decision log from HA Store on startup."""
        try:
            data = await self._store.async_load()
        except Exception as exc:
            _LOGGER.warning("Failed to load persisted decision log: %s", exc)
            return

        if not data:
            _LOGGER.debug("No persisted decision log found (first run)")
            return

        entries_raw = data.get("entries", [])
        count = 0
        for raw in entries_raw:
            try:
                entry = DecisionEntry(
                    timestamp=raw["timestamp"],
                    action=raw["action"],
                    original_action=raw["original_action"],
                    reason=raw["reason"],
                    override_reason=raw.get("override_reason"),
                    import_price=float(raw.get("import_price", 0.0)),
                    export_price=float(raw.get("export_price", 0.0)),
                    soc=float(raw.get("soc", 0.0)),
                    solar_power_kw=float(raw.get("solar_power_kw", 0.0)),
                    load_kw=float(raw.get("load_kw", 0.0)),
                    savings_impact=float(raw.get("savings_impact", 0.0)),
                )
                self._entries.append(entry)
                count += 1
            except (KeyError, TypeError, ValueError) as exc:
                _LOGGER.debug("Skipping malformed decision log entry: %s", exc)

        _LOGGER.info("Restored %d decision log entries from storage", count)

    def _schedule_save(self) -> None:
        """Schedule a coalesced write of the decision log to persistent storage."""
        self._store.async_delay_save(
            self._data_to_save,
            DECISION_LOG_SAVE_DELAY,
        )

    def _data_to_save(self) -> dict[str, Any]:
        """Return decision log data dict for Store serialization."""
        return {
            "entries": [asdict(e) for e in self._entries],
        }

    async def async_save(self) -> None:
        """Flush decision log to disk immediately (e.g. on shutdown)."""
        await self._store.async_save(self._data_to_save())


def generate_decision_reason(
    action: str,
    original_action: str,
    import_price: float,
    export_price: float,
    soc: float,
    solar_power_kw: float,
    load_kw: float,
    next_price_change: tuple[str, float] | None = None,
    schedule_actions: list[str] | None = None,
) -> str:
    """Generate a human-readable explanation for an optimizer decision.

    Prices are in $/kWh internally — displayed as c/kWh (x100).
    SOC is a fraction (0-1) — displayed as percentage.
    """
    import_c = import_price * 100
    export_c = export_price * 100
    soc_pct = soc * 100

    if action == "charge":
        reason = f"Charging battery at {import_c:.0f}c/kWh"
        if next_price_change is not None:
            next_time, next_price = next_price_change
            next_c = next_price * 100
            if next_c > import_c:
                reason += f" — storing for {next_c:.0f}c/kWh peak export at {next_time}"
            else:
                reason += f" — low price window"
        elif schedule_actions and "export" in schedule_actions:
            reason += " — storing for upcoming export window"
        else:
            reason += f" — import price below threshold"
        return reason

    if action in ("discharge", "export"):
        reason = f"Exporting to grid at {export_c:.0f}c/kWh"
        if export_c > 30:
            reason += f" — price spike, battery at {soc_pct:.0f}%"
        elif soc_pct > 80:
            reason += f" — battery at {soc_pct:.0f}%, capturing value"
        else:
            reason += f" — battery at {soc_pct:.0f}%"
        return reason

    if action == "self_consumption":
        if solar_power_kw > load_kw and solar_power_kw > 0.1:
            return (
                "Self-consumption — solar covering load, excess charging battery"
            )
        if solar_power_kw > 0.1:
            return (
                f"Self-consumption — solar {solar_power_kw:.1f}kW supplementing "
                f"battery for {load_kw:.1f}kW load"
            )
        return f"Self-consumption — battery serving {load_kw:.1f}kW home load"

    if action == "idle":
        reason = f"Holding battery at {soc_pct:.0f}%"
        if next_price_change is not None:
            next_time, next_price = next_price_change
            next_c = next_price * 100
            if next_c > import_c:
                reason += (
                    f" — saving charge for peak ({next_c:.0f}c/kWh at {next_time})"
                )
            else:
                reason += f" — waiting for better conditions"
        elif import_c < 10:
            reason += " — cheap import, grid serving load"
        else:
            reason += " — holding for upcoming opportunity"
        return reason

    return f"Action: {action} (import={import_c:.0f}c, export={export_c:.0f}c, SOC={soc_pct:.0f}%)"


def generate_override_reason(
    original_action: str,
    effective_action: str,
    override_type: str,
    context: dict[str, Any] | None = None,
) -> str | None:
    """Generate explanation when an optimizer action is overridden.

    Returns None if no override occurred.
    """
    if original_action == effective_action:
        return None

    ctx = context or {}

    if override_type == "calibration":
        return (
            f"Blocked {original_action} — calibration suspected, "
            f"using self_consumption"
        )

    if override_type == "demand_window":
        return (
            f"Override IDLE to self_consumption — demand charge window active, "
            f"minimizing grid import"
        )

    if override_type == "soc_at_reserve":
        soc_pct = ctx.get("soc_pct", 0)
        reserve_pct = ctx.get("reserve_pct", 0)
        return (
            f"Override {original_action} to self_consumption — "
            f"SOC {soc_pct:.0f}% at reserve floor {reserve_pct:.0f}%"
        )

    if override_type == "curtailment":
        export_c = ctx.get("export_price_c", 0)
        return (
            f"Blocked export — curtailment active, "
            f"export price {export_c:.0f}c/kWh"
        )

    if override_type == "demand_export_block":
        return (
            f"Override {original_action} to self_consumption — "
            f"near demand charge window, preserving battery"
        )

    if override_type == "hysteresis":
        count = ctx.get("count", 0)
        required = ctx.get("required", 0)
        return (
            f"Holding in {effective_action} — hysteresis {count}/{required}, "
            f"confirming commitment"
        )

    if override_type == "user_cooldown":
        remaining_min = ctx.get("remaining_min", 0)
        return (
            f"Suppressed {original_action} — user restore cooldown active "
            f"({remaining_min:.0f}min remaining)"
        )

    if override_type == "price_above_median":
        current_c = ctx.get("current_price_c", 0)
        median_c = ctx.get("median_price_c", 0)
        return (
            f"Override IDLE to self_consumption — import {current_c:.0f}c/kWh "
            f">= median {median_c:.0f}c/kWh, not cheap enough to justify grid import"
        )

    if override_type == "soc_at_idle_floor":
        soc_pct = ctx.get("soc_pct", 0)
        floor_pct = ctx.get("floor_pct", 0)
        return (
            f"Override IDLE to self_consumption — SOC {soc_pct:.0f}% "
            f"at/below idle floor {floor_pct:.0f}%, nothing to hold"
        )

    if override_type == "charge_soc_full":
        soc_pct = ctx.get("soc_pct", 0)
        return (
            f"Skipped charge — battery at {soc_pct:.0f}%, "
            f"switching to self_consumption"
        )

    return (
        f"Override {original_action} to {effective_action} "
        f"(reason: {override_type})"
    )
