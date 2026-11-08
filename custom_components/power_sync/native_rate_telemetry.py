"""Read provider reports for one VIN without issuing vehicle commands."""
from __future__ import annotations

import math
import time

from .const import (
    CONF_EV_PROVIDER,
    CONF_TESLA_BLE_VEHICLE_MAPPING,
    EV_PROVIDER_BOTH,
    EV_PROVIDER_TESLA_BLE,
)
from .optimization.tesla_schedules import _section, _timestamp


def vehicle_sources(hass, vin, *, include_tessie=False):
    """Return matching Fleet/Teslemetry vehicle coordinators only."""
    normalized = str(vin or "").upper()
    sources = []
    domains = ("teslemetry", "tesla_fleet", "tessie") if include_tessie else ("teslemetry", "tesla_fleet")
    for domain in domains:
        for entry in hass.config_entries.async_entries(domain):
            for vehicle in getattr(getattr(entry, "runtime_data", None), "vehicles", ()):
                if str(getattr(vehicle, "vin", "")).upper() != normalized:
                    continue
                coordinator = getattr(vehicle, "data_coordinator" if domain == "tessie" else "coordinator", None)
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


def _native_charge_source(hass, vin, entity_id):
    """Pin the native entry/runtime/device which owns one registered entity."""
    from homeassistant.helpers import entity_registry as er
    from .tesla_ev_observation import _device_identity, _identifiers, _provider_identifiers

    registration = er.async_get(hass).async_get(entity_id)
    domain = getattr(registration, "platform", None)
    if domain not in {"tesla_fleet", "teslemetry"} or getattr(registration, "disabled_by", None) is not None:
        return None
    entry_id = getattr(registration, "config_entry_id", None)
    device_id = getattr(registration, "device_id", None)
    unique_id = getattr(registration, "unique_id", None)
    if not all(isinstance(value, str) and value for value in (entry_id, device_id, unique_id)):
        return None
    normalized = str(vin or "").upper()
    matching = [source for source in vehicle_sources(hass, normalized)
                if source[0] == domain
                and getattr(source[1], "entry_id", None) == entry_id
                and _device_identity(hass, source[2], domain, normalized)[0] == device_id]
    if len(matching) != 1:
        return None
    _domain, entry, vehicle, coordinator = matching[0]
    device = getattr(vehicle, "device", None)
    if _provider_identifiers(device, domain) != frozenset({(domain, normalized)}):
        return None
    token = (domain, entry_id, device_id, unique_id,
             id(entry), id(getattr(entry, "runtime_data", None)),
             id(vehicle), id(coordinator), id(device), tuple(sorted(_identifiers(device))))
    return token


def native_charge_setting(hass, vin, entity_id, *, observation=None):
    """Read one exact native producer's charge document, never its HA projection."""
    from .tesla_ev_observation import get_tesla_vehicle_observation

    source = _native_charge_source(hass, vin, entity_id)
    if source is None:
        return None
    domain, entry_id, device_id = source[:3]
    normalized = str(vin or "").upper()
    if observation is None:
        observation = get_tesla_vehicle_observation(
            hass, normalized, preferred_entity_id=entity_id)
    if (observation is None or observation.vin != normalized
            or observation.domain != domain or observation.config_entry_id != entry_id
            or observation.device_id != device_id):
        return None
    current = observation.requested_current
    at = observation.source_charge_timestamp
    charge = dict(observation.raw_charge_state)
    raw_current = charge.get("charge_current_request", charge.get("charger_current_request"))
    if (type(current) not in (int, float) or not math.isfinite(current)
            or not float(current).is_integer() or not 0 <= current <= 80
            or type(raw_current) not in (int, float)
            or type(charge.get("timestamp")) not in (int, float)
            or type(at) not in (int, float) or not math.isfinite(at)
            or not 0 <= time.time() - at <= 15
            or observation.charge_state.lower() not in {
                "charging", "starting", "stopped", "complete", "disconnected", "nopower", "no_power"}
            or any(type(charge.get(field)) not in (int, float)
                   or not math.isfinite(charge[field]) or charge[field] < 0
                   for field in ("charger_actual_current", "charger_power"))):
        return None
    return {
        "amps": int(current), "at": at, "charge": charge,
        "observation": observation,
        "source": source,
        "coordinator": next(item[3] for item in vehicle_sources(hass, normalized)
                            if id(item[3]) == source[7]),
    }


