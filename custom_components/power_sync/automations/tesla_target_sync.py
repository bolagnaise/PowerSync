"""Synchronize an enabled Smart Schedule target with its own Tesla's charge limit."""
from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, replace
from datetime import timedelta
import logging
import math
import re
import time

from ..const import (
    CONF_EV_CHARGING_ENABLED, CONF_EV_PROVIDER, CONF_MONITORING_MODE,
    EV_PROVIDER_BOTH, EV_PROVIDER_TESLA_BLE, TESLA_BLE_SENSOR_CHARGE_LIMIT,
)
from ..tesla_ble import get_tesla_ble_observed_at
from ..tesla_ble_mapping import resolve_ble_prefixes
from ..tesla_ev_observation import MAX_SOURCE_AGE_SECONDS, get_tesla_vehicle_observation

_LOGGER = logging.getLogger(__name__)


def tesla_profile(vehicle_id):
    return bool(re.fullmatch(r"[A-HJ-NPR-Z0-9]{17}|ble_[a-z0-9_]+", str(vehicle_id)))


def valid_target(value):
    return type(value) in (int, float) and 50 <= value <= 100 and int(value) == value


@dataclass(frozen=True)
class TargetLink:
    # baseline is the last distinct vehicle limit accepted from its own telemetry.
    baseline: int | None = None
    observed_at: float = 0
    edited_at: float = 0
    sent_at: float = 0
    attempts: int = 0
    superseded: tuple[int, ...] = ()
    superseded_until: float = 0

    @classmethod
    def restore(cls, raw):
        if not isinstance(raw, dict):
            return cls()
        try:
            link = cls(**{**raw, "superseded": tuple(raw.get("superseded", ()))})
            if (link.baseline is None or valid_target(link.baseline)) and all(
                type(t) in (int, float) and math.isfinite(t) and t >= 0
                for t in (link.observed_at, link.edited_at, link.sent_at, link.superseded_until)
            ) and type(link.attempts) is int and link.attempts >= 0 and all(valid_target(t) for t in link.superseded):
                return link
        except (TypeError, ValueError):
            pass
        return cls()

    def app_edit(self, previous_target, now):
        superseded = self.superseded if now < self.superseded_until else ()
        if self.sent_at and valid_target(previous_target) and now - self.sent_at < 900:
            superseded = tuple(dict.fromkeys((*superseded, int(previous_target))))
        return replace(self, edited_at=now, sent_at=0, attempts=0, superseded=superseded,
                       superseded_until=now + 900 if superseded else 0)


def reconcile(target, link, observed, observed_at, now):
    """Return a schedule target, durable link state, and optional Tesla write."""
    if not valid_target(target) or not valid_target(observed):
        return target, link, False
    if not 0 <= now - observed_at <= MAX_SOURCE_AGE_SECONDS or observed_at < link.observed_at:
        return target, link, False
    if link.baseline is None:
        if not link.edited_at:
            target = min(target, observed)
        link = replace(link, baseline=observed, observed_at=observed_at,
                       edited_at=link.edited_at or (now if target != observed else 0))
    if observed == target:
        # A pre-edit sample or acknowledgement cannot settle a later write.
        if link.edited_at and observed_at <= max(link.edited_at, link.sent_at):
            return target, link, False
        if link.edited_at or link.baseline != observed:
            link = TargetLink(baseline=observed, observed_at=observed_at,
                              superseded=link.superseded, superseded_until=link.superseded_until)
        return target, link, False
    if observed in link.superseded and now < link.superseded_until and observed_at > link.observed_at:
        # A submitted older request can reach the vehicle after a newer ACK.
        # Consume the specific outstanding echo once and reconcile the latest edit.
        remaining = tuple(value for value in link.superseded if value != observed)
        link = replace(link, baseline=observed, observed_at=observed_at,
                       edited_at=link.edited_at or now, superseded=remaining)
    if observed != link.baseline and observed_at > max(link.observed_at, link.edited_at):
        # An echo from an older app command must not replace its successor.
        if link.sent_at and now - link.sent_at < 900:
            # A newer car edit wins. Keep the unresolved app write identifiable
            # if it reaches the car afterwards.
            pending = tuple(dict.fromkeys((*link.superseded, target)))
            return observed, TargetLink(baseline=observed, observed_at=observed_at,
                                        superseded=pending, superseded_until=now + 900), False
        return observed, TargetLink(baseline=observed, observed_at=observed_at,
                                    superseded=link.superseded, superseded_until=link.superseded_until), False
    if not link.edited_at:
        return target, link, False
    delay = min(900, 60 * 2 ** min(4, max(0, link.attempts - 1)))
    if observed_at <= link.sent_at or (link.sent_at and now - link.sent_at < delay):
        return target, link, False
    return target, link, True


