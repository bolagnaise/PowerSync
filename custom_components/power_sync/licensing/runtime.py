"""Persistent licensing client with injected storage/transport and no hardware dependency."""
from __future__ import annotations

import asyncio
import copy
import base64
import hashlib
import secrets
import time
import uuid
from contextlib import asynccontextmanager

from .admission import OptimizerAdmission
from .permission import verify_permission, PermissionError
from .policy import verify_policy

DATA_KEY = "power_sync_license_manager"
ADMISSION_KEY = "power_sync_licensing"
ACCOUNT_URL = "https://powersync.cc/app/ha"


class LicenseRuntime:
    def __init__(self, load, save, request, public_keys, *, wall_clock=time.time, monotonic=time.monotonic):
        self._load, self._save, self._request = load, save, request
        self._keys, self._wall, self._mono = public_keys, wall_clock, monotonic
        self._lock = asyncio.Lock()
        self._journal_lock = asyncio.Lock()
        self._control_locks = {}
        self._manual_command_owners = {}
        self._manual_command_depth = {}
        self.admission = OptimizerAdmission(enforced=True)
        self.policy = None
        self._policy_token = self._permission_token = None
        self.installation_id = self.credential = None
        self.paired = False
        self.minimum_epoch = self.minimum_revision = 0
        self.policy_epoch = self.policy_issued_at = 0
        self.entries = {}
        self._anchor = self._wall()
        self._anchor_mono = self._mono()
        self._policy_retry = self._permission_retry = 0
        self._last_saved_at = 0
        self.error = None
        self.storage_ok = True

    def now(self):
        # Neither an NTP correction nor a restart can roll time backwards to prolong a trial.
        return int(max(self._wall(), self._anchor + max(0, self._mono() - self._anchor_mono)))

    async def initialize(self):
        async with self._lock:
            try:
                stored = await self._load() or {}
                if not isinstance(stored, dict):
                    raise ValueError("invalid_storage")
                self.installation_id = str(uuid.UUID(stored["installation_id"])) if stored.get("installation_id") else str(uuid.uuid4())
                self.credential = stored.get("credential") or secrets.token_urlsafe(32)
                if len(self.credential) != 43:
                    raise ValueError("invalid_storage")
                self.paired = stored.get("paired") is True
                self.minimum_epoch = int(stored.get("minimum_epoch", 0))
                self.minimum_revision = int(stored.get("minimum_revision", 0))
                self.policy_epoch = int(stored.get("policy_epoch", 0))
                self.policy_issued_at = int(stored.get("policy_issued_at", 0))
                self.entries = stored.get("entries", {})
                if not isinstance(self.entries, dict) or any(not isinstance(key, str) or not isinstance(value, dict) for key, value in self.entries.items()):
                    raise ValueError("invalid_storage")
                self._anchor = max(self._wall(), float(stored.get("observed_at", 0)))
                self._anchor_mono = self._mono()
                self.admission.suspended = stored.get("suspended") is True
                self._policy_token = stored.get("policy")
                if self._policy_token:
                    # Expired policy is verified at its original issuance; it still fixes the cutoff.
                    self.policy = verify_policy(self._policy_token, self._keys, self.policy_issued_at, minimum_epoch=self.policy_epoch)
                self._permission_token = stored.get("permission")
                if self._permission_token:
                    try:
                        self.admission.replace(verify_permission(self._permission_token, self._keys, self.installation_id, self.now(),
                            minimum_epoch=max(self.minimum_epoch, self.policy_epoch), minimum_revision=self.minimum_revision))
                    except PermissionError:
                        self.admission.suspend()
                self._update_enforcement()
                await self.persist()
            except Exception:
                self.entries = {}  # Corrupt licensing data must not break free monitoring or manual controls.
                self.storage_ok = False
                self.error = "licensing_storage_unavailable"
                self.admission.enforced = True
                self.admission.suspend()

    def _update_enforcement(self):
        previous = self.admission.enforced
        self.admission.enforced = (self.policy is None or self.policy.enforces(self.now())
            or self.policy.mode == "shadow" and self.now() >= self.policy.expires_at)
        if self.admission.enforced and not previous:
            self.admission.generation += 1
            if not self.admission.permission or not self.admission.permission.allows_optimization(self.now()):
                self.admission.suspend()

    def permits(self, entry_id=None, generation=None, entry_generation=None):
        self._update_enforcement()
        state = self.entries.get(entry_id, {})
        if self.admission.enforced and self.admission.permission and self.admission.permission.epoch < self.policy_epoch:
            self.admission.suspend()
        return bool(self.storage_ok and self.admission.permits(self.now(), generation=generation)
                    and not state.get("resume_required", False) and not state.get("unloaded", False)
                    and (entry_generation is None or entry_generation == state.get("generation", 0)))

    def control_lock(self, entry_id):
        return self._control_locks.setdefault(entry_id, asyncio.Lock())

    @asynccontextmanager
    async def manual_command_scope(self, entry_id):
        """Drain optimizer writes before taking the battery route for a manual call."""
        task = asyncio.current_task()
        if self._manual_command_owners.get(entry_id) is task:
            self._manual_command_depth[entry_id] += 1
            try:
                yield
            finally:
                self._manual_command_depth[entry_id] -= 1
            return
        async with self.control_lock(entry_id):
            self._manual_command_owners[entry_id] = task
            self._manual_command_depth[entry_id] = 1
            try:
                yield
            finally:
                self._manual_command_depth.pop(entry_id, None)
                self._manual_command_owners.pop(entry_id, None)

    async def persist(self):
        observed = self.now()
        try:
            await self._save(copy.deepcopy({"installation_id": self.installation_id, "credential": self.credential,
                "paired": self.paired, "permission": self._permission_token, "policy": self._policy_token,
                "minimum_epoch": self.minimum_epoch, "minimum_revision": self.minimum_revision,
                "policy_epoch": self.policy_epoch, "policy_issued_at": self.policy_issued_at,
                "observed_at": observed, "suspended": self.admission.suspended, "entries": self.entries}))
            self._last_saved_at = observed
            if self.installation_id and self.credential:
                self.storage_ok = True
        except Exception:
            self.storage_ok = False
            self.admission.suspend()
            self.error = "licensing_storage_unavailable"
            raise

    async def maintain(self, *, force=False):
        async with self._lock:
            current = self.now()
            if force or current >= self._policy_retry:
                self._policy_retry = current + 300
                try:
                    response = await self._request("/policy", None)
                    policy = verify_policy(response["policy"], self._keys, current, minimum_epoch=self.policy_epoch)
                    if policy.epoch == self.policy_epoch and policy.issued_at < self.policy_issued_at:
                        raise PermissionError("stale_policy")
                    self.policy = policy
                    self.policy_epoch, self.policy_issued_at = policy.epoch, policy.issued_at
                    self._policy_token = response["policy"]
                    if self.admission.permission and self.admission.permission.epoch < policy.epoch:
                        self._permission_retry = 0
                    self._policy_retry = min(current + 3600, policy.expires_at)
                    self.error = None
                    await self.persist()
                except Exception:
                    self.error = "licensing_verification_unavailable"
            self._update_enforcement()
            permission = self.admission.permission
            if self.paired and (force or current >= self._permission_retry) and (force or not permission or current >= permission.refresh_after or permission.epoch < self.policy_epoch):
                self._permission_retry = current + 300
                try:
                    response = await self._request("/refresh", {"installation_id": self.installation_id, "credential": self.credential})
                    try:
                        permission = verify_permission(response["permission"], self._keys, self.installation_id, current,
                            minimum_epoch=max(self.minimum_epoch, self.policy_epoch), minimum_revision=self.minimum_revision)
                    except PermissionError:
                        # Recover from an accidentally fast clock after NTP is repaired. Only a newly
                        # signed installation revision verified against the corrected wall clock can reset it.
                        wall_now = int(self._wall())
                        if wall_now >= current - 300:
                            raise
                        permission = verify_permission(response["permission"], self._keys, self.installation_id, wall_now,
                            minimum_epoch=max(self.minimum_epoch, self.policy_epoch), minimum_revision=self.minimum_revision + 1)
                        if (permission.epoch, permission.revision) <= (self.minimum_epoch, self.minimum_revision):
                            raise PermissionError("clock_requires_new_revision")
                        self._anchor, self._anchor_mono = self._wall(), self._mono()
                        self._policy_retry = 0
                    self.admission.replace(permission)  # Denial closes admission before the next await.
                    self._permission_token = response["permission"]
                    self.minimum_epoch, self.minimum_revision = permission.epoch, permission.revision
                    self._permission_retry = permission.refresh_after
                    self.error = None
                    await self.persist()
                except Exception:
                    # A network/validation failure cannot overwrite the last signed permission or its deadline.
                    self.error = "licensing_verification_unavailable"
            self.admission.permits(current)
            if current - self._last_saved_at >= 300:
                await self.persist()

    async def start_pairing(self):
        if not self.storage_ok or not self._keys:
            raise ValueError("licensing_unavailable")
        hashed = base64.urlsafe_b64encode(hashlib.sha256(self.credential.encode()).digest()).decode().rstrip("=")
        response = await self._request("/pairing/start", {"installation_id": self.installation_id, "credential_hash": hashed})
        code = response.get("pairing_code")
        if not isinstance(code, str) or len(code) != 43:
            raise ValueError("invalid_pairing_response")
        return {"pairing_code": code, "verification_uri": ACCOUNT_URL, "expires_in": 600}

    async def finish_pairing(self, code):
        async with self._lock:
            response = await self._request("/pairing/exchange", {"pairing_code": code, "credential": self.credential})
            if response.get("paired") is not True or response.get("installation_id") != self.installation_id:
                raise ValueError("pairing_not_completed")
            self.paired = True
            await self.persist()
        await self.maintain(force=True)

    async def journal(self, entry_id):
        async with self._journal_lock:
            if not entry_id or not self.permits(entry_id):
                raise ValueError("optimizer_access_required")
            state = self.entries.setdefault(entry_id, {})
            if not state.get("recovery_pending"):
                state["recovery_pending"] = True
                await self.persist()  # Concurrent commands wait for the same durable marker.

    async def relinquish(self, entry_id):
        state = self.entries.setdefault(entry_id, {})
        state["generation"] = state.get("generation", 0) + 1
        async def relinquish_owned():
            state["recovery_pending"] = bool(state.get("ev_targets"))
            state.pop("pre_idle_reserve", None)
            state.pop("reserve_config_at_journal", None)
            try:
                await self.persist()
            except Exception:
                # persist() already closes paid admission. A local storage outage must
                # never prevent the user's own manual hardware command.
                pass
        if self._manual_command_owners.get(entry_id) is asyncio.current_task():
            await relinquish_owned()
        else:
            async with self.control_lock(entry_id):
                await relinquish_owned()

    async def resume(self, entry_id):
        if not self.storage_ok or not self.admission.permission or not self.admission.permission.allows_optimization(self.now()):
            if self.admission.enforced:
                raise ValueError("optimizer_access_required")
        if self.entries.get(entry_id, {}).get("recovery_pending"):
            raise ValueError("optimizer_recovery_pending")
        if self.admission.suspended:
            self.admission.resume(self.now())
        self.entries.setdefault(entry_id, {})["resume_required"] = False
        await self.persist()

    async def reset_pairing(self):
        async with self._lock:
            self.admission.suspend()
            self.admission.permission = None
            self._permission_token = None
            self.paired = False
            self.installation_id = str(uuid.uuid4())
            self.credential = secrets.token_urlsafe(32)
            self.minimum_epoch = self.minimum_revision = 0
            for state in self.entries.values():
                state["resume_required"] = True
            await self.persist()

    def status(self, entry_id=None):
        self._update_enforcement()
        result = self.admission.api_status(self.now())
        result["can_optimize"] = self.permits(entry_id)
        result["requires_resume"] = result["requires_resume"] or bool(self.entries.get(entry_id, {}).get("resume_required"))
        result["recovery_pending"] = bool(self.entries.get(entry_id, {}).get("recovery_pending") and not result["can_optimize"])
        result["paired"] = self.paired
        result["entry_id"] = entry_id
        result["verification_error"] = self.error
        result["legacy_access_until"] = self.policy.legacy_until if self.policy else None
        result["account_url"] = ACCOUNT_URL
        if self.policy is None:
            result["access_state"] = "checking"
        elif self.policy.mode == "enforced" and not self.policy.enforces(self.now()):
            result["access_state"] = "transition"
        return result
