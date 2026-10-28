"""Explicit Tesla export-preference evidence, without permissive defaults."""
from __future__ import annotations

from typing import NamedTuple

EXPORT_RULES = ("never", "pv_only", "battery_ok")


class ExportRuleReading(NamedTuple):
    value: str | None
    status: str
    path: str | None = None


def read_export_rule(site_info) -> ExportRuleReading:
    """Read supported cloud shapes; null is unavailable, not an enum value."""
    if not isinstance(site_info, dict):
        return ExportRuleReading(None, "unavailable")
    components = site_info.get("components")
    if components is not None and not isinstance(components, dict):
        return ExportRuleReading(None, "invalid")
    components = components or {}
    field = "customer_preferred_export_rule"
    locations = ((f"components.{field}", components), (field, site_info))
    values = []
    for path, obj in locations:
        value = obj.get(field)
        if value is not None:
            if value not in EXPORT_RULES:
                return ExportRuleReading(None, "invalid", path)
            values.append((path, value))
    if values:
        if any(value != values[0][1] for _, value in values):
            return ExportRuleReading(None, "conflict")
        return ExportRuleReading(values[0][1], "known", values[0][0])
    # Preserve the established explicit non-export restriction. False cannot
    # distinguish solar-only from battery export and supplies no exact original.
    if components.get("non_export_configured") is True:
        return ExportRuleReading("never", "known", "components.non_export_configured")
    return ExportRuleReading(None, "null" if any(field in obj for _, obj in locations) else "absent")


def tesla_export_rule_from_site_info(site_info) -> str | None:
    return read_export_rule(site_info).value
