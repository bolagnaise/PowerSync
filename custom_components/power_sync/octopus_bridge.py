"""Read-only, meter-bound bridge to BottlecapDave's public Octopus interfaces.

Private coordinator access is confined here for startup hydration and original
tariff provenance. Public entities/events use GBP; private results use pence.
No device or Octopus account actions are issued by this module.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import re
from datetime import datetime, timedelta, timezone
from typing import Any

_LOGGER = logging.getLogger(__name__)
RATE_EVENTS = tuple(f"octopus_energy_electricity_{day}_day_rates" for day in ("previous", "current", "next"))
PROGRAM_EVENTS = tuple(
    f"octopus_energy_{kind}_octoplus_{program}{suffix}"
    for kind in ("new", "all")
    for program, suffix in (("saving_session", "s" if kind == "all" else ""),
                            ("power_down_session", "s" if kind == "all" else ""),
                            ("free_electricity_session", "s" if kind == "all" else ""),
                            ("power_up_session", "s" if kind == "all" else ""))
)
# The legacy saving-session all-event is singular in released upstream versions.
PROGRAM_EVENTS += ("octopus_energy_all_octoplus_saving_sessions", "octopus_energy_all_octoplus_saving_session")
IMPORT_METER = "octopus_import_meter"
EXPORT_METER = "octopus_export_meter"


class OctopusSourceError(ValueError):
    """An installed upstream source cannot safely identify usable prices."""


def parse_time(value):
    try:
        value = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
        if not isinstance(value, datetime) or value.tzinfo is None:
            return None
        return value.astimezone(timezone.utc)
    except ValueError:
        return None


def states(hass, domain=None):
    source = getattr(hass, "states", None)
    if hasattr(source, "async_all"):
        return list(source.async_all(domain))
    if isinstance(source, dict):
        return [s for key, s in source.items() if domain is None or key.startswith(domain + ".")]
    return []


def canonical_id(hass, state):
    """Resolve a renamed entity through its stable upstream unique ID."""
    try:
        from homeassistant.helpers import entity_registry as er
        entry = er.async_get(hass).async_get(state.entity_id)
        if entry and entry.platform == "octopus_energy":
            return str(entry.unique_id).lower()
    except (ImportError, AttributeError, KeyError):
        pass
    return state.entity_id.split(".", 1)[-1].lower()


def _identity(hass, state):
    name = canonical_id(hass, state).replace("_export_current_rate", "_current_rate")
    match = re.fullmatch(r"octopus_energy_electricity_(.+)_([^_]+)_current_rate", name)
    return (match[2], match[1]) if match else None


def discover_meters(hass, *, now=None):
    """Return meter records, not a last-account-wins aggregate."""
    result = []
    now = now or datetime.now(timezone.utc)
    accounts = hass.data.get("octopus_energy", {})
    for account_id, data in accounts.items() if isinstance(accounts, dict) else ():
        if not isinstance(data, dict):
            continue
        account = getattr(data.get("ACCOUNT"), "account", None)
        if not isinstance(account, dict):
            continue
        for point in account.get("electricity_meter_points", []):
            for meter in point.get("meters", []):
                active_from, active_to = parse_time(meter.get("active_from")), parse_time(meter.get("active_to"))
                if (active_from and active_from > now) or (active_to and active_to <= now):
                    continue
                mpan, serial = str(point.get("mpan", "")), str(meter.get("serial_number", ""))
                if not mpan or not serial:
                    continue
                rates = data.get(f"ELECTRICITY_RATES_{mpan}_{serial}")
                result.append({"key": f"{account_id}|{mpan}|{serial}", "account_id": str(account_id),
                               "mpan": mpan, "serial": serial, "is_export": bool(meter.get("is_export", point.get("is_export", False))),
                               "agreements": point.get("agreements", []), "result": rates,
                               "property_id": point.get("property_id"), "account": account})
    for state in states(hass, "sensor"):
        if state.state in ("unknown", "unavailable", ""):
            continue
        identity = _identity(hass, state)
        is_export = bool((state.attributes or {}).get("is_export")) or "_export_" in canonical_id(hass, state)
        if not identity or any((m["mpan"].lower(), m["serial"].lower()) == identity and m["is_export"] == is_export for m in result):
            continue
        mpan, serial = identity
        attrs = state.attributes or {}
        result.append({"key": f"|{mpan}|{serial}", "account_id": "", "mpan": mpan, "serial": serial,
                       "is_export": is_export,
                       "agreements": [], "result": None, "property_id": None, "account": {}})
    return result


def meter_options(hass, is_export=False):
    return [{"value": m["key"], "label": f"{m['account_id']} · {m['mpan']} · {m['serial']}"}
            for m in discover_meters(hass) if m["is_export"] == is_export]


def _agreement(meter, now):
    agreements = []
    for agreement in meter["agreements"]:
        start = agreement.get("start") or agreement.get("valid_from")
        end = agreement.get("end") or agreement.get("valid_to")
        if start and (parse_time(start) is None or parse_time(start) > now):
            continue
        if end and (parse_time(end) is None or parse_time(end) <= now):
            continue
        agreements.append(agreement)
    return max(agreements, key=lambda a: parse_time(a.get("start") or a.get("valid_from")) or datetime.min.replace(tzinfo=timezone.utc), default={}).get("tariff_code")


def _entry(start, end, value, export, now, *, pounds=False, **metadata):
    start, end = parse_time(start), parse_time(end)
    try:
        price = float(value) * (100 if pounds else 1)
    except (ValueError, TypeError):
        return None
    if start is None or end is None or start >= end or not math.isfinite(price):
        return None
    return {"valid_from": start.isoformat(), "valid_to": end.isoformat(), "nemTime": end.isoformat(),
            "duration": max(1, math.ceil((end - start).total_seconds() / 60)),
            "type": "CurrentInterval" if start <= now < end else "ActualInterval" if end <= now else "ForecastInterval",
            "perKwh": round(-price if export else price, 8), "channelType": "feedIn" if export else "general",
            **metadata}


def _overlay(rows, live):
    """Replace only covered time; retain the remainder of block tariffs."""
    start, end = parse_time(live["valid_from"]), parse_time(live["valid_to"])
    merged = []
    for row in rows:
        left, right = parse_time(row["valid_from"]), parse_time(row["valid_to"])
        if right <= start or left >= end:
            merged.append(row)
        else:
            for a, b in ((left, min(right, start)), (max(left, end), right)):
                if a < b:
                    merged.append({**row, "valid_from": a.isoformat(), "valid_to": b.isoformat(),
                                   "nemTime": b.isoformat(), "duration": max(1, math.ceil((b-a).total_seconds()/60))})
    return merged + [live]


def _meter_rows(hass, meter, now, event_rates):
    export, result = meter["is_export"], meter["result"]
    rows, originals = [], []
    tariff = _agreement(meter, now)
    for collection, target in ((getattr(result, "rates", None) or getattr(result, "original_rates", None) or [], rows),
                               (getattr(result, "original_rates", None) or [], originals)):
        for rate in collection:
            row = _entry(rate.get("start") or rate.get("valid_from"), rate.get("end") or rate.get("valid_to"),
                         rate.get("value_inc_vat"), export, now, source="octopus_energy_internal",
                         is_intelligent_adjusted=bool(rate.get("is_intelligent_adjusted")), is_capped=bool(rate.get("is_capped")))
            if row:
                target.append(row)
    matching = []
    for state in states(hass):
        name = canonical_id(hass, state)
        prefix = f"octopus_energy_electricity_{meter['serial']}_{meter['mpan']}_".lower()
        # Historical export entity IDs can include a direction suffix.
        if not name.startswith(prefix):
            continue
        attrs = state.attributes or {}
        if (bool(attrs.get("is_export")) or "_export_" in name) != export:
            continue
        if name.endswith(("_current_day_rates", "_next_day_rates", "_previous_day_rates")):
            matching.append((state, attrs.get("rates", [])))
        elif name.endswith("_current_rate") and state.state not in ("unknown", "unavailable", ""):
            matching.append((state, [{**attrs, "value_inc_vat": state.state}]))
    # Current sensors usually follow daily events; use their publication time,
    # then prefer the current sensor for otherwise equivalent public snapshots.
    matching.sort(key=lambda item: (parse_time(getattr(item[0], "last_updated", None)) or datetime.min.replace(tzinfo=timezone.utc),
                                    canonical_id(hass, item[0]).endswith("_current_rate")))
    for state, rates in matching:
        adjusted = parse_time(getattr(result, "rates_last_adjusted", None))
        updated = parse_time(getattr(state, "last_updated", None))
        if adjusted and updated and updated < adjusted:
            continue
        attrs = state.attributes or {}
        tariff = attrs.get("tariff_code") or attrs.get("tariff") or tariff
        for rate in rates if isinstance(rates, list) else ():
            row = _entry(rate.get("start"), rate.get("end"), rate.get("value_inc_vat"), export, now, pounds=True,
                         source="octopus_energy_entities", is_intelligent_adjusted=bool(rate.get("is_intelligent_adjusted", attrs.get("is_intelligent_adjusted"))),
                         is_capped=bool(rate.get("is_capped", attrs.get("is_capped"))))
            if row:
                rows = _overlay(rows, row)
    for event in event_rates.values():
        data, received = event
        if str(data.get("mpan", "")).lower() != meter["mpan"].lower() or str(data.get("serial_number", "")).lower() != meter["serial"].lower():
            continue
        latest_public = max((parse_time(getattr(s, "last_updated", None)) or datetime.min.replace(tzinfo=timezone.utc)
                             for s, _ in matching), default=datetime.min.replace(tzinfo=timezone.utc))
        if received < latest_public or (parse_time(getattr(result, "rates_last_adjusted", None)) or received) > received:
            continue
        tariff = data.get("tariff_code") or tariff
        for rate in data.get("rates", []):
            row = _entry(rate.get("start"), rate.get("end"), rate.get("value_inc_vat"), export, now, pounds=True,
                         source="octopus_energy_event", is_intelligent_adjusted=bool(rate.get("is_intelligent_adjusted")),
                         is_capped=bool(rate.get("is_capped")))
            if row:
                rows = _overlay(rows, row)
    # Reclassify fragments after overlay, including the past part of a live block.
    for row in rows:
        a, b = parse_time(row["valid_from"]), parse_time(row["valid_to"])
        row["type"] = "CurrentInterval" if a <= now < b else "ActualInterval" if b <= now else "ForecastInterval"
    return sorted(rows, key=lambda r: r["valid_from"]), originals, tariff


def read_snapshot(hass, options=None, *, now=None, event_rates=None):
    """Return one selected-account Amber-compatible snapshot, or no upstream."""
    now, options = now or datetime.now(timezone.utc), options or {}
    meters = discover_meters(hass, now=now)
    if not meters:
        if options.get(IMPORT_METER):
            raise OctopusSourceError("Selected Octopus meter unavailable; waiting for its integration")
        return None
    imports = [m for m in meters if not m["is_export"]]
    selected = options.get(IMPORT_METER)
    imports = [m for m in imports if m["key"] == selected] if selected else imports
    if len(imports) != 1:
        raise OctopusSourceError("Select one Octopus import meter in PowerSync's Octopus settings")
    imp = imports[0]
    exports = [m for m in meters if m["is_export"] and m["account_id"] == imp["account_id"]
               and (not imp["property_id"] or m["property_id"] == imp["property_id"])]
    selected_export = options.get(EXPORT_METER)
    if selected_export:
        exports = [m for m in exports if m["key"] == selected_export]
        if len(exports) != 1:
            raise OctopusSourceError("Selected Octopus export meter is unavailable or belongs to another account/site")
    if not selected_export and exports and len(imp["account"].get("property_ids", [])) > 1:
        raise OctopusSourceError("Select an Octopus export meter for this property")
    if len(exports) > 1:
        raise OctopusSourceError("Select one Octopus export meter in PowerSync's Octopus settings")
    rows, originals, tariff = _meter_rows(hass, imp, now, event_rates or {})
    if not any(r["type"] == "CurrentInterval" for r in rows):
        raise OctopusSourceError("Octopus source has no valid current import rate")
    export_rows = _meter_rows(hass, exports[0], now, event_rates or {})[0] if exports else []
    export_known = any(r["type"] == "CurrentInterval" for r in export_rows)
    if not export_rows:
        # Zero is conservative for optimization; it is explicitly an unknown
        # estimate, never an invented SEG payment or measured export tariff.
        export_rows = [{**r, "channelType": "feedIn", "perKwh": 0.0, "source": "unknown_export",
                        "price_available": False} for r in rows]
    binding = {k: imp[k] for k in ("key", "account_id", "mpan", "serial")}
    binding["export_key"] = exports[0]["key"] if exports else None
    all_rows = rows + export_rows
    context = account_context(hass, binding, now=now)
    material = [(r["channelType"], r["valid_from"], r["valid_to"], r["perKwh"], r["type"],
                 r.get("is_intelligent_adjusted"), r.get("is_capped")) for r in all_rows]
    revision = hashlib.sha256(json.dumps([binding, tariff, material, context["programs"], context["dispatches"]], sort_keys=True, default=str).encode()).hexdigest()[:20]
    source = "octopus_energy_integration" if any(r["source"] == "octopus_energy_internal" for r in rows) else "octopus_energy_entities"
    return {"current": [r for r in all_rows if r["type"] == "CurrentInterval"], "forecast": all_rows,
            "export_rates": export_rows, "last_update": now, "source": source, "tariff_code": tariff,
            "product_code": "-".join(tariff.split("-")[2:-1]) if tariff else None, "revision": revision,
            "octopus": {"binding": binding, "health": "ready", "export_status": "known" if export_known else "unknown",
                        "original_rates": originals, "effective_rates": rows, "currency": "GBP",
                        "rate_unit": "p/kWh", "received_at": now.isoformat(),
                        "upstream_retrieved_at": str(getattr(imp["result"], "last_retrieved", "")),
                        "upstream_adjusted_at": str(getattr(imp["result"], "rates_last_adjusted", "")),
                        "external_battery_control": "INTELLI-FLUX" in (tariff or "").upper(),
                        **context}}


def related_entities(hass, binding):
    """Bound read-only diagnostics; never use delayed meter data as live power."""
    account = binding.get("account_id", "").lower().replace("-", "_")
    meter = binding.get("serial", "").lower()
    result = []
    all_states = states(hass)
    registry, peer_entries = None, set()
    try:
        from homeassistant.helpers import entity_registry as er
        registry = er.async_get(hass)
        for state in all_states:
            if _identity(hass, state) == (binding["mpan"].lower(), meter):
                entry = registry.async_get(state.entity_id)
                if entry:
                    peer_entries.add(entry.config_entry_id)
    except (ImportError, AttributeError, KeyError):
        pass
    for state in all_states:
        name = canonical_id(hass, state)
        if not name.startswith("octopus_energy_"):
            continue
        # Registry associations include Intelligent devices and gas meters
        # without relying on a user's entity name.
        belongs = bool(meter and f"_{meter}_" in name) or bool(account and name.replace("-", "_").startswith(f"octopus_energy_{account}_"))
        if registry is not None:
            current = registry.async_get(state.entity_id)
            belongs |= bool(current and current.config_entry_id in peer_entries)
        if belongs:
            result.append({"entity_id": state.entity_id, "state": state.state,
                           "unit": (state.attributes or {}).get("unit_of_measurement"),
                           "updated_at": str(getattr(state, "last_updated", ""))})
    return result


def account_context(hass, binding, *, now=None):
    """Expose dispatch/program/usage evidence without inventing bill credits."""
    now = now or datetime.now(timezone.utc)
    related = related_entities(hass, binding)
    identifiers = {r["entity_id"] for r in related}
    programs, dispatches = {}, []
    for state in sorted(states(hass), key=lambda s: "power_down" in canonical_id(hass, s) or "power_up" in canonical_id(hass, s)):
        if state.entity_id not in identifiers:
            continue
        name, attrs = canonical_id(hass, state), state.attributes or {}
        if "intelligent_dispatching" in name:
            dispatches.append({"entity_id": state.entity_id, "state": state.state,
                               "provider": attrs.get("provider"), "manual_refresh": attrs.get("manually_refresh_dispatches"),
                               **{key: [{k: str(v) if isinstance(v, datetime) else v for k, v in item.items()
                                        if k in ("start", "end", "source", "location", "charge_in_kwh")}
                                        for item in attrs.get(key, [])[:100] if isinstance(item, dict)]
                                  for key in ("planned_dispatches", "started_dispatches", "completed_dispatches")}})
        kind = "saving" if any(x in name for x in ("power_down", "saving_session")) else "free_electricity" if any(x in name for x in ("power_up", "free_electricity")) else None
        if kind:
            for membership, key in (("available", "available_events"), ("joined", "joined_events")):
                for event in attrs.get(key, []) or []:
                    start, end = parse_time(event.get("start")), parse_time(event.get("end"))
                    if not start or not end or start >= end:
                        continue
                    code = str(event.get("code") or event.get("id") or start.isoformat())
                    programs[(kind, code)] = {"code": code, "program": kind, "membership": membership,
                                             "start": start.isoformat(), "end": end.isoformat(),
                                             "octopoints_per_kwh": event.get("octopoints_per_kwh"),
                                             "entity_id": state.entity_id,
                                             "active": start <= now < end,
                                             "retail_rate_unchanged": kind == "free_electricity"}
    return {"programs": list(programs.values()), "dispatches": dispatches,
            "related_entities": related,
            "cost_basis": "Upstream estimates and reported rewards; account billing may differ"}


class OctopusUpdateBridge:
    """Coalesce upstream changes and known boundaries; drain on unload."""

    def __init__(self, hass, coordinator, options, on_change, prepare_programs=None):
        self.hass, self.coordinator, self.options, self.on_change = hass, coordinator, options, on_change
        self.event_rates = {}
        self.prepare_programs = prepare_programs
        self._unsub = []
        self._timer = None
        self._task = None
        self._pending = False
        self._closed = False

    def start(self):
        self._unsub.extend(self.hass.bus.async_listen(event, self._event) for event in RATE_EVENTS + PROGRAM_EVENTS)
        self._unsub.append(self.hass.bus.async_listen("state_changed", self._state))
        self._unsub.append(self.hass.bus.async_listen("homeassistant_started", lambda _: self.request()))
        self.request()

    def _event(self, event):
        data = event.data or {}
        if event.event_type in RATE_EVENTS and isinstance(data.get("rates"), list):
            self.event_rates[(event.event_type, str(data.get("mpan")), str(data.get("serial_number")))] = (data, datetime.now(timezone.utc))
        self.request()

    def _state(self, event):
        data = event.data or {}
        state = data.get("new_state") or data.get("old_state")
        if state is not None and canonical_id(self.hass, state).startswith("octopus_energy_"):
            self.request()

    def request(self):
        if self._closed:
            return
        self._pending = True
        if self._task is None or self._task.done():
            self._task = self.hass.async_create_task(self._run(), "powersync_octopus_updates")

    async def _run(self):
        try:
            while self._pending and not self._closed:
                self._pending = False
                await asyncio.sleep(0.1)
                if self._closed:
                    return
                try:
                    data = read_snapshot(self.hass, self.options, event_rates=self.event_rates)
                except OctopusSourceError as err:
                    from homeassistant.helpers.update_coordinator import UpdateFailed
                    self.coordinator.async_set_update_error(UpdateFailed(str(err)))
                    _LOGGER.warning("Octopus source unavailable: %s", err)
                    continue
                if data is None:
                    continue  # Existing REST coordinator remains the fallback.
                previous = (self.coordinator.data or {}).get("revision")
                if self.prepare_programs and (self.coordinator.data or {}).get("octopus", {}).get("programs") != data["octopus"]["programs"]:
                    await self.prepare_programs()
                if self._closed:
                    return
                for key in ("tariff_code", "product_code"):
                    if data.get(key):
                        setattr(self.coordinator, key, data[key])
                self.coordinator.async_set_updated_data(data)
                self._schedule_boundary(data)
                if previous != data["revision"] and getattr(self.hass, "is_running", False):
                    await self.on_change()
        except asyncio.CancelledError:
            raise
        except Exception:
            _LOGGER.exception("Octopus update bridge failed; periodic refresh remains available")

    def _schedule_boundary(self, data):
        if self._timer:
            self._timer()
        from homeassistant.helpers.event import async_track_point_in_utc_time
        now = datetime.now(timezone.utc)
        times = [parse_time(r[k]) for r in data["forecast"] for k in ("valid_from", "valid_to")]
        times += [parse_time(p[k]) for p in data.get("octopus", {}).get("programs", []) for k in ("start", "end")]
        times += [parse_time(p.get(k)) for d in data.get("octopus", {}).get("dispatches", [])
                  for p in d.get("planned_dispatches", []) + d.get("started_dispatches", []) for k in ("start", "end")]
        boundary = min((t for t in times if t and t > now), default=now + timedelta(minutes=5))
        self._timer = async_track_point_in_utc_time(self.hass, lambda _: self.request(), min(boundary, now + timedelta(minutes=5)))

    async def close(self):
        self._closed = True
        self._pending = False
        for unsub in self._unsub:
            unsub()
        self._unsub.clear()
        if self._timer:
            self._timer()
            self._timer = None
        if self._task and not self._task.done():
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
