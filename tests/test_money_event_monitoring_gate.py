"""Regression coverage for OB-37: monitoring mode must block ALL hardware
commands issued by the Tesla money-event managers and the demand-charging
toggle, on both entry and exit.

Follows the AST source-extraction pattern from
``tests/test_money_event_manager_optimizer_gate.py`` (itself modeled on
``tests/test_sungrow_curtailment_runtime.py``): rather than hardcoding
absolute line numbers (which drift as `__init__.py` churns), the four
recurring timer-callback closures are located dynamically inside
``async_setup_entry`` by name, then re-embedded verbatim (original
indentation restored from ``col_offset``) inside a stub ``async def
_run(): ...`` and exec'd against a controlled namespace with the manager/
coordinator objects replaced by lightweight call recorders.
"""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
INIT_PATH = ROOT / "custom_components" / "power_sync" / "__init__.py"

CONF_MONITORING_MODE = "monitoring_mode"


def _dedented_source(source: str, node: ast.AST) -> str:
    """Verbatim source for `node`, dedented so it can be exec'd as a
    top-level statement. ``ast.get_source_segment`` strips the first
    line's indentation (it starts at ``col_offset``) but continuation
    lines keep their original absolute indentation, so those need the
    same ``col_offset`` worth of leading spaces stripped."""
    segment = ast.get_source_segment(source, node)
    assert segment is not None, "empty source segment"
    lines = segment.splitlines()
    prefix = " " * node.col_offset
    dedented = [lines[0]] + [
        line[node.col_offset :] if line.startswith(prefix) else line
        for line in lines[1:]
    ]
    return "\n".join(dedented)


def _find_async_setup_entry(module: ast.Module) -> ast.AsyncFunctionDef:
    for node in module.body:
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "async_setup_entry":
            return node
    raise AssertionError("async_setup_entry not found")


def _find_closure(scope: ast.AST, func_name: str) -> ast.AsyncFunctionDef:
    candidates = [
        n
        for n in ast.walk(scope)
        if isinstance(n, ast.AsyncFunctionDef) and n.name == func_name
    ]
    assert len(candidates) == 1, (
        f"expected exactly one {func_name} closure, found {len(candidates)}"
    )
    return candidates[0]


def _locate():
    source = INIT_PATH.read_text()
    module = ast.parse(source)
    entry_fn = _find_async_setup_entry(module)

    return (
        source,
        _find_closure(entry_fn, "auto_aemo_spike_check"),
        _find_closure(entry_fn, "auto_saving_session_check"),
        _find_closure(entry_fn, "auto_demand_charging_check"),
    )


(
    _SOURCE,
    _AEMO_NODE,
    _SAVING_SESSION_NODE,
    _DEMAND_CHARGING_NODE,
) = _locate()


class _Logger:
    def info(self, *a, **k):
        pass

    def debug(self, *a, **k):
        pass

    def warning(self, *a, **k):
        pass

    def error(self, *a, **k):
        pass


def _base_ns(monitoring_mode: bool, **overrides) -> dict:
    ns = dict(
        CONF_MONITORING_MODE=CONF_MONITORING_MODE,
        _LOGGER=_Logger(),
        entry=SimpleNamespace(
            entry_id="entry-1",
            options={CONF_MONITORING_MODE: monitoring_mode},
            data={},
        ),
        hass=SimpleNamespace(data={}),
        dt_util=SimpleNamespace(now=lambda: None),
    )
    ns.update(overrides)

    def _is_monitoring_mode() -> bool:
        return ns["entry"].options.get(
            CONF_MONITORING_MODE, ns["entry"].data.get(CONF_MONITORING_MODE, False)
        )

    ns["_is_monitoring_mode"] = _is_monitoring_mode
    return ns


def _run_closure(node: ast.AsyncFunctionDef, ns: dict):
    src = "\n".join(
        [
            _dedented_source(_SOURCE, node),
            f"_result = {node.name}",
        ]
    )
    exec_ns: dict = {}
    exec(src, ns, exec_ns)
    closure = exec_ns["_result"]
    asyncio.run(closure(None))


