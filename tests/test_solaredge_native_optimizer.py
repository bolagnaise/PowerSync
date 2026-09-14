"""Exercise SolarEdge CONSUME cycles through a Home Assistant service bridge."""

from __future__ import annotations

import asyncio
import importlib
from types import SimpleNamespace

from test_battery_export_allowed_slots import _execution_coordinator
from test_battery_export_allowed_slots import opt_module as _opt_module_fixture
from test_solaredge_controller import SolarEdgeEnergyController, _MemoryStore, _SEHass
from test_solaredge_reconciliation_integration import system as _system_fixture

opt_module = _opt_module_fixture
system = _system_fixture


class _PowerSyncServiceBridge:
    """Route the optimizer's public service call into the native controller."""

    def __init__(self, hass, native_controller):
        self._native_controller = native_controller
        self._entity_services = hass.services
        self.self_consumption_attempts = 0

    async def async_call(
        self,
        domain: str,
        service: str,
        data: dict,
        blocking: bool = False,
        return_response: bool = False,
    ):
        if domain != "power_sync" or service != "set_self_consumption":
            return await self._entity_services.async_call(
                domain, service, data, blocking=blocking
            )

        self.self_consumption_attempts += 1
        confirmed = await self._native_controller.set_self_consumption(
            automatic=data.get("source") == "optimizer"
        )
        if not confirmed:
            raise RuntimeError("SolarEdge self-consumption was not confirmed")
        return {"success": True} if return_response else None


def _native_optimizer_stack(
    opt_module, monkeypatch, *, native_self_use_available: bool
):
    hass = _SEHass()
    hass.data = {}
    store = _MemoryStore()
    monkeypatch.setattr(
        SolarEdgeEnergyController, "_create_store", lambda self, identity: store
    )
    command = hass.states.get("select.solaredge_storage_command_mode")
    if native_self_use_available:
        command.attributes["options"].append("Maximize Self Consumption")

    native_controller = SolarEdgeEnergyController(hass, entity_prefix="solaredge")
    assert asyncio.run(native_controller.connect())
    bridge = _PowerSyncServiceBridge(hass, native_controller)
    hass.services = bridge

    battery_module = importlib.import_module(
        "power_sync.optimization.battery_controller"
    )
    battery = battery_module.BatteryControllerWrapper(hass, "solaredge")
    optimizer = _execution_coordinator(opt_module, battery, soc=0.55)
    optimizer.hass = hass
    optimizer.battery_system = "solaredge"
    optimizer._last_executed_action = "export"
    action = SimpleNamespace(action="self_consumption", power_w=0)
    return optimizer, action, bridge, native_controller, command


def _reconciled_native_optimizer_stack(opt_module, system):
    native_controller, hass, _, upstream, _, _, _ = system
    reconciliation = asyncio.run(native_controller.reconcile_result())
    assert reconciliation["success"] is True
    assert reconciliation["confirmation_source"] == (
        "fresh_native_self_consumption_poll"
    )

    entity_services = hass.services
    bridge = _PowerSyncServiceBridge(hass, native_controller)
    hass.services = bridge
    battery_module = importlib.import_module(
        "power_sync.optimization.battery_controller"
    )
    battery = battery_module.BatteryControllerWrapper(hass, "solaredge")
    optimizer = _execution_coordinator(opt_module, battery, soc=0.55)
    optimizer.hass = hass
    optimizer.battery_system = "solaredge"
    optimizer._last_executed_action = "export"
    action = SimpleNamespace(action="self_consumption", power_w=0)
    return optimizer, action, bridge, native_controller, upstream, entity_services


def test_confirmed_native_consume_advances_marker_and_is_not_repeated(
    opt_module, monkeypatch
):
    optimizer, action, bridge, native_controller, command = _native_optimizer_stack(
        opt_module, monkeypatch, native_self_use_available=True
    )

    asyncio.run(optimizer._execute_optimizer_action(action))
    asyncio.run(optimizer._execute_optimizer_action(action))

    assert bridge.self_consumption_attempts == 1
    assert command.state == "Maximize Self Consumption"
    assert native_controller.last_mutation["outcome"] == "confirmed"
    assert native_controller.last_mutation["operation"] == "set_self_consumption"
    assert optimizer._last_executed_planned_action == "self_consumption"
    assert optimizer._last_executed_action == "self_consumption"


def test_unavailable_native_self_use_keeps_consume_retryable(opt_module, monkeypatch):
    optimizer, action, bridge, native_controller, command = _native_optimizer_stack(
        opt_module, monkeypatch, native_self_use_available=False
    )

    asyncio.run(optimizer._execute_optimizer_action(action))
    asyncio.run(optimizer._execute_optimizer_action(action))

    assert bridge.self_consumption_attempts == 2
    assert command.state == "Stop"
    assert native_controller.last_mutation["outcome"] == "rejected"
    assert optimizer._last_executed_planned_action is None
    assert optimizer._last_executed_action == "export"


def test_reconciled_native_mode_with_unavailable_remote_fields_is_deduplicated(
    opt_module, system
):
    (
        optimizer,
        action,
        bridge,
        native_controller,
        upstream,
        entity_services,
    ) = _reconciled_native_optimizer_stack(opt_module, system)
    refreshes_after_reconciliation = upstream.refreshes

    asyncio.run(optimizer._execute_optimizer_action(action))
    asyncio.run(optimizer._execute_optimizer_action(action))

    assert bridge.self_consumption_attempts == 1
    assert upstream.refreshes == refreshes_after_reconciliation + 1
    assert entity_services.calls == []
    assert native_controller.last_mutation["outcome"] == "confirmed"
    assert native_controller.last_mutation["confirmation_source"] == (
        "fresh_native_self_consumption_poll"
    )
    assert optimizer._last_executed_planned_action == "self_consumption"
    assert optimizer._last_executed_action == "self_consumption"


def test_stale_native_poll_keeps_reconciled_consume_retryable(opt_module, system):
    (
        optimizer,
        action,
        bridge,
        native_controller,
        upstream,
        entity_services,
    ) = _reconciled_native_optimizer_stack(opt_module, system)
    upstream.advances = False

    asyncio.run(optimizer._execute_optimizer_action(action))
    asyncio.run(optimizer._execute_optimizer_action(action))

    assert bridge.self_consumption_attempts == 2
    assert entity_services.calls == []
    assert native_controller.last_mutation["outcome"] == "rejected"
    assert native_controller.last_mutation["operation"] == "set_self_consumption"
    assert optimizer._last_executed_planned_action is None
    assert optimizer._last_executed_action == "export"
