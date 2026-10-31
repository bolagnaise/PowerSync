"""Durable, shared prospective EV demand for rate-only native windows.

The journal is also the global VIN/provider identity claim. A timeout never
releases an uncertain current setting. Device I/O happens outside this lock;
callers must fence the returned revision after every device await.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
import math
import time
from uuid import uuid4

RUNTIME_KEY = "power_sync_native_rate_budget"


def site_identity(entry):
    config = {**entry.data, **entry.options}
    site = config.get("powerwall_local_energy_site_id") or config.get("tesla_energy_site_id")
    return str(site) if site is not None else None


def provider_device(identity):
    """Start switches and amp setters on one provider device are one car."""
    parts = identity.split(":", 3)
    return ":".join(parts[:3]) if len(parts) == 4 else identity


def managed_devices(record):
    return {provider_device(value) for value in record.get("identities", [record["identity"]])}


class SharedRateBudget:
    def __init__(self, store, *, clock=time.time):
        self.store, self.clock = store, clock
        self.entries = {}
        self.managed = {}
        self.armed_sites = set()
        self.observed = {}
        self.lock = asyncio.Lock()
        self.loaded = False
        self.revision = 0
        self.admission_locks = {}
        self.storage_tasks = set()
        self.provider_tasks = {}
        self.managed_tasks = {}
        self.managed_settle = {}

    async def drain_storage(self):
        while self.storage_tasks:
            await asyncio.gather(*tuple(self.storage_tasks), return_exceptions=True)

    def admission_lock(self, site):
        return self.admission_locks.setdefault(site, asyncio.Lock())

    async def load(self):
        async with self.lock:
            if self.loaded:
                return
            data = await self.store.async_load()
            entries = data.get("entries", {}) if isinstance(data, dict) else {}
            if not isinstance(entries, dict):
                raise ValueError("Native rate reservation journal is invalid")
            self._validate(entries)
            managed = data.get("managed", {}) if isinstance(data, dict) else {}
            if not isinstance(managed, dict):
                raise ValueError("Managed EV reservation journal is invalid")
            for record in managed.values():
                if (not isinstance(record, dict) or not isinstance(record.get("site"), str)
                        or not isinstance(record.get("identity"), str)
                        or not isinstance(record.get("reserved_w"), (int, float))
                        or not math.isfinite(record["reserved_w"]) or record["reserved_w"] < 0):
                    raise ValueError("Managed EV reservation journal is invalid")
                if (not isinstance(record.get("identities", [record["identity"]]), list)
                        or not all(isinstance(identity, str) for identity in record.get("identities", [record["identity"]]))):
                    raise ValueError("Managed EV reservation identities are invalid")
            native_devices = {provider_device(session["identity"])
                for entry in entries.values() for session in entry.get("sessions", {}).values()}
            native_vins = {vin for entry in entries.values() for vin in entry.get("sessions", {})}
            if any(vin in native_vins or managed_devices(record) & native_devices for vin, record in managed.items()):
                raise ValueError("Conflicting native and managed vehicle claims")
            self.entries = deepcopy(entries)
            self.managed = deepcopy(managed)
            self.loaded = True

    def native_max_claim(self, site):
        """Return the distinct durable native-Max barrier, never an EV arm."""
        return next((entry["native_max"] for entry in self.entries.values()
                     if entry.get("site") == site and entry.get("native_max")), None)

    async def claim_native_max(self, entry_id, site, token, deadline):
        """Atomically exclude prospective EV demand before native forcing."""
        await self.load()
        task = asyncio.create_task(self._claim_native_max(entry_id, site, token, deadline))
        self.storage_tasks.add(task)
        task.add_done_callback(self.storage_tasks.discard)
        return await asyncio.shield(task)

    async def _claim_native_max(self, entry_id, site, token, deadline):
        if (not isinstance(entry_id, str) or not entry_id or not isinstance(site, str) or not site
                or not isinstance(token, str) or not token or isinstance(deadline, bool)
                or not isinstance(deadline, (int, float)) or not math.isfinite(deadline)):
            raise ValueError("Invalid native Max site claim")
        async with self.lock:
            key = f"native_max:{entry_id}"
            old = self.entries.get(key)
            if old:
                return (old.get("site") == site and old.get("native_max", {}).get("token") == token
                        and old["native_max"].get("expires_at") == deadline)
            if (self.clock() >= deadline or self.native_max_claim(site) or self.site_armed(site)
                    or any(record.get("site") == site for record in self.managed.values())):
                return False
            entries = deepcopy(self.entries)
            entries[key] = {"site": site, "sessions": {}, "native_max": {
                "entry_id": entry_id, "token": token, "expires_at": deadline}}
            self._validate(entries)
            await self.store.async_save({"entries": entries, "managed": self.managed})
            self.entries = entries
            self.revision += 1
            return True

    async def release_native_max(self, entry_id, site, token):
        """Release only the exact Max generation after native cleanup."""
        await self.load()
        task = asyncio.create_task(self._release_native_max(entry_id, site, token))
        self.storage_tasks.add(task)
        task.add_done_callback(self.storage_tasks.discard)
        return await asyncio.shield(task)

    async def _release_native_max(self, entry_id, site, token):
        async with self.lock:
            key = f"native_max:{entry_id}"
            old = self.entries.get(key)
            if old is None:
                return True
            if old.get("site") != site or old.get("native_max", {}).get("token") != token:
                return False
            entries = deepcopy(self.entries)
            entries.pop(key)
            await self.store.async_save({"entries": entries, "managed": self.managed})
            self.entries = entries
            self.revision += 1
            return True

    @staticmethod
    def _validate(entries):
        vins, identities = set(), set()
        max_sites = set()
        for key, entry in entries.items():
            maximum = entry.get("native_max") if isinstance(entry, dict) else None
            if maximum is not None:
                if (not isinstance(maximum, dict) or not isinstance(entry.get("site"), str) or not entry["site"]
                        or not isinstance(maximum.get("entry_id"), str) or not maximum["entry_id"]
                        or key != f"native_max:{maximum['entry_id']}"
                        or not isinstance(maximum.get("token"), str) or not maximum["token"]
                        or isinstance(maximum.get("expires_at"), bool)
                        or not isinstance(maximum.get("expires_at"), (int, float))
                        or not math.isfinite(maximum["expires_at"]) or entry.get("sessions")
                        or entry["site"] in max_sites):
                    raise ValueError("Native Max reservation journal is invalid")
                max_sites.add(entry["site"])
        for entry in entries.values():
            if (isinstance(entry, dict) and entry.get("site") in max_sites
                    and entry.get("sessions")):
                raise ValueError("Native Max conflicts with native EV cleanup")
            if not isinstance(entry, dict) or not isinstance(entry.get("sessions", {}), dict):
                raise ValueError("Native rate reservation journal is invalid")
            for vin, session in entry.get("sessions", {}).items():
                if not isinstance(session, dict) or not isinstance(session.get("identity"), str):
                    raise ValueError("Native rate reservation identity is unavailable")
                key = str(vin).upper()
                identity = provider_device(session["identity"])
                if key in vins or identity in identities:
                    raise ValueError("Another PowerSync entry owns this vehicle")
                vins.add(key)
                identities.add(identity)
                unit = session.get("watts_per_amp")
                if not isinstance(unit, (int, float)) or isinstance(unit, bool) or not math.isfinite(unit) or unit <= 0:
                    raise ValueError("Native rate reservation power is unavailable")
                for field in ("baseline_amps", "attempted_amps"):
                    if type(session.get(field)) is not int or not 1 <= session[field] <= 80:
                        raise ValueError("Native rate reservation current is invalid")

    async def save_entry(self, entry_id, site, data):
        await self.load()
        task = asyncio.create_task(self._save_entry(entry_id, site, deepcopy(data)))
        self.storage_tasks.add(task)
        task.add_done_callback(self.storage_tasks.discard)
        # Cancellation of a caller cannot interrupt a durable commit while a
        # second controller publishes a conflicting revision.
        await asyncio.shield(task)

    async def _save_entry(self, entry_id, site, data):
        async with self.lock:
            if str(entry_id).startswith("native_max:") or data.get("native_max") is not None:
                raise ValueError("Native Max uses its own site admission")
            previous = self.entries.get(entry_id, {})
            if data.get("sessions") and self.native_max_claim(site):
                raise ValueError("Native Max owns this site's charging headroom")
            if previous.get("sessions") and previous.get("site") != site:
                raise ValueError("Native rate cleanup belongs to another site")
            entries = deepcopy(self.entries)
            entries[entry_id] = {**deepcopy(data), "site": site}
            self._validate(entries)
            for vin, session in data.get("sessions", {}).items():
                conflict = vin in self.managed or any(provider_device(session["identity"]) in
                    managed_devices(record) for record in self.managed.values())
                if conflict and vin not in previous.get("sessions", {}):
                    raise ValueError("Managed EV demand is not yet reconciled")
            await self.store.async_save({"entries": entries, "managed": self.managed})
            self.entries = entries
            self.revision += 1

    def observe(self, vin, *, watts, at, meter_at):
        if all(isinstance(v, (int, float)) and math.isfinite(v) for v in (watts, at, meter_at)):
            self.observed[vin] = (max(0.0, watts), at, meter_at)

    def unobserved_w(self, site, *, exclude_vin=None):
        if not self.loaded:
            return math.inf
        total = 0.0
        now = self.clock()
        for entry in self.entries.values():
            if entry.get("site") != site:
                continue
            for vin, session in entry.get("sessions", {}).items():
                if vin == exclude_vin:
                    continue
                amps = session["baseline_amps"] if session.get("recovering") else session.get("confirmed_amps", session["baseline_amps"])
                if session.get("uncertain"):
                    amps = max(amps, session["attempted_amps"])
                reserved = amps * session["watts_per_amp"]
                sample = self.observed.get(vin)
                # Old or unaligned car data cannot turn a reservation into
                # available battery headroom, including after a process restart.
                measured = 0.0
                if sample and 0 <= now-sample[1] <= 15 and 0 <= now-sample[2] <= 15 and abs(sample[1]-sample[2]) <= 5:
                    measured = sample[0]
                total += max(0.0, reserved-measured)
        if self.site_armed(site):
            for vin, record in self.managed.items():
                if vin == exclude_vin or record["site"] != site:
                    continue
                sample = self.observed.get(vin)
                measured = 0.0
                if sample and 0 <= now-sample[1] <= 15 and 0 <= now-sample[2] <= 15 and abs(sample[1]-sample[2]) <= 5:
                    measured = sample[0]
                total += max(0.0, record["reserved_w"]-measured)
        return total

    def site_armed(self, site):
        return site in self.armed_sites or any(entry.get("site") == site and entry.get("sessions")
                                             for entry in self.entries.values())

    def native_claimed(self, vin):
        return any(vin in entry.get("sessions", {}) for entry in self.entries.values())

    async def admit_managed(self, vin, record, evidence, valid):
        """Atomically commit prospective demand before a managed submission.

        ``evidence`` is a fresh aligned physical snapshot. Only a current
        reduction may proceed without additional available power; starts
        always reserve their full prospective draw.
        """
        await self.load()
        task = asyncio.create_task(self._admit_managed(vin, deepcopy(record), evidence, valid))
        self.storage_tasks.add(task)
        task.add_done_callback(self.storage_tasks.discard)
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            # The provider cannot have been called before admission returns.
            # Let its commit settle, then remove only this unsent attempt.
            ticket = await asyncio.shield(task)
            if ticket is not None:
                await self.abort_managed(ticket)
            raise

    async def _admit_managed(self, vin, record, evidence, valid):
        async with self.lock:
            if (not isinstance(record.get("site"), str) or not isinstance(record.get("identity"), str)
                    or not isinstance(record.get("reserved_w"), (int, float))
                    or isinstance(record["reserved_w"], bool) or not math.isfinite(record["reserved_w"])
                    or record["reserved_w"] < 0):
                raise ValueError("Invalid managed EV demand")
            if (not valid() or self.native_claimed(vin)
                    or any(provider_device(session["identity"]) == provider_device(record["identity"])
                        for entry in self.entries.values() for session in entry.get("sessions", {}).values())):
                return None
            maximum = self.native_max_claim(record["site"])
            if maximum:
                current, target = evidence.get("current_amps"), record.get("target_amps")
                if (evidence.get("rate_reduction") is not True or record.get("operation") != "rate"
                        or type(current) is not int or type(target) is not int
                        or not 0 <= target <= current <= 80):
                    return None
                record["native_max_token"] = maximum["token"]
            old = self.managed.get(vin)
            record["attempt_id"] = uuid4().hex
            record["identities"] = sorted({record["identity"], *(old.get("identities", [old["identity"]]) if old else [])})
            if old and old.get("entry_id") != record.get("entry_id"):
                return None
            if any(key != vin and managed_devices(value) & managed_devices(record)
                   for key, value in self.managed.items()):
                return None
            if old and old["identity"] != record["identity"]:
                # A provider fallback must not erase an uncertain earlier
                # demand. Keep its larger power envelope for the same VIN.
                record["reserved_w"] = max(record["reserved_w"], old["reserved_w"])
            if old:
                record["reserved_w"] = max(record["reserved_w"], old["reserved_w"])
            if self.site_armed(record["site"]) and not evidence.get("rate_reduction"):
                now = self.clock()
                numbers = [evidence.get(k) for k in ("grid_w", "cap_w", "actual_ev_w", "battery_pending_w", "vehicle_at", "meter_at")]
                if (not all(isinstance(n, (int, float)) and not isinstance(n, bool) and math.isfinite(n) for n in numbers)
                        or not 0 <= now-evidence["vehicle_at"] <= 15
                        or not 0 <= now-evidence["meter_at"] <= 15
                        or abs(evidence["vehicle_at"]-evidence["meter_at"]) > 5
                        or evidence["cap_w"] < 0):
                    return None
                prospective = (evidence["grid_w"] + self.unobserved_w(record["site"], exclude_vin=vin)
                    + max(0.0, record["reserved_w"]-evidence["actual_ev_w"])
                    + max(0.0, evidence["battery_pending_w"]))
                native_flexible = evidence.get("native_flexible_battery_w", 0.0)
                if (not isinstance(native_flexible, (int, float)) or isinstance(native_flexible, bool)
                        or not math.isfinite(native_flexible) or native_flexible < 0
                        or (native_flexible and (not isinstance(evidence.get("native_battery_proof"), tuple)
                            or not evidence["native_battery_proof"]
                            or not isinstance(evidence.get("actual_battery_charge_w"), (int, float))
                            or isinstance(evidence["actual_battery_charge_w"], bool)
                            or not math.isfinite(evidence["actual_battery_charge_w"])
                            or native_flexible > evidence["actual_battery_charge_w"]))):
                    return None
                borrowed = max(0.0, prospective-native_flexible-evidence["cap_w"])
                lendable = evidence.get("borrowable_battery_w", 0)
                if (borrowed and (evidence.get("battery_generation") is None
                        or not isinstance(lendable, (int, float)) or isinstance(lendable, bool)
                        or not math.isfinite(lendable) or borrowed > max(0.0, lendable))):
                    return None
            managed = deepcopy(self.managed)
            managed[vin] = record
            await self.store.async_save({"entries": self.entries, "managed": managed})
            self.managed = managed
            self.managed_settle.pop(vin, None)
            self.revision += 1
            return {"revision": self.revision, "vin": vin, "record": deepcopy(record),
                    "previous": deepcopy(old), "previous_task": self.managed_tasks.get(vin),
                    "battery_generation": evidence.get("battery_generation") if
                        self.site_armed(record["site"]) and not evidence.get("rate_reduction") else None}

    async def abort_managed(self, ticket):
        """Roll back a proven unsent attempt without erasing an older command."""
        task = asyncio.create_task(self._abort_managed(ticket))
        self.storage_tasks.add(task)
        task.add_done_callback(self.storage_tasks.discard)
        await asyncio.shield(task)

    async def _abort_managed(self, ticket):
        async with self.lock:
            vin = ticket["vin"]
            current = self.managed.get(vin)
            if current is None or current.get("attempt_id") != ticket["record"].get("attempt_id"):
                return
            managed = deepcopy(self.managed)
            if ticket["previous"] is None:
                managed.pop(vin)
            else:
                managed[vin] = deepcopy(ticket["previous"])
            await self.store.async_save({"entries": self.entries, "managed": managed})
            self.managed = managed
            self.managed_settle.pop(vin, None)
            self.managed_tasks.pop(vin, None)
            if ticket.get("previous_task") is not None:
                self.managed_tasks[vin] = ticket["previous_task"]
            self.revision += 1

    async def reconcile_managed(self, vin, sample):
        """Narrow/release only after the provider task and two settled reports."""
        task = asyncio.create_task(self._reconcile_managed(vin, deepcopy(sample)))
        self.storage_tasks.add(task)
        task.add_done_callback(self.storage_tasks.discard)
        await asyncio.shield(task)

    async def _reconcile_managed(self, vin, sample):
        async with self.lock:
            record = self.managed.get(vin)
            task = self.managed_tasks.get(vin)
            if record is None or (task is not None and not task.done()):
                return
            now = self.clock()
            stamps = [sample.get(k) for k in ("vehicle_at", "meter_at", "setting_at")]
            if (sample.get("identity") != record["identity"]
                    or not all(isinstance(at, (int, float)) and 0 <= now-at <= 15 for at in stamps)
                    or max(stamps)-min(stamps) > 5
                    or min(stamps) <= record.get("submitted_at", 0)
                    or not isinstance(sample.get("amps"), (int, float))
                    or not 0 <= sample["amps"] <= 80
                    or not isinstance(sample.get("actual_w"), (int, float))
                    or not math.isfinite(sample["actual_w"])):
                return
            self.observe(vin, watts=sample["actual_w"], at=sample["vehicle_at"], meter_at=sample["meter_at"])
            state = (sample["amps"], sample.get("charging"), sample.get("owner_active"))
            first = self.managed_settle.get(vin)
            if first is None or first[1] != state:
                self.managed_settle[vin] = (min(stamps), state)
                return
            if min(stamps)-first[0] < 3:
                return
            managed = deepcopy(self.managed)
            if (not sample.get("owner_active") and
                    (sample.get("charging") is False or sample["amps"] != record.get("target_amps"))):
                managed.pop(vin)
            elif (sample["amps"] == record.get("target_amps")
                    and sample["actual_w"] <= record.get("target_w", 0)+250):
                managed[vin]["reserved_w"] = record["target_w"]
                managed[vin]["uncertain"] = False
            else:
                return
            if managed == self.managed:
                return
            await self.store.async_save({"entries": self.entries, "managed": managed})
            self.managed = managed
            self.revision += 1


class EntryRateJournal:
    def __init__(self, budget, entry_id, site):
        self.budget, self.entry_id, self.site = budget, entry_id, site

    async def async_load(self):
        await self.budget.load()
        await self.budget.drain_storage()
        async with self.budget.lock:
            return deepcopy(self.budget.entries.get(self.entry_id, {}))

    async def async_save(self, data):
        await self.budget.save_entry(self.entry_id, self.site, data)


async def budget_for(hass):
    from homeassistant.helpers.storage import Store
    if RUNTIME_KEY not in hass.data:
        hass.data[RUNTIME_KEY] = SharedRateBudget(Store(hass, 1, "power_sync.tesla_native_rate_budget"))
    budget = hass.data[RUNTIME_KEY]
    await budget.load()
    return budget
