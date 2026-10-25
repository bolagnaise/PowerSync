"""Durable lifetime for an explicit Tesla-managed Max charging request.

Explicit Max and targetless larger-site optimiser requests use this actuator.
The charge storage/class names remain compatible with existing journals. Adapters own
provider I/O and must obtain/use a new write fence at every mutation boundary,
including retries. A five-second policy monitor is corrective, not a physical
power or instantaneous SOC limiter. Recovery restores; it never replays charge.
"""
from __future__ import annotations

import asyncio
from contextlib import suppress
from copy import deepcopy
import math
import time
from uuid import uuid4


class NativeMaxChargeError(Exception):
    """Charging was rejected, superseded, expired or could not be restored."""


class NativeMaxTariffRejected(NativeMaxChargeError):
    """The provider explicitly rejected the session's only tariff POST."""


class NativeMaxChargeSession:
    """One restoration owner, protected by the caller's shared site admission.

    ``adapter`` provides identity, guard(), capture(), resume(fence),
    apply(baseline, before_write), restore(baseline, before_write),
    claim(token, deadline), release(token), and synchronous authority().
    Provider commands return True only after their required acknowledgements
    and readback. Baselines contain restore data, never provider credentials.
    The adapter must finish older local/tariff ownership before capture().
    """

    def __init__(self, adapter, store, *, changed=None, clock=time.time,
                 monotonic=time.monotonic, interval=5):
        self.adapter, self.store, self.changed = adapter, store, changed
        self.clock, self.monotonic = clock, monotonic
        self.interval = interval
        self.record = None
        self.loaded = False
        self._generation = 0
        self._lock = asyncio.Lock()
        self._watch_task = None
        self._apply_task = None
        self._clearing = False

    def _abort_application(self):
        task = self._apply_task
        if task and not task.done():
            task.cancel()

    @property
    def pending(self):
        return self.record is not None or self._clearing

    def status(self):
        if not self.record:
            return {"active": False, "restore_pending": False}
        r = self.record
        return {"active": r["phase"] == "active", "restore_pending": r["phase"] in
                ("applying", "restoring", "settling", "restored"), "phase": r["phase"],
                "expires_at": r["expires_at"], "id": r["id"],
                "last_error": r.get("last_error"),
                "direction": r["baseline"].get("direction", "charge"),
                "startup_kick_w": r.get("startup_kick_w"),
                "handoff_phase": r.get("handoff_phase")}

    async def load(self):
        async with self._lock:
            if self.loaded:
                return
            data = await self.store.async_load()
            if data is not None:
                if (not isinstance(data, dict) or data.get("version") != 1
                        or not isinstance(data.get("id"), str) or not data["id"]
                        or not isinstance(data.get("identity"), str) or not data["identity"]
                        or data.get("phase") not in ("prepared", "applying", "active", "restoring", "settling", "restored")
                        or isinstance(data.get("expires_at"), bool)
                        or not isinstance(data.get("expires_at"), (int, float))
                        or not math.isfinite(data["expires_at"])
                        or not isinstance(data.get("baseline"), dict)):
                    raise NativeMaxChargeError("Native Max restore journal is invalid")
                cleanup = data.get("cleanup", {})
                if (not isinstance(cleanup, dict) or any(key not in
                        {"mode_released", "reserve_released", "tariff_released", "mode_disarmed", "export_released"}
                        or type(value) is not bool for key, value in cleanup.items())):
                    raise NativeMaxChargeError("Native Max cleanup journal is invalid")
                until = data.get("local_acceptance_until", 0)
                if (type(until) not in (int, float) or not math.isfinite(until) or until < 0
                        or data.get("tariff_intent") not in (None, "forcing", "baseline")):
                    raise NativeMaxChargeError("Native Max mutation journal is invalid")
                if (data["baseline"].get("direction", "charge") not in ("charge", "discharge")
                        or type(data.get("kick_pending", False)) is not bool):
                    raise NativeMaxChargeError("Native force direction or kick journal is invalid")
                self.record = deepcopy(data)
            self.loaded = True

    async def _save(self):
        # A cancelled caller must wait for an uncertain durable commit before
        # cleanup can clear it. Otherwise a late write can resurrect forcing.
        task = asyncio.create_task(self.store.async_save(deepcopy(self.record)))
        cancelled = False
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                cancelled = True
        task.result()
        if cancelled:
            raise asyncio.CancelledError
        if self.changed:
            await self.changed(self.status())

    def _active_fence(self, token, generation, checked_at):
        def fence():
            if (not self.record or self.record["id"] != token
                    or generation != self._generation
                    or self.record["identity"] != self.adapter.identity
                    or self.clock() >= self.record["expires_at"]
                    or not 0 <= self.monotonic() - checked_at <= 5
                    or self.adapter.authority() is not True):
                raise NativeMaxChargeError("Native Max write authority expired or changed")
        return fence

    async def _authorize(self, token, generation):
        if not self.record or self.record["id"] != token:
            raise NativeMaxChargeError("Native Max request was replaced")
        # A denied/unknown current policy never authorizes a provider write.
        if await self.adapter.guard() is not True:
            raise NativeMaxChargeError("Native Max current policy denied charging")
        fence = self._active_fence(token, generation, self.monotonic())
        fence()
        return fence

    async def _cancel_watch(self):
        task, self._watch_task = self._watch_task, None
        if task and task is not asyncio.current_task():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    async def _write_intent(self, kind, value=None):
        """Journal possible remote writes, including acceptance after timeout."""
        if not self.record or self.record["identity"] != self.adapter.identity:
            raise NativeMaxChargeError("Native Max mutation ownership changed")
        if kind == "wait_local":
            # One extra second avoids treating an inclusive gateway expiry as
            # closed. A restart uses the same durable, actual signature expiry.
            while self.record.get("local_acceptance_until", 0) + 1 > self.clock():
                await asyncio.sleep(min(1, self.record["local_acceptance_until"] + 1 - self.clock()))
            self.record.pop("local_acceptance_until", None)
        elif kind == "local":
            if type(value) not in (int, float) or not math.isfinite(value):
                raise NativeMaxChargeError("Native Max acceptance deadline is invalid")
            self.record["local_acceptance_until"] = max(value, self.record.get("local_acceptance_until", 0))
        elif kind == "kick":
            until, seconds, watts = value
            if (type(until) not in (int, float) or not math.isfinite(until)
                    or type(seconds) is not int or not 1 <= seconds <= 30
                    or type(watts) not in (int, float) or not math.isfinite(watts) or abs(watts) > 10000):
                raise NativeMaxChargeError("Native force kick intent is invalid")
            self.record.update(kick_pending=True, startup_kick_w=watts,
                               kick_lease_until=until + seconds, handoff_phase="starting")
            self.record["local_acceptance_until"] = max(until, self.record.get("local_acceptance_until", 0))
        elif kind == "kick_released":
            self.record.pop("kick_pending", None)
            self.record["handoff_phase"] = "tariff"
        elif kind == "local_confirmed":
            self.record.pop("local_acceptance_until", None)
        elif kind == "tariff":
            if value not in ("forcing", "baseline"):
                raise NativeMaxChargeError("Native Max tariff intent is invalid")
            self.record["tariff_intent"] = value
        elif kind == "tariff_confirmed":
            self.record.pop("tariff_intent", None)
        elif kind == "tariff_rejected":
            if value not in ("forcing", "baseline") or self.record.get("tariff_intent") != value:
                raise NativeMaxChargeError("Native Max rejected tariff intent changed")
            self.record.pop("tariff_intent")
        else:
            raise NativeMaxChargeError("Unknown Native Max mutation intent")
        await self._save()

    async def start(self, duration_seconds, *, deadline=None):
        if type(duration_seconds) is not int or not 1 <= duration_seconds <= 86400:
            raise ValueError("Native Max duration must be an integer from 1 to 86400 seconds")
        latest_deadline = self.clock() + duration_seconds
        if deadline is None:
            deadline = latest_deadline
        if (isinstance(deadline, bool) or not isinstance(deadline, (int, float))
                or not math.isfinite(deadline) or deadline > latest_deadline):
            raise ValueError("Native Max deadline cannot extend the original request")
        self._generation += 1
        generation = self._generation
        await self.load()
        async with self._lock:
            await self._cancel_watch()
            if self.record:
                await self._restore_locked()
            if generation != self._generation or self.clock() >= deadline:
                raise NativeMaxChargeError("Native Max request expired before acquisition")
            if self.adapter.authority() is not True or await self.adapter.guard() is not True:
                raise NativeMaxChargeError("Native Max current policy denied charging")
            baseline = await self.adapter.capture()
            if not isinstance(baseline, dict) or not baseline:
                raise NativeMaxChargeError("Native Max restore baseline is unavailable")
            token = uuid4().hex
            self.record = {"version": 1, "id": token, "identity": self.adapter.identity,
                           "phase": "prepared", "expires_at": deadline,
                           "baseline": deepcopy(baseline)}
            try:
                # Durable baseline and original deadline precede even admission.
                await self._save()
                if await self.adapter.claim(token, deadline) is not True:
                    raise NativeMaxChargeError("Native Max site admission was denied")
                fence = await self._authorize(token, generation)
                # Record possible mutation before the first provider command.
                self.record["phase"] = "applying"
                await self._save()
                self._watch_task = asyncio.create_task(self._watch(token, generation))
                async def apply():
                    fence()  # storage/admission awaits must not extend a grant
                    kick = getattr(self.adapter, "kick", None)
                    if kick is not None:
                        await kick(deepcopy(baseline), lambda: self._authorize(token, generation),
                                   deadline=deadline, write_intent=self._write_intent)
                    # A timed command is explicitly released before native
                    # tariff writes. No renewable local controller is started.
                    await self._write_intent("wait_local")
                    resume_fence = await self._authorize(token, generation)
                    if await self.adapter.resume(resume_fence, write_intent=self._write_intent) is not True:
                        raise NativeMaxChargeError("Local battery resume was not confirmed")
                    await self._write_intent("kick_released")
                    await self._authorize(token, generation)
                    if await self.adapter.apply(deepcopy(baseline),
                            lambda: self._authorize(token, generation), write_intent=self._write_intent) is not True:
                        raise NativeMaxChargeError("Native Max command readback was not confirmed")
                    await self._authorize(token, generation)
                self._apply_task = asyncio.create_task(apply())
                try:
                    await asyncio.wait_for(self._apply_task, max(0, deadline-self.clock()))
                except asyncio.CancelledError:
                    if not asyncio.current_task().cancelling():
                        raise NativeMaxChargeError("Native Max application was revoked") from None
                    raise
                except asyncio.TimeoutError:
                    if self._apply_task.done() and not self._apply_task.cancelled():
                        raise NativeMaxChargeError("Native Max provider timed out; cleanup retained") from None
                    raise NativeMaxChargeError("Native Max application deadline expired") from None
                finally:
                    self._apply_task = None
                self.record["phase"] = "active"
                await self._save()
                return self.status()
            except BaseException as error:
                if self.record:
                    self.record["last_error"] = type(error).__name__
                    try:
                        await self._restore_locked()
                    except BaseException:
                        # Keep the durable intent and a cleanup-only retry.
                        if self.record and (not self._watch_task or self._watch_task.done()):
                            self._watch_task = asyncio.create_task(self._watch(token, generation))
                raise

    async def _restore_locked(self):
        if not self.record:
            return
        token = self.record["id"]
        if self.record["identity"] != self.adapter.identity:
            raise NativeMaxChargeError("Native Max cleanup belongs to another paired identity")
        def restore_fence():
            if not self.record or self.record["id"] != token or self.record["identity"] != self.adapter.identity:
                raise NativeMaxChargeError("Native Max cleanup ownership changed")
        async def before_restore(progress=None):
            restore_fence()
            if progress is not None:
                if not isinstance(progress, dict) or any(type(v) is not bool for v in progress.values()):
                    raise NativeMaxChargeError("Native Max cleanup progress is invalid")
                self.record["cleanup"] = deepcopy(progress)
                await self._save()
                restore_fence()
            return restore_fence
        if self.record["phase"] not in ("prepared", "settling", "restored"):
            self.record["phase"] = "restoring"
            await self._save()
            await self._write_intent("wait_local")
            if self.record.get("kick_pending"):
                if await self.adapter.resume(restore_fence, write_intent=self._write_intent) is not True:
                    raise NativeMaxChargeError("Startup kick cleanup resume was not confirmed")
                await self._write_intent("kick_released")
            baseline = deepcopy(self.record["baseline"])
            baseline["_cleanup"] = deepcopy(self.record.get("cleanup", {}))
            baseline["_tariff_intent"] = self.record.get("tariff_intent")
            if await self.adapter.restore(baseline, before_restore,
                    write_intent=self._write_intent) is not True:
                raise NativeMaxChargeError("Native Max restore was not confirmed")
            if self.record.get("tariff_intent"):
                raise NativeMaxChargeError("Native Max tariff submission remains uncertain")
            self.record["phase"] = "settling"
            await self._save()
        if self.record["phase"] == "settling":
            if await self.adapter.settled(deepcopy(self.record["baseline"])) is not True:
                raise NativeMaxChargeError("Native Max settings restored; physical settling is pending")
        restore_limits = getattr(self.adapter, "restore_limits", None)
        if self.record["phase"] == "settling" and restore_limits is not None:
            await self._write_intent("wait_local")
            if await restore_limits(deepcopy(self.record["baseline"]), before_restore,
                    write_intent=self._write_intent) is not True:
                raise NativeMaxChargeError("Native Max site limit restore was not confirmed")
        # Settings readback alone does not release physical EV capacity.
        # A later failed journal clear must never replay native restore writes.
        self.record["phase"] = "restored"
        await self._save()
        if await self.adapter.release(token) is not True:
            raise NativeMaxChargeError("Native Max site release was not confirmed")
        old = self.record
        self.record = None
        self._clearing = True
        try:
            await self._save()
        except BaseException:
            self.record = old
            raise
        finally:
            self._clearing = False

    async def shorten(self, deadline):
        """Persist an earlier safety deadline without replaying a force command."""
        if type(deadline) not in (int, float) or not math.isfinite(deadline):
            raise ValueError("Native Max deadline is invalid")
        async with self._lock:
            if not self.record or self.record["phase"] != "active":
                raise NativeMaxChargeError("Native Max session is not active")
            if deadline < self.record["expires_at"]:
                self.record["expires_at"] = deadline
                try:
                    await self._save()
                except BaseException:
                    self.revoke()
                    raise
            if self.clock() >= self.record["expires_at"]:
                self.revoke()
                await self._restore_locked()
                raise NativeMaxChargeError("Native Max safety deadline expired")
            return self.status()

    def revoke(self):
        """Revoke before a caller waits for the shared site route lock."""
        self._generation += 1
        self._abort_application()

    async def stop(self):
        self.revoke()
        await self.load()
        async with self._lock:
            await self._cancel_watch()
            try:
                await self._restore_locked()
            except BaseException:
                if self.record:
                    self._watch_task = asyncio.create_task(
                        self._watch(self.record["id"], self._generation))
                raise
        return not self.pending

    async def recover(self):
        """Restore a previous process's intent; never issue a charge request."""
        await self.load()
        if self.record:
            return await self.stop()
        return True

    async def _watch(self, token, generation):
        while self.record and self.record["id"] == token:
            await asyncio.sleep(min(1, self.interval))
            try:
                r = self.record
                if not r or r["id"] != token:
                    return
                expired = self.clock() >= r["expires_at"] or generation != self._generation
                if r["phase"] in ("active", "applying") and not expired:
                    await self._authorize(token, generation)
                    await asyncio.sleep(max(0, self.interval - 1))
                    continue
                if r["phase"] in ("prepared", "applying") and not expired:
                    continue
                self._generation += 1
                self._abort_application()
                async with self._lock:
                    if self.record and self.record["id"] == token:
                        await self._restore_locked()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                if self.record and self.record["id"] == token:
                    self.record["last_error"] = type(error).__name__
                    # A policy error ends charging; retries only restore.
                    self._generation += 1
                    self._abort_application()
                    async with self._lock:
                        try:
                            await self._restore_locked()
                        except Exception:
                            pass
