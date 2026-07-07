"""Regression tests for optimizer restore-path retry contracts.

Two stuck-state bugs shared the same shape — a restore that failed once was
recorded as done and never re-attempted:

* ``_release_scheduled_ev_no_discharge_mode`` cleared its active flag BEFORE
  the hardware await, so an exception/False left the flag cleared and the
  early-return made every later call a no-op (stuck 0 W discharge cap).
* The self-consumption branch of ``_execute_optimizer_action`` ignored the
  return of ``set_self_consumption_mode()`` and advanced
  ``_last_executed_action`` unconditionally, so the change-detection skipped
  the command forever after a single failure (inverter left in prior forced
  mode on brands without drift checks).

Both must keep their pending state on failure so the next cycle retries.
"""

from __future__ import annotations

import asyncio
import importlib
import sys
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

# Reuse the HA/power_sync stub scaffolding from the sibling regression file
# (pytest prepends this directory to sys.path, so the import is stable).
from test_battery_export_allowed_slots import (
    _SENTINEL,
    _STUB_MODULE_NAMES,
    _install_ha_stubs,
    _install_power_sync_stubs,
)


@pytest.fixture()
def opt_module():
    saved_modules = {
        name: sys.modules.get(name, _SENTINEL)
        for name in _STUB_MODULE_NAMES
    }
    for name in _STUB_MODULE_NAMES:
        sys.modules.pop(name, None)

    _install_ha_stubs()
    _install_power_sync_stubs()
    module = importlib.import_module("power_sync.optimization.coordinator")
    try:
        yield module
    finally:
        for name in _STUB_MODULE_NAMES:
            if saved_modules[name] is _SENTINEL:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = saved_modules[name]


# ---------------------------------------------------------------------------
# _release_scheduled_ev_no_discharge_mode
# ---------------------------------------------------------------------------


def _release_coordinator(opt_module):
    coordinator = object.__new__(opt_module.OptimizationCoordinator)
    coordinator._scheduled_ev_no_discharge_active = True
    coordinator._monitoring_mode_active = lambda: False
    coordinator.energy_coordinator = None
    coordinator._executor = None
    return coordinator


def test_ev_release_exception_keeps_flag_and_retries(opt_module):
    async def _run():
        coordinator = _release_coordinator(opt_module)
        calls = []

        async def restore_no_discharge_mode():
            calls.append(True)
            if len(calls) == 1:
                raise TimeoutError("modbus timeout")
            return True

        coordinator.energy_coordinator = SimpleNamespace(
            restore_no_discharge_mode=restore_no_discharge_mode,
        )

        assert await coordinator._release_scheduled_ev_no_discharge_mode("t") is False
        # Failure must keep the mode active so the next cycle re-attempts.
        assert coordinator._scheduled_ev_no_discharge_active is True

        assert await coordinator._release_scheduled_ev_no_discharge_mode("t") is True
        assert coordinator._scheduled_ev_no_discharge_active is False
        # The second call must actually reach the hardware again.
        assert len(calls) == 2

    asyncio.run(_run())


def test_ev_release_false_result_keeps_flag(opt_module):
    async def _run():
        coordinator = _release_coordinator(opt_module)

        async def restore_no_discharge_mode():
            return False

        coordinator.energy_coordinator = SimpleNamespace(
            restore_no_discharge_mode=restore_no_discharge_mode,
        )

        assert await coordinator._release_scheduled_ev_no_discharge_mode("t") is False
        assert coordinator._scheduled_ev_no_discharge_active is True

    asyncio.run(_run())


def test_ev_release_noop_when_inactive(opt_module):
    async def _run():
        coordinator = _release_coordinator(opt_module)
        coordinator._scheduled_ev_no_discharge_active = False

        async def restore_no_discharge_mode():  # pragma: no cover - must not run
            raise AssertionError("hardware call issued while mode inactive")

        coordinator.energy_coordinator = SimpleNamespace(
            restore_no_discharge_mode=restore_no_discharge_mode,
        )

        assert await coordinator._release_scheduled_ev_no_discharge_mode("t") is True

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# _execute_optimizer_action self-consumption marker
# ---------------------------------------------------------------------------


def _execute_coordinator(opt_module, battery):
    coordinator = object.__new__(opt_module.OptimizationCoordinator)
    coordinator._executor = SimpleNamespace(battery_controller=battery)
    coordinator.hass = SimpleNamespace(data={})
    coordinator.entry_id = "test-entry"
    coordinator.battery_system = "solax"
    coordinator.energy_coordinator = None
    coordinator._config = opt_module.OptimizationConfig(
        interval_minutes=5,
        horizon_hours=24,
    )
    coordinator._entry = SimpleNamespace(options={}, data={})
    coordinator._monitoring_mode_active = lambda: False
    coordinator._get_active_force_state = lambda: None
    coordinator._scheduled_ev_preserve_active = lambda: False
    coordinator._scheduled_ev_no_discharge_active = False
    coordinator._last_executed_action = "charge"
    coordinator._last_executed_planned_action = "charge"
    coordinator._pre_idle_backup_reserve = None
    coordinator._idle_hold_reserve = None
    return coordinator


def _self_consumption_action():
    return SimpleNamespace(
        timestamp=datetime(2026, 7, 6, 18, 0, tzinfo=timezone.utc),
        action="self_consumption",
        power_w=0.0,
        soc=0.8,
        battery_charge_w=0.0,
        battery_discharge_w=0.0,
    )


def test_failed_self_consumption_restore_keeps_marker_and_retries(opt_module):
    async def _run():
        calls = []

        async def set_self_consumption_mode():
            calls.append(True)
            # First attempt fails (base BatteryController returns False
            # instead of raising), second succeeds.
            return len(calls) > 1

        battery = SimpleNamespace(set_self_consumption_mode=set_self_consumption_mode)
        coordinator = _execute_coordinator(opt_module, battery)

        await coordinator._execute_optimizer_action(_self_consumption_action())
        assert calls == [True]
        # Failure must not advance the marker — that masked the failure and
        # the change-detection then skipped the command forever.
        assert coordinator._last_executed_action == "charge"

        await coordinator._execute_optimizer_action(_self_consumption_action())
        assert len(calls) == 2
        assert coordinator._last_executed_action == "self_consumption"

    asyncio.run(_run())


def test_successful_self_consumption_restore_advances_marker(opt_module):
    async def _run():
        calls = []

        async def set_self_consumption_mode():
            calls.append(True)
            return True

        battery = SimpleNamespace(set_self_consumption_mode=set_self_consumption_mode)
        coordinator = _execute_coordinator(opt_module, battery)

        await coordinator._execute_optimizer_action(_self_consumption_action())
        assert calls == [True]
        assert coordinator._last_executed_action == "self_consumption"

        # Marker recorded — the redundant-call dedup must now hold.
        await coordinator._execute_optimizer_action(_self_consumption_action())
        assert calls == [True]

    asyncio.run(_run())
