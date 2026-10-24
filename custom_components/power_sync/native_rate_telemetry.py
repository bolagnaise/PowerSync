"""Read existing provider reports for one VIN without refreshing or waking it."""
from __future__ import annotations

import math

from .optimization.tesla_schedules import _section, _timestamp


def vehicle_reports(hass, vin):
    reports = []
    for domain in ("teslemetry", "tesla_fleet"):
        for entry in hass.config_entries.async_entries(domain):
            for vehicle in getattr(getattr(entry, "runtime_data", None), "vehicles", ()):
                if str(getattr(vehicle, "vin", "")).upper() != vin:
                    continue
                coordinator = getattr(vehicle, "coordinator", None)
                if getattr(coordinator, "last_update_success", False) is not True:
                    continue
                data = getattr(coordinator, "data", None)
                if not isinstance(data, dict):
                    continue
                reports.append(data)
    return reports


def vehicle_report(hass, vin):
    """Use the newest charge report independently of schedule freshness."""
    return max(vehicle_reports(hass, vin),
        key=lambda data: _timestamp(_section(data, "charge_state").get("timestamp")), default=None)


def newer_schedule_report(hass, vin, cached_at):
    """A newer disabled/edited rule invalidates the old cached window."""
    latest, stamp = None, cached_at
    for data in vehicle_reports(hass, vin):
        section = _section(data, "charge_schedule_data")
        at = _timestamp(section.get("timestamp_seconds", section.get("timestamp")))
        if at > stamp:
            latest, stamp = data, at
    return latest, stamp


def charging_unit_w(charge, profile):
    """Use explicit electrical metadata; missing phases cannot mean one phase."""
    voltage = charge.get("charger_voltage") or profile.get("voltage")
    phases = profile.get("phases", charge.get("charger_phases"))
    if isinstance(voltage, bool) or isinstance(phases, bool):
        return 0.0
    try:
        voltage = float(voltage)
        phases = float(phases)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(voltage) or not 90 <= voltage <= 300 or phases not in (1, 2, 3):
        return 0.0
    # Tesla reports three-phase charging as either two or three, depending
    # on the vehicle/provider. An explicit user profile accepts only 1 or 3.
    if "phases" in profile and phases not in (1, 3):
        return 0.0
    reported = charge.get("charger_phases")
    if "phases" in profile and reported in (1, 2, 3):
        if phases != (3 if reported in (2, 3) else 1):
            return 0.0
    return voltage * (3 if phases in (2, 3) else 1)
