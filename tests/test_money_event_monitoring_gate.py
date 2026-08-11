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
from datetime import datetime, timedelta, timezone
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


def _find_closure(
    scope: ast.AST, func_name: str
) -> ast.FunctionDef | ast.AsyncFunctionDef:
    candidates = [
        n
        for n in ast.walk(scope)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        and n.name == func_name
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
        _find_closure(entry_fn, "_demand_grid_charging_protection_active"),
        _find_closure(entry_fn, "_enforce_demand_grid_charging_protection"),
    )


(
    _SOURCE,
    _AEMO_NODE,
    _SAVING_SESSION_NODE,
    _DEMAND_CHARGING_NODE,
    _DEMAND_PROTECTION_NODE,
    _DEMAND_ENFORCEMENT_NODE,
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
        dt_util=SimpleNamespace(
            now=lambda: datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc)
        ),
        timedelta=timedelta,
    )
    ns.update(overrides)

    def _is_monitoring_mode() -> bool:
        return ns["entry"].options.get(
            CONF_MONITORING_MODE, ns["entry"].data.get(CONF_MONITORING_MODE, False)
        )

    ns["_is_monitoring_mode"] = _is_monitoring_mode

    def _aemo_dispatch_entry_data():
        """Mirror the setup-generation resolver for extracted demand helpers."""
        domain = ns["hass"].data.get(ns.get("DOMAIN", "power_sync"), {})
        entry_data = domain.get(ns["entry"].entry_id)
        return entry_data if isinstance(entry_data, dict) else None

    ns["_aemo_dispatch_entry_data"] = _aemo_dispatch_entry_data
    return ns


def _run_closure(node: ast.AsyncFunctionDef, ns: dict):
    if (
        node is _DEMAND_CHARGING_NODE
        and "_demand_grid_charging_protection_active" not in ns
    ):
        ns["_demand_grid_charging_protection_active"] = _run_sync_closure(
            _DEMAND_PROTECTION_NODE, ns
        )
    if (
        node is _DEMAND_CHARGING_NODE
        and "_enforce_demand_grid_charging_protection" not in ns
    ):
        ns["demand_grid_charging_control_lock"] = asyncio.Lock()
        ns["_enforce_demand_grid_charging_protection"] = (
            _run_sync_closure(_DEMAND_ENFORCEMENT_NODE, ns)
        )
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


def _run_sync_closure(
    node: ast.FunctionDef | ast.AsyncFunctionDef, ns: dict
):
    src = "\n".join(
        [
            _dedented_source(_SOURCE, node),
            f"_result = {node.name}",
        ]
    )
    exec_ns: dict = {}
    exec(src, ns, exec_ns)
    return exec_ns["_result"]


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
    def __init__(self, *, peak_start=None, peak_end=None):
        self.enabled = True
        self.peak_start = peak_start
        self.peak_end = peak_end
        self.observed_times = []

    def _is_in_peak_period(self, now):
        self.observed_times.append(now)
        if self.peak_start is None:
            return True
        return self.peak_start <= now < self.peak_end


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


def _demand_callback_namespace(
    now,
    *,
    currently_disabled=False,
    peak_start=None,
    peak_end=None,
):
    peak_start = peak_start or (now + timedelta(minutes=1))
    peak_end = peak_end or (now + timedelta(minutes=21))
    dc_coordinator = _DemandChargeCoordinator(
        peak_start=peak_start,
        peak_end=peak_end,
    )
    ts_coordinator = _TeslaCoordinator()
    entry_id = "entry-1"
    hass = SimpleNamespace(
        data={
            "power_sync": {
                entry_id: {
                    "demand_charge_coordinator": dc_coordinator,
                    "tesla_coordinator": ts_coordinator,
                    "grid_charging_disabled_for_demand": currently_disabled,
                }
            }
        }
    )
    ns = _base_ns(
        monitoring_mode=False,
        hass=hass,
        dt_util=SimpleNamespace(now=lambda: now),
        entry=SimpleNamespace(entry_id=entry_id, options={}, data={}),
        DOMAIN="power_sync",
    )
    return ns, dc_coordinator, ts_coordinator


def test_demand_charging_prearms_one_minute_before_peak_without_widening_tracking():
    now = datetime(2026, 8, 9, 14, 54, tzinfo=timezone.utc)
    ns, dc_coordinator, ts_coordinator = _demand_callback_namespace(now)

    _run_closure(_DEMAND_CHARGING_NODE, ns)

    assert ts_coordinator.grid_charging_calls == [False]
    assert set(dc_coordinator.observed_times) == {
        now,
        now + timedelta(minutes=1),
    }


