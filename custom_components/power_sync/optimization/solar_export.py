"""Provider-neutral Profit Max solar-export charge-hold lifecycle."""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Any

_LOGGER = logging.getLogger(__name__)

_NO_INDEPENDENT_BLOCK = {
    "tesla": "no_independent_charge_block",
    "goodwe": "no_proven_reversible_charge_block",
    "alphaess": "no_proven_reversible_charge_block",
    "esy_sunhome": "no_independent_charge_block",
    "saj_h2": "no_proven_reversible_charge_block",
    "solaredge": "no_proven_reversible_charge_block",
    "anker_solix": "no_proven_reversible_charge_block",
    "custom": "no_safe_semantic_charge_block_configured",
}


@dataclass(frozen=True)
class SolarExportCapability:
    """Resolved hardware capability for a zero-charge solar-export hold."""

    supported: bool
    reason: str
    export_limit_kw: float | None = None
    adapter: str | None = None
    verification: str | None = None
    targets: int = 0

    def as_dict(self) -> dict[str, Any]:
        """Return JSON-safe capability diagnostics."""
        return {
            key: value
            for key, value in {
                "supported": self.supported,
                "reason": self.reason,
                "export_limit_kw": self.export_limit_kw,
                "adapter": self.adapter,
                "verification": self.verification,
                "targets": self.targets,
            }.items()
            if value is not None
        }


def _finite_positive(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) and parsed > 0 else None


def _cloud_setting_value(payload: Any) -> float | None:
    """Extract a numeric setting from the response shapes used by FoxESS."""
    if isinstance(payload, (int, float, str)):
        try:
            parsed = float(payload)
        except (TypeError, ValueError):
            return None
        return parsed if math.isfinite(parsed) and parsed >= 0 else None
    if isinstance(payload, dict):
        for key in ("value", "settingValue", "data", "result"):
            if key in payload:
                value = _cloud_setting_value(payload[key])
                if value is not None:
                    return value
    if isinstance(payload, list):
        for item in payload:
            value = _cloud_setting_value(item)
            if value is not None:
                return value
    return None