class TeslaTargetSync:
    def __init__(self, hass, entry, executor, store):
        self.hass, self.entry, self.executor, self.store = hass, entry, executor, store
        self.tasks = {}
        self.dirty = set()
        self.listeners = {}
        self.status = {}
        self.declined = {}
        self.closed = False
        self.seen = {}
        self.events = {}
        self.sources = {}

    def supports(self, vin):
        if not tesla_profile(vin):
            return False
        if not vin.startswith("ble_"):
            return True
        config = {**self.entry.data, **self.entry.options}
        return (config.get(CONF_EV_PROVIDER) in (EV_PROVIDER_TESLA_BLE, EV_PROVIDER_BOTH)
                and vin[4:] in resolve_ble_prefixes(self.hass, config))

    def start(self):
        from homeassistant.helpers.event import async_track_time_interval
        async def check_bound_vehicles(_now):
            self.refresh()
        self.cancel_timer = async_track_time_interval(self.hass, check_bound_vehicles, timedelta(seconds=30))
        self.entry.async_on_unload(self.close)
        self.refresh()

    def close(self):
        self.closed = True
        self.cancel_timer()
        for _, cancel in self.listeners.values():
            cancel()
        self.listeners.clear()
        for task in self.tasks.values():
            task.cancel()

    def refresh(self, *_):
        for vin, profile in list(self.executor._settings.items()):
            if profile.enabled and self.supports(vin):
                self.request(vin)

    def prepare_edit(self, vin, existing, candidate, edits):
        if not self.supports(vin):
            return
        if not candidate.enabled or not existing or not existing.enabled:
            candidate._tesla_target_link = {}
        if candidate.enabled and "target_soc" in edits and (not existing or existing.target_soc != candidate.target_soc):
            if not valid_target(candidate.target_soc):
                raise ValueError("Tesla charge limits must be whole percentages from 50 to 100.")
            link = TargetLink.restore(candidate._tesla_target_link)
            previous = existing.target_soc if existing else None
            candidate._tesla_target_link = asdict(link.app_edit(previous, time.time()))

    def request(self, vin):
        if self.closed or not self.supports(vin):
            return
        self.dirty.add(vin)
        if vin not in self.tasks:
            self.tasks[vin] = self.hass.async_create_task(self._run(vin))

    async def _run(self, vin):
        try:
            while vin in self.dirty and not self.closed:
                self.dirty.discard(vin)
                await self._tick(vin)
        except Exception:
            _LOGGER.exception("Could not synchronize Tesla Smart Schedule target")
        finally:
            self.tasks.pop(vin, None)

    def write_block(self):
        config = {**self.entry.data, **self.entry.options}
        if config.get(CONF_MONITORING_MODE):
            return "Monitoring Mode prevents a Tesla charge-limit write."
        if config.get(CONF_EV_CHARGING_ENABLED) is False:
            return "Tesla EV charging control is disabled."
        return None

    def get_status(self, vin):
        profile = self.executor._settings.get(vin)
        if not (profile and profile.enabled and self.supports(vin)):
            return None
        previous = self.status.get(vin)
        if previous and previous["status"] == "synced" and previous.get("observed_limit") != profile.target_soc:
            return {"status": "pending", "reason": "Waiting for Tesla to confirm the updated limit."}
        return previous or {"status": "waiting_for_vehicle", "reason": "Waiting for a fresh Tesla charge limit."}

    def _report(self, vin, status, reason=None, observed=None, *, changed=False):
        message = {"status": status}
        if reason:
            message["reason"] = reason
        if observed is not None:
            message["observed_limit"] = observed
        if self.status.get(vin) != message or changed:
            self.status[vin] = message
            self.hass.bus.async_fire("power_sync_ev_target_updated", {
                "entry_id": self.entry.entry_id, "vehicle_id": vin,
                "target_soc": self.executor._settings[vin].target_soc, "status": status,
            })

    async def _sample(self, vin):
        from homeassistant.helpers.event import async_track_state_change_event
        from .actions import _get_tesla_ev_entity, _resolve_ble_prefix_for_vehicle

        native_entity = ble_entity = None
        config = {**self.entry.data, **self.entry.options}
        if not vin.startswith("ble_"):
            native_entity = await _get_tesla_ev_entity(
                self.hass, r"(?:number|sensor)\..*_charge_limit(?:_soc)?(?:_\d+)?$",
                vin, config_entry=self.entry, warn_on_missing=False,
            )
        if vin.startswith("ble_") or config.get(CONF_EV_PROVIDER) == EV_PROVIDER_BOTH:
            prefix = _resolve_ble_prefix_for_vehicle(self.hass, self.entry, vin)
            if prefix:
                ble_entity = TESLA_BLE_SENSOR_CHARGE_LIMIT.format(prefix=prefix)
        self.sources[vin] = native_entity, ble_entity
        entities = tuple(entity for entity in (native_entity, ble_entity) if entity)
        previous = self.listeners.get(vin)
        if not self.closed and (previous is None or previous[0] != entities):
            if previous:
                previous[1]()
                self.listeners.pop(vin)
            if entities:
                async def state_changed(_event):
                    self.events[vin] = self.events.get(vin, 0) + 1
                    self.request(vin)
                self.listeners[vin] = (entities, async_track_state_change_event(self.hass, entities, state_changed))
        return self._read(vin)

    def _read(self, vin):
        native_entity, ble_entity = self.sources.get(vin, (None, None))
        samples = []
        if not vin.startswith("ble_"):
            native = get_tesla_vehicle_observation(self.hass, vin, preferred_entity_id=native_entity)
            if native and native.vin == vin and valid_target(native.charge_limit_soc):
                samples.append((native.source_charge_timestamp, int(native.charge_limit_soc)))
        if ble_entity:
            sample = self.hass.states.get(ble_entity)
            observed_at = get_tesla_ble_observed_at(self.hass, sample)
            try:
                value = float(sample.state)
            except (AttributeError, TypeError, ValueError):
                value = None
            if observed_at and valid_target(value):
                samples.append((observed_at.timestamp(), int(value)))
        now = time.time()
        return max((s for s in samples if 0 <= now-s[0] <= MAX_SOURCE_AGE_SECONDS),
                   key=lambda s: (s[0], -s[1]), default=None)

    async def _tick(self, vin):
        from .actions import _action_set_ev_charge_limit
        from .. import _update_powerwall_store

        profile = self.executor._settings.get(vin)
        if not profile or not profile.enabled:
            listener = self.listeners.pop(vin, None)
            if listener:
                listener[1]()
            self.status.pop(vin, None)
            self.declined.pop(vin, None)
            self.seen.pop(vin, None)
            return
        event = self.events.get(vin, 0)
        sample = await self._sample(vin)
        async with self.executor._settings_write_lock:
            if self.closed or not profile.enabled or self.executor._settings.get(vin) is not profile:
                return
            if not valid_target(profile.target_soc):
                self._report(vin, "unsupported_target", "Tesla supports charge limits from 50% to 100%.")
                return
            if sample is None:
                self._report(vin, "waiting_for_vehicle", "Waiting for a fresh Tesla charge limit.")
                return
            at, observed = sample
            now = time.time()
            last = self.seen.get(vin)
            if last and (at < last[0] or (at == last[0] and observed != last[1])):
                return
            self.seen[vin] = sample
            old = TargetLink.restore(profile._tesla_target_link)
            if at < old.observed_at:
                return
            target, link, send = reconcile(profile.target_soc, old, observed, at, now)
            blocked = self.write_block()
            if send and not blocked:
                link = replace(link, sent_at=now, attempts=link.attempts+1)
            changed = target != profile.target_soc
            if link != old or changed:
                journal = asdict(link)
                def patch(data):
                    profiles = dict(data.get("auto_schedule_settings") or {})
                    profiles[vin] = {**profile.to_dict(), "target_soc": target, "_tesla_target_link": journal}
                    return {"auto_schedule_settings": profiles}
                if not await _update_powerwall_store(self.store, patch):
                    return
                profile._tesla_target_link = journal
                profile.target_soc = target
                if changed:
                    state = self.executor.get_state(vin)
                    state.current_plan = state.current_window = None
                    state.last_plan_update = None
            if blocked and link.edited_at:
                self._report(vin, "blocked", blocked, observed, changed=changed)
                return
            if self.declined.get(vin) == (target, link.sent_at) and link.edited_at and not send:
                self._report(vin, "blocked", "Tesla declined the charge-limit command. Waiting to retry.", observed)
                return
            self.declined.pop(vin, None)
            confirmed = observed == target and not link.edited_at
            self._report(vin, "synced" if confirmed else "pending",
                         None if confirmed else "Waiting for Tesla to confirm the updated limit.", observed, changed=changed)
        if not send or self.closed:
            return
        def current():
            live = self._read(vin)
            if live is None or live[0] < at or live[1] != observed:
                if not self.closed:
                    self.dirty.add(vin)
                return False
            return (not self.closed and 0 <= time.time() - at <= MAX_SOURCE_AGE_SECONDS
                    and self.events.get(vin, 0) == event and self.executor._settings.get(vin) is profile
                    and profile.enabled and profile.target_soc == target
                    and profile._tesla_target_link == asdict(link)
                    and not self.write_block())
        if current():
            accepted = await _action_set_ev_charge_limit(self.hass, self.entry, {
                "charger_type": "tesla", "vehicle_vin": vin, "percent": target,
                "_target_sync_current": current,
            })
            if accepted is False and current():
                self.declined[vin] = (target, link.sent_at)
                self._report(vin, "blocked", "Tesla declined the charge-limit command. Waiting to retry.", observed)
