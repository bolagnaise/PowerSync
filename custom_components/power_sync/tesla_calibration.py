"""Shared Tesla Powerwall calibration state handling.

Tesla exposes an explicit ``BatteryCalibration`` alert through the local
DeviceControllerQuery path.  Older PowerSync releases could only infer a
calibration after repeated cloud operation-mode verification failures.  This
module keeps both signals as independent sources and derives the compatibility
``calibration_suspected`` flag used by the optimiser and service guards.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable


CALIBRATION_SOURCE_LOCAL_ALERT = "local_alert"
CALIBRATION_SOURCE_MODE_STICK = "mode_stick"
_CALIBRATION_ALERT_NAME = "batterycalibration"


def _normalized_alert_name(value: Any) -> str:
    """Return a firmware-tolerant alert identifier."""
    if value is None:
        return ""
    return "".join(character for character in str(value).lower() if character.isalnum())


def powerwall_calibration_alert_active(alerts: Any) -> bool:
    """Return True when a local Powerwall alert set reports calibration."""
    if not isinstance(alerts, Iterable) or isinstance(alerts, (str, bytes, dict)):
        return False
    for alert in alerts:
        if isinstance(alert, dict):
            name = alert.get("name") or alert.get("alert_name")
        else:
            name = alert
        if _normalized_alert_name(name) == _CALIBRATION_ALERT_NAME:
            return True
    return False


def calibration_sources(entry_data: dict[str, Any]) -> tuple[str, ...]:
    """Return normalized active calibration sources, including legacy state."""
    raw_sources = entry_data.get("_calibration_sources")
    sources = {
        str(source)
        for source in raw_sources or []
        if isinstance(source, str) and source
    }
    if not sources and entry_data.get("calibration_suspected"):
        legacy_source = entry_data.get("calibration_source")
        sources.add(
            legacy_source
            if isinstance(legacy_source, str) and legacy_source
            else CALIBRATION_SOURCE_MODE_STICK
        )
    return tuple(sorted(sources))


@dataclass(frozen=True)
class CalibrationTransition:
    """Result of updating one calibration evidence source."""

    was_active: bool
    is_active: bool
    started: bool
    completed: bool
    sources: tuple[str, ...]


def set_calibration_source(
    entry_data: dict[str, Any],
    source: str,
    active: bool,
    *,
    now: datetime | None = None,
) -> CalibrationTransition:
    """Set one source and update the public aggregate calibration state."""
    if not source:
        raise ValueError("calibration source is required")

    sources = set(calibration_sources(entry_data))
    was_active = bool(sources)
    if active:
        sources.add(source)
    else:
        sources.discard(source)

    ordered_sources = tuple(sorted(sources))
    is_active = bool(ordered_sources)
    started = is_active and not was_active
    completed = was_active and not is_active

    entry_data["_calibration_sources"] = list(ordered_sources)
    entry_data["calibration_suspected"] = is_active
    entry_data["calibration_sources"] = list(ordered_sources)
    entry_data["calibration_source"] = (
        CALIBRATION_SOURCE_LOCAL_ALERT
        if CALIBRATION_SOURCE_LOCAL_ALERT in sources
        else ordered_sources[0] if ordered_sources else None
    )

    if started or (is_active and entry_data.get("calibration_detected_at") is None):
        entry_data["calibration_detected_at"] = now or datetime.now(timezone.utc)
    elif completed:
        entry_data["calibration_detected_at"] = None

    return CalibrationTransition(
        was_active=was_active,
        is_active=is_active,
        started=started,
        completed=completed,
        sources=ordered_sources,
    )


def clear_calibration_sources(entry_data: dict[str, Any]) -> CalibrationTransition:
    """Clear every calibration source for the explicit reset service."""
    was_active = bool(calibration_sources(entry_data))
    entry_data["_calibration_sources"] = []
    entry_data["calibration_sources"] = []
    entry_data["calibration_source"] = None
    entry_data["calibration_suspected"] = False
    entry_data["calibration_detected_at"] = None
    entry_data["_calibration_alert_clear_polls"] = 0
    return CalibrationTransition(
        was_active=was_active,
        is_active=False,
        started=False,
        completed=was_active,
        sources=(),
    )


def dispatch_calibration_state(hass: Any, entry_id: str) -> None:
    """Notify HA entities after any calibration evidence transition."""
    try:
        from homeassistant.helpers import dispatcher
    except ImportError:
        return

    sender = getattr(dispatcher, "async_dispatcher_send", None)
    if callable(sender):
        sender(hass, f"power_sync_calibration_state_{entry_id}")
