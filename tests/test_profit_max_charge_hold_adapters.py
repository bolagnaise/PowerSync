"""Provider conformance tests for Profit Max charge-hold adapters."""
from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

_PATH = (
    Path(__file__).resolve().parent.parent
    / "custom_components"
    / "power_sync"
    / "optimization"
    / "solar_export.py"
)
_SPEC = importlib.util.spec_from_file_location("solar_export_adapters", _PATH)
assert _SPEC and _SPEC.loader
_MODULE = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _MODULE
_SPEC.loader.exec_module(_MODULE)
resolve_solar_export_adapter = _MODULE.resolve_solar_export_adapter
SolarExportHoldController = _MODULE.SolarExportHoldController


class _Store:
    def __init__(self):
        self.data = {}
        self.saved = []

    async def async_load(self):
        return dict(self.data)

    async def async_save(self, value):
        self.data = dict(value)
        self.saved.append(dict(value))


class _SolaxController:
    def __init__(self):
        self._entity_map = {"charge_current": "number.solax_charge_current"}
        self.value = 25.0
        self.writes = []

    def _ensure_entity_map(self):
        return None

    async def _ensure_connected(self):
        return None

    def _read_float(self, key):
        return self.value if key == "charge_current" else None

    async def _set_number(self, key, value):
        assert key == "charge_current"
        self.writes.append(float(value))
        self.value = float(value)


class _NeovoltChild:
    def __init__(self, mode="Normal", *, fail_hold=False):
        self.mode = mode
        self.fail_hold = fail_hold
        self.hold_calls = 0
        self.restore_calls = []

    def get_dispatch_mode(self):
        return self.mode

    async def set_no_battery_charge(self):
        self.hold_calls += 1
        if self.fail_hold:
            return False
        self.mode = "No Battery Charge"
        return True

    async def restore_normal(self, target_mode=None):
        self.restore_calls.append(target_mode)
        self.mode = target_mode or "Normal"
        return True


class _FroniusController:
    def __init__(self, api_mode):
        self.api_mode = api_mode
        self.storage_mode = "Auto"
        self.ensure_calls = []
        self.mode_writes = []
        # The solar-export hold must not depend on this entity being present.
        self._entity_map = {"storage_control_mode": "select.storage_control_mode"}

    def _ensure_command_entities(self, required, *, available_required=()):
        self.ensure_calls.append((required, available_required))
        return all(key in self._entity_map for key in required)

    def get_status(self):
        return {"mode": self.storage_mode}

    async def block_charging(self):
        self.mode_writes.append("Block Charging")
        self.storage_mode = "Block Charging"
        return True

    async def restore_normal(self):
        self.mode_writes.append("Auto")
        self.storage_mode = "Auto"
        return True


def test_solax_limit_adapter_captures_applies_verifies_and_restores_exact_value():
    controller = _SolaxController()
    coordinator = SimpleNamespace(
        _controller=controller,
        data={},
        restore_normal=lambda: None,
    )
    adapter = resolve_solar_export_adapter("solax", coordinator)

    plan = asyncio.run(adapter.prepare_charge_hold("entry", "slot-1"))
    assert plan["targets"] == [{"id": "battery", "value": 25.0}]
    assert controller.writes == []

    assert asyncio.run(adapter.apply_charge_hold(plan))
    assert asyncio.run(adapter.verify_charge_hold(plan))
    assert controller.writes == [0.0]

    assert asyncio.run(adapter.clear_charge_hold(plan))
    assert asyncio.run(adapter.restore_normal(plan))
    assert asyncio.run(adapter.verify_charge_hold_cleared(plan))
    assert controller.writes == [0.0, 25.0, 25.0]


