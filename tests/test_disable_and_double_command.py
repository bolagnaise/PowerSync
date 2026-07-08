"""Regression tests for OB-10 and OB-11 optimizer concurrency bugs.

OB-10: ``_on_price_update`` spawned an UNTRACKED background task running
``_run_optimization``. ``disable()`` cancelled ``_polling_task``,
``_initial_opt_task``, ``_deferred_restore_task`` and
``_settings_reoptimize_task``, but not the price-triggered solve. A
price-solve already in flight when ``disable()`` ran would complete
afterwards and call ``_execute_optimizer_action``, re-commanding the battery
after ``disable()`` had already restored normal operation.

OB-11: two independent ~5-minute cadences (the polling loop and the
``DataUpdateCoordinator`` refresh) both call
``_execute_cached_current_action_if_changed``, whose dedup reads
``_last_executed_action`` — written only at the END of
``_execute_optimizer_action`` after awaited hardware I/O. With no
reentrancy guard, both cadences can pass the dedup check before either has
written the marker, so at an action-transition boundary both issue the same
hardware command (double force-timer extension, double Tesla TOU upload).

Both fixes live in ``custom_components/power_sync/optimization/coordinator.py``.
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


async def _noop_async(*args, **kwargs) -> None:
    return None


class _FakeTask:
    """Minimal stand-in for an asyncio.Task's cancellation surface."""

    def __init__(self) -> None:
        self.cancelled = False
        self._done = False

    def done(self) -> bool:
        return self._done

    def cancel(self) -> None:
        self.cancelled = True


# ---------------------------------------------------------------------------
# OB-10: price-triggered solve survives disable()
# ---------------------------------------------------------------------------


def _disable_coordinator(opt_module):
    coordinator = object.__new__(opt_module.OptimizationCoordinator)
    coordinator._enabled = True
    coordinator._monitoring_mode_active = lambda: False
    # Not "idle" — skips the pre-idle backup-reserve restore branch, which
    # would otherwise need a real battery_controller/energy_coordinator stub.
    coordinator._last_executed_action = "self_consumption"
    coordinator._scheduled_ev_no_discharge_active = False
    coordinator._polling_task = None
    coordinator._initial_opt_task = None
    coordinator._deferred_restore_task = None
    coordinator._settings_reoptimize_task = None
    coordinator._price_listener_unsub = None
    coordinator._octopus_gate_listener_unsub = None
    coordinator._executor = None
    coordinator._ev_coordinator = None
    coordinator._cost_store = SimpleNamespace(async_save=_noop_async)
    coordinator._cost_data_to_save = lambda: {}
    return coordinator


def test_disable_cancels_untracked_price_reoptimize_task(opt_module):
    """disable() must cancel a price-triggered solve stored on the instance.

    Pre-fix, ``_on_price_update`` never stored the task handle at all, so
    there was nothing for ``disable()`` to find/cancel — the task ran to
    completion regardless of disable(). This asserts the handle now exists
    and disable() cancels + clears it.
    """

    async def _run():
        coordinator = _disable_coordinator(opt_module)
        fake_task = _FakeTask()
        coordinator._price_reoptimize_task = fake_task

        await coordinator.disable()

        assert fake_task.cancelled is True
        assert coordinator._price_reoptimize_task is None

    asyncio.run(_run())


def test_disable_tolerates_missing_price_reoptimize_task_attr(opt_module):
    """Coordinators that never fired a price update (attribute unset) must
    still disable cleanly."""

    async def _run():
        coordinator = _disable_coordinator(opt_module)
        assert not hasattr(coordinator, "_price_reoptimize_task")

        await coordinator.disable()  # must not raise AttributeError

    asyncio.run(_run())


def _execute_coordinator(opt_module, battery, enabled: bool = True):
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
    coordinator._enabled = enabled
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


def test_execute_optimizer_action_noop_when_disabled(opt_module):
    """A solve in flight when disable() ran must not command the battery.

    Pre-fix, ``_execute_optimizer_action`` had no ``_enabled`` guard at all,
    so this call would reach ``set_self_consumption_mode()`` and advance the
    marker even with the optimizer disabled — exactly the OB-10 failure
    mode (a stale in-flight solve re-commanding hardware after disable()).
    """

    async def _run():
        calls = []

        async def set_self_consumption_mode():
            calls.append(True)
            return True

        battery = SimpleNamespace(set_self_consumption_mode=set_self_consumption_mode)
        coordinator = _execute_coordinator(opt_module, battery, enabled=False)

        await coordinator._execute_optimizer_action(_self_consumption_action())

        assert calls == []
        assert coordinator._last_executed_action == "charge"  # unchanged

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# OB-11: double hardware command at slot boundaries
# ---------------------------------------------------------------------------


def _cached_action_coordinator(opt_module, action_name="charge", last_executed="self_consumption"):
    coordinator = object.__new__(opt_module.OptimizationCoordinator)
    coordinator._enabled = True
    coordinator._executor = SimpleNamespace(battery_controller=object())
    coordinator._optimization_lock = asyncio.Lock()
    coordinator._last_executed_action = last_executed
    action = SimpleNamespace(action=action_name)
    coordinator._get_current_action = lambda: action
    return coordinator


def test_concurrent_cached_action_calls_issue_command_once(opt_module):
    """Two concurrent cadences hitting an action transition must not both
    issue the hardware command.

    Simulates the polling loop and the DataUpdateCoordinator refresh both
    crossing the same wall-clock boundary right as the schedule transitions
    from "self_consumption" to "charge". ``_execute_optimizer_action`` is
    stubbed with a fake that mimics its real shape: it records the call,
    then awaits (like the real awaited hardware I/O), and only advances
    ``_last_executed_action`` once that await completes — reproducing the
    exact race window OB-11 fixes (the marker used for dedup is written only
    at the END of the awaited hardware call).
    """

    async def _run():
        coordinator = _cached_action_coordinator(opt_module)
        execute_calls = []
        release_event = asyncio.Event()
        started_event = asyncio.Event()

        async def fake_execute_optimizer_action(action):
            execute_calls.append(action.action)
            started_event.set()
            # Yield control here, exactly like the real awaited battery I/O
            # inside _execute_optimizer_action — this is the window where a
            # second concurrent caller can currently interleave and issue a
            # second command before the marker below is written.
            await release_event.wait()
            coordinator._last_executed_action = action.action

        coordinator._execute_optimizer_action = fake_execute_optimizer_action

        task_a = asyncio.create_task(
            coordinator._execute_cached_current_action_if_changed()
        )
        # Let task_a run until it is inside the "hardware call" and blocked
        # on release_event (holding the execution lock, post-fix).
        await started_event.wait()

        task_b = asyncio.create_task(
            coordinator._execute_cached_current_action_if_changed()
        )
        # Give task_b a chance to run its synchronous prefix (dedup check,
        # which pre-fix still passes because the marker hasn't been written
        # yet) and reach its own call into _execute_optimizer_action / the
        # lock acquisition.
        await asyncio.sleep(0)

        release_event.set()
        await asyncio.gather(task_a, task_b)

        # Pre-fix this is ["charge", "charge"] — both cadences pass the
        # dedup check before either writes the marker, so both issue the
        # command. Post-fix, task_b waits on the execution lock, then
        # re-checks the marker (now "charge") and dedups without a second
        # hardware call.
        assert execute_calls == ["charge"]
        assert coordinator._last_executed_action == "charge"

    asyncio.run(_run())
