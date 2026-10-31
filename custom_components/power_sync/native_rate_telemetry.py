"""Read provider reports for one VIN without issuing vehicle commands."""
from __future__ import annotations

import math

from .const import (
    CONF_EV_PROVIDER,
    CONF_TESLA_BLE_VEHICLE_MAPPING,
    EV_PROVIDER_BOTH,
    EV_PROVIDER_TESLA_BLE,
)
from .optimization.tesla_schedules import _section, _timestamp


def vehicle_sources(hass, vin):
    """Return matching Fleet/Teslemetry vehicle coordinators only."""
    normalized = str(vin or "").upper()
    sources = []
    for domain in ("teslemetry", "tesla_fleet"):
        for entry in hass.config_entries.async_entries(domain):
            for vehicle in getattr(getattr(entry, "runtime_data", None), "vehicles", ()):
                if str(getattr(vehicle, "vin", "")).upper() != normalized:
                    continue
                coordinator = getattr(vehicle, "coordinator", None)
                if coordinator is not None:
                    sources.append((domain, entry, vehicle, coordinator))
    return sources


def vehicle_reports(hass, vin):
    reports = []
    for _domain, _entry, _vehicle, coordinator in vehicle_sources(hass, vin):
        if getattr(coordinator, "last_update_success", False) is not True:
            continue
        data = getattr(coordinator, "data", None)
        if isinstance(data, dict):
            reports.append(data)
    return reports


async def refresh_vehicle_sources(hass, vin, *, timeout=5):
    """Boundedly request matching provider coordinators to update themselves.

    This is used only on an explicit native Max admission. The ordinary
    lifecycle continues to consume cached coordinator reports and never polls
    a paid provider merely to clear a journal.
    """
    import asyncio
    calls = []
    for _domain, _entry, _vehicle, coordinator in vehicle_sources(hass, vin):
        refresh = getattr(coordinator, "async_request_refresh", None)
        if callable(refresh):
            calls.append(asyncio.wait_for(refresh(), timeout=timeout))
    if not calls:
        return False
    results = await asyncio.gather(*calls, return_exceptions=True)
    return any(not isinstance(result, BaseException) for result in results)


def _ble_vehicle_report(hass, entry, vin):
    """Build a report only from an explicitly VIN-mapped BLE bridge.

    A configured first-prefix fallback is deliberately insufficient for a
    durable VIN reservation. Both current and charging-state receipts must be
    present and correlated; missing electrical metadata is acceptable only for
    the zero-current stopped proof.
    """
    config = {**getattr(entry, "data", {}), **getattr(entry, "options", {})}
    try:
        from .tesla_ble_mapping import configured_ble_prefixes, parse_tesla_ble_vehicle_mapping
        prefix = parse_tesla_ble_vehicle_mapping(
            config.get(CONF_TESLA_BLE_VEHICLE_MAPPING)
        ).get(str(vin).upper())
        if prefix is None or prefix not in configured_ble_prefixes(config):
            return None
        from .tesla_ble import (
            get_tesla_ble_charge_current_state,
            get_tesla_ble_charging_state,
            get_tesla_ble_observed_at,
        )
        current = get_tesla_ble_charge_current_state(hass, prefix)
        charging = get_tesla_ble_charging_state(hass, prefix)
        # HA update timestamps can replay an equal BLE value. Only the raw
        # per-field receipt entities establish a new vehicle observation.
        if (not getattr(current, "entity_id", None)
                or not getattr(charging, "entity_id", None)
                or hass.states.get(f"{current.entity_id}_received") is None
                or hass.states.get(f"{charging.entity_id}_received") is None):
            return None
        current_at = get_tesla_ble_observed_at(hass, current)
        charging_at = get_tesla_ble_observed_at(hass, charging)
        if current is None or charging is None or current_at is None or charging_at is None:
            return None
        try:
            amps = float(current.state)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(amps) or amps < 0:
            return None
        state = str(getattr(charging, "state", "") or "").strip()
        if not state:
            return None
        observed = min(current_at, charging_at).timestamp()
        return {"charge_state": {
            "timestamp": observed,
            "charging_state": state,
            "charger_actual_current": amps,
        }}
    except Exception:
        return None


def vehicle_report_for_entry(hass, entry, vin):
    """Return the newest cloud or exact local report for this VIN."""
    reports = vehicle_reports(hass, vin)
    config = {**getattr(entry, "data", {}), **getattr(entry, "options", {})}
    provider = config.get(CONF_EV_PROVIDER)
    local = None
    if provider in (EV_PROVIDER_TESLA_BLE, EV_PROVIDER_BOTH):
        local = _ble_vehicle_report(hass, entry, vin)
    if local is not None:
        reports.append(local)
    return max(reports,
        key=lambda data: _timestamp(_section(data, "charge_state").get("timestamp")), default=None)


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
