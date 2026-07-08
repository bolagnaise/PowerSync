"""Regression coverage for OB-12: AlphaESS curtailment must not drop an
active force/optimizer dispatch.

``inverters/alphaess.py::AlphaESSController.curtail()`` releases the active
dispatch block (writes REG_DISPATCH_START=0) before writing the export
limit register, because on Smile firmware an active dispatch overrides the
export-limit register anyway. That means the *caller* in __init__.py must
never invoke ``controller.curtail()`` while a force/optimizer dispatch is
in flight, or a paid-to-charge/discharge window gets cancelled at the
worst possible moment.

This mirrors the FoxESS (`_foxess_force_dispatch_active`) and SolarEdge
(`_solaredge_force_dispatch_active`) curtailment guards, using the
runtime source-extraction pattern from
tests/test_sungrow_curtailment_runtime.py: pull the handler's source via
``ast``, ``exec`` it with fakes standing in for the free variables normally
supplied by ``async_setup_entry``'s closure, and drive it directly.
"""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parent.parent
INIT_PATH = ROOT / "custom_components" / "power_sync" / "__init__.py"

DOMAIN = "power_sync"
ENTRY_ID = "test_entry"


def _function_source(name: str) -> str:
    """Extract a function nested inside async_setup_entry, by name."""
    source = INIT_PATH.read_text()
    module = ast.parse(source)
    for node in module.body:
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "async_setup_entry":
            for child in node.body:
                if isinstance(child, (ast.AsyncFunctionDef, ast.FunctionDef)) and child.name == name:
                    segment = ast.get_source_segment(source, child)
                    assert segment is not None
                    return segment
    raise AssertionError(f"{name} not found")


class FakeAlphaESSController:
    """Records curtail()/restore() calls without touching real Modbus I/O."""

    def __init__(self, curtail_ok: bool = True, restore_ok: bool = True):
        self.curtail_calls = 0
        self.restore_calls = 0
        self._curtail_ok = curtail_ok
        self._restore_ok = restore_ok

    async def curtail(self) -> bool:
        self.curtail_calls += 1
        return self._curtail_ok

    async def restore(self) -> bool:
        self.restore_calls += 1
        return self._restore_ok


def _load_handler(entry_data: dict, *, force_charge_active: bool = False,
                   force_discharge_active: bool = False,
                   optimizer_force_matches=None):
    """Exec handle_alphaess_curtailment with fakes standing in for the
    free variables normally provided by async_setup_entry's closure."""
    entry = SimpleNamespace(
        options={},
        data={"alphaess_dc_curtailment_enabled": True},
        entry_id=ENTRY_ID,
    )
    hass = SimpleNamespace(data={DOMAIN: {ENTRY_ID: entry_data}})

    def _optimizer_current_force_action_matches(force_type: str) -> bool:
        if optimizer_force_matches is None:
            return False
        return optimizer_force_matches(force_type)

    namespace = {
        "DOMAIN": DOMAIN,
        "hass": hass,
        "entry": entry,
        "CONF_ALPHAESS_DC_CURTAILMENT_ENABLED": "alphaess_dc_curtailment_enabled",
        # Not exercised in these tests since feedin_price is always passed
        # explicitly, but the handler references the name.
        "get_current_prices_for_curtailment": lambda *a, **k: (None, None, None),
        "amber_coordinator": None,
        "localvolts_coordinator": None,
        "aemo_sensor_coordinator": None,
        "flow_power_kwatch_coordinator": None,
        "octopus_coordinator": None,
        "_LOGGER": SimpleNamespace(
            debug=lambda *a, **k: None,
            info=lambda *a, **k: None,
            warning=lambda *a, **k: None,
            error=lambda *a, **k: None,
        ),
        "force_charge_state": {"active": force_charge_active},
        "force_discharge_state": {"active": force_discharge_active},
        "_optimizer_current_force_action_matches": _optimizer_current_force_action_matches,
    }
    exec(_function_source("handle_alphaess_curtailment"), namespace)
    return namespace["handle_alphaess_curtailment"], hass


def _entry_data_with_controller(current_state: str = "normal", **controller_kwargs) -> tuple[dict, FakeAlphaESSController]:
    controller = FakeAlphaESSController(**controller_kwargs)
    entry_data = {
        "alphaess_curtailment_state": current_state,
        "alphaess_coordinator": SimpleNamespace(_controller=controller),
    }
    return entry_data, controller


