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


class NativeMaxRestorePending(NativeMaxChargeError):
    """Settings are restored; physical verification still owns the session."""


class NativeMaxTariffRejected(NativeMaxChargeError):
    """The provider explicitly rejected the session's only tariff POST."""


class NativeMaxTariffNotSubmitted(NativeMaxChargeError):
    """The provider boundary was never entered for this tariff upload."""


class NativeMaxExportNotSubmitted(NativeMaxChargeError):
    """The cloud export-setting boundary was never entered."""


class NativeMaxExportRejected(NativeMaxChargeError):
    """The provider explicitly rejected an export-setting request."""


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
                "export_rule_source": r["baseline"].get("export_rule_source"),
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
                if "normal_tariff" in data and (not isinstance(data["normal_tariff"], dict)
                                                or not data["normal_tariff"]):
                    raise NativeMaxChargeError("Native Max normal tariff journal is invalid")
                if (not isinstance(cleanup, dict) or any(key not in
                        {"mode_released", "reserve_released", "tariff_released", "mode_disarmed", "export_released", "grid_released"}
                        or type(value) is not bool for key, value in cleanup.items())):
                    raise NativeMaxChargeError("Native Max cleanup journal is invalid")
                receipt = data.get("pairing_recovery")
                if receipt is not None and (not isinstance(receipt, dict)
                        or set(receipt) != {"site", "identity", "token", "confirmed"}
                        or any(not isinstance(receipt.get(key), str) or not receipt[key]
                               for key in ("site", "identity", "token"))
                        or receipt.get("token") != data["id"]
                        or type(receipt.get("confirmed")) is not bool):
                    raise NativeMaxChargeError("Native Max pairing recovery journal is invalid")
                until = data.get("local_acceptance_until", 0)
                if (type(until) not in (int, float) or not math.isfinite(until) or until < 0
                        or data.get("tariff_intent") not in (None, "forcing", "baseline")):
                    raise NativeMaxChargeError("Native Max mutation journal is invalid")
                if (data["baseline"].get("direction", "charge") not in ("charge", "discharge")
                        or type(data.get("kick_pending", False)) is not bool
                        or type(data.get("grid_charge_intent", False)) is not bool):
                    raise NativeMaxChargeError("Native force direction or kick journal is invalid")
                if data.get("grid_charge_intent") and (
                        data["baseline"].get("direction", "charge") != "charge"
                        or data["baseline"].get("grid_charge_blocked") is not True):
                    raise NativeMaxChargeError("Grid charge restoration journal is invalid")
                source = data["baseline"].get("export_rule_source", "local")
                untracked = data["baseline"].get("export_permission_untracked", False)
                if (type(data.get("export_conflict", False)) is not bool
                        or source not in ("local", "cloud", "untracked")) or data.get("export_intent") not in (
                        None, "never", "pv_only", "battery_ok"):
                    raise NativeMaxChargeError("Export permission recovery journal is invalid")
                if (type(untracked) is not bool or (source == "untracked") != untracked
                        or (untracked and (data["baseline"].get("direction") != "discharge"
                            or "export_rule" in data["baseline"] or data.get("export_conflict")
                            or data.get("export_intent") not in (None, "battery_ok")))):
                    raise NativeMaxChargeError("Untracked export permission journal is invalid")
                if data.get("export_intent") and (source not in ("cloud", "untracked")
                        or data["baseline"].get("direction") != "discharge"
                        or (not untracked and data["baseline"].get("export_rule")
                            not in ("never", "pv_only", "battery_ok"))):
                    raise NativeMaxChargeError("Export permission restoration baseline is invalid")
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

    async def _authorize(self, token, generation, *, native_config=None,
                         preparing=False):
        if not self.record or self.record["id"] != token:
            raise NativeMaxChargeError("Native Max request was replaced")
        # A denied/unknown current policy never authorizes a provider write.
        guard_kwargs = {}
        if native_config is not None:
            guard_kwargs["native_config"] = native_config
        if preparing:
            guard_kwargs["preparing"] = True
        permitted = await self.adapter.guard(**guard_kwargs)
        if permitted is not True:
            raise NativeMaxChargeError("Native Max current policy denied charging")
        fence = self._active_fence(token, generation, self.monotonic())
        fence()
        return fence

    async def _claim(self, token, deadline, *, preparing=False):
        return await self.adapter.claim(
            token, deadline, **({"preparing": True} if preparing else {}))

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
        elif kind in ("local", "grid_charge"):
            if type(value) not in (int, float) or not math.isfinite(value):
                raise NativeMaxChargeError("Native Max acceptance deadline is invalid")
            self.record["local_acceptance_until"] = max(value, self.record.get("local_acceptance_until", 0))
            if kind == "grid_charge":
                if self.record["baseline"].get("grid_charge_blocked") is not True:
                    raise NativeMaxChargeError("Grid charge restoration baseline is unavailable")
                self.record["grid_charge_intent"] = True
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
        elif kind == "normal_tariff":
            if not isinstance(value, dict) or not value or self.record.get("tariff_intent"):
                raise NativeMaxChargeError("Native Max normal tariff replacement is invalid")
            self.record["normal_tariff"] = deepcopy(value)
        elif kind == "tariff":
            if value not in ("forcing", "baseline"):
                raise NativeMaxChargeError("Native Max tariff intent is invalid")
            self.record["tariff_intent"] = value
        elif kind == "tariff_confirmed":
            self.record.pop("tariff_intent", None)
        elif kind in ("tariff_rejected", "tariff_not_submitted"):
            if value not in ("forcing", "baseline") or self.record.get("tariff_intent") != value:
                raise NativeMaxChargeError("Native Max rejected tariff intent changed")
            self.record.pop("tariff_intent")
        elif kind == "export":
            if (value not in ("never", "pv_only", "battery_ok")
                    or self.record["baseline"].get("export_rule_source") not in ("cloud", "untracked")
                    or self.record["baseline"].get("direction") != "discharge"
                    or (self.record["baseline"].get("export_permission_untracked") is True
                        and value != "battery_ok")):
                raise NativeMaxChargeError("Cloud export permission intent is invalid")
            if self.record.get("export_intent") not in (None, value):
                raise NativeMaxChargeError("An earlier cloud export write remains uncertain")
            self.record["export_intent"] = value
        elif kind in ("export_confirmed", "export_not_submitted", "export_rejected"):
            self.record.pop("export_intent", None)
        elif kind == "export_conflict":
            if type(value) is not bool or self.record["baseline"].get("export_rule_source") != "cloud":
                raise NativeMaxChargeError("Export conflict recovery state is invalid")
            self.record["export_conflict"] = value
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
            preparation = (getattr(self.adapter, "direction", "charge") == "discharge")
            if self.adapter.authority() is not True:
                raise NativeMaxChargeError("Native Max current policy denied charging")
            admission_identity = self.adapter.identity
            prepare_admission = getattr(self.adapter, "prepare_admission", None)
            if callable(prepare_admission):
                await prepare_admission()
                if (generation != self._generation or self.clock() >= deadline
                        or self.adapter.identity != admission_identity):
                    raise NativeMaxChargeError("Native Max request expired or changed before acquisition")
                if self.adapter.authority() is not True:
                    raise NativeMaxChargeError("Native Max current policy denied charging")
            initial_guard = ({"preparing": True} if preparation else {})
            if await self.adapter.guard(**initial_guard) is not True:
                raise NativeMaxChargeError("Native Max current policy denied charging")
            preparation = bool(getattr(self.adapter, "export_preparation_required", False))
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
                if await self._claim(token, deadline, preparing=preparation) is not True:
                    raise NativeMaxChargeError("Native Max site admission was denied")
                fence = await self._authorize(token, generation, preparing=preparation)
                # Record possible mutation before the first provider command.
                self.record["phase"] = "applying"
                await self._save()
                self._watch_task = asyncio.create_task(self._watch(token, generation))
                async def apply():
                    fence()  # storage/admission awaits must not extend a grant
                    prepare = getattr(self.adapter, "prepare", None)
                    if prepare is not None and await prepare(deepcopy(baseline),
                            lambda native_config=None: self._authorize(
                                token, generation, native_config=native_config,
                                preparing=preparation),
                            write_intent=self._write_intent) is not True:
                        raise NativeMaxChargeError("Native Max preparation readback was not confirmed")
                    if preparation:
                        # A fresh full guard and exact-token claim upgrade must
                        # complete before any kick/resume/tariff write.
                        await self._authorize(token, generation)
                        if await self._claim(token, deadline) is not True:
                            raise NativeMaxChargeError("Native Max coexistence admission was denied")
                    post_prepare = getattr(self.adapter, "prepare_after_claim", None)
                    if post_prepare is not None and await post_prepare(
                            deepcopy(baseline),
                            lambda native_config=None: self._authorize(
                                token, generation, native_config=native_config),
                            write_intent=self._write_intent) is not True:
                        raise NativeMaxChargeError("Native Max export permission preparation was not confirmed")
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
                            lambda native_config=None: self._authorize(
                                token, generation, native_config=native_config),
                            write_intent=self._write_intent) is not True:
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
            await self._retire_after_pairing_change()
            return
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
        revalidate = getattr(self.adapter, "restoration_tariff_current", None)
        if self.record["phase"] in ("settling", "restored") and callable(revalidate):
            current = await revalidate()
            restore_fence()
            if current is False:
                # Older journals may have released a different temporary
                # controller tariff. Reopen only that field for owned cleanup.
                self.record["phase"] = "restoring"
                self.record.setdefault("cleanup", {})["tariff_released"] = False
                await self._save()
                restore_fence()
            elif current is not True:
                raise NativeMaxRestorePending("Native Max tariff restoration readback is pending")
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
            baseline["_grid_charge_intent"] = self.record.get("grid_charge_intent", False)
            baseline["_export_intent"] = self.record.get("export_intent")
            baseline["_export_conflict"] = self.record.get("export_conflict", False)
            baseline["_normal_tariff"] = deepcopy(self.record.get("normal_tariff"))
            baseline["_session_id"] = token
            baseline["_expires_at"] = self.record["expires_at"]
            if await self.adapter.restore(baseline, before_restore,
                    write_intent=self._write_intent) is not True:
                raise NativeMaxChargeError("Native Max restore was not confirmed")
            if (self.record.get("tariff_intent") or self.record.get("export_intent")
                    or self.record.get("export_conflict")):
                raise NativeMaxChargeError("Native Max provider submission remains uncertain")
            self.record["phase"] = "settling"
            await self._save()
        if self.record["phase"] == "settling":
            if await self.adapter.settled(deepcopy(self.record["baseline"])) is not True:
                raise NativeMaxRestorePending("Native Max settings restored; physical settling is pending")
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
        await self._clear_record()

    async def _retire_after_pairing_change(self):
        """Retire a proven completed charge; never transfer old write authority."""
        site_for = getattr(self.adapter, "pairing_recovery_site", None)
        confirm = getattr(self.adapter, "pairing_recovery_complete", None)
        release = getattr(self.adapter, "release_after_pairing_change", None)
        if not callable(site_for) or not callable(confirm) or not callable(release):
            raise NativeMaxChargeError("Native Max cleanup belongs to another paired identity")
        token, original = self.record["id"], self.record["identity"]
        identity = self.adapter.identity

        def fence():
            if (not self.record or self.record["id"] != token
                    or self.record["identity"] != original or self.adapter.identity != identity
                    or not self.adapter.pairing_recovery_current()):
                raise NativeMaxChargeError("Native Max pairing recovery ownership changed")

        site = await site_for(deepcopy(self.record))
        fence()
        if not site:
            raise NativeMaxChargeError("Native Max cleanup belongs to another paired identity")
        # Preserve independent same-site proof before releasing the budget. A
        # failed journal clear must remain recoverable after that release.
        if "pairing_recovery" not in self.record:
            self.record["pairing_recovery"] = {
                "site": site, "identity": identity, "token": token, "confirmed": False}
            await self._save()
        fence()
        if await confirm(deepcopy(self.record), fence) is not True:
            raise NativeMaxRestorePending("Native Max pairing recovery readback is pending")
        fence()
        self.record["phase"] = "restored"
        self.record["pairing_recovery"]["confirmed"] = True
        self.record.setdefault("cleanup", {})["tariff_released"] = True
        await self._save()
        fence()
        if await release(deepcopy(self.record), fence) is not True:
            raise NativeMaxChargeError("Native Max site release was not confirmed")
        fence()
        await self._clear_record()

    async def _clear_record(self):
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
            restoring = False
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
                        restoring = True
                        await self._restore_locked()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                if self.record and self.record["id"] == token:
                    self.record["last_error"] = type(error).__name__
                    # A policy error ends charging; retries only restore.
                    self._generation += 1
                    self._abort_application()
                    if not restoring:
                        # Policy loss needs immediate cleanup, but a failed
                        # cleanup must not be retried again in the same tick.
                        async with self._lock:
                            try:
                                await self._restore_locked()
                            except Exception:
                                pass
                    if self.record and self.record["id"] == token:
                        # Failed readback/settling keeps the durable owner.
                        # Back off without holding the lock so explicit stop
                        # and lifecycle recovery can still run immediately.
                        await asyncio.sleep(max(30, self.interval))
