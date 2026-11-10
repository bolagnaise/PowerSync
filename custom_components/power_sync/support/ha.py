"""HA lifecycle/local consent adapter; remote reads receive primitive snapshots only."""
from __future__ import annotations

import asyncio
from collections import deque
from contextlib import suppress
from datetime import datetime, timezone
import json
import math
import time
from pathlib import Path

from aiohttp import ClientTimeout, web
from homeassistant.components.http import HomeAssistantView
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store

from .collector import snapshot, select, action_event
from .history import Recorder
from .protocol import BROKER, CATEGORIES, MAX_RESULT, ROLES, SupportError, digest, encode, reference, number, CONTRACT_DIGEST
from .session import Session

DATA = "power_sync_support"


def stamp(value):
    return value.timestamp() if isinstance(value, datetime) and value.tzinfo else None


def state_unit(state):
    """Copy cached string/StrEnum units into the closed primitive projection."""
    value = state.attributes.get("unit_of_measurement")
    return str.__str__(value) if isinstance(value, str) else None


def fields(obj):
    """Read stored fields only, never properties or an application API getter."""
    return obj if type(obj) is dict else vars(obj) if hasattr(type(obj), "__dict__") and hasattr(obj, "__dict__") else {}


def customer_support_identity(value):
    """Project staff email identities without changing signed authority or receipts."""
    if type(value) is dict:
        return {key: "dev@powersync.cc" if key == "actor" and isinstance(item, str) and "@" in item
            else customer_support_identity(item) for key, item in value.items()}
    if type(value) is list:
        return [customer_support_identity(item) for item in value]
    return value