# export_earnings = -feedin_price; feedin_price=2.0 -> export_earnings=-2.0 (<1c -> curtail)
NEGATIVE_EARNINGS_FEEDIN_PRICE = 2.0
# feedin_price=-5.0 -> export_earnings=5.0 (>=1c -> restore/no curtail)
POSITIVE_EARNINGS_FEEDIN_PRICE = -5.0


def test_alphaess_curtailment_skips_when_force_charge_active():
    """OB-12: an in-flight force/optimizer CHARGE dispatch must survive a
    negative-price curtailment tick — curtail() must not be called."""
    entry_data, controller = _entry_data_with_controller("normal")
    handler, hass = _load_handler(entry_data, force_charge_active=True)

    asyncio.run(handler(feedin_price=NEGATIVE_EARNINGS_FEEDIN_PRICE, import_price=30.0))

    assert controller.curtail_calls == 0, "curtail() must not release an active force dispatch"
    assert hass.data[DOMAIN][ENTRY_ID]["alphaess_curtailment_state"] == "normal"


def test_alphaess_curtailment_skips_when_force_discharge_active():
    """Same guard must apply for an in-flight force/optimizer DISCHARGE."""
    entry_data, controller = _entry_data_with_controller("normal")
    handler, hass = _load_handler(entry_data, force_discharge_active=True)

    asyncio.run(handler(feedin_price=NEGATIVE_EARNINGS_FEEDIN_PRICE, import_price=30.0))

    assert controller.curtail_calls == 0
    assert hass.data[DOMAIN][ENTRY_ID]["alphaess_curtailment_state"] == "normal"


def test_alphaess_curtailment_skips_when_optimizer_reports_active_force_state():
    """The optimization_coordinator.get_active_force_state() path (used when
    the optimizer, not the manual force_charge_state/force_discharge_state
    dicts, owns the dispatch) must also block curtailment."""
    entry_data, controller = _entry_data_with_controller("normal")
    entry_data["optimization_coordinator"] = SimpleNamespace(
        get_active_force_state=lambda: {"active": True, "type": "charge"}
    )
    handler, hass = _load_handler(entry_data)

    asyncio.run(handler(feedin_price=NEGATIVE_EARNINGS_FEEDIN_PRICE, import_price=30.0))

    assert controller.curtail_calls == 0
    assert hass.data[DOMAIN][ENTRY_ID]["alphaess_curtailment_state"] == "normal"


def test_alphaess_curtailment_curtails_when_idle_and_export_earnings_low():
    """Normal, non-force curtailment behavior must be preserved: with no
    force/optimizer dispatch active, low export earnings still triggers
    curtail()."""
    entry_data, controller = _entry_data_with_controller("normal")
    handler, hass = _load_handler(entry_data)

    asyncio.run(handler(feedin_price=NEGATIVE_EARNINGS_FEEDIN_PRICE, import_price=30.0))

    assert controller.curtail_calls == 1
    assert hass.data[DOMAIN][ENTRY_ID]["alphaess_curtailment_state"] == "curtailed"


def test_alphaess_curtailment_restores_when_earnings_recover_and_idle():
    """Sanity check the restore path (positive export earnings) is untouched
    by this fix when no force dispatch is active."""
    entry_data, controller = _entry_data_with_controller("curtailed")
    handler, hass = _load_handler(entry_data)

    asyncio.run(handler(feedin_price=POSITIVE_EARNINGS_FEEDIN_PRICE, import_price=30.0))

    assert controller.restore_calls == 1
    assert hass.data[DOMAIN][ENTRY_ID]["alphaess_curtailment_state"] == "normal"


def test_alphaess_curtailment_handler_source_has_force_dispatch_guard():
    """Static check that the guard mirrors the FoxESS/SolarEdge naming and
    structure (checked before the curtail-transition branch)."""
    handler = _function_source("handle_alphaess_curtailment")

    assert "_alphaess_force_dispatch_active" in handler
    assert "force_charge_state.get(\"active\")" in handler
    assert "get_active_force_state" in handler
    assert "_optimizer_current_force_action_matches(\"charge\")" in handler

    guard_index = handler.index("if _alphaess_force_dispatch_active():")
    curtail_call_index = handler.index("success = await controller.curtail()")
    assert guard_index < curtail_call_index, (
        "the force-dispatch guard must be checked before controller.curtail() is invoked"
    )