def test_demand_grid_charging_protection_policy_honors_disabled_and_allow_gates():
    now = datetime(2026, 8, 9, 14, 54, tzinfo=timezone.utc)
    ns, dc_coordinator, _ = _demand_callback_namespace(now)
    protection_active = _run_sync_closure(_DEMAND_PROTECTION_NODE, ns)

    assert protection_active(now) is True
    dc_coordinator.enabled = False
    assert protection_active(now) is False
    dc_coordinator.enabled = True
    ns["hass"].data["power_sync"]["entry-1"][
        "demand_allow_grid_charging"
    ] = True
    assert protection_active(now) is False


def test_demand_charging_only_reenables_after_true_peak_end():
    before_start = datetime(2026, 8, 9, 14, 54, tzinfo=timezone.utc)
    ns, _, ts_coordinator = _demand_callback_namespace(
        before_start, currently_disabled=True
    )

    _run_closure(_DEMAND_CHARGING_NODE, ns)

    # A callback one minute before the start must not re-enable charging.
    assert ts_coordinator.grid_charging_calls == [False]

    at_end = datetime(2026, 8, 9, 15, 15, tzinfo=timezone.utc)
    ns, _, ts_coordinator = _demand_callback_namespace(
        at_end,
        currently_disabled=True,
        peak_start=datetime(2026, 8, 9, 14, 55, tzinfo=timezone.utc),
        peak_end=at_end,
    )
    _run_closure(_DEMAND_CHARGING_NODE, ns)

    assert ts_coordinator.grid_charging_calls == [True]


def test_demand_grid_charging_write_converges_when_boundary_changes_mid_call():
    async def run_case(now_before, now_after, expected_calls, expected_active):
        now_ref = [now_before]
        ns, _, ts_coordinator = _demand_callback_namespace(
            now_before,
            currently_disabled=not expected_calls[0],
            peak_start=datetime(2026, 8, 9, 14, 55, tzinfo=timezone.utc),
            peak_end=datetime(2026, 8, 9, 15, 15, tzinfo=timezone.utc),
        )
        ns["dt_util"] = SimpleNamespace(now=lambda: now_ref[0])

        original_write = ts_coordinator.set_grid_charging_enabled

        async def boundary_changing_write(enabled):
            result = await original_write(enabled)
            if len(ts_coordinator.grid_charging_calls) == 1:
                now_ref[0] = now_after
            return result

        ts_coordinator.set_grid_charging_enabled = boundary_changing_write
        ns["_demand_grid_charging_protection_active"] = _run_sync_closure(
            _DEMAND_PROTECTION_NODE, ns
        )
        ns["demand_grid_charging_control_lock"] = asyncio.Lock()
        enforce = _run_sync_closure(_DEMAND_ENFORCEMENT_NODE, ns)

        success, protection_active = await enforce(ts_coordinator)

        assert success is True
        assert protection_active is expected_active
        assert ts_coordinator.grid_charging_calls == expected_calls
        assert ns["hass"].data["power_sync"]["entry-1"][
            "grid_charging_disabled_for_demand"
        ] is expected_active

    asyncio.run(
        run_case(
            datetime(2026, 8, 9, 14, 53, 59, tzinfo=timezone.utc),
            datetime(2026, 8, 9, 14, 54, tzinfo=timezone.utc),
            [True, False],
            True,
        )
    )
    asyncio.run(
        run_case(
            datetime(2026, 8, 9, 15, 14, 59, tzinfo=timezone.utc),
            datetime(2026, 8, 9, 15, 15, tzinfo=timezone.utc),
            [False, True],
            False,
        )
    )


def test_tou_sync_uses_boundary_rechecking_demand_enforcement():
    module = ast.parse(_SOURCE)
    sync_tou = next(
        node
        for node in ast.walk(module)
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name == "_handle_sync_tou_internal"
    )
    sync_source = ast.get_source_segment(_SOURCE, sync_tou)
    assert sync_source is not None
    assert "await _enforce_demand_grid_charging_protection(" in sync_source
    assert ".set_grid_charging_enabled(" not in sync_source


def _find_demand_timer_call(entry_fn: ast.AsyncFunctionDef) -> ast.Call:
    for node in ast.walk(entry_fn):
        if not isinstance(node, ast.Call):
            continue
        if not (
            isinstance(node.func, ast.Name)
            and node.func.id == "async_track_utc_time_change"
        ):
            continue
        if any(
            isinstance(arg, ast.Name) and arg.id == "auto_demand_charging_check"
            for arg in node.args
        ):
            return node
    raise AssertionError("demand charging timer registration not found")


def test_demand_charging_enforcement_is_minute_aligned():
    module = ast.parse(_SOURCE)
    timer_call = _find_demand_timer_call(_find_async_setup_entry(module))
    second = next(
        keyword.value
        for keyword in timer_call.keywords
        if keyword.arg == "second"
    )
    assert isinstance(second, ast.Constant)
    assert second.value == 0