class SupportRuntime:
    def __init__(self, hass, entry):
        self.hass, self.entry = hass, entry
        self.session = Session()
        self.recorder = Recorder()
        self.store = Store(hass, 1, f"power_sync.support.{entry.entry_id}", private=True, atomic_writes=True)
        self.recording = self.persistent = False
        self.frame = None
        self.manifest = {}
        self.mapping_digest = None
        self.closed = False
        self.task = self.transport_task = None
        self.descriptor = None
        self.code = None
        self.policy = None
        self.error = None
        self.last_saved = 0
        self.archives = {}
        self.receipt = []
        self.save_lock = asyncio.Lock()
        self.unsubs = []
        self.revocations = []
        self.pending_plans = deque(maxlen=4)
        self.observer_owner = None
        self.ready = False
        self.archive_packets = {}

    def resolve_manifest(self):
        from homeassistant.helpers import entity_registry as er
        result = {}
        registry = er.async_get(self.hass)
        for entity in registry.entities.values():
            if entity.config_entry_id != self.entry.entry_id or entity.disabled_by:
                continue
            for role in ROLES:
                if entity.entity_id.startswith("sensor.") and (entity.unique_id == f"{self.entry.entry_id}_{role}"):
                    result[role] = entity.entity_id
        # External configured sensors are allowed only for these reviewed roles.
        options = {**self.entry.data, **self.entry.options}
        for role, keys in {"grid_power": ("cloud_flow_grid_entity", "grid_power_entity"),
                "solar_power": ("cloud_flow_solar_entity", "solar_power_entity"),
                "battery_power": ("cloud_flow_battery_power_entity",),
                "battery_level": ("cloud_flow_battery_soc_entity",),
                "home_load": ("cloud_flow_load_entity",)}.items():
            for key in keys:
                entity_id = options.get(key)
                if isinstance(entity_id, str) and entity_id.startswith("sensor.") and entity_id in registry.entities:
                    result.setdefault(role, entity_id)
        return result

    def capture(self):
        manifest = self.resolve_manifest()
        revision = digest(manifest)
        if self.mapping_digest is not None and revision != self.mapping_digest:
            self.session.close("mapping_changed")
            self.recorder = Recorder(max_bytes=self.recorder.max_bytes)
            self.pending_plans.clear()
            self.dismiss_notification()
        self.manifest, self.mapping_digest = manifest, revision
        entities = {}
        for role, entity_id in manifest.items():
            state = self.hass.states.get(entity_id)
            if state is None:
                continue
            try:
                value = float(state.state)
            except (ValueError, TypeError):
                value = None
            entities[role] = {"value": value, "unit": state_unit(state),
                "source_time": None, "reported_at": stamp(getattr(state, "last_reported", None) or state.last_updated)}
        runtime = self.hass.data.get("power_sync", {}).get(self.entry.entry_id, {})
        opt = fields(runtime.get("optimization_coordinator"))
        schedule = fields(opt.get("_current_schedule"))
        slots = []
        for index, action in enumerate(schedule.get("actions", [])[:576]):
            raw = dict(fields(action))
            raw["timestamp"] = stamp(raw.get("timestamp"))
            for target, source in (("import_price", "_last_import_prices"), ("export_price", "_last_export_prices"), ("solar_kw", "_last_solar_forecast"), ("load_kw", "_last_load_forecast")):
                values = opt.get(source)
                if isinstance(values, list) and index < len(values):
                    raw[target] = number(values[index])
            slots.append(raw)
        raw_settings = {**self.entry.data, **self.entry.options}
        config = fields(opt.get("_config"))
        price = fields(opt.get("price_coordinator"))
        price_data = price.get("data") if type(price.get("data")) is dict else {}
        octopus = price_data.get("octopus") if type(price_data.get("octopus")) is dict else {}
        monitoring = (raw_settings.get("monitoring_mode") is True or runtime.get("_monitoring_handoff_active") is True
            or opt.get("battery_system") == "custom" or price.get("external_battery_control") is True
            or octopus.get("external_battery_control") is True
            or (opt.get("battery_system") == "amber_smartshift" and raw_settings.get("amber_smartshift_managed") is not True))
        state = {**config, **fields(opt.get("_last_optimizer_result")), "enabled": opt.get("_enabled"), "monitoring_mode": monitoring,
            "planned_action": opt.get("_last_executed_planned_action"), "effective_action": opt.get("_last_executed_action")}
        force = dict(fields(opt.get("_optimizer_force_state")))
        if isinstance(force.get("expires_at"), datetime):
            force["expires_at"] = stamp(force["expires_at"])
        self.frame = snapshot({"entities": entities, "schedule": slots, "optimizer": state,
            "settings": {**raw_settings, **config}, "force": force,
            "plan_time": stamp(opt.get("_last_update_time"))}, captured_at=time.time(), version=self.version)
        return self.frame

    def publish_plan(self, schedule, imports, exports, solar, load, config, result):
        # Called only at the existing solve commit. Primitive bounded copies only;
        # no HA registry, locks, serialization, tasks, disk or network on this path.
        if not self.recording and not (self.session.state == "active" and "optimizer" in self.session.scope.get("categories", [])):
            return
        if len(self.pending_plans) == self.pending_plans.maxlen:
            self.recorder.dropped += 1
            return
        from .collector import project_fields
        slots = []
        for index, action in enumerate(fields(schedule).get("actions", [])[:576]):
            row = project_fields(fields(action))
            row["timestamp"] = stamp(fields(action).get("timestamp"))
            for name, values in (("import_price", imports), ("export_price", exports), ("solar_kw", solar), ("load_kw", load)):
                row[name] = number(values[index]) if isinstance(values, list) and index < len(values) else None
            slots.append(row)
        self.pending_plans.append({"at": time.time(), "schedule": slots, "settings": project_fields(fields(config)),
            "state": project_fields(fields(result))})

    def drain_plans(self):
        while self.pending_plans:
            value = self.pending_plans.popleft()
            plan = snapshot({"schedule": value["schedule"], "settings": value["settings"], "optimizer": value["state"],
                "plan_time": value["at"]}, captured_at=value["at"], version=self.version)
            plan["optimizer"][0].update(input_provenance="solve_commit", input_bundle_complete=False, price_unit="currency/kWh")
            # The bundle records reviewed core inputs, not all provider/EV internals.
            self.frame["optimizer"] = plan["optimizer"]
            if self.recording or self.session.state == "active":
                self.recorder.append(plan)

    async def setup(self):
        self.version = json.loads(await self.hass.async_add_executor_job((Path(__file__).parent.parent / "manifest.json").read_text))["version"]
        stored = await self.store.async_load() or {}
        if type(stored) is not dict:
            stored = {}
        self.recording, self.persistent = stored.get("recording") is True, stored.get("persistent") is True
        self.archives = {key: value for key, value in (stored.get("archives") or {}).items() if type(value) is dict
            and set(value) <= {"session_id", "revoke_token", "expires_at", "pending", "shared", "confirmed"} and value.get("session_id") == key} if type(stored.get("archives")) is dict else {}
        self.receipt = stored.get("receipt", [])[-32:] if type(stored.get("receipt")) is list else []
        self.revocations = [x for x in stored.get("revocations", [])[:8] if type(x) is dict and set(x) == {"session_id", "revoke_token", "expires_at"}]
        self.capture()
        if self.recording and self.persistent and stored.get("mapping_digest") == self.mapping_digest:
            # Only restore data written by the typed collector, not arbitrary storage dictionaries.
            from .collector import restore_frame
            def restore():
                recorder = Recorder(max_bytes=self.recorder.max_bytes)
                recorder.restore([restore_frame(x) for x in stored.get("records", [])[:6000]], time.time())
                return recorder
            self.recorder = await self.hass.async_add_executor_job(restore)
        self.capture()
        coordinator = self.hass.data.get("power_sync", {}).get(self.entry.entry_id, {}).get("optimization_coordinator")
        if coordinator is not None:
            coordinator._support_plan_observer = self.publish_plan
            self.observer_owner = coordinator
        self.ready = True
        # Lifetime loops must not enter HA's startup task barrier.
        self.task = self.hass.async_create_background_task(
            self.record_loop(), f"power_sync_support_recorder_{self.entry.entry_id}")
        for event_type in ("power_sync_optimizer_action", "power_sync_ev_command"):
            self.unsubs.append(self.hass.bus.async_listen(event_type, self.observe))

    def observe(self, event):
        # HA dispatches these existing events independently of the actuator.
        permitted = self.session.state == "active" and bool({"control_trace", "logs"} & set(self.session.scope.get("categories", [])))
        if self.closed or not (self.recording or permitted):
            return
        raw = event.data
        if raw.get("entry_id") != self.entry.entry_id:
            return
        try:
            timestamp = datetime.fromisoformat(raw["timestamp"]).timestamp()
            if not math.isfinite(timestamp) or abs(time.time() - timestamp) > 300:
                return
            kind = "optimizer_action" if event.event_type == "power_sync_optimizer_action" else "ev_command"
            projected = action_event(raw, kind, timestamp)
            frame = {"schema_version": 1, "captured_at": time.time(), "source": "support_recorder", "entities": [],
                "control_trace": [projected], "logs": [projected]}
            self.recorder.append(frame)
        except Exception:
            self.recorder.dropped += 1

    async def request(self, path, body=None, *, token=None):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        async with async_get_clientsession(self.hass).request("GET" if body is None else "POST", BROKER + path,
                json=body, headers=headers, allow_redirects=False, timeout=ClientTimeout(total=12)) as response:
            chunks, size = [], 0
            while chunk := await response.content.read(8192):
                size += len(chunk)
                if size > 16384:
                    raise SupportError("invalid_broker_response")
                chunks.append(chunk)
            raw = b"".join(chunks)
            if len(raw) > 16384:
                raise SupportError("support_service_unavailable")
            data = json.loads(raw)
            if response.status in {403, 404} and type(data) is dict and data.get("error") in {"session_closed", "session_not_found", "pairing_expired"} and path.endswith("/poll"):
                return {"closed": True}
            if response.status != 200:
                raise SupportError("support_service_unavailable")
            if type(data) is not dict:
                raise SupportError("invalid_broker_response")
            return data

    async def start(self, owner, body):
        for runtime in self.hass.data.get(DATA, {}).values():
            if runtime is not self and runtime.session.key is not None:
                raise SupportError("another_session_active")
        if self.session.key is not None:
            self.session.close("replaced")
        if self.transport_task:
            self.transport_task.cancel()
            await asyncio.gather(self.transport_task, return_exceptions=True)
            self.transport_task = None
        await self.drain_archives()
        await self.drain_revocations()
        if self.revocations:
            raise SupportError("previous_revocation_pending")
        if len(self.archives) >= 8:
            raise SupportError("revoke_previous_archives_first")
        self.capture()
        policy = await self.request("/support/v1/policy")
        if not isinstance(policy.get("disclosure"), str) or not isinstance(policy.get("policy_digest"), str):
            raise SupportError("support_policy_unavailable")
        now = time.time()
        if number(body.get("history_seconds", 0)) is None or not 0 <= body.get("history_seconds", 0) <= 86400:
            raise SupportError("invalid_history_range")
        for other in self.hass.data.get(DATA, {}).values():
            if other is not self and other.session.key is not None:
                raise SupportError("another_session_active")
        scope = self.session.begin(owner, reference(self.entry.entry_id + self.mapping_digest), body.get("categories"),
            now - body.get("history_seconds", 0), body.get("duration", 3600), include_recorder=body.get("ha_recorder") is True)
        generation = self.session.generation
        self.policy = policy
        self.descriptor = self.code = None
        try:
            reply = await self.request("/support/v1/register", {**scope, "policy_digest": policy["policy_digest"], "contract_digest": CONTRACT_DIGEST, "invitation_token": body.get("invitation_token")})
        except (Exception, asyncio.CancelledError):
            # No pairing code or poller exists yet. Return to preparation rather
            # than leaving an unusable pending session after a rejected/lost reply.
            if generation == self.session.generation:
                self.session.close("registration_failed")
            raise
        self.session.check(active=False)
        if generation != self.session.generation or self.closed:
            raise SupportError("session_replaced")
        if not isinstance(reply.get("token"), str) or not isinstance(reply.get("code"), str):
            self.session.close("invalid_registration")
            raise SupportError("invalid_registration")
        self.session.token, self.code = reply["token"], reply["code"]
        if not isinstance(reply.get("live_revoke_token"), str) or not isinstance(reply.get("archive_revoke_token"), str):
            self.session.close("invalid_registration")
            raise SupportError("invalid_registration")
        self.revocations.append({"session_id": self.session.id, "revoke_token": reply["live_revoke_token"], "expires_at": self.session.scope["expires_at"]})
        self.archives[self.session.id] = {"session_id": self.session.id, "revoke_token": reply["archive_revoke_token"],
            "expires_at": self.session.scope["expires_at"] + 86400, "pending": False, "shared": False}
        # Delete-only capabilities are durable before an archive may ever be uploaded.
        await self.save()
        self.error = None
        self.transport_task = self.hass.async_create_background_task(
            self.poll_loop(generation), f"power_sync_support_poll_{self.entry.entry_id}")

    async def confirm(self, owner, descriptor_digest):
        if self.descriptor is None or self.policy is None:
            raise SupportError("invitation_not_claimed")
        if descriptor_digest != digest(self.descriptor):
            raise SupportError("invitation_changed_review_again")
        generation, token = self.session.generation, self.session.token
        packet = self.session.confirm(owner, self.descriptor, policy_digest=self.policy["policy_digest"])
        reply = await self.request("/support/v1/activate", {"session_id": self.session.id, **packet}, token=token)
        if generation != self.session.generation or reply.get("scope_digest") != self.session.scope_digest:
            self.session.close("activation_mismatch")
            raise SupportError("activation_mismatch")
        self.session.activated()
        self.code = None
        self.error = None
        from homeassistant.components import persistent_notification
        persistent_notification.async_create(self.hass,
            "Read-only PowerSync support is active. Review or revoke it in PowerSync Support.",
            title="PowerSync support access", notification_id=f"power_sync_support_{self.entry.entry_id}")

    async def revoke(self):
        old_id, token = getattr(self.session, "id", None), self.session.token
        self.session.close("revoked")  # Close before any await, including a failed offline revoke.
        if not self.recording:
            self.recorder = Recorder(max_bytes=self.recorder.max_bytes)
        if self.transport_task:
            self.transport_task.cancel()
            await asyncio.gather(self.transport_task, return_exceptions=True)
            self.transport_task = None
        self.code = None
        self.descriptor = None
        await self.save()
        await self.drain_revocations()
        if self.revocations:
            self.error = "stopped_locally_central_confirmation_pending"
        from homeassistant.components import persistent_notification
        persistent_notification.async_dismiss(self.hass, f"power_sync_support_{self.entry.entry_id}")

    async def share_archive(self, owner, duration):
        self.session.check()
        if owner != self.session.owner or type(duration) is not int or not 60 <= duration <= 86400 or "history" not in self.session.scope["categories"]:
            raise SupportError("archive_consent_denied")
        generation, session_id, token = self.session.generation, self.session.id, self.session.token
        previous = self.archives.get(session_id)
        if previous and previous.get("shared"):
            packet = self.archive_packets.get(session_id)
            if previous.get("pending") or previous.get("confirmed") or packet is None:
                raise SupportError("archive_already_shared_or_revoked")
            reply = await self.request("/support/v1/archive", {"session_id": session_id, **packet}, token=token)
            previous["confirmed"] = True
            previous["expires_at"] = reply["archive_expires_at"]
            self.archive_packets.pop(session_id, None)
            await self.save()
            if generation != self.session.generation:
                previous["pending"] = True
                await self.save()
                await self.drain_archives()
            return
        now = time.time()
        q = {"category": "history", "start": max(self.session.scope["history_start"], now - 3600), "end": now, "source": "support_recorder"}
        result = await self.collect(q)
        from .protocol import validate_evidence
        validate_evidence(result)
        if generation != self.session.generation:
            raise SupportError("session_replaced")
        self.session.check()
        packet = self.session.sign({"kind": "archive", "session_id": self.session.id, "scope_digest": self.session.scope_digest,
            "result": result, "result_digest": digest(result), "expires_at": now + duration})
        record = self.archives.get(session_id)
        if record is None or record.get("pending") or record.get("shared"):
            raise SupportError("archive_already_shared_or_revoked")
        self.archive_packets[session_id] = packet
        record["shared"] = True  # Immutable, one archive per session; uncertain uploads remain revocable.
        await self.save()
        if generation != self.session.generation or record.get("pending"):
            raise SupportError("session_replaced")
        reply = await self.request("/support/v1/archive", {"session_id": session_id, **packet}, token=token)
        record["expires_at"] = reply["archive_expires_at"]
        record["confirmed"] = True
        self.archive_packets.pop(session_id, None)
        await self.save()
        if generation != self.session.generation:
            record["pending"] = True
            await self.save()
            await self.drain_archives()
            raise SupportError("session_replaced")

    async def revoke_archive(self):
        # Revoke all separately retained archives, including uncertain uploads.
        for record in self.archives.values():
            record["pending"] = True
        await self.save()
        await self.drain_archives()
        if any(record.get("pending") for record in self.archives.values()):
            self.error = "archive_revocation_pending"

    async def drain_archives(self):
        changed = False
        active_id = self.session.id if self.session.key is not None else None
        for session_id, record in list(self.archives.items()):
            if (not record.get("shared") and session_id != active_id) or number(record.get("expires_at")) is None or record["expires_at"] <= time.time():
                if self.archives.get(session_id) is record:
                    del self.archives[session_id]
                    self.archive_packets.pop(session_id, None)
                    changed = True
                continue
            if not record.get("pending"):
                continue
            try:
                reply = await self.request("/support/v1/archive-revoke", {"session_id": session_id}, token=record["revoke_token"])
                self.receipt = reply.get("receipt", self.receipt)[-32:]
            except Exception:
                continue
            if self.archives.get(session_id) is record:
                del self.archives[session_id]
                self.archive_packets.pop(session_id, None)
                changed = True
        if not any(x.get("pending") for x in self.archives.values()) and self.error == "archive_revocation_pending":
            self.error = None
        if changed:
            await self.save()

    async def drain_revocations(self):
        active_id = self.session.id if self.session.key is not None else None
        for item in list(self.revocations):
            if item["session_id"] == active_id:
                continue
            if number(item.get("expires_at")) is None or item["expires_at"] <= time.time():
                self.revocations.remove(item)
                continue
            try:
                reply = await self.request("/support/v1/end-live", {"session_id": item["session_id"]}, token=item["revoke_token"])
                self.receipt = reply.get("receipt", self.receipt)[-32:]
            except Exception:
                continue
            if item in self.revocations:
                self.revocations.remove(item)
        if not self.revocations and self.error == "stopped_locally_central_confirmation_pending":
            self.error = None
        await self.save()

    async def save(self):
        async with self.save_lock:
            # Snapshot immutable byte rows on the HA loop; decode on an executor.
            rows = tuple(self.recorder._rows) if self.recording and self.persistent else ()
            def decode_rows():
                return [json.loads(raw) for _, raw in rows]
            records = await self.hass.async_add_executor_job(decode_rows)
            await self.store.async_save({"recording": self.recording, "persistent": self.persistent,
                "mapping_digest": self.mapping_digest, "records": records, "archives": self.archives,
                "revocations": self.revocations, "receipt": self.receipt})

    async def record_loop(self):
        while not self.closed:
            try:
                await self.drain_archives()
                if self.revocations and self.session.key is None:
                    await self.drain_revocations()
                if not self.recording and self.session.key is None:
                    self.recorder = Recorder(max_bytes=self.recorder.max_bytes)
                if self.recording or self.session.key is not None:
                    frame = self.capture()
                    self.drain_plans()
                    if self.recording:
                        self.recorder.append(frame)
                    elif self.session.state == "active" and "history" in self.session.scope["categories"]:
                        approved = set(self.session.scope["categories"]) | {"schema_version", "captured_at", "source", "coherence", "installation_assurance", "truncated"}
                        self.recorder.append({key: value for key, value in frame.items() if key in approved})
                    if self.recording and self.persistent and time.monotonic() - self.last_saved >= 60:
                        await self.save()
                        self.last_saved = time.monotonic()
            except Exception:
                self.error = "local_diagnostics_unavailable"
            await asyncio.sleep(30)

    async def collect(self, q):
        self.drain_plans()
        if q["category"] in {"history", "control_trace", "logs"}:
            if q.get("source") == "ha_recorder":
                if not self.session.scope.get("ha_recorder") or "entities" not in self.session.scope.get("categories", []):
                    raise SupportError("category_denied")
                return await self.recorder_import(q)
            result = self.recorder.window(q["start"], q["end"], category=None if q["category"] == "history" else q["category"])
            if q["category"] == "history":
                approved = set(self.session.scope["categories"]) - {"history"}
                base = {"schema_version", "captured_at", "source", "coherence", "installation_assurance", "truncated"}
                result["records"] = [{key: value for key, value in row.items() if key in base | approved} for row in result["records"]]
            return result
        return select(self.frame, q["category"])

    async def poll_loop(self, generation):
        delay = 1
        while not self.closed and generation == self.session.generation and self.session.key is not None:
            try:
                self.session.check(active=False)
                reply = await self.request("/support/v1/poll", {"session_id": self.session.id}, token=self.session.token)
                self.session.check(active=False)
                if generation != self.session.generation:
                    return
                if reply.get("closed"):
                    self.session.close("broker_revoked")
                    self.dismiss_notification()
                    return
                self.receipt = reply.get("receipt", self.receipt)[-32:]
                self.descriptor = reply.get("descriptor")
                if self.session.state == "active":
                    # Recheck mapping before every disclosure, including releases.
                    if digest(self.resolve_manifest()) != self.mapping_digest:
                        self.capture()
                        self.dismiss_notification()
                        return
                    for challenge in reply.get("releases", [])[:1]:
                        if generation != self.session.generation:
                            return
                        packet = self.session.release(challenge)
                        await self.request("/support/v1/release", {"session_id": self.session.id, **packet}, token=self.session.token)
                    for job in reply.get("jobs", [])[:1]:
                        q, packet = self.session.admit(job)
                        if packet is None:
                            result = await asyncio.wait_for(self.collect(q), timeout=5)
                            if generation != self.session.generation:
                                return
                            packet = self.session.seal(job, result)
                        self.session.check()
                        await self.request("/support/v1/upload", {"session_id": self.session.id, **packet}, token=self.session.token)
                delay = 1
                self.error = None
            except asyncio.CancelledError:
                raise
            except Exception:
                self.error = "support_connection_unavailable"
                delay = min(60, delay * 2)
            if self.session.key is None:
                self.dismiss_notification()
                return
            await asyncio.sleep(delay)
        if generation == self.session.generation:
            self.dismiss_notification()

    def dismiss_notification(self):
        from homeassistant.components import persistent_notification
        persistent_notification.async_dismiss(self.hass, f"power_sync_support_{self.entry.entry_id}")

    async def recorder_import(self, q):
        from homeassistant.components.recorder import get_instance
        from homeassistant.components.recorder.history import state_changes_during_period
        from homeassistant.helpers import entity_registry as er
        from functools import partial
        registry = er.async_get(self.hass)
        ids = []
        for entity_id in self.manifest.values():
            row = registry.entities.get(entity_id)
            # Historical identity must be established, not inferred from today's name.
            created, modified = stamp(getattr(row, "created_at", None)), stamp(getattr(row, "modified_at", None))
            if created is not None and modified is not None and created <= q["start"] and modified <= q["start"]:
                ids.append(entity_id)
        if not ids:
            return {"category": "history", "source": "ha_recorder", "availability": "unsupported", "reason": "historical_identity_unverified", "records": []}
        def read():
            records, size = [], 0
            for role, entity_id in self.manifest.items():
                if entity_id not in ids[:7]:
                    continue
                data = state_changes_during_period(self.hass, datetime.fromtimestamp(q["start"], timezone.utc),
                    datetime.fromtimestamp(q["end"], timezone.utc), entity_id=entity_id, no_attributes=False,
                    include_start_time_state=False, limit=1000)
                for state in data.get(entity_id, [])[:1000]:
                    try:
                        value = float(state.state)
                    except (ValueError, TypeError):
                        continue
                    frame = snapshot({"entities": {role: {"value": value, "unit": state_unit(state),
                        "source_time": None, "reported_at": stamp(state.last_updated)}}}, captured_at=stamp(state.last_updated) or q["start"], version=self.version)
                    for row in frame["entities"]:
                        size += len(encode(row))
                        if size > 160 * 1024:
                            return {"category": "history", "source": "ha_recorder", "records": records, "truncated": True, "availability": "available"}
                        records.append(row)
            return {"category": "history", "source": "ha_recorder", "records": records, "availability": "available" if records else "not_recorded"}
        return await get_instance(self.hass).async_add_executor_job(read)

    async def stop(self):
        self.closed = True
        self.session.close("unloaded")
        self.dismiss_notification()
        if self.observer_owner is not None and fields(self.observer_owner).get("_support_plan_observer") == self.publish_plan:
            del self.observer_owner._support_plan_observer
        self.pending_plans.clear()
        for unsubscribe in self.unsubs:
            with suppress(Exception):
                unsubscribe()
        self.unsubs.clear()
        for task in (self.task, self.transport_task):
            if task:
                task.cancel()
        await asyncio.gather(*(t for t in (self.task, self.transport_task) if t), return_exceptions=True)
        if self.ready and self.recording and self.persistent:
            with suppress(Exception):
                await self.save()

    def status(self):
        status = self.session.status()
        if status["state"] == "closed":
            self.code = self.descriptor = None
        return customer_support_identity({**status, "code": self.code, "invitation": self.descriptor, "descriptor_digest": digest(self.descriptor) if self.descriptor else None, "policy": self.policy,
            "recording": self.recording, "persistent": self.persistent, "error": self.error,
            "recorder_bytes": self.recorder.bytes, "dropped_records": self.recorder.dropped, "access_receipt": list(self.receipt), "archive_expires_at": max((x["expires_at"] for x in self.archives.values() if x.get("confirmed")), default=None),
            "archive_present": any(x.get("shared") for x in self.archives.values()),
            "archive_uncertain": any(x.get("shared") and not x.get("confirmed") for x in self.archives.values()),
            "archive_pending": any(x.get("pending") for x in self.archives.values())})


