"""Renewable local Powerwall dispatch leases for force and optimizer controls.

Thanks to Scotty (cool.scotty) and Brett (Teslemetry). Scotty identified and
tested the timed local command. Unlike tariff-based
dispatch, it requests battery watts directly. Every renewal uses fresh local
telemetry and the caller's current safety/ownership gates.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from contextlib import suppress
from typing import Any

class DispatchCommandFailed(Exception):
    """Local access/command failed; fallback is safe only after release."""


_LOGGER = logging.getLogger(__name__)
LEASE_SECONDS = 30
RENEW_SECONDS = 10


def _number(value: Any, label: str) -> float:
    if isinstance(value, bool) or value is None:
        raise ValueError(f"Missing or invalid {label}")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"Non-finite {label}")
    return result


def _percent(value: Any, label: str) -> float:
    value = _number(value, label)
    if value <= 1:
        value *= 100
    return max(0.0, min(100.0, value))


def validate_dispatch_config(mode: str, config: dict) -> None:
    """Known policy denials apply even when local telemetry is unavailable."""
    if mode not in ("charge", "discharge"):
        raise ValueError("Unknown dispatch mode")
    maximum = _number(config.get(f"optimization_max_{mode}_w", 5000), f"{mode} limit")
    if maximum <= 0:
        raise ValueError(f"{mode} limit blocks battery dispatch")
    if mode == "charge":
        if config.get("optimization_allow_grid_charge", True) is not True:
            raise ValueError("Grid charging is disabled")
        if _percent(config.get("optimization_grid_charge_soc_cap", 1), "charge ceiling") <= 0:
            raise ValueError("Charge ceiling blocks battery dispatch")
    else:
        limit = config.get("optimization_max_grid_export_w")
        if limit is not None and _number(limit, "grid export limit") <= 0:
            raise ValueError("Grid export limit blocks battery dispatch")


def site_grid_limits_w(snapshot: Any) -> tuple[float | None, float | None]:
    """Read signed Tesla site-meter limits in kW from this fresh snapshot.

    Missing fields and Tesla's 1e9 sentinel mean unset. A failed config read
    cannot establish that limits are unset; stop rather than erase a cap.
    """
    raw = getattr(snapshot, "raw", {}) or {}
    config = raw.get("config", {})
    if not isinstance(config, dict):
        raise ValueError("Gateway site limits unavailable")
    info = config.get("site_info", {})
    if not isinstance(info, dict):
        raise ValueError("Gateway site limits unavailable")

    def bound(key: str, sign: int) -> float | None:
        value = info.get(key)
        if value is None:
            return None
        watts = _number(value, "gateway site limit")
        if watts * sign < 0:
            raise ValueError("Invalid gateway site limit sign")
        return None if abs(watts) >= 1e9 else abs(watts) * 1000

    return bound("max_site_meter_power_ac", 1), bound("min_site_meter_power_ac", -1)



def manual_power_limits_kw(snapshot: Any, config: dict, learned_discharge_w: float | None = None, learned_charge_w: float | None = None) -> dict[str, float]:
    """UI bounds: configured site export first, learned battery bound otherwise."""
    site_import, site_export = site_grid_limits_w(snapshot)
    load, solar = _number(snapshot.load_w, "home power"), _number(snapshot.solar_w, "solar power")
    charge = _number(config.get("optimization_max_charge_w", 5000), "charge limit")
    if learned_charge_w is not None:
        charge = min(charge, _number(learned_charge_w, "gateway charge limit"))
    discharge = _number(config.get("optimization_max_discharge_w", 5000), "discharge limit")
    user_import = config.get("optimization_max_grid_import_w")
    if user_import not in (None, 0):
        site_import = min(site_import if site_import is not None else math.inf, _number(user_import, "import limit"))
    if site_import is not None:
        charge = min(charge, site_import - load + solar)
    user_export = config.get("optimization_max_grid_export_w")
    if user_export is not None:
        site_export = min(site_export if site_export is not None else math.inf, _number(user_export, "export limit"))
    if learned_discharge_w is not None:
        discharge = min(discharge, _number(learned_discharge_w, "gateway discharge limit"))
    export = (site_export if site_export is not None else discharge + solar - load) if discharge > 0 else 0
    return {"charge_input_max_kw": math.floor(max(0, charge) + 1e-6) / 1000,
            "grid_export_max_kw": math.floor(max(0, export) + 1e-6) / 1000}


def battery_watts(mode: str, target_w: float, snapshot: Any, config: dict, *, target_basis: str = "battery") -> float:
    """Convert a grid-export target or charging rate into safe battery watts.

    Zero target is the existing manual 'device maximum' sentinel, resolved by
    the caller. Positive discharge includes home load less solar generation.
    The export envelope clamps target_w before this function is called.
    """
    validate_dispatch_config(mode, config)
    if target_basis not in ("battery", "grid_import") or (target_basis == "grid_import" and mode != "charge"):
        raise ValueError("Invalid dispatch target basis")
    target = _number(target_w, "power target")
    soc = _number(snapshot.soc, "battery SOC")
    if not 0 <= soc <= 100:
        raise ValueError("Battery SOC is out of range")
    if snapshot.grid_status != "SystemGridConnected":
        raise ValueError("Local dispatch requires confirmed grid connection")
    if target <= 0:
        raise ValueError("Local dispatch requires a positive power target")
    load = _number(snapshot.load_w, "home power")
    solar = _number(snapshot.solar_w, "solar power")
    site_import, site_export = site_grid_limits_w(snapshot)
    reserve = _percent(config.get("hardware_backup_reserve", 0), "backup reserve")
    if snapshot.backup_reserve_percent is not None:
        reserve = max(reserve, _number(snapshot.backup_reserve_percent, "gateway reserve"))
    if mode == "discharge":
        reserve = max(reserve, _percent(config.get("optimization_backup_reserve", .2), "reserve floor"))
        if soc <= reserve:
            raise ValueError("Battery is at its discharge reserve floor")
        limit = config.get("optimization_max_grid_export_w")
        if limit is not None:
            target = min(target, _number(limit, "grid export limit"))
        if site_export is not None:
            target = min(target, site_export)
        if target <= 0:
            raise ValueError("Grid export limit blocks force discharge")
        maximum = _number(config.get("optimization_max_discharge_w", 5000), "discharge limit")
        if maximum <= 0:
            raise ValueError("Discharge limit must be positive")
        return max(0.0, min(maximum, target + load - solar))
    if mode != "charge":
        raise ValueError("Unknown dispatch mode")
    ceiling = _percent(config.get("optimization_grid_charge_soc_cap", 1), "charge ceiling")
    if soc >= ceiling:
        raise ValueError("Battery is at its charge ceiling")
    if config.get("optimization_allow_grid_charge", True) is not True:
        raise ValueError("Grid charging is disabled")
    maximum = _number(config.get("optimization_max_charge_w", 5000), "charge limit")
    import_limit = config.get("optimization_max_grid_import_w")
    # Existing setup uses None/zero for an automatic (unset) site import cap.
    headroom = maximum if import_limit in (None, 0) else _number(import_limit, "grid import limit") - load + solar
    if site_import is not None:
        headroom = min(headroom, site_import - load + solar)
    reservation = _number(config.get("native_ev_unobserved_w", 0), "native EV reservation")
    if reservation < 0:
        raise ValueError("Invalid native EV reservation")
    if target_basis == "grid_import":
        if headroom < 0:
            # Do not pin the battery at zero output while uncontrolled loads
            # exceed the hard connection bound. Release to native operation.
            raise ValueError("Home load exceeds the grid import limit")
        # Home and EV demand share this grid target. Never turn a charging
        # request into discharging when uncontrolled demand already exceeds it.
        power = max(0.0, min(target - load + solar - reservation, maximum, headroom - reservation))
        return -power
    power = min(target, maximum, headroom - reservation)
    if power <= 0:
        raise ValueError("No grid import headroom for battery charging")
    return -power


class LocalBatteryDispatch:
    """Serialize requests and release on expiry, failed renewal or shutdown.

    persist() records intent BEFORE the first command. stop() clears it only
    after the specific resume acknowledgement. A lost process can leave at
    most a thirty-second hardware lease; restart cleanup never replays power.
    """

    def __init__(self, client, config, allowed, persist, stopped, guarded_write=None, acknowledged=None, failed=None):
        self.client = client
        self.config = config
        self.allowed = allowed
        self.persist = persist
        self.stopped = stopped
        self.guarded_write = guarded_write
        self.acknowledged = acknowledged
        self.failed = failed
        self._lock = asyncio.Lock()
        self._task = None
        self._deadline = 0.0
        self._mode = ""
        self._target = 0.0
        self._target_basis = "battery"
        self.grid_import_estimate_w = None
        self.pending = False
        self._command_may_have_reached = False
        self._site_limits = None
        self._generation = 0
        self._last_watts = 0.0
        self._pending_watts = None

    @property
    def charge_generation(self):
        """Only an active grid-following charge may lend EV headroom."""
        if self.pending and self._mode == "charge" and self._target_basis == "grid_import":
            return self._generation
        return None

    @property
    def prospective_charge_w(self):
        """Accepted and possibly submitted draw, including an ACK in flight."""
        return max((max(0.0, -value) for value in (self._last_watts, self._pending_watts)
                    if value is not None), default=0.0)

    async def refresh_current_generation(self, expected_generation):
        """Apply the current reservation without replacing owner or deadline."""
        async with self._lock:
            if (expected_generation is None or self.charge_generation != expected_generation
                    or time.monotonic() >= self._deadline):
                return None
            try:
                watts = await self._write_with_limit_retry()
            except BaseException:
                await self._cancel_task()
                await self._release()
                raise
            return {"generation": self._generation, "battery_power_w": watts,
                    "acknowledged_at": time.time()}

    def assume_pending(self):
        """Recover durable intent without replaying its power request."""
        self.pending = True
        self._command_may_have_reached = True

    def _device_discharge_limit(self) -> float:
        return self._device_limit("discharge")

    def _device_limit(self, mode: str) -> float:
        value = getattr(self.client, f"dispatch_{mode}_limit_w", None)
        return _number(value, "gateway dispatch limit") if value is not None else math.inf

    async def check_tariff_fallback(self, mode: str) -> None:
        """A full-rate tariff cannot enforce a native site-meter bound.

        Require fresh, explicit unset evidence even after reload: an unknown
        or failed read cannot erase a previously configured installer limit.
        """
        if (self.config().get("native_ev_rate_armed")
                or _number(self.config().get("native_ev_unobserved_w", 0), "native EV reservation") > 0):
            raise ValueError("Tariff fallback cannot enforce reserved EV headroom")
        # Tariff forcing has no wattage control. A learned native ceiling
        # cannot be guaranteed by the configured optimiser power setting.
        if math.isfinite(self._device_limit(mode)):
            raise ValueError("Tariff fallback cannot enforce the learned gateway dispatch limit")
        try:
            snapshot = await self.client.get_snapshot()
            limits = site_grid_limits_w(snapshot)
            info = (getattr(snapshot, "raw", {}) or {}).get("config", {}).get("site_info", {})
            key = "max_site_meter_power_ac" if mode == "charge" else "min_site_meter_power_ac"
            if info.get(key) is None:
                raise ValueError("Gateway site limits unavailable")
            if limits[0 if mode == "charge" else 1] is not None:
                raise ValueError("Tariff fallback cannot enforce the gateway site limit")
            if (self.config().get("native_ev_rate_armed")
                    or _number(self.config().get("native_ev_unobserved_w", 0), "native EV reservation") > 0):
                raise ValueError("Tariff fallback cannot enforce reserved EV headroom")
        except Exception as err:
            raise ValueError("Tariff fallback blocked by gateway site limits or unavailable limit evidence") from err

    async def start(self, mode: str, target_w: float, duration_seconds: int, *, allowed=None, guarded_write=None, target_basis="battery") -> dict:
        if type(duration_seconds) is not int or not 1 <= duration_seconds <= 86400:
            raise ValueError("Invalid local dispatch duration")
        if target_basis not in ("battery", "grid_import") or (target_basis == "grid_import" and mode != "charge"):
            raise ValueError("Invalid dispatch target basis")
        async with self._lock:
            await self._cancel_task()
            self._generation += 1
            if allowed is not None:
                self.allowed = allowed
            self.guarded_write = guarded_write
            self._mode, self._target = mode, target_w
            self._target_basis = target_basis
            self._deadline = time.monotonic() + duration_seconds
            self.pending = True
            try:
                await self.persist()
                watts = await self._write_with_limit_retry()
            except BaseException:
                await self._release()
                raise
            self._task = asyncio.create_task(self._renew())
            return {"success": True, "command_status": "accepted", "battery_power_w": watts,
                    "requested_power_w": target_w, "power_target_basis": target_basis,
                    "gateway_charge_limit_w": self._device_limit("charge") if mode == "charge" and math.isfinite(self._device_limit("charge")) else None,
                    "gateway_discharge_limit_w": self._device_discharge_limit() if mode == "discharge" and math.isfinite(self._device_discharge_limit()) else None,
                    "control_path": "powerwall_local_dispatch"}

    async def _write_with_limit_retry(self) -> float:
        previous_limit = self._device_limit(self._mode)
        try:
            return await self._write()
        except DispatchCommandFailed:
            current_limit = self._device_limit(self._mode)
            if not 0 < current_limit < previous_limit:
                raise
            # Keep ownership and the original deadline. Only a typed resume
            # permits one retry with fresh telemetry and current safety gates.
            await self._release(retain_intent=True)
            await self.persist()
            return await self._write()

    async def _write(self) -> float:
        validate_dispatch_config(self._mode, self.config())
        if not self.allowed(self._mode):
            raise ValueError("Local dispatch blocked by control ownership or safety policy")
        try:
            snapshot = await self.client.get_snapshot()
        except Exception as err:
            raise DispatchCommandFailed("Local Powerwall telemetry is unavailable") from err
        observed_at = time.monotonic()
        limits = site_grid_limits_w(snapshot)
        if self._site_limits is not None and any(
            old is not None and new is None for old, new in zip(self._site_limits, limits)
        ):
            # Explicit sentinel values can clear a configured bound; partial
            # config responses cannot silently turn a known bound into unset.
            info = (getattr(snapshot, "raw", {}) or {}).get("config", {}).get("site_info", {})
            for old, new, key in zip(self._site_limits, limits, ("max_site_meter_power_ac", "min_site_meter_power_ac")):
                if old is not None and new is None and info.get(key) is None:
                    raise ValueError("Gateway site limits unavailable")
        self._site_limits = limits
        config = self.config()
        if not self.allowed(self._mode):
            raise ValueError("Local dispatch blocked by control ownership or safety policy")
        remaining = min(LEASE_SECONDS, math.ceil(self._deadline - time.monotonic()))
        if remaining < 1:
            raise ValueError("Local dispatch window expired")

        async def write(target):
            # Recheck after the network-envelope await and before the command.
            if not self.allowed(self._mode):
                raise ValueError("Local dispatch safety policy changed")
            if time.monotonic() - observed_at > 5:
                raise ValueError("Local dispatch telemetry aged out before command")
            remaining = min(LEASE_SECONDS, math.ceil(self._deadline - time.monotonic()))
            if remaining < 1:
                raise ValueError("Local dispatch window expired before command")
            current_config = dict(self.config())
            limit_key = f"optimization_max_{self._mode}_w"
            current_config[limit_key] = min(
                _number(current_config.get(limit_key, 5000), f"{self._mode} limit"),
                self._device_limit(self._mode),
            )
            watts = battery_watts(self._mode, target, snapshot, current_config, target_basis=self._target_basis)
            self._command_may_have_reached = True
            self._pending_watts = watts
            try:
                accepted = await self.client.dispatch_battery_power(watts, remaining)
            except Exception as err:
                raise DispatchCommandFailed("Local battery dispatch transport failed") from err
            if not accepted:
                raise DispatchCommandFailed("Gateway did not acknowledge local battery dispatch")
            self._last_watts = watts
            self._pending_watts = None
            self.grid_import_estimate_w = (_number(snapshot.load_w, "home power")
                - _number(snapshot.solar_w, "solar power") - watts) if self._target_basis == "grid_import" else None
            if self.acknowledged is not None:
                await self.acknowledged(watts, remaining)
            return True

        if self._mode == "discharge" and self.guarded_write is not None:
            battery = _number(snapshot.battery_w, "battery power")
            grid = _number(snapshot.grid_w, "grid power")
            controlled_export = max(0.0, min(battery, -grid))
            if not await self.guarded_write(write, controlled_export):
                raise ValueError("Local dispatch blocked by network export envelope")
        else:
            await write(self._target)
        return self._last_watts

    async def _renew(self):
        try:
            while True:
                await asyncio.sleep(min(RENEW_SECONDS, max(0, self._deadline - time.monotonic())))
                async with self._lock:
                    if time.monotonic() >= self._deadline:
                        await self._release()
                        return
                    await self._write_with_limit_retry()
        except asyncio.CancelledError:
            raise
        except Exception:
            _LOGGER.exception("Powerwall local dispatch renewal stopped; releasing lease")
            # Retry release only; never renew power after a failed cycle.
            for attempt in range(3):
                try:
                    async with self._lock:
                        await self._release()
                    return
                except Exception:
                    if attempt < 2:
                        await asyncio.sleep(RENEW_SECONDS)
                    else:
                        _LOGGER.error("Powerwall local cleanup remains pending for restart or manual retry")

    async def _cancel_task(self):
        task, self._task = self._task, None
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await task

    async def _release(self, *, retain_intent=False):
        if not self.pending:
            return
        if not self._command_may_have_reached:
            self.pending = False
            await self.stopped()
            return
        try:
            if not await self.client.resume_battery_operation():
                raise ValueError("Gateway did not acknowledge resume; local cleanup remains pending")
        except Exception:
            _LOGGER.exception("Powerwall local resume failed; intent retained for recovery")
            if self.failed is not None:
                await self.failed()
            raise
        self._command_may_have_reached = False
        self._pending_watts = None
        self._last_watts = 0.0
        if retain_intent:
            return
        self.pending = False
        await self.stopped()

    async def stop(self):
        async with self._lock:
            self._generation += 1
            await self._cancel_task()
            await self._release()
