"""Verified hybrid control through the upstream GoodWe integration's client.

The entity registry selects the device; entity state is never register readback.
This adapter borrows the upstream client and never connects or disconnects it.
Only PowerSync commands are serialized here. Modbus provides no cross-controller
compare-and-set, so other EMS writers must not run concurrently with PowerSync.
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
import logging
import math
from typing import Any

from .goodwe_battery import (
    GoodWeBatteryController,
    CURTAILMENT_METHOD_EMS_LOAD_FOLLOWING,
    CURTAILMENT_METHOD_EXPORT_LIMIT,
)

_LOGGER = logging.getLogger(__name__)
_TIMEOUT = 5.0
_MODES = {"auto": 1, "charge_pv": 2, "conserve": 6, "buy_power": 9,
          "sell_power": 10, "charge_battery": 11, "discharge_battery": 12}


class GoodWeSharedControlError(RuntimeError):
    """The selected upstream client cannot safely perform this operation."""


class GoodWeSharedController(GoodWeBatteryController):
    """Share an identity-bound GoodWe client with HA entity telemetry."""

    entity_ems_control = True

    def __init__(self, hass: Any, prefix: str, telemetry: Any, *,
                 curtailment_method: str = CURTAILMENT_METHOD_EXPORT_LIMIT,
                 entry_id: str = "",
                 control_allowed: Callable[[], bool]) -> None:
        super().__init__("", curtailment_method=curtailment_method)
        self.hass = hass
        self.prefix = prefix
        self.entry_id = entry_id
        self._recovery_issue = False
        self.telemetry = telemetry
        self._control_allowed = control_allowed
        self._command_lock = asyncio.Lock()
        self._operation_client = None
        self._recovering = False
        self._last_native_ems: tuple[int, int] | None = None
        self._binding: dict[str, str] | None = None
        self._saved_binding: dict[str, str] | None = None
        self._write_attempted = False
        self._invalid_snapshot: dict[str, Any] | None = None
        self._persist_callback: Callable[[Any], Awaitable[None]] | None = None
        self._curtailment_allowed: Callable[[], bool] = lambda: False

    def set_restore_persistence(self, callback: Callable[[Any], Awaitable[None]],
                                allowed: Callable[[], bool]) -> None:
        """Install the entry's durable store before allowing curtailment."""
        self._persist_callback = callback
        self._curtailment_allowed = allowed

    def _resolve(self, *, check_recovery: bool = True) -> tuple[Any, dict[str, str]]:
        from homeassistant.helpers import device_registry as dr, entity_registry as er

        registry = er.async_get(self.hass)
        ids = [f"select.{self.prefix}_ems_mode", f"number.{self.prefix}_ems_power_limit"]
        # Include the actual resolved telemetry sensors, not a guessed prefix.
        self.telemetry.telemetry_ready()
        ids.extend(self.telemetry.control_binding_entities())
        entries = [registry.async_get(entity_id) for entity_id in ids]
        if not entries or any(e is None for e in entries):
            raise GoodWeSharedControlError("GoodWe EMS/telemetry entity registry is incomplete")
        first = entries[0]
        config_id, device_id = first.config_entry_id, first.device_id
        if not config_id or not device_id or any(
            e.config_entry_id != config_id or e.device_id != device_id
            or e.platform != "goodwe" for e in entries
        ):
            raise GoodWeSharedControlError("GoodWe EMS and telemetry must belong to the same device and entry")
        entry = self.hass.config_entries.async_get_entry(config_id)
        if entry is None or entry.domain != "goodwe" or getattr(entry.state, "value", entry.state) != "loaded":
            raise GoodWeSharedControlError("The selected GoodWe integration is not loaded")
        runtime = getattr(entry, "runtime_data", None)
        if runtime is None:
            runtime = self.hass.data.get("goodwe", {}).get(config_id)
        inverter = getattr(runtime, "inverter", None)
        device = dr.async_get(self.hass).async_get(device_id)
        serial = str(getattr(inverter, "serial_number", "") or "").strip()
        if not serial or device is None or ("goodwe", serial) not in device.identifiers:
            raise GoodWeSharedControlError("GoodWe runtime and selected device identity do not match")
        binding = {"config_entry_id": config_id, "device_id": device_id, "serial": serial}
        if check_recovery and (self._invalid_snapshot is not None or (self._saved_binding and binding != self._saved_binding)):
            self._sync_recovery_issue(True)
            raise GoodWeSharedControlError("Saved GoodWe recovery belongs to a different or unproven device binding")
        if check_recovery:
            self._sync_recovery_issue(False)
        return inverter, binding

    def _sync_recovery_issue(self, active: bool) -> None:
        if active == self._recovery_issue:
            return
        self._recovery_issue = active
        try:
            from homeassistant.helpers import issue_registry as ir

            issue_id = f"goodwe_recovery_{self.entry_id or self.prefix}"
            if active:
                ir.async_create_issue(self.hass, "power_sync", issue_id,
                    is_fixable=False, severity=ir.IssueSeverity.WARNING,
                    translation_key="goodwe_recovery_identity")
            else:
                ir.async_delete_issue(self.hass, "power_sync", issue_id)
        except (ImportError, AttributeError):
            _LOGGER.debug("GoodWe recovery issue registry is not available")

    def control_surface_ready(self) -> bool:
        try:
            self._resolve()
            return True
        except Exception:
            return False

    async def connect(self) -> bool:
        inverter, binding = self._resolve()
        if self._operation_client is not None and inverter is not self._operation_client:
            raise GoodWeSharedControlError("GoodWe runtime was replaced during the command")
        if self._command_lock.locked():
            self._operation_client = inverter
        self._inverter, self._binding = inverter, binding
        return True

    @asynccontextmanager
    async def _transaction(self):
        async with self._command_lock:
            try:
                yield
            finally:
                self._operation_client = None

    async def disconnect(self) -> None:
        """Release our reference; upstream owns its transport and polling."""
        self._inverter = None

    def _check_binding(self) -> None:
        inverter, binding = self._resolve()
        if inverter is not self._inverter or binding != self._binding:
            raise GoodWeSharedControlError("GoodWe integration changed during the command; refusing stale client")

    async def _call(self, method: str, *args: Any, write: bool = False) -> Any:
        self._check_binding()
        if write and not self._recovering and not self._control_allowed():
            raise GoodWeSharedControlError("GoodWe telemetry/control is not ready")
        result = await asyncio.wait_for(getattr(self._inverter, method)(*args), timeout=_TIMEOUT)
        self._check_binding()
        return result

    async def get_runtime_data(self) -> dict[str, Any]:
        inverter, _binding = self._resolve(check_recovery=False)
        data = self.telemetry.get_runtime_data()
        data["rated_power_w"] = getattr(inverter, "rated_power", None)
        return data

    def _require_settings(self, names: dict[str, int]) -> None:
        settings = {s.id_: s for s in self._inverter.settings()}
        if any(name not in settings or settings[name].offset != address
               for name, address in names.items()):
            raise GoodWeSharedControlError("This GoodWe model does not expose the required hybrid register map")
        if "ems_power_limit" in names and settings["ems_power_limit"].unit != "W":
            raise GoodWeSharedControlError("GoodWe EMS power target must use watts")

    def _require_transport(self) -> None:
        if any(not callable(getattr(self._inverter, name, None)) for name in (
            "_read_command", "_write_multi_command", "_read_from_socket",
        )):
            raise GoodWeSharedControlError("GoodWe shared control requires FC03 readback and documented FC16 writes")

    async def _read(self, name: str) -> int:
        value = await self._call("read_setting", name)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise GoodWeSharedControlError(f"GoodWe {name} readback is unavailable")
        if int(value) != value or not -32768 <= value <= 65535:
            raise GoodWeSharedControlError(f"GoodWe {name} readback is invalid")
        return int(value) & 0xFFFF

    async def _read_ems_setting(self, name: str) -> int:
        return await self._read(name)

    async def _read_grid_export_setting(self, name: str) -> int:
        return await self._read(name)

    async def _read_register(self, register: int) -> int:
        return await self._read(f"modbus_{register}")

    async def _write_pair(self, address: int, first: int, second: int) -> None:
        self._check_binding()
        multi = getattr(self._inverter, "_write_multi_command", None)
        if not callable(multi) or not callable(getattr(self._inverter, "_read_from_socket", None)):
            raise GoodWeSharedControlError("GoodWe shared control requires documented FC16 writes")
        frame = multi(address, int(first).to_bytes(2, "big") + int(second).to_bytes(2, "big"))
        await self._call("_read_from_socket", frame, write=True)

    async def _write_ems(self, mode: int, power_w: int) -> None:
        await self._write_pair(47511, mode, power_w)

    async def _ems_pair(self) -> tuple[int, int]:
        return await self._read_pair(47511)

    async def _export_pair(self) -> tuple[int, int]:
        return await self._read_pair(47509)

    async def _read_pair(self, address: int) -> tuple[int, int]:
        self._check_binding()
        frame = self._inverter._read_command(address, 2)
        response = await self._call("_read_from_socket", frame)
        raw = response.response_data()
        if not isinstance(raw, bytes) or len(raw) != 4:
            raise GoodWeSharedControlError("GoodWe register pair readback is malformed")
        return int.from_bytes(raw[:2], "big"), int.from_bytes(raw[2:], "big")

    async def _verify_ems(self, expected_mode: int, expected_power: int) -> bool:
        return await self._ems_pair() == (expected_mode, expected_power)

    async def _verify_grid_export_settings(self, enabled: int, limit: int) -> bool:
        actual_enabled, actual_limit = await self._export_pair()
        return actual_enabled == enabled and (not enabled or actual_limit == limit)

    def get_grid_export_restore_state(self) -> dict[str, Any] | None:
        if self._invalid_snapshot is not None:
            return dict(self._invalid_snapshot)
        state = super().get_grid_export_restore_state()
        return dict(state, binding=dict(self._saved_binding), write_attempted=self._write_attempted) if state and self._saved_binding else None

    def restore_grid_export_restore_state(self, state: Any) -> bool:
        # Entity-route snapshots always carry identity. Never migrate a direct
        # controller's legacy snapshot to a different connection implicitly.
        binding = state.get("binding") if isinstance(state, dict) else None
        if (not isinstance(binding, dict) or type(state.get("write_attempted")) is not bool
                or set(binding) != {"config_entry_id", "device_id", "serial"}
                or any(not isinstance(v, str) or not v for v in binding.values())):
            self._invalid_snapshot = dict(state) if isinstance(state, dict) else {"invalid": True}
            return True  # Retain Pending/recovery evidence; all controls refuse.
        method = state.get("method", CURTAILMENT_METHOD_EXPORT_LIMIT)
        fields = ("ems_mode", "ems_power_limit") if method == CURTAILMENT_METHOD_EMS_LOAD_FOLLOWING else ("grid_export", "grid_export_limit")
        if method not in {CURTAILMENT_METHOD_EXPORT_LIMIT, CURTAILMENT_METHOD_EMS_LOAD_FOLLOWING} or any(
            type(state.get(k)) is not int or not 0 <= state[k] <= 65535 for k in fields
        ) or (fields[0] == "ems_mode" and state[fields[0]] != 1) or (fields[0] == "grid_export" and state[fields[0]] not in (0, 1)):
            self._invalid_snapshot = dict(state)
            return True
        self._saved_binding = dict(binding)
        self._write_attempted = state["write_attempted"]
        return super().restore_grid_export_restore_state(state)

    async def _persist(self, state: Any) -> None:
        if self._persist_callback is None:
            raise GoodWeSharedControlError("GoodWe durable recovery store is not ready")
        await self._persist_callback(state)
        self._check_binding()

    async def _clear_claim(self) -> None:
        await self._persist(None)
        self._saved_ems_mode = self._saved_ems_power_limit = None
        self._saved_grid_export_enabled = self._saved_grid_export_limit = None
        self._saved_method = self._saved_binding = None
        self._grid_export_state_saved = False
        self._write_attempted = False

    async def curtail(self) -> bool:
        async with self._transaction():
            try:
                await self.connect()
                if not self._curtailment_allowed() or not self._control_allowed():
                    return False
                if self._grid_export_state_saved and not self._write_attempted:
                    await self._clear_claim()
                    return False
                if self._grid_export_state_saved and self._saved_method != self.curtailment_method:
                    await self._release()
                    return False
                ems = self.uses_ems_load_following
                self._require_transport()
                self._require_settings({"ems_mode": 47511, "ems_power_limit": 47512})
                if await self._read_register(35187) != 1:
                    raise GoodWeSharedControlError("GoodWe curtailment requires on-grid operation")
                mode, target = await self._ems_pair()
                export_mode_allowed = mode == 1 or (
                    (mode, target) == self._last_native_ems and mode in {2, 6, 11}
                )
                if ems and await self._read_register(47505) != 10:
                    raise GoodWeSharedControlError("GoodWe load following requires existing EMS fallback 10; it will not be changed")
                if ems:
                    selector = self.hass.states.get(f"select.{self.prefix}_ems_mode")
                    number = self.hass.states.get(f"number.{self.prefix}_ems_power_limit")
                    if (selector is None or "buy_power" not in selector.attributes.get("options", [])
                            or number is None or float(number.attributes.get("min", 1)) > 0):
                        raise GoodWeSharedControlError("GoodWe entity controls do not advertise BuyPower with a zero-watt target")
                if not self._grid_export_state_saved:
                    if (ems and mode != 1) or (not ems and not export_mode_allowed):
                        raise GoodWeSharedControlError("GoodWe EMS is externally owned; refusing curtailment takeover")
                    self._saved_binding = dict(self._binding)
                    self._saved_method = self.curtailment_method
                    if ems:
                        self._saved_ems_mode, self._saved_ems_power_limit = mode, target
                    else:
                        self._require_settings({"grid_export": 47509, "grid_export_limit": 47510})
                        enabled, limit = await self._export_pair()
                        if enabled not in (0, 1):
                            raise GoodWeSharedControlError("Invalid GoodWe export enable readback")
                        self._saved_grid_export_enabled, self._saved_grid_export_limit = enabled, limit
                    self._grid_export_state_saved = True
                    await self._persist(self.get_grid_export_restore_state())
                    # No recovery write is attempted on a pre-write refusal.
                    if await self._ems_pair() != (mode, target):
                        raise GoodWeSharedControlError("GoodWe EMS changed before curtailment; refusing first write")
                    if not ems and await self._export_pair() != (enabled, limit):
                        raise GoodWeSharedControlError("GoodWe export limit changed before curtailment; refusing first write")
                elif ems and (mode, target) != (9, 0):
                    # Do not fight an upstream/native controller on a retry.
                    await self._clear_claim()
                    return False
                elif not ems and (not export_mode_allowed or await self._export_pair() != (1, 0)):
                    # A changed EMS owner does not transfer ownership of our
                    # separate export-limit bank. Restore that bank if it is
                    # still ours, without changing the foreign EMS command.
                    await self._release()
                    return False
                if not self._curtailment_allowed():
                    return False
                previous_attempt = self._write_attempted
                self._write_attempted = True
                try:
                    await self._persist(self.get_grid_export_restore_state())
                except BaseException:
                    self._write_attempted = previous_attempt
                    raise
                still_current = (
                    self._curtailment_allowed()
                    and await self._ems_pair() == (mode, target)
                    and await self._read_register(35187) == 1
                    and (not ems or await self._read_register(47505) == 10)
                    and (ems or await self._export_pair() == ((enabled, limit) if not previous_attempt else (1, 0)))
                )
                if not still_current:
                    self._write_attempted = previous_attempt
                    await self._persist(self.get_grid_export_restore_state())
                    return False
                if ems:
                    await self._write_ems(9, 0)
                    return await self._verify_ems(9, 0)
                await self._write_pair(47509, 1, 0)
                return await self._verify_grid_export_settings(1, 0)
            except Exception as exc:
                _LOGGER.warning("GoodWe shared curtailment deferred: %s", exc)
                return False

    async def _release(self, allow_zero_export_limit: bool = True) -> bool:
        if not self._grid_export_state_saved:
            return self._invalid_snapshot is None
        if not self._write_attempted:
            await self._clear_claim()
            return True
        if self._saved_method == CURTAILMENT_METHOD_EMS_LOAD_FOLLOWING:
            current = await self._ems_pair()
            baseline = (self._saved_ems_mode, self._saved_ems_power_limit)
            if current == (9, 0) and current != baseline:
                await self._restore_pair(47511, *baseline)
                if not await self._verify_ems(*baseline):
                    return False
        else:
            current = await self._export_pair()
            baseline = (self._saved_grid_export_enabled, self._saved_grid_export_limit)
            if current == (1, 0) and current != baseline:
                enabled, limit = baseline
                if not allow_zero_export_limit and enabled and not limit:
                    enabled, limit = 0, 65535
                await self._restore_pair(47509, enabled, limit)
                if not await self._verify_grid_export_settings(enabled, limit):
                    return False
        # Original state is idempotent recovery. A different, freshly read state
        # is a foreign takeover: retire our claim without overwriting it.
        await self._clear_claim()
        return True

    async def _restore_pair(self, address: int, first: int, second: int) -> None:
        # Fresh exact-device ownership readback above authorizes only restoring
        # our saved baseline, even while ordinary startup/monitoring gates hold
        # new commands. This is never an override for a new control request.
        self._recovering = True
        try:
            await self._write_pair(address, first, second)
        finally:
            self._recovering = False

    async def restore(self, *, allow_zero_export_limit: bool = True) -> bool:
        async with self._transaction():
            try:
                await self.connect()
                return await self._release(allow_zero_export_limit)
            except Exception as exc:
                _LOGGER.warning("GoodWe shared recovery remains pending: %s", exc)
                return False

    async def set_ems_mode(self, option: str, power_w: float,
                           fallback_option: str | None = None,
                           reset_power_limit: bool = False) -> bool:
        if option == "buy_power":
            return await self.curtail() if power_w == 0 and self.curtailment_method == CURTAILMENT_METHOD_EMS_LOAD_FOLLOWING else False
        async with self._transaction():
            try:
                await self.connect()
                self._require_transport()
                self._require_settings({"ems_mode": 47511, "ems_power_limit": 47512})
                state = self.hass.states.get(f"select.{self.prefix}_ems_mode")
                options = state.attributes.get("options", []) if state else []
                selected = next((o for o in (option, fallback_option) if o in options and o in _MODES), None)
                if selected is None or not math.isfinite(power_w) or power_w < 0:
                    return False
                if selected not in {"auto", "conserve"} and power_w <= 0:
                    return False
                if not self._control_allowed() or not await self._release():
                    return False
                _, current_power = await self._ems_pair()
                if power_w > 0 or reset_power_limit:
                    rated = float(getattr(self._inverter, "rated_power", 0) or 0)
                    number = self.hass.states.get(f"number.{self.prefix}_ems_power_limit")
                    maximum = float(number.attributes.get("max", 0)) if number else 0
                    if not math.isfinite(rated) or not math.isfinite(maximum) or rated <= 0 or maximum <= 0:
                        return False
                    target = int(min(power_w or rated, rated, maximum, 32768))
                    if target <= 0:
                        return False
                else:
                    target = 0 if selected == "buy_power" else current_power
                if selected != option:
                    _LOGGER.info("GoodWe EMS using supported %s instead of %s", selected, option)
                await self._write_ems(_MODES[selected], target)
                confirmed = await self._verify_ems(_MODES[selected], target)
                self._last_native_ems = (_MODES[selected], target) if confirmed else None
                return confirmed
            except Exception as exc:
                _LOGGER.warning("GoodWe shared EMS command not confirmed: %s", exc)
                return False

    async def set_backup_reserve(self, percent: int) -> bool:
        async with self._transaction():
            try:
                await self.connect()
                if type(percent) not in (int, float) or not math.isfinite(percent) or not 0 <= percent <= 100:
                    return False
                dod = 100 - int(percent)
                current = await self._call("get_ongrid_battery_dod")
                if current != dod:
                    await self._call("set_ongrid_battery_dod", dod, write=True)
                return await self._call("get_ongrid_battery_dod") == dod
            except Exception as exc:
                _LOGGER.warning("GoodWe reserve write not confirmed: %s", exc)
                return False

    async def get_backup_reserve(self) -> int:
        async with self._transaction():
            await self.connect()
            return 100 - int(await self._call("get_ongrid_battery_dod"))

    async def _apply_operation_mode(self, *args: Any, **kwargs: Any) -> bool:
        raise GoodWeSharedControlError("Shared GoodWe control uses EMS registers, never UDP operation modes")

    async def force_charge(self, power_pct: int = 100, soc_target: int = 100) -> bool:
        inverter, _binding = self._resolve()
        power = float(getattr(inverter, "rated_power", 0) or 0) * power_pct / 100
        return await self.set_ems_mode("charge_pv", power, fallback_option="charge_battery")

    async def force_discharge(self, power_pct: int = 100, soc_floor: int = 10) -> bool:
        inverter, _binding = self._resolve()
        power = float(getattr(inverter, "rated_power", 0) or 0) * power_pct / 100
        return await self.set_ems_mode("discharge_battery", power, fallback_option="sell_power")

    async def restore_normal(self) -> bool:
        return await self.set_ems_mode("auto", 0, reset_power_limit=True)

    async def release_ems_hold(self) -> bool:
        return await self.restore()

    async def set_grid_export_limit(self, watts: int) -> bool:
        async with self._transaction():
            try:
                await self.connect()
                self._require_settings({"grid_export_limit": 47510})
                if type(watts) is not int or not 0 <= watts <= 65535:
                    return False
                if not self._control_allowed() or not await self._release():
                    return False
                await self._call("set_grid_export_limit", watts, write=True)
                return await self._read("grid_export_limit") == watts
            except Exception as exc:
                _LOGGER.warning("GoodWe export limit not confirmed: %s", exc)
                return False
