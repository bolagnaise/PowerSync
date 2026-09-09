"""Exercise reconciliation through the real guarded upstream readback."""

import asyncio
import copy
import sys

import pytest
from test_solaredge_controller import SolarEdgeEnergyController, _MemoryStore, _SEHass
from test_solaredge_readback import (
    _Coordinator,
    _Device,
    _Entry,
    _Hub,
    _install_ha_modules,
    _Inverter,
)


@pytest.fixture
def system(monkeypatch):
    entry = _Entry()
    device = _Device({"entry-a"}, {("solaredge_modbus_multi", "SE5000_SERIAL-A")})
    _install_ha_modules(monkeypatch, entry, device)
    enums = sys.modules["custom_components.solaredge_modbus_multi.const"]
    enums.STORAGE_MODE.update(
        {3: "Charge from Solar Power and Grid", 4: "Discharge to Maximize Export"}
    )
    inverter = _Inverter("SE5000_SERIAL-A")
    inverter.decoded_storage_control.update(
        control_mode=1,
        ac_charge_policy=1,
        default_mode=7,
        command_mode=65535,
        charge_limit=11400.0,
        discharge_limit=11400.0,
        command_timeout=3600,
        backup_reserve=10.0,
    )
    coordinator = _Coordinator()
    hub = _Hub([inverter])
    coordinator._hub = hub
    hass = _SEHass()
    hass.data = {
        "solaredge_modbus_multi": {"entry-a": {"hub": hub, "coordinator": coordinator}}
    }
    for entity in (
        "select.solaredge_storage_command_mode",
        "number.solaredge_storage_charge_limit",
        "number.solaredge_storage_discharge_limit",
        "number.solaredge_storage_command_timeout",
    ):
        hass.states.get(entity).state = "unavailable"
    reserve = hass.states.get("number.solaredge_backup_reserve")
    if reserve:
        reserve.state = "10"
    hass.states.get("switch.solaredge_allow_grid_charge").state = "on"
    store = _MemoryStore()
    store.data = {
        "health": "reconciliation_required",
        "generation": 8,
        "intent_generation": 8,
        "baseline": {
            "storage_control_mode": "Remote Control",
            "storage_command_mode": "Maximize Self Consumption",
            "charge_power_limit": 11400,
            "discharge_power_limit": 11400,
            "command_timeout": 3600,
            "backup_reserve": 10,
            "allow_grid_charge": "on",
        },
        "owned": {"command_timeout": 240, "charge_power_limit": 0},
        "pending_mutation": {
            "operation": "force_discharge",
            "entity_id": "number.solaredge_storage_discharge_limit",
            "intended_value": 5000,
        },
        "in_progress": True,
    }
    monkeypatch.setattr(
        SolarEdgeEnergyController, "_create_store", lambda self, identity: store
    )
    controller = SolarEdgeEnergyController(hass, entity_prefix="solaredge")
    return controller, hass, inverter, coordinator, hub, store, entry


def test_native_site_snapshot_reconciles_then_blocks_stale_dispatch(system):
    async def scenario():
        controller, hass, _, coordinator, _, store, _ = system
        result = await controller.reconcile_result()
        assert result["success"]
        assert result["confirmation_source"] == "fresh_native_self_consumption_poll"
        assert store.data["health"] == "ready"
        assert store.data["baseline"] is None
        assert store.data["owned"] == {}
        assert coordinator.refreshes == 1
        assert not await controller.force_charge(15, 2000)
        assert not await controller.force_discharge(15, 2000)
        assert await controller.restore_normal()
        assert hass.services.calls == []
        assert controller._coordinator().baseline is None

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "failure,reason",
    [
        ("identity", "identity_mismatch"),
        ("timestamp", "readback_not_fresh"),
        ("object", "readback_not_fresh"),
        ("poll", "readback_not_fresh"),
        ("busy", "upstream_write_busy"),
    ],
)
def test_upstream_failure_preserves_journal(system, failure, reason):
    controller, hass, _, coordinator, hub, store, entry = system
    before = copy.deepcopy(store.data)
    if failure == "identity":
        entry.unique_id = "other_storage_command_mode"
    elif failure == "timestamp":
        coordinator.advances = False
    elif failure == "object":
        coordinator.replaces_storage = False
    elif failure == "poll":
        coordinator.succeeds = False
    else:
        hub.has_write = 57358
    result = asyncio.run(controller.reconcile_result())
    assert not result["success"]
    assert result["reason"] == reason
    assert controller.control_health == "reconciliation_required"
    assert store.data == before
    assert hass.services.calls == []