def native_charge_setting_current(hass, vin, entity_id, captured):
    """Keep a pending admission/reconciliation on the same native evidence."""
    if not isinstance(captured, dict):
        return False
    if (_native_charge_source(hass, vin, entity_id) != captured["source"]
            or not 0 <= time.time() - captured["at"] <= 15):
        return False
    current = native_charge_setting(hass, vin, entity_id)
    if current is None or current["at"] < captured["at"]:
        # A direct cloud return is intentionally not published into HA's
        # coordinator. Its exact response remains usable while the older
        # cached document catches up, within the original freshness window.
        data = getattr(captured.get("coordinator"), "data", {})
        if not isinstance(data, dict) or ("vin" in data and str(data["vin"]).upper() != str(vin).upper()):
            return False
        stamp = _section(data, "charge_state").get("timestamp")
        return (captured.get("explicit_read") is True
                and type(stamp) in (int, float) and math.isfinite(stamp)
                and _timestamp(stamp) < captured["at"])
    if current["source"] != captured["source"]:
        return False
    before, after = captured["observation"], current["observation"]
    return all(getattr(before, field) == getattr(after, field) for field in (
        "requested_current", "charge_state", "charger_actual_current", "charger_power"))


def ble_native_setting_target(hass, entry, vin, entity_id, params):
    """Use only the native observation producer pinned by this BLE attempt."""
    from .tesla_ev_recovery import pending_ble_attempt, _compatible

    config = {**entry.data, **entry.options}
    runtime = hass.data.get("power_sync", {}).get(entry.entry_id)
    marker = params.get("_tesla_ble_native_observation_target")
    if (config.get(CONF_EV_PROVIDER) != EV_PROVIDER_BOTH
            or not isinstance(runtime, dict)
            or runtime.get("_tesla_ev_recovery_sidecar_hold") is not None
            or not isinstance(marker, dict)):
        return None
    ticket = pending_ble_attempt(hass, entry, vin)
    if (ticket is None or ticket.runtime_ref is not runtime
            or ticket.attempt_id != marker.get("attempt_id")
            or ticket.vehicle_id != vin or ticket.route.get("kind") != "ble"
            or ticket.route.get("entity_id") != entity_id):
        return None
    fallback = ticket.route.get("fallback")
    if (not isinstance(fallback, dict) or fallback.get("kind") != "ha"
            or fallback.get("entity_id") != marker.get("entity_id")
            or fallback.get("entity_identity") != marker.get("identity")
            or not _compatible(hass, entry, runtime, vin, fallback)
            or _native_charge_source(hass, vin, fallback["entity_id"]) is None):
        return None
    return fallback["entity_id"], ticket.attempt_id, ticket.route


async def async_refresh_native_charge_setting(hass, entry, vin, entity_id, *, authorize,
                                             timeout=5, deadline=None):
    """Explicit admission/recovery read; routine telemetry never spends credits."""
    from .const import EV_PROVIDER_FLEET_API
    from .tesla_ev_observation import async_read_tesla_vehicle, async_read_independent_tesla_vehicle

    runtime = hass.data.get("power_sync", {}).get(entry.entry_id)
    source = _native_charge_source(hass, vin, entity_id)
    if source is None:
        return None
    end = time.monotonic() + timeout

    def current():
        config = {**entry.data, **entry.options}
        return (hass.data.get("power_sync", {}).get(entry.entry_id) is runtime
                and not runtime.get("powerwall_local_dispatch_stopping")
                and config.get(CONF_EV_PROVIDER, EV_PROVIDER_FLEET_API) in {EV_PROVIDER_FLEET_API, EV_PROVIDER_BOTH}
                and _native_charge_source(hass, vin, entity_id) == source
                and (deadline is None or type(deadline) in (int, float)
                     and math.isfinite(deadline) and time.time() < deadline)
                and authorize() is True and time.monotonic() < end)

    def remaining():
        value = end - time.monotonic()
        if deadline is not None:
            value = min(value, deadline - time.time())
        return max(0.001, value)

    if not isinstance(runtime, dict) or not current():
        return None
    observation = await async_read_tesla_vehicle(
        hass, vin, preferred_entity_id=entity_id, timeout=remaining())
    if not current():
        return None
    result = native_charge_setting(hass, vin, entity_id, observation=observation) if observation is not None else None
    if result is None:
        observation = await async_read_independent_tesla_vehicle(
            hass, vin, preferred_entity_id=entity_id, timeout=remaining(),
            allow_paid_refresh=source[0] == "teslemetry", authorize=current)
        if not current() or observation is None:
            return None
        result = native_charge_setting(hass, vin, entity_id, observation=observation)
    if result is not None:
        result["explicit_read"] = True
    return result


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
    reported_voltage = charge.get("charger_voltage")
    voltage = reported_voltage or profile.get("voltage")
    # A stopped Tesla can report a small standby voltage before the EVSE is
    # energized. Use the explicit configured electrical bound for prospective
    # demand only with a fresh, independently zero-effect charge document.
    # Invalid active readings must never gain measured power or headroom.
    if (type(reported_voltage) in (int, float) and 0 < reported_voltage < 90
            and "voltage" in profile and "phases" in profile
            and str(charge.get("charging_state", "")).lower() in {
                "stopped", "complete", "disconnected", "nopower", "no_power"}
            and all(type(charge.get(field)) in (int, float) and charge[field] == 0
                    for field in ("charger_actual_current", "charger_power"))
            and 0 <= time.time() - _timestamp(charge.get("timestamp")) <= 15):
        voltage = profile["voltage"]
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