def test_multi_neovolt_partial_apply_compensates_every_stack():
    first = _NeovoltChild()
    second = _NeovoltChild(fail_hold=True)
    async def restore_all():
        results = [
            await child.restore_normal("Normal") for child in (first, second)
        ]
        return all(results)

    coordinator = SimpleNamespace(
        _controller=SimpleNamespace(_controllers=[first, second]),
        data={},
        restore_normal=restore_all,
    )
    adapter = resolve_solar_export_adapter("neovolt", coordinator)
    store = _Store()
    lifecycle = SolarExportHoldController(store, adapter)

    assert not asyncio.run(lifecycle.apply("entry", "slot-1"))
    assert first.hold_calls == 1
    assert second.hold_calls == 1
    assert first.restore_calls == ["Normal", "Normal"]
    assert second.restore_calls == ["Normal", "Normal"]
    assert store.data == {}


def test_fronius_solar_export_hold_preserves_battery_api_mode_through_lifecycle():
    for initial_api_mode in ("Auto", "Manual"):
        controller = _FroniusController(initial_api_mode)
        coordinator = SimpleNamespace(
            _controller=controller,
            data={},
            restore_normal=controller.restore_normal,
        )
        adapter = resolve_solar_export_adapter("fronius_reserva", coordinator)
        lifecycle = SolarExportHoldController(_Store(), adapter)

        capability = adapter.capability()
        assert capability.supported
        assert controller.ensure_calls[-1] == (
            ("storage_control_mode",),
            ("storage_control_mode",),
        )
        assert asyncio.run(lifecycle.apply("entry", "slot-1"))
        assert lifecycle.status["phase"] == "active"
        assert controller.storage_mode == "Block Charging"
        assert controller.api_mode == initial_api_mode

        assert asyncio.run(lifecycle.clear("transition"))
        assert controller.storage_mode == "Auto"
        assert controller.api_mode == initial_api_mode
        assert controller.mode_writes == ["Block Charging", "Auto", "Auto"]


def test_consecutive_solar_export_slots_keep_original_restore_baseline():
    controller = _SolaxController()
    coordinator = SimpleNamespace(_controller=controller, data={})
    adapter = resolve_solar_export_adapter("solax", coordinator)
    lifecycle = SolarExportHoldController(_Store(), adapter)

    assert asyncio.run(lifecycle.apply("entry", "slot-1"))
    assert asyncio.run(lifecycle.apply("entry", "slot-2"))
    assert controller.writes == [0.0]
    assert lifecycle.status["plan_generation"] == "slot-2"
    assert lifecycle.status["plan"]["targets"][0]["value"] == 25.0

    assert asyncio.run(lifecycle.clear("transition"))
    assert controller.value == 25.0


def test_zero_or_unknown_normal_limit_fails_closed_and_restores_provider_normal():
    controller = _SolaxController()
    controller.value = 0.0

    async def restore_normal():
        controller.value = 25.0
        return True

    adapter = resolve_solar_export_adapter(
        "solax",
        SimpleNamespace(
            _controller=controller,
            data={},
            restore_normal=restore_normal,
        ),
    )
    lifecycle = SolarExportHoldController(_Store(), adapter)

    assert not asyncio.run(lifecycle.apply("entry", "slot-1"))
    assert controller.value == 25.0
    assert not lifecycle.active


def test_mismatched_persisted_adapter_fails_closed_and_retains_cleanup():
    controller = _SolaxController()
    adapter = resolve_solar_export_adapter(
        "solax", SimpleNamespace(_controller=controller, data={})
    )
    store = _Store()
    lifecycle = SolarExportHoldController(store, adapter)
    lifecycle._state = {
        "schema_version": 2,
        "phase": "active",
        "adapter": "foxess.modbus.max_charge_current.v1",
        "plan": {
            "schema_version": 1,
            "adapter": "foxess.modbus.max_charge_current.v1",
            "targets": [{"id": "battery", "value": 40.0}],
        },
    }

    assert not asyncio.run(lifecycle.clear("startup_reconciliation"))
    assert lifecycle.status["phase"] == "clear_pending"
    assert controller.writes == []
