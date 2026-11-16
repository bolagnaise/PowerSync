"""Local, opt-in EV history acquisition; no provider reads or vehicle wakeups."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import logging

from .ev_history import EVHistoryPoint, RETENTION_DAYS, forecast_arrival, valid_soc

_LOGGER = logging.getLogger(__name__)
MAX_POINTS = 6000


@dataclass(frozen=True)
class EVHistorySources:
    soc: str
    home: str
    binary: bool = False

    @property
    def identity(self):
        return f"{self.soc}|{self.home}"


def presence(value, binary=False):
    value = str(value or "").lower()
    if value in {"unknown", "unavailable", "none", ""}:
        return None
    if binary:
        return True if value == "on" else False if value == "off" else None
    return value == "home"


def discover_sources(hass, entry, identifiers) -> dict[str, EVHistorySources]:
    """Bind SOC and presence to the same vehicle, never another site's meter."""
    from homeassistant.helpers import device_registry as dr, entity_registry as er
    from ..automations.ev_charging_planner import _iter_tesla_vehicle_devices
    from ..tesla_ble import get_tesla_ble_battery_state

    result = {}
    opts = {**entry.data, **entry.options}
    registry = er.async_get(hass)
    devices = list(_iter_tesla_vehicle_devices(dr.async_get(hass)))
    for identifier, vin in identifiers.items():
        vin = str(vin or identifier)
        if vin.startswith("ble_"):
            prefix = vin[4:]
            soc = get_tesla_ble_battery_state(hass, prefix)
            tracker = hass.states.get(f"device_tracker.{prefix}_location")
            # A sleeping car or offline bridge is not proof the car is away.
            if soc is not None and tracker is not None:
                result[identifier] = EVHistorySources(soc.entity_id, tracker.entity_id)
            continue
        if identifier in {"generic_ev", "_default", "ev"}:
            from ..automations.shared_generic_charger import shared_data, selected_profile
            shared = shared_data(hass, entry)
            if shared is not None:
                profile = selected_profile(shared)
                soc_id = str(profile.get("soc_entity") or "").strip() if profile else None
            else:
                soc_id = opts.get("generic_charger_soc_entity")
            registration = registry.entities.get(soc_id) if soc_id else None
            home_ids = [entity.entity_id for entity in registry.entities.values()
                        if registration and registration.device_id
                        and entity.device_id == registration.device_id
                        and entity.entity_id.startswith("device_tracker.")
                        and not getattr(entity, "disabled_by", None)]
            # Unplugged does not establish away. A generic car needs its own
            # presence source, bound to the same device as its selected SOC.
            if soc_id and len(home_ids) == 1:
                result[identifier] = EVHistorySources(soc_id, home_ids[0])
            continue
        candidates = []
        for device, device_vin in devices:
            if device_vin.upper() != vin.upper():
                continue
            entities = [e.entity_id for e in registry.entities.values()
                        if e.device_id == device.id and not getattr(e, "disabled_by", None)]
            soc_ids = [eid for eid in entities if eid.startswith("sensor.")
                       and any(key in eid for key in ("battery_level", "state_of_charge", "_soc"))]
            home_ids = [eid for eid in entities if
                        (eid.startswith("device_tracker.") and "location" in eid)
                        or (eid.startswith("binary_sensor.") and "located_at_home" in eid)]
            for soc_id in sorted(soc_ids):
                for home_id in sorted(home_ids):
                    soc_state, home_state = hass.states.get(soc_id), hass.states.get(home_id)
                    if soc_state is None or home_state is None:
                        continue
                    available = (valid_soc(soc_state.state) is not None
                                 and presence(home_state.state, home_id.startswith("binary_sensor.")) is not None)
                    candidates.append((not available, soc_id, home_id))
        if candidates:
            _, soc_id, home_id = min(candidates)
            result[identifier] = EVHistorySources(soc_id, home_id, home_id.startswith("binary_sensor."))
    return result


