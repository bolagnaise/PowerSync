"""Classify Tesla Powerwall alerts exposed by local TEDAPI/V1R.

Tesla's local ``activeAlerts`` feed mixes faults with persistent status,
successful-operation, and maintenance-process records.  V1R usually exposes
only the alert name, so unknown alerts must remain actionable unless Tesla
provides an explicitly non-error severity or the exact name is a researched,
well-understood informational record.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any


# These exact records describe normal state, completed operations, or a
# maintenance process.  Keep this allowlist deliberately narrow: common alerts
# such as SiteMeterComms still indicate a real performance problem.
_EXPECTED_INFORMATIONAL_ALERT_NAMES = frozenset(
    {
        "batterycalibration",
        "fwupdatesucceeded",
        "gridcodeswrite",
        "podcommissiontime",
        "systemconnectedtogrid",
    }
)

_NON_ERROR_SEVERITIES = frozenset(
    {
        "info",
        "information",
        "informational",
        "process",
        "success",
    }
)


def _normalized_alert_value(value: Any) -> str:
    """Return a firmware-tolerant alert name or severity identifier."""
    if value is None:
        return ""
    return "".join(
        character for character in str(value).lower() if character.isalnum()
    )


def powerwall_alert_name(alert: Any) -> str:
    """Return the display name from a TEDAPI alert value."""
    if isinstance(alert, dict):
        value = alert.get("name") or alert.get("alert_name")
    else:
        value = alert
    return str(value) if value not in (None, "") else "Unknown"


def powerwall_alert_is_informational(alert: Any) -> bool:
    """Return True only when an alert is explicitly known to be non-error."""
    if isinstance(alert, dict):
        severity = alert.get("severity") or alert.get("alert_severity")
        if _normalized_alert_value(severity) in _NON_ERROR_SEVERITIES:
            return True
    return (
        _normalized_alert_value(powerwall_alert_name(alert))
        in _EXPECTED_INFORMATIONAL_ALERT_NAMES
    )


def split_powerwall_alerts(
    alerts: Any,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split raw local alerts into actionable and informational details."""
    if not isinstance(alerts, Iterable) or isinstance(alerts, (str, bytes, dict)):
        return [], []

    actionable: list[dict[str, Any]] = []
    informational: list[dict[str, Any]] = []
    for alert in alerts:
        detail = dict(alert) if isinstance(alert, dict) else {"name": alert}
        if powerwall_alert_is_informational(detail):
            informational.append(detail)
        else:
            actionable.append(detail)
    return actionable, informational


def powerwall_alert_attributes(alerts: Any) -> dict[str, Any]:
    """Build HA attributes while retaining excluded raw alert evidence."""
    if not isinstance(alerts, Iterable) or isinstance(alerts, (str, bytes, dict)):
        return {}
    all_details = [
        dict(alert) if isinstance(alert, dict) else {"name": alert}
        for alert in alerts
    ]
    if not all_details:
        return {}
    actionable, informational = split_powerwall_alerts(all_details)

    severities: dict[str, Any] = {}
    for alert in actionable:
        name = powerwall_alert_name(alert)
        severity = alert.get("severity") or alert.get("alert_severity")
        if severity is not None:
            severities[name] = severity

    informational_severities: dict[str, Any] = {}
    for alert in informational:
        name = powerwall_alert_name(alert)
        severity = alert.get("severity") or alert.get("alert_severity")
        if severity is not None:
            informational_severities[name] = severity

    return {
        "alerts": [powerwall_alert_name(alert) for alert in actionable],
        "severities": severities,
        "alert_details": actionable,
        "informational_alerts": [
            powerwall_alert_name(alert) for alert in informational
        ],
        "informational_severities": informational_severities,
        "informational_alert_details": informational,
        "all_alerts": [powerwall_alert_name(alert) for alert in all_details],
        "all_alert_details": all_details,
    }
