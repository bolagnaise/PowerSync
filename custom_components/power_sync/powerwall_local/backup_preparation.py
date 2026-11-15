"""Expose the existing owned Max Backup lifecycle without replaying starts."""
import asyncio
from copy import deepcopy
import time

from .battery_output import digest, request_identity


def validate_backup_preparation(config, snapshot):
    from .battery_dispatch import site_grid_limits_w, _number, _percent
    if (snapshot.grid_status != "SystemGridConnected" or snapshot.grid_charging_enabled is not True
            or config.get("optimization_allow_grid_charge", True) is not True
            or _number(config.get("optimization_max_charge_w", 5000), "charge limit") <= 0
            or _percent(config.get("optimization_grid_charge_soc_cap", 1), "charge ceiling") < 100):
        raise ValueError("Backup charging is not permitted by the current settings")
    if not isinstance((getattr(snapshot, "raw", {}) or {}).get("config", {}).get("site_info"), dict):
        raise ValueError("Backup preparation requires fresh native configuration")
    imported, exported = site_grid_limits_w(snapshot)
    if imported == 0:
        raise ValueError("The native import limit blocks backup charging")
    for native, key, recorded in ((imported, "optimization_max_grid_import_w", "native_site_import_cap_w"),
                                  (exported, "optimization_max_grid_export_w", "native_site_export_cap_w")):
        bounds = [config.get(recorded)]
        configured = config.get(key)
        if configured is not None and (key.endswith("export_w") or configured != 0):
            bounds.append(configured)
        for value in bounds:
            if value is not None and (native is None or native > _number(value, "site limit")):
                raise ValueError("Backup preparation requires these site limits to be enforced natively")


class BackupPreparationControl:
    def __init__(self, *, load, save, status, start, stop, allowed, prepare=None, reconcile=None):
        self.load, self.save, self.status = load, save, status
        self.start, self.stop, self.allowed = start, stop, allowed
        self.prepare, self.reconcile = prepare, reconcile
        self.lock = asyncio.Lock()

    async def read(self):
        operation = await self.status()
        pending = any(r.get("phase") == "prepared" for r in (await self.load() or {}).values())
        return {"schema_version": 1, "capabilities": {"supported": True,
            "readable": operation.get("readable", True), "writable": self.allowed() and operation.get("can_start") and not pending and not operation.get("operation")},
            **operation, "recovery_required": pending, "physical_effect_verified": False}

    async def execute(self, data, context=None):
        if not isinstance(data, dict) or set(data) - {"operation", "request_id", "duration_minutes", "operation_id", "entry_id"}:
            raise ValueError("Invalid backup request")
        request_identity(data.get("request_id"))
        operation = data.get("operation", "start")
        if operation not in ("start", "cancel", "reconcile"):
            raise ValueError("Invalid backup operation")
        async with self.lock:
            requests = await self.load() or {}
            fingerprint = digest({k: v for k, v in data.items() if k != "entry_id"})
            old = requests.get(data["request_id"])
            if old:
                if old["payload_hash"] != fingerprint:
                    raise ValueError("Backup request ID conflict")
                return {"success": old["phase"] in ("completed", "active"), "replayed": True, **await self.read()}
            status = await self.read()
            owned = status.get("operation")
            if operation == "reconcile":
                if self.reconcile is not None:
                    await self.reconcile(context)
                remaining = (await self.read()).get("operation")
                if remaining and remaining["phase"] != "active":
                    raise ValueError("Backup readback remains unconfirmed")
                for record in requests.values():
                    if record["phase"] == "prepared":
                        record["phase"] = "active" if remaining and record.get("operation_id") == remaining["operation_id"] else "rejected"
                requests[data["request_id"]] = {"payload_hash": fingerprint, "phase": "completed"}
                await self.save(deepcopy(requests))
                return {"success": True, **await self.read()}
            if operation == "cancel":
                if owned:
                    if operation == "cancel" and data.get("operation_id") != owned["operation_id"]:
                        raise ValueError("Backup operation changed")
                    await self.stop(owned["operation_id"], context)
                remaining = (await self.read()).get("operation")
                if remaining:
                    raise ValueError("Backup cleanup remains pending")
                for record in requests.values():
                    if record["phase"] == "prepared":
                        record["phase"] = "completed"
                requests[data["request_id"]] = {"payload_hash": fingerprint, "phase": "completed"}
                await self.save(deepcopy(requests))
                return {"success": True, **await self.read()}
            minutes = data.get("duration_minutes")
            if type(minutes) is not int or not 1 <= minutes <= 1440:
                raise ValueError("Backup duration must be 1 to 1440 whole minutes")
            if owned or any(r["phase"] == "prepared" for r in requests.values()) or not self.allowed():
                raise ValueError("Backup control is owned or unavailable")
            if self.prepare is not None:
                await self.prepare(minutes, context)
                if not self.allowed() or (await self.status()).get("operation"):
                    raise ValueError("Backup authority changed during preparation")
            for key in list(requests):
                if len(requests) < 32:
                    break
                if requests[key]["phase"] != "prepared":
                    requests.pop(key)
            requests[data["request_id"]] = {"payload_hash": fingerprint, "phase": "prepared", "started_at": time.time()}
            try:
                await self.save(deepcopy(requests))
                await self.start(minutes, context, requests[data["request_id"]]["started_at"])
            except BaseException:
                # The existing service persists its cleanup intent before any
                # actuator call. No owned record means this refusal sent none.
                owned_after = (await self.status()).get("operation")
                if not owned_after:
                    requests[data["request_id"]]["phase"] = "rejected"
                else:
                    requests[data["request_id"]]["operation_id"] = owned_after["operation_id"]
                await self.save(deepcopy(requests))
                raise
            result = await self.read()
            if not result.get("operation") or result["operation"]["phase"] != "active":
                if not result.get("operation"):
                    requests[data["request_id"]]["phase"] = "rejected"
                    await self.save(deepcopy(requests))
                raise ValueError("Backup preparation readback is unconfirmed")
            requests[data["request_id"]]["phase"] = "active"
            requests[data["request_id"]]["operation_id"] = result["operation"]["operation_id"]
            await self.save(deepcopy(requests))
            return {"success": True, "verification": "gateway_event_readback", **result}
