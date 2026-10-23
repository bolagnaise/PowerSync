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
LEASE_SECONDS = 60
RENEW_SECONDS = 15


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


def battery_watts(mode: str, target_w: float, snapshot: Any, config: dict) -> float:
    """Convert a grid-export target or charging rate into safe battery watts.

    Zero target is the existing manual 'device maximum' sentinel, resolved by
    the caller. Positive discharge includes home load less solar generation.
    The export envelope clamps target_w before this function is called.
    """
    validate_dispatch_config(mode, config)
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
    power = min(target, maximum, headroom)
    if power <= 0:
        raise ValueError("No grid import headroom for battery charging")
    return -power


class LocalBatteryDispatch:
    """Serialize requests and release on expiry, failed renewal or shutdown.

    persist() records intent BEFORE the first command. stop() clears it only
    after the specific resume acknowledgement. A lost process can leave at
    most a sixty-second hardware lease; restart cleanup never replays power.
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
        self.pending = False
        self._command_may_have_reached = False

    def assume_pending(self):
        """Recover durable intent without replaying its power request."""
        self.pending = True
        self._command_may_have_reached = True

    async def start(self, mode: str, target_w: float, duration_seconds: int, *, allowed=None, guarded_write=None) -> dict:
        if type(duration_seconds) is not int or not 1 <= duration_seconds <= 86400:
            raise ValueError("Invalid local dispatch duration")
        async with self._lock:
            await self._cancel_task()
            if allowed is not None:
                self.allowed = allowed
            self.guarded_write = guarded_write
            self._mode, self._target = mode, target_w
            self._deadline = time.monotonic() + duration_seconds
            self.pending = True
            try:
                await self.persist()
                watts = await self._write()
            except BaseException:
                # A timeout may have reached the gateway. Never fall through
                # to tariff control while a local request may still own it.
                await self._release()
                raise
            self._task = asyncio.create_task(self._renew())
            return {"success": True, "command_status": "accepted", "battery_power_w": watts,
                    "control_path": "powerwall_local_dispatch"}

    async def _write(self) -> float:
        validate_dispatch_config(self._mode, self.config())
        if not self.allowed(self._mode):
            raise ValueError("Local dispatch blocked by control ownership or safety policy")
        try:
            snapshot = await self.client.get_snapshot()
        except Exception as err:
            raise DispatchCommandFailed("Local Powerwall telemetry is unavailable") from err
        observed_at = time.monotonic()
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
            watts = battery_watts(self._mode, target, snapshot, self.config())
            self._command_may_have_reached = True
            try:
                accepted = await self.client.dispatch_battery_power(watts, remaining)
            except Exception as err:
                raise DispatchCommandFailed("Local battery dispatch transport failed") from err
            if not accepted:
                raise DispatchCommandFailed("Gateway did not acknowledge local battery dispatch")
            self._last_watts = watts
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
                    await self._write()
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

    async def _release(self):
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
        self.pending = False
        self._command_may_have_reached = False
        await self.stopped()

    async def stop(self):
        async with self._lock:
            await self._cancel_task()
            await self._release()