class SolarExportChargeHoldAdapter:
    """Resolve and execute an exact provider-specific charge-only primitive."""

    def __init__(self, battery_system: str, coordinator: Any) -> None:
        self.system = str(battery_system or "").lower()
        self.coordinator = coordinator
        self.key = self._adapter_key()

    def _adapter_key(self) -> str | None:
        if self.coordinator is None:
            return None
        if self.system == "sigenergy":
            return "sigenergy.modbus.charge_limit.v1"
        if self.system == "sungrow":
            if hasattr(self.coordinator, "_coord1") and hasattr(
                self.coordinator, "_coord2"
            ):
                return "sungrow.dual_modbus.charge_limit.v1"
            return "sungrow.modbus.charge_limit.v1"
        if self.system == "foxess":
            name = type(self.coordinator).__name__
            if name == "FoxESSCloudEnergyCoordinator":
                return "foxess.cloud.max_charge_current.v1"
            if name == "FoxESSEntityEnergyCoordinator":
                return "foxess.entity.max_charge_current.v1"
            if name == "FoxESSEnergyCoordinator":
                return "foxess.modbus.max_charge_current.v1"
            return None
        if self.system == "solax":
            return "solax.entity.charge_current.v1"
        if self.system == "fronius_reserva":
            return "fronius_reserva.entity.block_charging.v1"
        if self.system == "neovolt":
            return "neovolt.entity.no_battery_charge.v1"
        return None

    @property
    def available(self) -> bool:
        """Return whether this configured control plane has a known primitive."""
        return self.key is not None

    def capability(self) -> SolarExportCapability:
        """Resolve current availability without inferring it from object presence."""
        if not self.available:
            return SolarExportCapability(
                False,
                _NO_INDEPENDENT_BLOCK.get(
                    self.system, "unsupported_battery_system"
                ),
            )

        native_enabled = getattr(self.coordinator, "_native_integration_enabled", None)
        if callable(native_enabled) and not native_enabled():
            return SolarExportCapability(
                False, "native_control_unavailable", adapter=self.key
            )

        export_limit_kw = self._export_limit_kw()
        targets = self._target_count()
        if targets <= 0:
            return SolarExportCapability(
                False, "incomplete_target_discovery", adapter=self.key
            )

        if self.system in {"sigenergy", "sungrow", "foxess", "solax"}:
            if not self._has_observable_limit():
                return SolarExportCapability(
                    False,
                    "charge_limit_readback_unavailable",
                    export_limit_kw=export_limit_kw,
                    adapter=self.key,
                    verification="exact_limit_readback",
                    targets=targets,
                )
        elif self.system == "fronius_reserva":
            controller = getattr(self.coordinator, "_controller", None)
            checker = getattr(controller, "_ensure_command_entities", None)
            if callable(checker) and not checker(
                ("battery_api_mode", "storage_control_mode"),
                available_required=("battery_api_mode", "storage_control_mode"),
            ):
                return SolarExportCapability(
                    False, "charge_block_entities_unavailable", adapter=self.key
                )
            status = controller.get_status() if controller else {}
            mode = status.get("mode")
            if not mode:
                return SolarExportCapability(
                    False, "storage_mode_readback_unavailable", adapter=self.key
                )
            if "auto" not in str(mode).lower():
                return SolarExportCapability(
                    False,
                    "storage_not_in_normal_auto_mode",
                    adapter=self.key,
                    verification="exact_mode_readback",
                    targets=targets,
                )
        elif self.system == "neovolt":
            controllers = list(
                getattr(getattr(self.coordinator, "_controller", None), "_controllers", [])
            )
            modes = [c.get_dispatch_mode() for c in controllers]
            if not controllers or any(mode is None for mode in modes):
                return SolarExportCapability(
                    False, "dispatch_mode_readback_unavailable", adapter=self.key
                )
            if any(
                not {
                    "No Battery Charge",
                    "Idle (No Dispatch)",
                }.intersection(set(c._dispatch_mode_options()))
                for c in controllers
                if callable(getattr(c, "_dispatch_mode_options", None))
            ):
                return SolarExportCapability(
                    False, "charge_block_option_unavailable", adapter=self.key
                )
            if any(str(mode) != "Normal" for mode in modes):
                return SolarExportCapability(
                    False,
                    "dispatch_not_in_normal_mode",
                    adapter=self.key,
                    verification="exact_mode_readback",
                    targets=targets,
                )

        return SolarExportCapability(
            True,
            "supported",
            export_limit_kw=export_limit_kw,
            adapter=self.key,
            verification=(
                "exact_mode_readback"
                if self.system in {"fronius_reserva", "neovolt"}
                else "exact_limit_readback"
            ),
            targets=targets,
        )

    def _target_count(self) -> int:
        if self.system == "sungrow" and hasattr(self.coordinator, "_coord1"):
            return 2
        if self.system == "neovolt":
            return len(
                getattr(getattr(self.coordinator, "_controller", None), "_controllers", [])
            )
        return 1 if self.coordinator is not None else 0

    def _export_limit_kw(self) -> float | None:
        data = getattr(self.coordinator, "data", None) or {}
        limit = _finite_positive(data.get("export_limit_kw"))
        if limit is not None:
            return limit
        limit_w = _finite_positive(data.get("export_limit_w"))
        return limit_w / 1000.0 if limit_w is not None else None

    def _has_observable_limit(self) -> bool:
        if self.system == "sigenergy":
            controller = getattr(self.coordinator, "_controller", None)
            return callable(getattr(controller, "get_charge_rate_limit_kw", None))
        if self.system == "sungrow" and hasattr(self.coordinator, "_coord1"):
            return all(
                _finite_positive((getattr(child, "data", None) or {}).get(
                    "charge_rate_limit_kw"
                ))
                is not None
                for child in (self.coordinator._coord1, self.coordinator._coord2)
            )
        data = getattr(self.coordinator, "data", None) or {}
        if self.system == "sungrow":
            return _finite_positive(data.get("charge_rate_limit_kw")) is not None
        if self.system == "foxess":
            if self.key == "foxess.cloud.max_charge_current.v1":
                return callable(
                    getattr(getattr(self.coordinator, "_client", None), "get_device_setting", None)
                )
            return _finite_positive(data.get("max_charge_current_a")) is not None
        if self.system == "solax":
            controller = getattr(self.coordinator, "_controller", None)
            if controller is None:
                return False
            controller._ensure_entity_map()
            return (
                controller._entity_map.get("charge_current") is not None
                and _finite_positive(controller._read_float("charge_current")) is not None
            )
        return False

    async def prepare_charge_hold(
        self, owner_id: str, plan_generation: str
    ) -> dict[str, Any] | None:
        """Capture all restore state without performing a hardware write."""
        capability = self.capability()
        if not capability.supported:
            return None
        targets = await self._read_targets()
        if not targets or len(targets) != capability.targets:
            return None
        if self.system in {"sigenergy", "sungrow", "foxess", "solax"} and any(
            _finite_positive(target.get("value")) is None for target in targets
        ):
            return None
        return {
            "schema_version": 1,
            "adapter": self.key,
            "owner_id": owner_id,
            "plan_generation": plan_generation,
            "targets": targets,
            "verification": capability.verification,
        }

    async def apply_charge_hold(self, plan: dict[str, Any]) -> bool:
        """Apply every target and collect failures without short-circuiting."""
        if not self._plan_matches(plan):
            return False
        results: list[bool] = []
        for target in plan.get("targets", []):
            try:
                results.append(bool(await self._write_target(target, hold=True)))
            except Exception:
                _LOGGER.exception("Profit Max: charge-hold apply failed for %s", target.get("id"))
                results.append(False)
        return bool(results) and all(results)

    async def verify_charge_hold(self, plan: dict[str, Any]) -> bool:
        """Verify all targets from current provider state."""
        current = await self._read_targets()
        if len(current) != len(plan.get("targets", [])):
            return False
        if self.system == "fronius_reserva":
            return all("block charging" in str(t.get("value", "")).lower() for t in current)
        if self.system == "neovolt":
            return all(
                str(t.get("value")) in {"No Battery Charge", "Idle (No Dispatch)"}
                for t in current
            )
        return all(abs(float(t.get("value", math.inf))) <= 0.001 for t in current)

    async def clear_charge_hold(self, plan: dict[str, Any]) -> bool:
        """Clear every target, including targets whose apply outcome was unknown."""
        if plan.get("legacy_sigenergy"):
            return bool(await self.coordinator.exit_solar_export_hold(plan.get("owner_id")))
        if not self._plan_matches(plan):
            return False
        results: list[bool] = []
        for target in plan.get("targets", []):
            try:
                results.append(bool(await self._write_target(target, hold=False)))
            except Exception:
                _LOGGER.exception("Profit Max: charge-hold clear failed for %s", target.get("id"))
                results.append(False)
        return bool(results) and all(results)

    async def restore_normal(self, plan: dict[str, Any] | None) -> bool:
        """Issue an idempotent provider-normal reconciliation."""
        if plan and plan.get("legacy_sigenergy"):
            return bool(await self.coordinator.exit_solar_export_hold(plan.get("owner_id")))
        if not plan:
            normal = getattr(self.coordinator, "restore_normal", None)
            return bool(await normal()) if callable(normal) else True
        if not self._plan_matches(plan):
            return False
        # Limit primitives remain in the user's existing mode; restoring the
        # captured limit is the narrow normal-control reconciliation.
        if self.system in {"sigenergy", "sungrow", "foxess", "solax"}:
            results = []
            for target in plan.get("targets", []):
                try:
                    results.append(bool(await self._write_target(target, hold=False)))
                except Exception:
                    results.append(False)
            return bool(results) and all(results)
        normal = getattr(self.coordinator, "restore_normal", None)
        return bool(await normal()) if callable(normal) else False

    async def verify_charge_hold_cleared(self, plan: dict[str, Any]) -> bool:
        """Verify the captured normal value after compensation."""
        if plan.get("legacy_sigenergy"):
            return True  # The v2.12.1058 exit method includes exact readback.
        current = await self._read_targets()
        expected = plan.get("targets", [])
        if len(current) != len(expected):
            return False
        for actual, baseline in zip(current, expected):
            if self.system in {"fronius_reserva", "neovolt"}:
                if str(actual.get("value")) != str(baseline.get("value")):
                    return False
            else:
                try:
                    if abs(float(actual.get("value")) - float(baseline.get("value"))) > 0.01:
                        return False
                except (TypeError, ValueError):
                    return False
        return True

    def migrate_legacy_plan(self, state: dict[str, Any]) -> dict[str, Any] | None:
        """Map the v2.12.1058 Sigenergy record onto the generic identity."""
        if state.get("adapter") != "sigenergy" or self.system != "sigenergy":
            return None
        return {
            "schema_version": 1,
            "adapter": self.key,
            "owner_id": state.get("owner_id"),
            "plan_generation": state.get("plan_generation"),
            "targets": [],
            "verification": "legacy_exact_exit_readback",
            "legacy_sigenergy": True,
        }

    def _plan_matches(self, plan: dict[str, Any]) -> bool:
        return plan.get("adapter") == self.key and plan.get("schema_version") == 1

    async def _read_targets(self) -> list[dict[str, Any]]:
        if self.system == "sigenergy":
            async with self.coordinator._controller:
                value = await self.coordinator._controller.get_charge_rate_limit_kw()
            return [{"id": "ess", "value": value}] if value is not None else []

        if self.system == "sungrow":
            children = (
                [self.coordinator._coord1, self.coordinator._coord2]
                if hasattr(self.coordinator, "_coord1")
                else [self.coordinator]
            )
            targets = []
            for index, child in enumerate(children):
                await child.async_request_refresh()
                value = (child.data or {}).get("charge_rate_limit_kw")
                if value is None:
                    return []
                targets.append({"id": f"inverter_{index + 1}", "value": float(value)})
            return targets

        if self.system == "foxess":
            if self.key == "foxess.cloud.max_charge_current.v1":
                payload = await self.coordinator._client.get_device_setting(
                    "MaxSetChargeCurrent"
                )
                value = _cloud_setting_value(payload)
            elif self.key == "foxess.entity.max_charge_current.v1":
                value = self.coordinator._controller.get_status().get(
                    "max_charge_current_a"
                )
            else:
                await self.coordinator.async_request_refresh()
                value = (self.coordinator.data or {}).get("max_charge_current_a")
            return [{"id": "battery", "value": float(value)}] if value is not None else []

        if self.system == "solax":
            controller = self.coordinator._controller
            await controller._ensure_connected()
            value = controller._read_float("charge_current")
            return [{"id": "battery", "value": float(value)}] if value is not None else []

        if self.system == "fronius_reserva":
            status = self.coordinator._controller.get_status()
            value = status.get("mode")
            return [{"id": "storage", "value": value}] if value is not None else []

        if self.system == "neovolt":
            controllers = self.coordinator._controller._controllers
            values = [controller.get_dispatch_mode() for controller in controllers]
            if any(value is None for value in values):
                return []
            return [
                {"id": f"stack_{index + 1}", "value": value}
                for index, value in enumerate(values)
            ]
        return []

    async def _write_target(self, target: dict[str, Any], *, hold: bool) -> bool:
        value = 0.0 if hold else target.get("value")
        target_id = target.get("id")
        if self.system == "sigenergy":
            async with self.coordinator._controller:
                return bool(await self.coordinator._controller.set_charge_rate_limit(float(value)))
        if self.system == "sungrow":
            if hasattr(self.coordinator, "_coord1"):
                index = int(str(target_id).rsplit("_", 1)[-1]) - 1
                child = (self.coordinator._coord1, self.coordinator._coord2)[index]
                return bool(await child.set_charge_rate_limit(float(value)))
            return bool(await self.coordinator.set_charge_rate_limit(float(value)))
        if self.system == "foxess":
            return bool(await self.coordinator.set_charge_rate_limit(float(value)))
        if self.system == "solax":
            controller = self.coordinator._controller
            await controller._ensure_connected()
            await controller._set_number("charge_current", float(value))
            return True
        if self.system == "fronius_reserva":
            if hold:
                return bool(await self.coordinator._controller.block_charging())
            return bool(await self.coordinator.restore_normal())
        if self.system == "neovolt":
            index = int(str(target_id).rsplit("_", 1)[-1]) - 1
            controller = self.coordinator._controller._controllers[index]
            if hold:
                return bool(await controller.set_no_battery_charge())
            return bool(await controller.restore_normal(str(value)))
        return False


