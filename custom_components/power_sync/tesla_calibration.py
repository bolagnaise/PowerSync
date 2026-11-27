"""Shared Tesla Powerwall calibration state handling.

Tesla exposes explicit ``BatteryCalibration`` and ``CalibrationRequested``
alerts through the local DeviceControllerQuery path.  ``BatteryCalibration``
always pauses automatic control.  ``CalibrationRequested`` is only a request:
some gateways keep it raised for days while following owner commands, while
others take over the battery straight away and ignore charge commands.  It
therefore pauses control only after Tesla is seen discharging against an
active PowerSync charge or hold, or resting against a force charge, until the alert clears, a drain-and-refill
cycle completes, 72 hours pass, or the owner runs ``refresh_calibration``
(which also acknowledges a lingering request until it clears).  Older PowerSync releases could only infer a
calibration after repeated cloud operation-mode verification failures.  This
module keeps both signals as independent sources and derives the compatibility
``calibration_suspected`` flag used by the optimiser and service guards.

A gateway can also keep following charge commands while refusing forced
export: under Time-Based Control it holds the battery and the house imports
from the grid.  That evidence sets a separate export-only latch
(``calibration_export_refused``), so PowerSync stops forcing and planning
battery export without pausing charging, until the request clears, a
drain-and-refill cycle completes, 72 hours pass, or the owner runs
``refresh_calibration``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable


CALIBRATION_SOURCE_LOCAL_ALERT = "local_alert"
CALIBRATION_SOURCE_MODE_STICK = "mode_stick"
_ACTIVE_CALIBRATION_ALERT_NAME = "batterycalibration"
_REQUESTED_CALIBRATION_ALERT_NAME = "calibrationrequested"
_CALIBRATION_ALERT_NAMES = frozenset(
    {_ACTIVE_CALIBRATION_ALERT_NAME, _REQUESTED_CALIBRATION_ALERT_NAME}
)
CALIBRATION_REQUEST_ACK_KEY = "_calibration_request_acknowledged"
# CalibrationRequested only pauses control once Tesla is seen overriding an
# active PowerSync charge/hold.  The latch records that takeover so the pause
# survives PowerSync releasing its own command, and is bounded below.
CALIBRATION_TAKEOVER_SINCE_KEY = "_calibration_takeover_since"
CALIBRATION_TAKEOVER_LATCH_KEY = "_calibration_takeover_latch"
CALIBRATION_TAKEOVER_MIN_DISCHARGE_W = 1000.0
CALIBRATION_TAKEOVER_CONFIRM_SECONDS = 180.0
# Tesla can also hold the battery idle ("resting") against a force charge.
CALIBRATION_IGNORED_CHARGE_MAX_W = 500.0
CALIBRATION_IGNORED_CHARGE_MAX_SOC = 95.0
CALIBRATION_IGNORED_CHARGE_MIN_TARGET_W = 1000.0
# Tesla can also refuse a force discharge: with Time-Based Control the
# gateway holds (or trickle-charges) the battery and the house imports from
# the grid, even with ample charge.  Observed 2026-11-27 under a lingering
# CalibrationRequested: battery 0 to -0.3 kW, grid +2.5 kW at 77% SOC.
CALIBRATION_REFUSED_DISCHARGE_MAX_W = 500.0
CALIBRATION_REFUSED_DISCHARGE_MIN_IMPORT_W = 500.0
CALIBRATION_REFUSED_DISCHARGE_MIN_SOC = 20.0
CALIBRATION_REFUSED_DISCHARGE_RESERVE_MARGIN = 10.0
CALIBRATION_EXPORT_REFUSED_KEY = "calibration_export_refused"
CALIBRATION_EXPORT_REFUSED_SINCE_KEY = "_calibration_export_refused_since"
CALIBRATION_EXPORT_REFUSED_LATCH_KEY = "_calibration_export_refused_latch"
# Tesla completes a calibration within 72 hours; a requested-only pause never
# outlives that, nor a completed drain-and-refill cycle.
CALIBRATION_TAKEOVER_MAX_SECONDS = 72 * 3600
CALIBRATION_CYCLE_LOW_SOC = 20.0
CALIBRATION_CYCLE_FULL_SOC = 98.0


def _normalized_alert_name(value: Any) -> str:
    """Return a firmware-tolerant alert identifier."""
    if value is None:
        return ""
    return "".join(character for character in str(value).lower() if character.isalnum())


def powerwall_calibration_alert_names(alerts: Any) -> frozenset[str]:
    """Return the normalized calibration alert names in a local alert set."""
    if not isinstance(alerts, Iterable) or isinstance(alerts, (str, bytes, dict)):
        return frozenset()
    found: set[str] = set()
    for alert in alerts:
        if isinstance(alert, dict):
            name = alert.get("name") or alert.get("alert_name")
        else:
            name = alert
        normalized = _normalized_alert_name(name)
        if normalized in _CALIBRATION_ALERT_NAMES:
            found.add(normalized)
    return frozenset(found)


def powerwall_calibration_alert_active(alerts: Any) -> bool:
    """Return True when a local Powerwall alert set reports calibration."""
    return bool(powerwall_calibration_alert_names(alerts))


def powerwall_calibration_request_only(alerts: Any) -> bool:
    """Return True when only ``CalibrationRequested`` reports calibration."""
    return powerwall_calibration_alert_names(alerts) == frozenset(
        {_REQUESTED_CALIBRATION_ALERT_NAME}
    )


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
    """Clear every calibration source for the explicit reset service.

    The reset also acknowledges a ``CalibrationRequested``-only alert so the
    next local poll does not immediately re-pause control while that alert
    remains raised.  The acknowledgement ends when a valid poll no longer
    reports a calibration alert, and never suppresses ``BatteryCalibration``.
    """
    was_active = bool(calibration_sources(entry_data))
    if was_active:
        entry_data[CALIBRATION_REQUEST_ACK_KEY] = True
    entry_data.pop(CALIBRATION_TAKEOVER_SINCE_KEY, None)
    entry_data.pop(CALIBRATION_TAKEOVER_LATCH_KEY, None)
    if clear_calibration_export_refusal(entry_data):
        entry_data[CALIBRATION_REQUEST_ACK_KEY] = True
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


def calibration_export_refused(entry_data: Any) -> bool:
    """Return True while Tesla is known to refuse forced battery export."""
    return isinstance(entry_data, dict) and bool(
        entry_data.get(CALIBRATION_EXPORT_REFUSED_KEY)
    )


def clear_calibration_export_refusal(entry_data: dict[str, Any]) -> bool:
    """Clear the export-only latch; return True when it was set."""
    was_set = calibration_export_refused(entry_data)
    entry_data.pop(CALIBRATION_EXPORT_REFUSED_SINCE_KEY, None)
    entry_data.pop(CALIBRATION_EXPORT_REFUSED_LATCH_KEY, None)
    entry_data[CALIBRATION_EXPORT_REFUSED_KEY] = False
    return was_set


def dispatch_calibration_state(hass: Any, entry_id: str) -> None:
    """Notify HA entities after any calibration evidence transition."""
    try:
        from homeassistant.helpers import dispatcher
    except ImportError:
        return

    sender = getattr(dispatcher, "async_dispatcher_send", None)
    if callable(sender):
        sender(hass, f"power_sync_calibration_state_{entry_id}")
