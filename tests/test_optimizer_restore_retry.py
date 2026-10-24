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
import ast
import copy
import importlib
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
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


_ROOT = Path(__file__).resolve().parent.parent
_INIT_PATH = _ROOT / "custom_components" / "power_sync" / "__init__.py"
_BATTERY_CONTROLLER_PATH = (
    _ROOT / "custom_components" / "power_sync" / "optimization" / "battery_controller.py"
)


def _load_ast_node(node, namespace, filename):
    node = copy.deepcopy(node)
    node.returns = None
    for arg in node.args.args:
        arg.annotation = None
    exec(  # noqa: S102 - Execute the repository function under test.
        compile(
            ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[])),
            str(filename),
            "exec",
        ),
        namespace,
    )
    return namespace[node.name]


def _foxess_service_handler_node():
    module = ast.parse(_INIT_PATH.read_text())
    setup = next(
        node
        for node in module.body
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name == "async_setup_entry"
    )
    handler = next(
        node
        for node in setup.body
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name == "handle_set_self_consumption"
    )
    foxess_branch = next(
        node
        for node in ast.walk(handler)
        if isinstance(node, ast.If)
        and isinstance(node.test, ast.Name)
        and node.test.id == "is_foxess"
    )
    handler.body = copy.deepcopy(foxess_branch.body)
    return handler


def _fronius_service_handler_node():
    module = ast.parse(_INIT_PATH.read_text())
    setup = next(
        node
        for node in module.body
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name == "async_setup_entry"
    )
    handler = next(
        node
        for node in setup.body
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name == "handle_set_self_consumption"
    )
    fronius_branch = next(
        node
        for node in ast.walk(handler)
        if isinstance(node, ast.If)
        and isinstance(node.test, ast.Name)
        and node.test.id == "is_fronius_reserva_sc"
    )
    handler.body = copy.deepcopy(fronius_branch.body)
    return handler


def _foxess_wrapper_method_node():
    module = ast.parse(_BATTERY_CONTROLLER_PATH.read_text())
    wrapper = next(
        node
        for node in ast.walk(module)
        if isinstance(node, ast.ClassDef)
        and node.name == "BatteryControllerWrapper"
    )
    return next(
        node
        for node in wrapper.body
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name == "set_self_consumption_mode"
    )


def _fronius_runtime(*, outcome="true", coordinator=True, user_owned_override=False):
    """Load the real Fronius service branch and wrapper with fake transport."""
    restore_calls = []
    state = {"outcome": outcome}

    async def restore_normal():
        restore_calls.append(True)
        if state["outcome"] == "exception":
            raise TimeoutError("simulated Modbus timeout")
        return state["outcome"] == "true"

    cleanup_calls = []

    def clear_self_consumption_state():
        cleanup_calls.append("clear")

    async def persist_force_mode_state():
        cleanup_calls.append("persist")

    entry_data = {}
    if coordinator:
        entry_data["fronius_reserva_coordinator"] = SimpleNamespace(
            restore_normal=restore_normal
        )
    service_hass = SimpleNamespace(data={"power_sync": {"entry": entry_data}})
    handler = _load_ast_node(
        _fronius_service_handler_node(),
        {
            "hass": service_hass,
            "entry": SimpleNamespace(entry_id="entry"),
            "DOMAIN": "power_sync",
            "user_owned_override": user_owned_override,
            "_clear_self_consumption_state": clear_self_consumption_state,
            "persist_force_mode_state": persist_force_mode_state,
            "_guarded_self_consumption_write": lambda writer: writer(),
            "_LOGGER": logging.getLogger(__name__),
            "HomeAssistantError": RuntimeError,
        },
        _INIT_PATH,
    )

    service_calls = []

    async def async_call(domain, service, data, blocking):
        service_calls.append((domain, service, data, blocking))
        return await handler(None)

    wrapper_self = SimpleNamespace(
        hass=SimpleNamespace(services=SimpleNamespace(async_call=async_call))
    )
    wrapper = _load_ast_node(
        _foxess_wrapper_method_node(),
        {
            "_LOGGER": logging.getLogger(__name__),
            "optimizer_command": lambda method: method,
            "optimizer_service_data": lambda _controller, data: data,
        },
        _BATTERY_CONTROLLER_PATH,
    )

    async def set_self_consumption_mode():
        return await wrapper(wrapper_self)

    return (
        SimpleNamespace(set_self_consumption_mode=set_self_consumption_mode),
        entry_data,
        restore_calls,
        service_calls,
        state,
        restore_normal,
        cleanup_calls,
    )


