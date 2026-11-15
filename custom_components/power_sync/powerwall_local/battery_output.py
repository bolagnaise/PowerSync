"""Versioned fixed-output requests, independent of legacy Force semantics.

The existing site controller owns hardware, leases and resume recovery. This
module owns request validation/idempotency and never sends a device command.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
import hashlib
import json
import math
import re
import time
from uuid import uuid4


class BatteryOutputError(ValueError):
    def __init__(self, code, message=None):
        self.code = code
        super().__init__(message or code.replace("_", " "))


def request_identity(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9._:-]{1,80}", value):
        raise BatteryOutputError("invalid_request_id")
    return value


def validate_output_request(data):
    if not isinstance(data, dict) or set(data) - {
        "power_w", "duration_seconds", "request_id", "expected_control_revision", "takeover", "entry_id", "target_soc",
    }:
        raise BatteryOutputError("invalid_battery_output_request")
    power, seconds = data.get("power_w"), data.get("duration_seconds")
    if type(power) is not int or not -10000 <= power <= 10000:
        raise BatteryOutputError("invalid_power_w")
    if "target_soc" in data:
        validate_target_soc(power, data["target_soc"])
    if type(seconds) is not int or not 1 <= seconds <= 86400:
        raise BatteryOutputError("invalid_duration_seconds")
    request_identity(data.get("request_id"))
    revision = data.get("expected_control_revision")
    if not isinstance(revision, str) or not re.fullmatch(r"[a-f0-9]{64}", revision):
        raise BatteryOutputError("invalid_control_revision")
    if "takeover" in data and type(data["takeover"]) is not bool:
        raise BatteryOutputError("invalid_takeover")
    return {key: data[key] for key in (
        "power_w", "duration_seconds", "request_id", "expected_control_revision", "takeover", "target_soc",
    ) if key in data}


def validate_target_soc(power, target):
    if type(target) is not int or not 1 <= target <= 100 or power == 0:
        raise BatteryOutputError("invalid_target_soc")


def check_target_soc(power, target, soc):
    """A supervisory SOC target never changes the signed watt command."""
    if target is None:
        return
    validate_target_soc(power, target)
    if type(soc) not in (int, float) or not math.isfinite(soc) or not 0 <= soc <= 100:
        raise BatteryOutputError("battery_soc_unavailable")
    if (power < 0 and soc >= target) or (power > 0 and soc <= target):
        raise BatteryOutputError("battery_target_reached")


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


class BatteryOutputControl:
    """Persist each request before admission and never replay its power."""

    def __init__(self, *, load, save, revision, status, start, stop, allowed, clock=time.time):
        self.load, self.save = load, save
        self.revision, self.status = revision, status
        self.start, self.stop, self.allowed, self.clock = start, stop, allowed, clock
        self.lock = asyncio.Lock()

    async def read(self):
        state = await self.load() or {}
        current = self.status()
        return {"schema_version": 1, "control_revision": self.revision(),
                "capabilities": {"battery_output": {"version": 2,
                    "supported": self.allowed(), "zero_hold_supported": self.allowed(), "target_soc_supported": self.allowed(),
                    "max_power_w": 10000}},
                "operation": self._operation(state.get("operation"), current)}

    def _operation(self, record, current):
        if not record:
            return None
        result = deepcopy(record)
        matching = current.get("operation_id") == record["operation_id"]
        result["active"] = bool(matching and current.get("active"))
        result["commanded_w"] = current.get("commanded_w") if matching and result["active"] else None
        result["command_status"] = current.get("command_status") if matching else None
        result["tracking"] = "unknown"  # Acknowledgement is not measured power.
        result["observed"] = current.get("observed")
        if matching:
            result["completion_reason"] = current.get("completion_reason") or result.get("completion_reason")
        if matching and current.get("release_pending"):
            result["phase"] = "recovery_required"
        elif result["active"]:
            result["phase"] = "active"
        elif result["phase"] == "active" and not current.get("release_pending"):
            result["phase"] = "completed"
        return result

    async def dispatch(self, data, context=None):
        request = validate_output_request(data)
        async with self.lock:
            state = await self.load() or {}
            requests = state.get("requests", {})
            old = state.get("operation")
            current = self.status()
            # Cleanup belongs to the physical controller. Once no release is
            # owed, an operation that lost ownership cannot remain live in the
            # request journal (including a crash between preparation and ACK).
            if not current.get("release_pending") and self.allowed():
                for record in requests.values():
                    if record.get("phase") not in ("completed", "rejected", "superseded") and record.get("operation_id") != current.get("operation_id"):
                        record["phase"] = "superseded"
                if old and old.get("phase") == "active" and not current.get("active"):
                    old["phase"] = "completed"
                    requests[old["request_id"]] = old
            previous = requests.get(request["request_id"])
            payload_hash = digest(request)
            if previous:
                if previous["payload_hash"] != payload_hash:
                    raise BatteryOutputError("request_id_conflict")
                return {"success": previous.get("phase") == "active", "replayed": True,
                        "control_revision": self.revision(),
                        "operation": self._operation(previous, self.status())}
            if request["expected_control_revision"] != self.revision():
                raise BatteryOutputError("control_revision_conflict")
            if not self.allowed():
                raise BatteryOutputError("battery_output_unavailable")
            current = self.status()
            if current.get("release_pending"):
                raise BatteryOutputError("previous_control_recovery_required")
            if current.get("any_active") and request.get("takeover") is not True:
                raise BatteryOutputError("explicit_takeover_required")
            operation = {**request, "operation_id": uuid4().hex, "payload_hash": payload_hash,
                         "phase": "prepared", "started_at": self.clock(),
                         "expires_at": self.clock() + request["duration_seconds"],
                         "power_target_basis": "battery_output"}
            # Keep bounded request history; never evict an unresolved request.
            requests = dict(requests)
            for key in list(requests):
                if len(requests) < 32:
                    break
                if requests[key].get("phase") in ("completed", "rejected", "superseded"):
                    requests.pop(key)
            if len(requests) >= 32:
                raise BatteryOutputError("request_history_reconciliation_required")
            requests[request["request_id"]] = operation
            state = {"requests": requests, "operation": operation}
            await self.save(deepcopy(state))
            try:
                result = await self.start(operation, context)
                if not result or result.get("success") is not True:
                    raise BatteryOutputError("battery_output_not_started")
                operation["phase"] = "active"
            except BaseException:
                operation["phase"] = ("recovery_required" if self.status().get("release_pending") else "rejected")
                await self.save(deepcopy(state))
                raise
            await self.save(deepcopy(state))
            return {"success": True, "control_revision": self.revision(),
                    "operation": self._operation(operation, self.status())}

    async def end(self, data, context=None):
        if not isinstance(data, dict) or set(data) - {
            "operation_id", "request_id", "expected_control_revision", "entry_id",
        }:
            raise BatteryOutputError("invalid_stop_request")
        request_identity(data.get("request_id"))
        async with self.lock:
            state = await self.load() or {}
            operation = state.get("operation")
            if not operation or operation["operation_id"] != data.get("operation_id"):
                raise BatteryOutputError("operation_identity_conflict")
            if operation.get("stop_request_id") == data["request_id"] and operation["phase"] == "completed":
                return {"success": True, "replayed": True, **await self.read()}
            # A stop is bound to its immutable operation, not to mutable
            # approval/policy revisions. It can never release a successor.
            current = self.status()
            if current.get("operation_id") != operation["operation_id"]:
                raise BatteryOutputError("operation_ownership_changed")
            operation.update(phase="stopping", completion_reason="stopped", stop_request_id=data["request_id"])
            state.setdefault("requests", {})[operation["request_id"]] = operation
            await self.save(deepcopy(state))
            try:
                await self.stop(operation, context)
                operation["phase"] = "completed"
            except BaseException:
                operation["phase"] = "recovery_required"
                await self.save(deepcopy(state))
                raise
            await self.save(deepcopy(state))
            return {"success": True, **await self.read()}