def test_tesla_force_charge_expiry_is_anchored_before_setup_awaits():
    module = ast.parse(_SOURCE)
    handler = next(
        node
        for node in ast.walk(module)
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name == "handle_force_charge"
    )

    anchor = next(
        node
        for node in ast.walk(handler)
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name)
            and target.id == "tesla_requested_expiry"
            for target in node.targets
        )
    )
    assert isinstance(anchor.value, ast.BinOp)
    assert isinstance(anchor.value.left, ast.Call)
    assert isinstance(anchor.value.left.func, ast.Attribute)
    assert isinstance(anchor.value.left.func.value, ast.Name)
    assert anchor.value.left.func.value.id == "dt_util"
    assert anchor.value.left.func.attr == "now"

    setup_await = next(
        node
        for node in ast.walk(handler)
        if isinstance(node, ast.Await)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
        and node.value.func.id == "_require_tesla_force_grid_charging_baselines"
    )
    assert anchor.lineno < setup_await.lineno

    expiry_use = next(
        node
        for node in ast.walk(handler)
        if isinstance(node, ast.Assign)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Attribute)
        and node.value.func.attr == "astimezone"
        and isinstance(node.value.func.value, ast.Name)
        and node.value.func.value.id == "tesla_requested_expiry"
    )
    assert anchor.lineno < expiry_use.lineno


def test_tesla_force_charge_and_delayed_kick_honor_demand_race_guards():
    module = ast.parse(_SOURCE)
    functions = {
        node.name: node
        for node in ast.walk(module)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name in {
            "handle_force_charge",
            "handle_restore_normal",
            "_tesla_charge_kick",
            "_charge_kick_is_current",
        }
    }
    force_charge = functions["handle_force_charge"]
    force_source = ast.get_source_segment(_SOURCE, force_charge)
    assert force_source is not None

    baseline_await = next(
        node
        for node in ast.walk(force_charge)
        if isinstance(node, ast.Await)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
        and node.value.func.id == "_require_tesla_force_grid_charging_baselines"
    )
    protection_checks_after_baseline = [
        node
        for node in ast.walk(force_charge)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_demand_grid_charging_protection_active"
        and node.lineno > baseline_await.lineno
    ]
    assert protection_checks_after_baseline, (
        "force charge must re-check demand protection after slow Tesla baseline setup"
    )

    grid_apply = next(
        node
        for node in ast.walk(force_charge)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_tesla_force_apply_grid_charging"
        and any(keyword.arg == "is_current" for keyword in node.keywords)
    )
    grid_guard = next(
        keyword.value
        for keyword in grid_apply.keywords
        if keyword.arg == "is_current"
    )
    assert isinstance(grid_guard, ast.Lambda)
    assert any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_demand_grid_charging_protection_active"
        for node in ast.walk(grid_guard)
    )
    grid_guard_source = ast.get_source_segment(_SOURCE, grid_guard)
    assert grid_guard_source is not None
    grid_guard_ns = {
        "_command_generation": [1],
        "_restore_gen": 1,
        "_demand_grid_charging_protection_active": lambda: True,
    }
    grid_guard_fn = eval(grid_guard_source, grid_guard_ns)
    assert grid_guard_fn() is False
    grid_guard_ns["_demand_grid_charging_protection_active"] = lambda: False
    assert grid_guard_fn() is True

    reserve_apply = next(
        node
        for node in ast.walk(force_charge)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_tesla_force_apply_backup_reserve"
    )
    grid_to_reserve = force_source.splitlines()[
        grid_apply.lineno - force_charge.lineno : reserve_apply.lineno - force_charge.lineno
    ]
    assert any("_cleanup_failed_tesla_force_charge" in line for line in grid_to_reserve)

    kick_current = functions["_charge_kick_is_current"]
    kick_current_source = _dedented_source(_SOURCE, kick_current)
    assert "_demand_grid_charging_protection_active()" in kick_current_source
    kick_ns = {
        "_tesla_charge_kick_generation": [1],
        "_command_generation": [1],
        "_tesla_operation_generation": [1],
        "kick_generation": 1,
        "command_generation": 1,
        "operation_generation": 1,
        "_demand_grid_charging_protection_active": lambda: True,
    }
    kick_exec_ns: dict = {}
    exec(
        "\n".join(
            [kick_current_source, "_result = _charge_kick_is_current()"]
        ),
        kick_ns,
        kick_exec_ns,
    )
    assert kick_exec_ns["_result"] is False

    restore_source = ast.get_source_segment(_SOURCE, functions["handle_restore_normal"])
    assert restore_source is not None
    assert "demand_grid_protection_active" in restore_source
    assert "_demand_grid_charging_protection_active()" in restore_source
