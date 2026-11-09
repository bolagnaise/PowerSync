"""Bounded discovery of solar hidden by a native site-export limit.

This grants temporary permission to the existing curtailment writer. It never
executes vehicle commands, manufactures surplus, or resumes a lease after reload.
"""
from __future__ import annotations

import asyncio
import math
import time
from datetime import datetime, timedelta, timezone


def candidate_token(state):
    params = state.get("params") or {}
    owner = state.get("ownership") or {}
    return tuple(params.get(key) for key in ("dynamic_mode", "owner_mode", "vehicle_vin", "charger_type")) + tuple(
        owner.get(key) for key in ("owner", "owner_mode", "session_id", "ownership_generation", "started_at")
    )


def usable_discovery_sample(status, *, now=None):
    """Require an advancing-capable native sample, not cached control intent."""
    now = now or datetime.now(timezone.utc)
    at = status.get("site_observed_at")
    if not isinstance(at, datetime) or at.tzinfo is None:
        return False
    age = (now - at).total_seconds()
    if not 0 <= age <= 90:
        return False
    values = [status.get(k) for k in ("battery_soc", "solar_power", "grid_power", "battery_power")]
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in values):
        return False
    soc, solar, grid, battery = values
    return (soc >= 99 and solar > 50 and -100 <= grid <= 100 and battery <= 50
            and status.get("native_zero_export") is True
            and not status.get("ac_production_curtailed"))


def idle_load_probe(entry_data, vehicle_id, state, status, *, eligible,
                    min_amps, power_kw, power_available, clock, utcnow, schedule):
    """Bound the legacy zero-export load trial separately from measured surplus.

    Native Sigenergy/Sungrow use restore-first discovery instead. This retains
    the existing load-following bootstrap for other controllers, with one shared
    trial, a real settling deadline, and immediate unsafe-load rejection.
    """
    record = entry_data.get("solar_ev_load_probe")
    now = clock()
    if record and record["phase"] in {"accepted", "cooldown"}:
        if now < record["not_before"]:
            return None
        entry_data.pop("solar_ev_load_probe", None)
        record = None
    if record and record["state"] is not state:
        if record["phase"] == "waiting":
            entry_data.pop("solar_ev_load_probe", None)
            record = None
        elif now < record["not_before"]:
            return None
        else:
            record = None
    if record and record["phase"] in {"trial", "expired"}:
        at = status.get("site_observed_at")
        values = [status.get(key) for key in ("grid_power", "battery_power")]
        valid = (isinstance(at, datetime) and at.tzinfo is not None
                 and 0 <= (utcnow() - at).total_seconds() <= 90
                 and all(isinstance(v, (int, float)) and not isinstance(v, bool)
                         and math.isfinite(v) for v in values))
        unsafe = not valid or values[0] > 100 or values[1] > 50
        if record["phase"] == "expired" or now >= record["deadline"] or unsafe:
            record["phase"] = "expired"
            record["cancel"]()
            if (state.get("current_amps", 0) == 0 and not state.get("charging_started")
                    and not state.get("transport_recovery_pending") and not state.get("stop_outcome")):
                record["phase"] = "cooldown"
                return None
            return 0  # Bypass the ordinary low-surplus stop-delay grace.
        if (at > record["started_at"] and power_available and power_kw > 0.05):
            record["phase"] = "accepted"
            record["cancel"]()
            return None  # Subsequent control uses only measured surplus.
        return min_amps
    if record and record["phase"] in {"accepted", "expired"}:
        return None
    if record and record["phase"] == "waiting" and not eligible:
        state["high_surplus_start"] = None
        entry_data.pop("solar_ev_load_probe", None)
        return None
    if not eligible:
        return None
    at = status.get("site_observed_at")
    if not isinstance(at, datetime) or at.tzinfo is None or not 0 <= (utcnow() - at).total_seconds() <= 90:
        return None
    if record is None:
        record = {
            "state": state, "vehicle_id": vehicle_id, "phase": "waiting",
            "ready_at": now + max(0, float((state.get("params") or {}).get("sustained_surplus_minutes", 2))) * 60,
            "not_before": now,
        }
        entry_data["solar_ev_load_probe"] = record
    if now >= record["ready_at"]:
        record.update(phase="trial", deadline=now + 90, not_before=now + 900, started_at=utcnow())
        record["cancel"] = schedule(record, 90)
    return min_amps