class SupportView(HomeAssistantView):
    url = "/api/power_sync/support"
    name = "api:power_sync:support"
    requires_auth = True

    def __init__(self, hass):
        self.hass = hass

    async def get(self, request):
        if not request.get("hass_user") or not request["hass_user"].is_admin:
            return web.json_response({"error": "admin_required"}, status=403)
        return web.json_response({"entries": [{"entry_id": key, "status": runtime.status()} for key, runtime in self.hass.data.get(DATA, {}).items()]})

    async def post(self, request):
        user = request.get("hass_user")
        if not user or not user.is_admin:
            return web.json_response({"error": "admin_required"}, status=403)
        try:
            chunks, length = [], 0
            while chunk := await request.content.read(4097):
                length += len(chunk)
                if length > 4096:
                    raise SupportError("request_too_large")
                chunks.append(chunk)
            raw = b"".join(chunks)
            if len(raw) > 4096:
                raise SupportError("request_too_large")
            body = json.loads(raw)
            if type(body) is not dict or set(body) - {"action", "entry_id", "categories", "history_seconds", "duration", "ha_recorder", "recording", "persistent", "invitation_token", "descriptor_digest"}:
                raise SupportError("invalid_request")
            runtime = self.hass.data.get(DATA, {}).get(body.get("entry_id"))
            if runtime is None:
                raise SupportError("entry_unavailable")
            action = body.get("action")
            if action == "start":
                await runtime.start(user.id, body)
            elif action == "confirm":
                await runtime.confirm(user.id, body.get("descriptor_digest"))
            elif action == "revoke":
                await runtime.revoke()
            elif action == "archive":
                await runtime.share_archive(user.id, body.get("duration", 3600))
            elif action == "archive_revoke":
                await runtime.revoke_archive()
            elif action == "recording":
                if type(body.get("recording")) is not bool or type(body.get("persistent")) is not bool:
                    raise SupportError("invalid_recording_consent")
                runtime.recording, runtime.persistent = body["recording"], body["persistent"] and body["recording"]
                if not runtime.recording:
                    runtime.recorder = Recorder(max_bytes=runtime.recorder.max_bytes)
                await runtime.save()
            elif action == "bundle":
                frame = runtime.capture()
                return web.json_response({"snapshot": frame, "history": runtime.recorder.window(time.time() - 3600, time.time())})
            else:
                raise SupportError("unsupported_action")
            return web.json_response(runtime.status())
        except SupportError as err:
            return web.json_response({"error": str(err)}, status=409)
        except Exception:
            return web.json_response({"error": "support_unavailable"}, status=503)