def _foxess_runtime(*, outcome="true", coordinator=True, curtailment=False):
    """Load the real service branch and wrapper with a fake transport boundary."""
    restore_calls = []
    state = {"outcome": outcome}

    async def restore_normal():
        restore_calls.append(True)
        if state["outcome"] == "exception":
            raise TimeoutError("simulated Modbus timeout")
        return state["outcome"] == "true"

    entry_data = {}
    if coordinator:
        entry_data["foxess_coordinator"] = SimpleNamespace(
            restore_normal=restore_normal
        )
    if curtailment:
        entry_data["foxess_curtailment_state"] = "curtailed"
    service_hass = SimpleNamespace(data={"power_sync": {"entry": entry_data}})
    handler = _load_ast_node(
        _foxess_service_handler_node(),
        {
            "hass": service_hass,
            "entry": SimpleNamespace(entry_id="entry"),
            "source": "optimizer",
            "DOMAIN": "power_sync",
            "_guarded_self_consumption_write": lambda writer: writer(),
            "_LOGGER": logging.getLogger(__name__),
            "HomeAssistantError": RuntimeError,
        },
        _INIT_PATH,
    )

    service_calls = []

    async def async_call(domain, service, data, blocking):
        service_calls.append((domain, service, data, blocking))
        return await handler(None)

    wrapper_self = SimpleNamespace(
        hass=SimpleNamespace(services=SimpleNamespace(async_call=async_call))
    )
    wrapper = _load_ast_node(
        _foxess_wrapper_method_node(),
        {
            "_LOGGER": logging.getLogger(__name__),
            # This extracted method tests acknowledgement propagation. Licensing
            # admission and entry routing have their own runtime regressions.
            "optimizer_command": lambda method: method,
            "optimizer_service_data": lambda _controller, data: data,
        },
        _BATTERY_CONTROLLER_PATH,
    )

    async def set_self_consumption_mode():
        return await wrapper(wrapper_self)

    return (
        SimpleNamespace(set_self_consumption_mode=set_self_consumption_mode),
        entry_data,
        restore_calls,
        service_calls,
        state,
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


@pytest.mark.parametrize("failure", ["false", "exception"])
@pytest.mark.parametrize("previous_action", ["charge", "self_consumption"])
def test_failed_foxess_ev_preserve_keeps_action_and_retries(
    opt_module, failure, previous_action
):
    """A rejected hold must not become the displayed executed action."""
    async def _run():
        calls = []

        async def set_no_discharge_mode():
            calls.append(True)
            if len(calls) == 1:
                if failure == "exception":
                    raise TimeoutError("simulated FoxESS Modbus timeout")
                return False
            return True

        coordinator = _execute_coordinator(opt_module, SimpleNamespace())
        coordinator.battery_system = "foxess"
        coordinator.energy_coordinator = SimpleNamespace(
            set_no_discharge_mode=set_no_discharge_mode
        )
        coordinator._scheduled_ev_preserve_active = lambda: True
        coordinator._last_executed_action = previous_action
        coordinator._last_executed_planned_action = previous_action

        await coordinator._execute_optimizer_action(_self_consumption_action())
        assert calls == [True]
        assert coordinator._scheduled_ev_no_discharge_active is False
        assert coordinator._last_executed_action == previous_action
        assert coordinator._last_executed_planned_action == previous_action

        await coordinator._execute_optimizer_action(_self_consumption_action())
        assert calls == [True, True]
        assert coordinator._scheduled_ev_no_discharge_active is True
        assert coordinator._last_executed_action == "no_discharge"
        assert coordinator._last_executed_planned_action == "self_consumption"

        # An accepted hold remains active without a duplicate hardware write.
        await coordinator._execute_optimizer_action(_self_consumption_action())
        assert calls == [True, True]

    asyncio.run(_run())


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


@pytest.mark.parametrize("failure", ["false", "exception"])
def test_foxess_service_failure_reaches_optimizer_retry(opt_module, failure):
    """A FoxESS restore failure must cross service and wrapper boundaries."""
    async def _run():
        battery, _entry_data, restore_calls, service_calls, state = _foxess_runtime(
            outcome=failure
        )
        coordinator = _execute_coordinator(opt_module, battery)
        coordinator.battery_system = "foxess"

        await coordinator._execute_optimizer_action(_self_consumption_action())
        assert len(service_calls) == 1
        assert len(restore_calls) == 1
        assert coordinator._last_executed_action == "charge"

        state["outcome"] = "true"
        await coordinator._execute_optimizer_action(_self_consumption_action())
        assert len(service_calls) == 2
        assert len(restore_calls) == 2
        assert coordinator._last_executed_action == "self_consumption"

    asyncio.run(_run())


@pytest.mark.parametrize("outcome", ["false", "exception"])
def test_foxess_service_failure_returns_false_from_wrapper(outcome):
    """The wrapper must not acknowledge an unconfirmed FoxESS restore."""
    async def _run():
        battery, _entry_data, restore_calls, service_calls, _state = _foxess_runtime(
            outcome=outcome
        )
        assert await battery.set_self_consumption_mode() is False
        assert len(service_calls) == 1
        assert len(restore_calls) == 1

    asyncio.run(_run())


def test_foxess_missing_coordinator_is_not_acknowledged():
    async def _run():
        battery, _entry_data, restore_calls, service_calls, _state = _foxess_runtime(
            coordinator=False
        )
        assert await battery.set_self_consumption_mode() is False
        assert len(service_calls) == 1
        assert restore_calls == []

    asyncio.run(_run())


@pytest.mark.parametrize("failure", ["false", "exception"])
def test_fronius_service_failure_reaches_optimizer_retry(opt_module, failure):
    """A Fronius restore failure must cross service and wrapper boundaries."""
    async def _run():
        battery, _entry_data, restore_calls, service_calls, state, _restore_normal, _cleanup_calls = (
            _fronius_runtime(outcome=failure)
        )
        coordinator = _execute_coordinator(opt_module, battery)
        coordinator.battery_system = "fronius_reserva"

        await coordinator._execute_optimizer_action(_self_consumption_action())
        assert len(service_calls) == 1
        assert len(restore_calls) == 1
        assert coordinator._last_executed_action == "charge"

        state["outcome"] = "true"
        await coordinator._execute_optimizer_action(_self_consumption_action())
        assert len(service_calls) == 2
        assert len(restore_calls) == 2
        assert coordinator._last_executed_action == "self_consumption"

    asyncio.run(_run())


def test_fronius_missing_coordinator_reaches_optimizer_retry(opt_module):
    async def _run():
        battery, entry_data, restore_calls, service_calls, _state, restore_normal, _cleanup_calls = (
            _fronius_runtime(coordinator=False)
        )
        coordinator = _execute_coordinator(opt_module, battery)
        coordinator.battery_system = "fronius_reserva"

        await coordinator._execute_optimizer_action(_self_consumption_action())
        assert len(service_calls) == 1
        assert restore_calls == []
        assert coordinator._last_executed_action == "charge"

        entry_data["fronius_reserva_coordinator"] = SimpleNamespace(
            restore_normal=restore_normal
        )
        await coordinator._execute_optimizer_action(_self_consumption_action())
        assert len(service_calls) == 2
        assert len(restore_calls) == 1
        assert coordinator._last_executed_action == "self_consumption"

    asyncio.run(_run())


@pytest.mark.parametrize("outcome", ["false", "exception"])
def test_fronius_service_failure_returns_false_from_wrapper(outcome):
    async def _run():
        battery, _entry_data, restore_calls, service_calls, _state, _restore_normal, _cleanup_calls = (
            _fronius_runtime(outcome=outcome)
        )
        assert await battery.set_self_consumption_mode() is False
        assert len(service_calls) == 1
        assert len(restore_calls) == 1

    asyncio.run(_run())


def test_fronius_missing_coordinator_is_not_acknowledged():
    async def _run():
        battery, _entry_data, restore_calls, service_calls, _state, _restore_normal, _cleanup_calls = (
            _fronius_runtime(coordinator=False)
        )
        assert await battery.set_self_consumption_mode() is False
        assert len(service_calls) == 1
        assert restore_calls == []

    asyncio.run(_run())


def test_fronius_manual_failure_clears_self_consumption_state():
    async def _run():
        battery, _entry_data, _restore_calls, _service_calls, _state, _restore_normal, cleanup_calls = (
            _fronius_runtime(outcome="false", user_owned_override=True)
        )
        assert await battery.set_self_consumption_mode() is False
        assert cleanup_calls == ["clear", "persist"]

    asyncio.run(_run())


def test_foxess_optimizer_curtailment_ownership_blocks_restore():
    """Optimizer-owned curtailment remains an explicit no-write boundary."""
    async def _run():
        battery, _entry_data, restore_calls, service_calls, _state = _foxess_runtime(
            curtailment=True
        )
        assert await battery.set_self_consumption_mode() is True
        assert len(service_calls) == 1
        assert restore_calls == []

    asyncio.run(_run())


def test_calibration_holds_active_optimizer_force_without_hardware_refresh(opt_module):
    """An expiring optimizer force must not refresh while calibration is active."""
    async def _run():
        calls = []

        async def force_charge(**kwargs):
            calls.append(kwargs)
            return True

        battery = SimpleNamespace(force_charge=force_charge)
        coordinator = _execute_coordinator(opt_module, battery)
        coordinator.hass.data = {
            "power_sync": {"test-entry": {"calibration_suspected": True}}
        }
        coordinator._get_active_force_state = lambda: {
            "active": True, "source": "optimizer", "type": "charge"
        }

        await coordinator._execute_optimizer_action(
            SimpleNamespace(
                timestamp=datetime(2026, 7, 6, 18, 0, tzinfo=timezone.utc),
                action="charge", power_w=20_000.0, soc=0.8,
                battery_charge_w=20_000.0, battery_discharge_w=0.0,
            )
        )

        assert calls == []

    asyncio.run(_run())