class _SpikeManagerRecorder:
    def __init__(self):
        self.calls = 0

    async def check_and_handle_spike(self):
        self.calls += 1


class _SavingSessionRecorder:
    def __init__(self):
        self.calls = 0

    async def check_and_handle_sessions(self):
        self.calls += 1


def test_aemo_spike_check_blocked_by_monitoring_mode():
    manager = _SpikeManagerRecorder()
    ns = _base_ns(monitoring_mode=True, aemo_spike_manager=manager)
    _run_closure(_AEMO_NODE, ns)
    assert manager.calls == 0, (
        "AEMOSpikeManager.check_and_handle_spike must not run (tariff "
        "upload / mode POST) while monitoring mode is active"
    )

    manager = _SpikeManagerRecorder()
    ns = _base_ns(monitoring_mode=False, aemo_spike_manager=manager)
    _run_closure(_AEMO_NODE, ns)
    assert manager.calls == 1, "check must still run when monitoring mode is off"


def test_saving_session_check_blocked_by_monitoring_mode():
    manager = _SavingSessionRecorder()
    ns = _base_ns(monitoring_mode=True, saving_session_tariff_manager=manager)
    _run_closure(_SAVING_SESSION_NODE, ns)
    assert manager.calls == 0, (
        "SavingSessionTariffManager.check_and_handle_sessions must not run "
        "(tariff upload) while monitoring mode is active"
    )

    manager = _SavingSessionRecorder()
    ns = _base_ns(monitoring_mode=False, saving_session_tariff_manager=manager)
    _run_closure(_SAVING_SESSION_NODE, ns)
    assert manager.calls == 1


class _DemandChargeCoordinator:
    def _is_in_peak_period(self, now):
        return True


class _TeslaCoordinator:
    def __init__(self):
        self.grid_charging_calls: list = []

    async def set_grid_charging_enabled(self, enabled: bool) -> bool:
        self.grid_charging_calls.append(enabled)
        return True


def test_demand_charging_check_blocked_by_monitoring_mode():
    dc_coordinator = _DemandChargeCoordinator()
    ts_coordinator = _TeslaCoordinator()
    entry_id = "entry-1"
    hass = SimpleNamespace(
        data={
            "power_sync": {
                entry_id: {
                    "demand_charge_coordinator": dc_coordinator,
                    "tesla_coordinator": ts_coordinator,
                }
            }
        }
    )
    ns = _base_ns(
        monitoring_mode=True,
        hass=hass,
        entry=SimpleNamespace(
            entry_id=entry_id,
            options={CONF_MONITORING_MODE: True},
            data={},
        ),
        DOMAIN="power_sync",
    )
    # _is_monitoring_mode reads ns["entry"] at call time, which was reset above.
    ns["_is_monitoring_mode"] = lambda: ns["entry"].options.get(CONF_MONITORING_MODE, False)
    _run_closure(_DEMAND_CHARGING_NODE, ns)
    assert ts_coordinator.grid_charging_calls == [], (
        "set_grid_charging_enabled must not be called while monitoring mode "
        "is active — it is a direct Tesla Fleet API hardware command"
    )

    ts_coordinator = _TeslaCoordinator()
    hass.data["power_sync"][entry_id]["tesla_coordinator"] = ts_coordinator
    ns["hass"] = hass
    ns["entry"] = SimpleNamespace(
        entry_id=entry_id, options={CONF_MONITORING_MODE: False}, data={}
    )
    ns["_is_monitoring_mode"] = lambda: ns["entry"].options.get(CONF_MONITORING_MODE, False)
    _run_closure(_DEMAND_CHARGING_NODE, ns)
    assert ts_coordinator.grid_charging_calls == [False], (
        "grid charging control must still work when monitoring mode is off"
    )
