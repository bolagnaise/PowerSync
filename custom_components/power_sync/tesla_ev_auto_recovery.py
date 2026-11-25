"""Recover eligible Tesla EV command holds independently of charging plans."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import timedelta
import logging
import math
import time
from uuid import uuid4

from . import tesla_ev_recovery as recovery
from .sensitive_logging import install_vin_log_filter
from .tesla_native_recovery import (
    ACTIVE_KEY, _candidates, _no_writers, async_reconcile_native_history,
)

WORKER_KEY = "_tesla_ev_auto_recovery"
STATE_KEY = "automatic_native_recovery"
INTERVAL_SECONDS = 60
MAX_RETRY_SECONDS = 3600
# Repeat an unchanged "still held" reason at most this often.
HELD_LOG_REPEAT_SECONDS = 1800
_LOGGER = logging.getLogger(__name__)
install_vin_log_filter(_LOGGER)


def _valid_record(record):
    return (isinstance(record, dict)
        and type(record.get("attempts")) is int and record["attempts"] >= 0
        and all(type(record.get(key)) in (int, float)
            and math.isfinite(record[key]) and record[key] >= 0
            for key in ("last_attempt_at", "next_attempt_at")))


class NativeRecoveryWorker:
    """One bounded exact-vehicle recovery attempt per tick."""

    def __init__(self, hass, entry, runtime):
        self.hass, self.entry, self.runtime = hass, entry, runtime
        self.task = None
        self.cancel_timer = None
        self.closed = False
        self.stock_checked_at = {}
        self.held_logged = {}

    def current(self):
        return (not self.closed
            and recovery._runtime(self.hass, self.entry) is self.runtime
            and self.runtime.get(WORKER_KEY) is self
            and not self.runtime.get(recovery._RECOVERY_STOPPING_KEY)
            and not self.runtime.get("powerwall_local_dispatch_stopping")
            and recovery._sidecar_ready(self.runtime)
            and self.runtime.get(recovery._SIDECAR_HOLD_KEY) is None)

    async def _reserve(self, vin, source_timestamp=None):
        """Save the retry schedule; crashes must not leave an in-flight latch."""
        async with recovery._journal_lock(self.runtime):
            if not self.current():
                return None
            state = self.runtime[recovery._SIDECAR_STATE_KEY]
            records = state.get(STATE_KEY, {})
            if not isinstance(records, dict):
                return None
            previous = records.get(vin)
            if previous is not None and not _valid_record(previous):
                return None
            now = time.time()
            native = state.get("native_reconciliation", {}).get(vin, {})
            minimum_retry = max(native.get("retry_after", 0),
                self.runtime.get("_tesla_native_reconciliation_retry_after", {}).get(vin, 0))
            if type(minimum_retry) not in (int, float) or not math.isfinite(minimum_retry) or minimum_retry > now:
                return None
            count = previous["attempts"] if previous else 0
            if previous and previous["next_attempt_at"] > now:
                prior_source = previous.get("last_source_timestamp", previous["last_attempt_at"])
                if (type(source_timestamp) not in (int, float) or not math.isfinite(source_timestamp)
                        or type(prior_source) not in (int, float) or source_timestamp <= prior_source):
                    return None
            before = deepcopy(state)
            delay = min(MAX_RETRY_SECONDS, INTERVAL_SECONDS * 2 ** min(count, 6))
            record = {"attempts": count + 1,
                "last_attempt_at": now, "next_attempt_at": now + delay,
                "state": "reading", "token": uuid4().hex,
                "controls_issued": False, "allow_paid_refresh": False,
                "acquisition": "existing_vehicle_telemetry"}
            if type(source_timestamp) in (int, float) and math.isfinite(source_timestamp):
                record["last_source_timestamp"] = source_timestamp
            state.setdefault(STATE_KEY, {})[vin] = record
            recovery._next_revision(state)
            if not await recovery._save_sidecar(self.hass, self.entry, self.runtime, state):
                recovery._restore_sidecar_state(state, before)
                return None
            return record if self.current() else None

    async def _finish(self, vin, record, success, cancelled=False):
        # Stop scheduling before cancellation, but allow the same runtime to
        # save its diagnostic result before lifecycle shutdown fences storage.
        if (recovery._runtime(self.hass, self.entry) is not self.runtime
                or self.runtime.get(recovery._RECOVERY_STOPPING_KEY)):
            return
        async with recovery._journal_lock(self.runtime):
            state = self.runtime[recovery._SIDECAR_STATE_KEY]
            current = state.get(STATE_KEY, {}).get(vin)
            if not isinstance(current, dict) or current.get("token") != record["token"]:
                return
            before = deepcopy(state)
            current["state"] = ("reconciled" if success else "cancelled" if cancelled else "waiting_for_vehicle_data")
            current["last_result"] = deepcopy(state.get("native_reconciliation", {}).get(vin))
            retry_after = (current["last_result"] or {}).get("retry_after", 0)
            if type(retry_after) in (int, float) and math.isfinite(retry_after):
                current["next_attempt_at"] = max(current["next_attempt_at"], retry_after)
            recovery._next_revision(state)
            if not await recovery._save_sidecar(self.hass, self.entry, self.runtime, state):
                recovery._restore_sidecar_state(state, before)
            elif success:
                _LOGGER.info("Automatic Tesla EV recovery reconciled retained command history; normal control admission can resume")

    def _owner_free(self, vin):
        from .automations.ev_ownership import get_ev_ownership

        if not self.current() or not _no_writers(recovery, self.hass, vin):
            return False
        lease_id, owner = get_ev_ownership(self.hass, self.entry, vin)
        return lease_id is None and owner is None

    async def _expire_settled_ble(self, vin):
        """Retire BLE attempts whose effect a fresh vehicle report already shows."""
        from .tesla_ev_observation import get_tesla_vehicle_observation

        if not self._owner_free(vin):
            retired, reason = False, "a command writer or charging owner is active"
        else:
            retired, reason = await recovery.async_expire_settled_ble_attempts(
                self.hass, self.entry, vin,
                get_tesla_vehicle_observation(self.hass, vin),
                authorize=lambda: self._owner_free(vin),
            )
        if retired:
            self.held_logged.pop(vin, None)
            _LOGGER.info(
                "Automatic Tesla EV recovery for %s: %s; normal control admission can resume",
                vin, reason,
            )
            return True
        last = self.held_logged.get(vin)
        now = time.time()
        if last is None or last[0] != reason or now - last[1] >= HELD_LOG_REPEAT_SECONDS:
            self.held_logged[vin] = (reason, now)
            _LOGGER.info("Tesla EV commands for %s remain held: %s", vin, reason)
        return False

    async def _recover_stock(self, vin):
        from .automations.ev_ownership import get_ev_ownership
        from .const import CONF_MONITORING_MODE
        from .tesla_ble_stop_recovery import ACTIVE_KEY as BLE_RECOVERY_ACTIVE
        from .tesla_ev_stock_recovery import async_recover_vehicle_transport

        def authorized():
            if (not self.current() or self.runtime.get(BLE_RECOVERY_ACTIVE) is not None
                    or self.entry.options.get(CONF_MONITORING_MODE,
                                              self.entry.data.get(CONF_MONITORING_MODE, False))
                    or not _no_writers(recovery, self.hass, vin)):
                return False
            lease_id, owner = get_ev_ownership(self.hass, self.entry, vin)
            return lease_id is None and owner is None

        if authorized():
            await async_recover_vehicle_transport(
                self.hass, self.entry, vin, automatic=True, background_authorize=authorized,
            )

    async def async_tick(self, _now=None):
        if not self.current() or (self.task is not None and not self.task.done()):
            return
        self.task = asyncio.current_task()
        try:
            if self.runtime.get(ACTIVE_KEY) is not None:
                return
            root = self.runtime.get(recovery.RECOVERY_KEY)
            if not isinstance(root, dict) or not isinstance(root.get("vehicles"), dict):
                return
            records = self.runtime[recovery._SIDECAR_STATE_KEY].get(STATE_KEY, {})
            if not isinstance(records, dict):
                return
            # Least recently attempted first: one failing VIN cannot starve
            # another car. The ordering survives a Home Assistant restart.
            def attempted(vin):
                row = records.get(vin)
                return max(row["last_attempt_at"] if _valid_record(row) else 0,
                           self.stock_checked_at.get(vin, 0))
            for vin in sorted(root["vehicles"], key=lambda vin: (attempted(vin), vin)):
                if not self.current():
                    return
                route = recovery.get_vehicle_route(self.hass, self.entry, vin)
                if route and route.get("reason") == "ble_recovery":
                    self.stock_checked_at[vin] = time.time()
                    if not await self._expire_settled_ble(vin):
                        await self._recover_stock(vin)
                    return
                if not route or route.get("reason") != "native_recovery":
                    continue
                if (_candidates(recovery, self.hass, self.entry, self.runtime, vin,
                        check_settling=False) is None or not _no_writers(recovery, self.hass, vin)):
                    continue
                if _candidates(recovery, self.hass, self.entry, self.runtime, vin) is None:
                    # This only establishes a missing legacy settling clock;
                    # it cannot reach provider reads until settling completes.
                    await async_reconcile_native_history(self.hass, self.entry, vin,
                        authorize=self.current, allow_paid_refresh=False, cached_only=True)
                    continue
                from .tesla_ev_observation import get_tesla_vehicle_observation
                candidates = _candidates(recovery, self.hass, self.entry, self.runtime, vin)
                if candidates is None:
                    continue
                preferred = next(iter(candidates[1].values()))["route"]["entity_id"]
                observed = get_tesla_vehicle_observation(self.hass, vin, preferred_entity_id=preferred)
                record = await self._reserve(vin, getattr(observed, "source_charge_timestamp", None))
                if record is None:
                    continue
                def authorized():
                    return (self.current()
                        and self.runtime[recovery._SIDECAR_STATE_KEY].get(STATE_KEY, {}).get(vin, {}).get("token") == record["token"])
                success = False
                cancelled = False
                try:
                    success = await async_reconcile_native_history(self.hass, self.entry, vin,
                        authorize=authorized, allow_paid_refresh=False, cached_only=True)
                except asyncio.CancelledError:
                    cancelled = True
                    raise
                finally:
                    await self._finish(vin, record, success, cancelled)
                return
        except asyncio.CancelledError:
            raise
        except Exception:
            _LOGGER.exception("Automatic Tesla EV recovery remains pending; retained history is preserved")
        finally:
            self.task = None

    async def async_stop(self):
        self.closed = True
        if self.cancel_timer is not None:
            self.cancel_timer()
            self.cancel_timer = None
        task = self.task
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


def start(hass, entry):
    """Attach only after entry setup, including sidecar hydration, is complete."""
    runtime = recovery._runtime(hass, entry)
    if runtime is None or runtime.get(WORKER_KEY) is not None or not recovery._sidecar_ready(runtime):
        return
    from homeassistant.helpers.event import async_track_time_interval

    worker = NativeRecoveryWorker(hass, entry, runtime)
    runtime[WORKER_KEY] = worker
    worker.cancel_timer = async_track_time_interval(hass, worker.async_tick, timedelta(seconds=INTERVAL_SECONDS))


async def async_stop(hass, entry):
    runtime = recovery._runtime(hass, entry)
    worker = runtime.get(WORKER_KEY) if runtime is not None else None
    if isinstance(worker, NativeRecoveryWorker):
        await worker.async_stop()