def normalize_history(raw, sources, now):
    """Join change events without inventing coverage during unavailable periods."""
    soc_states = sorted(raw.get(sources.soc, []), key=lambda s: s.last_changed)
    home_states = sorted(raw.get(sources.home, []), key=lambda s: s.last_changed)
    events = sorted({s.last_changed for s in [*soc_states, *home_states]
                     if s.last_changed.tzinfo is not None and s.last_changed < now})
    points = []
    soc_index = home_index = -1
    for stamp in events:
        while soc_index + 1 < len(soc_states) and soc_states[soc_index + 1].last_changed <= stamp:
            soc_index += 1
        while home_index + 1 < len(home_states) and home_states[home_index + 1].last_changed <= stamp:
            home_index += 1
        soc_state = soc_states[soc_index] if soc_index >= 0 else None
        home_state = home_states[home_index] if home_index >= 0 else None
        soc = valid_soc(soc_state.state) if soc_state else None
        if soc_state and (stamp - soc_state.last_changed).total_seconds() > 12 * 3600:
            soc = None
        home = presence(home_state.state, sources.binary) if home_state else None
        if home is True and home_state and home_state.last_changed == stamp:
            # Providers can publish the location and arrival SOC a few seconds
            # apart. Choose the nearest measured SOC, bounded to 30 minutes;
            # never interpret a stale work SOC as a measured arrival SOC.
            nearby = [s for s in soc_states
                      if abs((s.last_changed - stamp).total_seconds()) <= 1800
                      and s.last_changed < now and valid_soc(s.state) is not None]
            soc = valid_soc(min(nearby, key=lambda s: abs((s.last_changed - stamp).total_seconds())).state) if nearby else None
        points.append(EVHistoryPoint(stamp, soc, home))
    return points


class EVHistoryRecorder:
    """Retain bounded local summaries beyond Recorder's raw-history purge."""

    def __init__(self, hass, entry_id):
        from .coalescing_store import CoalescingStore
        self.hass = hass
        self.store = CoalescingStore(hass, 1, f"power_sync_ev_history_{entry_id}")
        self.loaded = False
        self.points = {}
        self.refreshed = {}
        self.status = {}

    async def forecasts(self, sources_by_vehicle, now):
        if not self.loaded:
            data = await self.store.async_load() or {}
            self.points = data.get("sources", {}) if isinstance(data, dict) else {}
            if not isinstance(self.points, dict):
                self.points = {}
            self.loaded = True
        result = {}
        self.status = {}
        cutoff = (now - timedelta(days=RETENTION_DAYS)).timestamp()
        active_sources = {source.identity for source in sources_by_vehicle.values()}
        # Source reassignment cannot keep another vehicle's learned records.
        self.points = {key: rows for key, rows in self.points.items() if key in active_sources}
        for vehicle_id, source in sources_by_vehicle.items():
            key = source.identity
            rows = self.points.get(key, [])
            if not isinstance(rows, list):
                rows = []
            rows = [r for r in rows if isinstance(r, list) and len(r) == 3
                    and isinstance(r[0], (int, float)) and cutoff <= r[0] <= now.timestamp()
                    and (r[2] is None or isinstance(r[2], bool))]
            if now.timestamp() - self.refreshed.get(key, 0) >= 3600:
                try:
                    from homeassistant.components.recorder import get_instance
                    from homeassistant.components.recorder.history import get_significant_states
                    raw = await get_instance(self.hass).async_add_executor_job(
                        get_significant_states, self.hass,
                        now - timedelta(days=30), now, [source.soc, source.home],
                    )
                    normalized = await self.hass.async_add_executor_job(
                        normalize_history, raw, source, now,
                    )
                    rows.extend([[p.timestamp.timestamp(), p.soc, p.home] for p in normalized])
                except Exception as err:
                    _LOGGER.debug("EV history backfill unavailable: %s", err)
                self.refreshed[key] = now.timestamp()
            soc_state = self.hass.states.get(source.soc)
            home_state = self.hass.states.get(source.home)
            soc = valid_soc(soc_state.state) if soc_state else None
            home = presence(home_state.state, source.binary) if home_state else None
            # last_updated reflects measured updates rather than a SOC change;
            # sleeping vehicles can retain a SOC for the stationary workday.
            updated = getattr(soc_state, "last_updated", None)
            if updated is None or abs(now.timestamp() - updated.timestamp()) > 12 * 3600:
                soc = None
            rows = sorted({r[0]: r for r in rows}.values())
            latest = rows[-1] if rows else None
            if (latest is None or now.timestamp() - latest[0] >= 1800
                    or latest[1:] != [soc, home]):
                rows.append([now.timestamp(), soc, home])
            self.points[key] = rows[-MAX_POINTS:]
            points = [EVHistoryPoint(datetime.fromtimestamp(r[0], timezone.utc),
                                     valid_soc(r[1]), r[2]) for r in self.points[key]]
            forecast = await self.hass.async_add_executor_job(
                lambda: forecast_arrival(points, now=now, current_soc=soc, home=home),
            )
            self.status[vehicle_id] = (forecast.to_dict() if forecast else {
                "source": "live" if home is True else "learning",
                "reason": "Vehicle is home; actual SOC is authoritative" if home is True else
                "Need two comparable weekday trips with known SOC and home presence",
            })
            if forecast:
                result[vehicle_id] = forecast
        self.store.async_delay_save(lambda: {"sources": self.points}, 60)
        return result