@pytest.mark.parametrize(
    "field,value,reason",
    [
        ("command_mode", None, "malformed_snapshot"),
        ("charge_limit", -1, "malformed_snapshot"),
        ("discharge_limit", float("inf"), "malformed_snapshot"),
        ("command_timeout", 0xFFFFFFFF, "malformed_snapshot"),
        ("default_mode", None, "malformed_snapshot"),
        ("default_mode", 4, "active_command"),
        ("command_mode", 3, "active_command"),
    ],
)
def test_remote_invalid_or_active_state_rejected(system, field, value, reason):
    controller, hass, inverter, _, _, store, _ = system
    inverter.decoded_storage_control.update(control_mode=4, command_mode=7)
    inverter.decoded_storage_control[field] = value
    before = copy.deepcopy(store.data)
    result = asyncio.run(controller.reconcile_result())
    assert not result["success"]
    assert result["reason"] == reason
    assert store.data == before
    assert controller.control_health == "reconciliation_required"
    assert hass.services.calls == []


def test_persistence_failure_restores_full_safety_state(system, monkeypatch):
    async def scenario():
        controller, hass, _, _, _, store, _ = system
        session = controller._coordinator()
        await controller._load_session(session)
        before = copy.deepcopy(
            {
                key: getattr(session, key)
                for key in (
                    "baseline",
                    "owned",
                    "pending_mutation",
                    "last_mutation",
                    "health",
                    "generation",
                    "intent_generation",
                )
            }
        )

        async def fail(data):
            raise OSError("storage failure")

        monkeypatch.setattr(store, "async_save", fail)
        result = await controller.reconcile_result()
        assert result["reason"] == "persistence_failed"
        assert {key: getattr(session, key) for key in before} == before
        assert hass.services.calls == []

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "field,value", [("backup_reserve", 11.0), ("ac_charge_policy", 0)]
)
def test_native_applicable_baseline_mismatch_stays_blocked(system, field, value):
    controller, hass, inverter, _, _, store, _ = system
    inverter.decoded_storage_control[field] = value
    before = copy.deepcopy(store.data)
    result = asyncio.run(controller.reconcile_result())
    assert result["reason"] == "baseline_mismatch"
    expected = "backup_reserve" if field == "backup_reserve" else "allow_grid_charge"
    assert expected in result["fields"]
    assert store.data == before
    assert hass.services.calls == []


@pytest.mark.parametrize("mismatch", [False, True])
def test_live_service_path_uses_coordinator_controller_and_upstream(system, mismatch):
    import ast
    from pathlib import Path

    from test_solaredge_runtime import _load_node, _setup_node

    controller, hass, inverter, upstream, _, store, _ = system
    if mismatch:
        inverter.decoded_storage_control["backup_reserve"] = 11.0
    path = (
        Path(__file__).resolve().parents[1]
        / "custom_components/power_sync/coordinator.py"
    )
    klass = next(
        node
        for node in ast.parse(path.read_text()).body
        if getattr(node, "name", None) == "SolarEdgeEnergyCoordinator"
    )
    methods = {
        name: _load_node(
            next(node for node in klass.body if getattr(node, "name", None) == name), {}
        )
        for name in ("reconcile_result", "reconcile", "_control_result")
    }
    coordinator = type("CoordinatorMethods", (), methods)()
    coordinator._controller = controller
    coordinator.data = {"battery_level": 65}
    coordinator.async_set_updated_data = lambda data: setattr(coordinator, "data", data)
    hass.data["power_sync"] = {"entry": {"solaredge_coordinator": coordinator}}
    handler = _load_node(
        _setup_node("handle_reconcile_solaredge_control"),
        {
            "hass": hass,
            "DOMAIN": "power_sync",
            "HomeAssistantError": RuntimeError,
        },
    )
    call = type(
        "Call",
        (),
        {"data": {"entry_id": "entry", "acknowledge": True}, "return_response": True},
    )()
    result = asyncio.run(handler(call))
    assert result["success"] is (not mismatch)
    assert result["reason"] == ("baseline_mismatch" if mismatch else "reconciled")
    assert coordinator.data["control_health"] == (
        "reconciliation_required" if mismatch else "ready"
    )
    assert store.data["health"] == coordinator.data["control_health"]
    assert upstream.refreshes == 1
    assert hass.services.calls == []