async def setup(hass, entry):
    runtimes = hass.data.setdefault(DATA, {})
    runtime = SupportRuntime(hass, entry)
    try:
        await asyncio.wait_for(runtime.setup(), timeout=5)
        if not hass.data.get("power_sync_support_view_registered"):
            hass.http.register_view(SupportView(hass))
            hass.data["power_sync_support_view_registered"] = True
        runtimes[entry.entry_id] = runtime
    except BaseException:
        await asyncio.wait_for(runtime.stop(), timeout=5)
        raise
    for value in runtimes.values():
        value.recorder.max_bytes = 8 * 1024 * 1024 // len(runtimes)
        value.recorder.trim(time.time())


async def unload(hass, entry):
    if runtime := hass.data.get(DATA, {}).pop(entry.entry_id, None):
        await asyncio.wait_for(runtime.stop(), timeout=5)


async def remove(hass, entry):
    """Erase local evidence and attempt remote cleanup without restoring read access."""
    runtime = SupportRuntime(hass, entry)
    stored = await runtime.store.async_load() or {}
    if type(stored) is not dict:
        stored = {}
    runtime.revocations = stored.get('revocations', [])[:8]
    runtime.archives = stored.get('archives', {}) if type(stored.get('archives')) is dict else {}
    for record in runtime.archives.values():
        record['pending'] = True
    # Erase evidence before fallible remote work. Only delete capabilities remain.
    await runtime.save()
    with suppress(Exception):
        async with asyncio.timeout(10):
            await runtime.drain_revocations()
            await runtime.drain_archives()
    if not runtime.revocations and not runtime.archives:
        await runtime.store.async_remove()
    else:
        from homeassistant.components import persistent_notification
        persistent_notification.async_create(hass,
            'Local support access and history were removed. Central deletion could not be confirmed. '
            'Contact PowerSync support to revoke the session; existing live access cannot release new data, '
            'and a separately approved archive expires within its original deadline (at most 24 hours).',
            title='PowerSync support cleanup pending', notification_id=f'power_sync_support_removal_{entry.entry_id}')
