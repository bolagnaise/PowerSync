"""Tests for the persisted Profit Max solar-export hold lifecycle."""
from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path

_PATH = (
    Path(__file__).resolve().parent.parent
    / "custom_components"
    / "power_sync"
    / "optimization"
    / "solar_export.py"
)
_SPEC = importlib.util.spec_from_file_location("solar_export_under_test", _PATH)
assert _SPEC and _SPEC.loader
_MODULE = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _MODULE
_SPEC.loader.exec_module(_MODULE)
SolarExportHoldController = _MODULE.SolarExportHoldController


class _Store:
    def __init__(self, initial=None):
        self.data = dict(initial or {})
        self.saved = []

    async def async_load(self):
        return dict(self.data)

    async def async_save(self, value):
        self.data = dict(value)
        self.saved.append(dict(value))


class _Adapter:
    def __init__(self, *, apply=True, clear=True, restore=True):
        self.apply_result = apply
        self.clear_result = clear
        self.restore_result = restore
        self.entered = []
        self.exited = []
        self.restored = []
        self.held = False

    async def prepare_charge_hold(self, owner, generation):
        return {
            "schema_version": 1,
            "adapter": "test.limit.v1",
            "owner_id": owner,
            "plan_generation": generation,
            "targets": [{"id": "battery", "value": 5.0}],
        }

    async def apply_charge_hold(self, plan):
        self.entered.append(plan["owner_id"])
        self.held = self.apply_result
        return self.apply_result

    async def verify_charge_hold(self, plan):
        return self.held

    async def clear_charge_hold(self, plan):
        self.exited.append(plan.get("owner_id"))
        if self.clear_result:
            self.held = False
        return self.clear_result

    async def restore_normal(self, plan):
        self.restored.append(plan.get("owner_id") if plan else None)
        if self.restore_result:
            self.held = False
        return self.restore_result

    async def verify_charge_hold_cleared(self, plan):
        return not self.held

    def migrate_legacy_plan(self, state):
        if state.get("adapter") != "sigenergy":
            return None
        return {
            "schema_version": 1,
            "adapter": "test.limit.v1",
            "owner_id": state.get("owner_id"),
            "targets": [{"id": "battery", "value": 5.0}],
        }


def test_apply_is_idempotent_and_clear_removes_persisted_owner():
    store = _Store()
    adapter = _Adapter()
    controller = SolarExportHoldController(store, adapter)

    assert asyncio.run(controller.apply("entry", "generation"))
    assert asyncio.run(controller.apply("entry", "generation"))
    assert adapter.entered == ["entry"]
    assert controller.active
    assert asyncio.run(controller.clear("transition"))
    assert adapter.exited == ["entry"]
    assert adapter.restored == ["entry"]
    assert store.data == {}


def test_failed_apply_compensates_and_does_not_report_active():
    store = _Store()
    adapter = _Adapter(apply=False, clear=True)
    controller = SolarExportHoldController(store, adapter)

    assert not asyncio.run(controller.apply("entry", "generation"))
    assert adapter.exited == ["entry"]
    assert adapter.restored == ["entry"]
    assert not controller.active
    assert store.data == {}


def test_failed_clear_persists_retry_and_startup_reconciles():
    state = {
        "phase": "active",
        "owner_id": "entry",
        "plan_generation": "old",
        "adapter": "sigenergy",
    }
    store = _Store(state)
    adapter = _Adapter(clear=False)
    controller = SolarExportHoldController(store, adapter)

    assert not asyncio.run(controller.async_reconcile_startup())
    assert controller.status["phase"] == "clear_pending"
    adapter.clear_result = True
    assert asyncio.run(controller.async_reconcile_startup())
    assert store.data == {}


def test_failed_clear_still_restores_normal_and_retains_retry_state():
    store = _Store()
    adapter = _Adapter(clear=False, restore=True)
    controller = SolarExportHoldController(store, adapter)

    assert asyncio.run(controller.apply("entry", "generation"))
    assert not asyncio.run(controller.clear("transition"))
    assert adapter.exited == ["entry"]
    assert adapter.restored == ["entry"]
    assert controller.status["phase"] == "clear_pending"


def test_dashboard_prefers_additive_solar_export_details():
    dashboard = (
        Path(__file__).resolve().parent.parent
        / "custom_components"
        / "power_sync"
        / "frontend"
        / "power-sync-strategy.js"
    ).read_text()

    assert "data.current_action_detail || data.current_action" in dashboard
    assert "action: action.action_detail || action.action" in dashboard
    assert "solar_export: { label: 'Solar Export'" in dashboard