class SolarEVHeadroom:
    """One non-renewable site lease, with durable pending closure and backoff."""

    def __init__(self, *, identity, saved, persist, eligible, reconcile, abort,
                 schedule, current, clock=time.monotonic, utcnow=None):
        self.identity = list(identity)
        self.saved = dict(saved) if isinstance(saved, dict) else ({"pending": True, "identity": None} if saved is not None else {})
        self.persist = persist
        self.eligible = eligible
        self.reconcile = reconcile
        self.abort = abort
        self.schedule = schedule
        self.current = current
        self.clock = clock
        self.utcnow = utcnow or (lambda: datetime.now(timezone.utc))
        self.lease = None
        self.phase = "closing" if self.saved.get("pending") else "closed"
        self.lock = asyncio.Lock()
        self.cancel = None
        self.error = None
        self._abort_running = False

    def permits(self):
        return bool(self.current() and self.lease and self.phase in {"discovery", "trial"}
                    and self.clock() < self.lease["deadline"])

    @property
    def candidate(self):
        return self.lease["candidate"] if self.permits() else None

    async def request(self, candidate, status, *, sustained_seconds=0, baseline_amps=0, baseline_power_kw=0):
        """Only the EV decision loop may request a lease; price polling cannot."""
        async with self.lock:
            if not self.current() or self.lease or self.saved.get("pending"):
                return False
            try:
                not_before = datetime.fromisoformat(self.saved["not_before"])
                if self.utcnow() < not_before:
                    return False
            except (KeyError, TypeError, ValueError):
                if self.saved:
                    self.error = "invalid retained headroom budget"
                    return False
            if self.saved and self.saved.get("identity") != self.identity:
                # Only a confirmed closed record may yield to a new source.
                # Pending mutations were rejected above and remain retained.
                self.saved = {}
            if not usable_discovery_sample(status, now=self.utcnow()):
                return False
            if not await self.eligible(candidate) or not self.current():
                return False
            # Bootstrap must fit the user's sustained-surplus timer as well as
            # the existing read/command window. This is a software permission
            # deadline, not a hardware settling guarantee.
            try:
                duration = max(180.0, float(sustained_seconds) + 150.0) if baseline_amps <= 0 else 120.0
            except (TypeError, ValueError, OverflowError):
                return False
            if not math.isfinite(duration) or not 0 < duration <= 3600:
                return False
            record = {
                "identity": self.identity, "pending": True,
                "vehicle_id": candidate[0], "owner_token": list(candidate[2]),
                "not_before": (self.utcnow() + timedelta(seconds=max(900, duration * 6))).isoformat(),
            }
            try:
                await self.persist(record)
            except Exception:
                self.error = "could not persist pending headroom restoration"
                return False
            self.saved = record
            if (not self.current() or not await self.eligible(candidate)
                    or not usable_discovery_sample(status, now=self.utcnow())):
                self.phase = "closing"
                await self._close_locked(abort=False)
                return False
            self.lease = {
                "candidate": candidate, "deadline": self.clock() + duration,
                "baseline_amps": baseline_amps, "released_at": None,
                "baseline_power_kw": baseline_power_kw,
                "ev_commanded": False,
                "last_sample": None, "last_power": None, "stable": 0,
            }
            self.phase = "discovery"
            admitted_lease = self.lease
            self.cancel = self.schedule(duration, lambda: self.expire(admitted_lease))
            try:
                opened = await self.reconcile(opening=True)
            except Exception:
                opened = False
            if not opened or not self.permits():
                await self._close_locked(abort=False)
                return False
            self.lease["released_at"] = self.utcnow()
            return True

    async def command_started(self, candidate, target_power_kw=None):
        """Persist trial cleanup before the existing EV executor may submit."""
        async with self.lock:
            if not self.permits() or self.lease["candidate"] is not candidate or not await self.eligible(candidate):
                return False
            record = {**self.saved, "ev_abort_pending": True}
            try:
                await self.persist(record)
            except Exception:
                self.error = "could not persist EV trial cleanup"
                return False
            self.saved = record
            if not self.permits() or not await self.eligible(candidate):
                return False
            self.lease["ev_commanded"] = True
            self.lease["commanded_at"] = self.utcnow()
            self.lease["target_power_kw"] = target_power_kw
            return True

    async def observe(self, candidate, status, *, power_kw, power_available, safe_amps):
        """Close on acceptance or unsafe/expired trials, using fresh site reads."""
        async with self.lock:
            lease = self.lease
            if not lease or lease["candidate"] is not candidate:
                return
            lease["safe_amps"] = safe_amps
            if not self.permits() or not await self.eligible(candidate):
                await self._close_locked(abort=True)
                return
            at = status.get("site_observed_at")
            if (not isinstance(at, datetime) or at.tzinfo is None
                    or at <= lease["released_at"]
                    or (lease["last_sample"] is not None and at <= lease["last_sample"])):
                return
            if not 0 <= (self.utcnow() - at).total_seconds() <= 90:
                await self._close_locked(abort=True)
                return
            values = [status.get(k) for k in ("grid_power", "battery_power")]
            if (any(isinstance(v, bool) or not isinstance(v, (int, float))
                    or not math.isfinite(v) for v in values)
                    or values[0] > 100 or values[1] > 50):
                await self._close_locked(abort=True)
                return
            lease["last_sample"] = at
            if not power_available or not math.isfinite(power_kw) or power_kw <= 0.05:
                return
            if (not lease.get("ev_commanded") or at <= lease["commanded_at"]
                    or power_kw <= lease["baseline_power_kw"] + 0.05):
                return
            self.phase = "trial"
            previous = lease["last_power"]
            lease["stable"] = lease["stable"] + 1 if previous is not None and abs(power_kw - previous) <= 0.1 else 1
            lease["last_power"] = power_kw
            target = lease.get("target_power_kw")
            if lease["stable"] >= 2 or (target is not None and power_kw >= target * 0.8):
                await self._close_locked(abort=False)

    async def expire(self, expected_lease=None):
        # Revoke synchronously before waiting for any in-flight writer.
        if expected_lease is not None and self.lease is not expected_lease:
            return
        if not self.lease and not self.saved.get("pending"):
            return
        self.phase = "closing"
        async with self.lock:
            if expected_lease is not None and self.lease is not expected_lease:
                return
            await self._close_locked(abort=True)

    async def recover(self):
        """A restart reconciles the pending mutation and never resumes discovery."""
        async with self.lock:
            if self.saved.get("pending") and (self.lease is None or self.phase == "closing"):
                await self._close_locked(abort=bool(self.saved.get("ev_abort_pending")))

    async def close(self):
        if not self.lease and not self.saved.get("pending"):
            return
        self.phase = "closing"
        async with self.lock:
            await self._close_locked(abort=True)

    async def _close_locked(self, *, abort):
        self.phase = "closing"
        if self._abort_running:
            # Let the EV decision unwind its outer command/ownership lock.
            return
        if self.cancel:
            self.cancel()
            self.cancel = None
        if not self.current() or self.saved.get("identity") != self.identity:
            self.error = "headroom restoration remains pending for its original source"
            return
        # Reapply the native limit before waiting on a slow EV command/stop.
        # A pending EV transaction must not extend the export exception.
        try:
            closed = await self.reconcile(opening=False)
        except Exception:
            closed = False
        abort_ok = True
        if abort and (self.saved.get("ev_abort_pending") or (self.lease and self.lease.get("ev_commanded"))):
            # The EV loop owns its command lock before entering this runtime.
            # Timer cleanup must not hold our lock while waiting for that same
            # command lock. Permission is already revoked, admission remains
            # blocked by pending/lease, and competing closers return above.
            self._abort_running = True
            self.lock.release()
            try:
                abort_ok = bool(await self.abort(self.lease or {
                    "candidate": None, "vehicle_id": self.saved.get("vehicle_id"),
                    "owner_token": self.saved.get("owner_token"),
                    "baseline_amps": 0, "safe_amps": 0,
                }))
            except Exception:
                abort_ok = False
            finally:
                await self.lock.acquire()
                self._abort_running = False
            if not self.current() or self.saved.get("identity") != self.identity:
                self.error = "headroom restoration remains pending for its original source"
                return
            if not abort_ok:
                self.error = "EV trial stop remains unconfirmed"
                self.saved = {**self.saved, "ev_abort_pending": True}
                try:
                    await self.persist(self.saved)
                except Exception:
                    pass  # The original pending native mutation is retained.
        if not self.current() or not closed:
            self.error = "native export-limit restoration remains pending"
            return
        if not abort_ok:
            return
        record = {**self.saved, "pending": False, "ev_abort_pending": False}
        try:
            await self.persist(record)
        except Exception:
            self.error = "could not persist confirmed headroom closure"
            return
        self.saved = record
        self.lease = None
        self.phase = "cooldown"
        self.error = None
