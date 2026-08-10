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
    def __init__(self, *, apply=True, clear=True):
        self.apply_result = apply
        self.clear_result = clear
        self.entered = []
        self.exited = []

    async def enter_solar_export_hold(self, owner):
        self.entered.append(owner)
        return self.apply_result

    async def exit_solar_export_hold(self, owner):
        self.exited.append(owner)
        return self.clear_result


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
    assert store.data == {}


def test_failed_apply_compensates_and_does_not_report_active():
    store = _Store()
    adapter = _Adapter(apply=False, clear=True)
    controller = SolarExportHoldController(store, adapter)

    assert not asyncio.run(controller.apply("entry", "generation"))
    assert adapter.exited == ["entry"]
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