class SolarExportHoldController:
    """Own and recover the temporary zero-charge hardware limit."""

    def __init__(self, store: Any, adapter: SolarExportChargeHoldAdapter) -> None:
        self._store = store
        self._adapter = adapter
        self._state: dict[str, Any] = {}

    @property
    def active(self) -> bool:
        """Return whether a hold may still be applied to hardware."""
        return self._state.get("phase") in {
            "apply_pending",
            "active",
            "clear_pending",
        }

    @property
    def status(self) -> dict[str, Any]:
        """Return a safe copy of persisted lifecycle diagnostics."""
        return dict(self._state)

    def capability(self) -> dict[str, Any]:
        """Return current provider capability unless cleanup owns the hardware."""
        if self.active:
            return {"supported": False, "reason": "cleanup_pending"}
        return self._adapter.capability().as_dict()

    async def async_reconcile_startup(self) -> bool:
        """Clear any hold left by a restart before optimization resumes."""
        loaded = await self._store.async_load()
        self._state = dict(loaded or {})
        if not self.active:
            return True
        if not self._state.get("plan"):
            plan = self._adapter.migrate_legacy_plan(self._state)
            if plan is not None:
                self._state["plan"] = plan
                self._state["adapter"] = plan["adapter"]
                self._state["schema_version"] = 2
                await self._store.async_save(self._state)
        _LOGGER.warning(
            "Profit Max: recovering stale solar-export hold from phase %s",
            self._state.get("phase"),
        )
        return await self.clear("startup_reconciliation")

    async def apply(self, owner_id: str, plan_generation: str) -> bool:
        """Apply a hold idempotently and persist ownership around hardware I/O."""
        if (
            self._state.get("phase") == "active"
            and self._state.get("owner_id") == owner_id
        ):
            try:
                verified = await self._adapter.verify_charge_hold(
                    self._state.get("plan") or {}
                )
            except Exception as err:
                verified = False
                self._state["last_error"] = str(err)
            if verified:
                self._state["plan_generation"] = plan_generation
                self._state["operation_id"] = f"{owner_id}:{plan_generation}"
                await self._store.async_save(self._state)
                return True
            return await self._fail_and_compensate("active_verification_failed")

        try:
            plan = await self._adapter.prepare_charge_hold(owner_id, plan_generation)
        except Exception as err:
            _LOGGER.exception("Profit Max: charge-hold preparation failed")
            await self._restore_without_plan()
            self._state = {"last_error": str(err), "phase": "idle"}
            return False
        if plan is None:
            await self._restore_without_plan()
            self._state = {"last_error": "preparation_unavailable", "phase": "idle"}
            return False

        self._state = {
            "schema_version": 2,
            "phase": "apply_pending",
            "owner_id": owner_id,
            "plan_generation": plan_generation,
            "operation_id": f"{owner_id}:{plan_generation}",
            "adapter": plan.get("adapter"),
            "plan": plan,
        }
        await self._store.async_save(self._state)
        try:
            applied = bool(await self._adapter.apply_charge_hold(plan))
            verified = applied and bool(await self._adapter.verify_charge_hold(plan))
        except Exception as err:
            verified = False
            self._state["last_error"] = str(err)

        if verified:
            self._state["phase"] = "active"
            self._state.pop("last_error", None)
            await self._store.async_save(self._state)
            return True
        return await self._fail_and_compensate("apply_or_verification_failed")

    async def _fail_and_compensate(self, reason: str) -> bool:
        self._state["phase"] = "clear_pending"
        self._state.setdefault("last_error", reason)
        await self._store.async_save(self._state)
        await self.clear(reason)
        return False

    async def _restore_without_plan(self) -> bool:
        """Best-effort normal reconciliation when preparation produced no plan."""
        try:
            return bool(await self._adapter.restore_normal(None))
        except Exception:
            _LOGGER.exception(
                "Profit Max: provider-normal fallback failed after preparation"
            )
            return False

    async def clear(self, reason: str) -> bool:
        """Restore normal charge capacity; retain retry state on failure."""
        if not self.active:
            return True
        self._state["phase"] = "clear_pending"
        self._state["clear_reason"] = reason
        await self._store.async_save(self._state)
        plan = self._state.get("plan") or {}
        try:
            cleared = bool(await self._adapter.clear_charge_hold(plan))
        except Exception as err:
            cleared = False
            self._state["last_error"] = str(err)
        try:
            restored = bool(await self._adapter.restore_normal(plan))
        except Exception as err:
            restored = False
            self._state["last_error"] = str(err)
        try:
            verified = bool(
                await self._adapter.verify_charge_hold_cleared(plan)
            )
        except Exception as err:
            verified = False
            self._state["last_error"] = str(err)

        if cleared and restored and verified:
            self._state = {}
            await self._store.async_save(self._state)
            return True

        self._state.setdefault("last_error", "clear_or_restore_failed")
        await self._store.async_save(self._state)
        return False


def resolve_solar_export_adapter(
    battery_system: str, coordinator: Any
) -> SolarExportChargeHoldAdapter:
    """Resolve one explicit adapter identity for the configured control plane."""
    return SolarExportChargeHoldAdapter(battery_system, coordinator)
