"""Regression tests for RSV-4 (OB-3 + OB-8).

OB-3: Tesla has no ``set_no_discharge_mode`` primitive, so the scheduled
EV-preserve path falls back to ``_set_idle_hold_mode(preserve_charge=True)``,
which raises the backup reserve toward SOC exactly like an IDLE hold — but
records ``_last_executed_action = "no_discharge"``, not ``"idle"``. In
``disable()`` the pre-IDLE backup-reserve restore was gated on
``_last_executed_action == "idle"``, so it never ran for this path, and the
sibling EV no-discharge release only restores work mode (never touches
``_pre_idle_backup_reserve``) — the elevated reserve was stranded
indefinitely for Tesla users on a scheduled EV-preserve window.

OB-8: enabling monitoring mode fires a ``restore_normal`` cleanup that
releases force modes/native control, but never restores an
IDLE/EV-elevated backup reserve, and the restore-side monitoring gate then
blocks the optimizer's own retries — reserve stuck elevated forever.

Fix: split the reserve restore in ``disable()`` from the work-mode restore
(gate the former on ``_pre_idle_backup_reserve is not None``, independent of
``_last_executed_action``; the latter stays gated on ``== "idle"`` only, to
avoid double-firing ``restore_work_mode_from_idle`` against the EV
no-discharge release's own restore). ``_restore_pre_idle_backup_reserve``
gains a ``bypass_monitoring`` parameter used by exactly one caller: the
``ProviderConfigView.post`` monitoring-enable branch in ``__init__.py``,
fired after ``SERVICE_RESTORE_NORMAL`` succeeds.
"""

from __future__ import annotations

import ast
import asyncio
import importlib
import sys
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

ROOT = Path(__file__).resolve().parent.parent
INIT_PATH = ROOT / "custom_components" / "power_sync" / "__init__.py"
COORDINATOR_PATH = (
    ROOT / "custom_components" / "power_sync" / "optimization" / "coordinator.py"
)


@pytest.fixture()
def opt_module():
    saved_modules = {
        name: sys.modules.get(name, _SENTINEL) for name in _STUB_MODULE_NAMES
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


def _disable_coordinator(opt_module, *, last_executed_action, pre_idle_reserve, ev_active):
    coordinator = object.__new__(opt_module.OptimizationCoordinator)
    coordinator._enabled = True
    coordinator.entry_id = "entry-1"
    coordinator.hass = SimpleNamespace(data={"power_sync": {"entry-1": {}}})
    coordinator._monitoring_mode_active = lambda: False
    coordinator._last_executed_action = last_executed_action
    coordinator._pre_idle_backup_reserve = pre_idle_reserve
    coordinator._idle_hold_reserve = pre_idle_reserve
    coordinator._scheduled_ev_no_discharge_active = ev_active
    coordinator._polling_task = None
    coordinator._initial_opt_task = None
    coordinator._deferred_restore_task = None
    coordinator._settings_reoptimize_task = None
    coordinator._price_reoptimize_task = None
    coordinator._price_listener_unsub = None
    coordinator._octopus_gate_listener_unsub = None
    coordinator._executor = None
    coordinator._ev_coordinator = None
    coordinator._cost_store = SimpleNamespace(async_save=_noop_async)
    coordinator._cost_data_to_save = lambda: {}
    return coordinator


# ---------------------------------------------------------------------------
# OB-3: disable() must restore the reserve stranded by Tesla's EV-preserve
# no_discharge path, not just the idle path.
# ---------------------------------------------------------------------------


def test_s4_disable_restores_reserve_stranded_by_tesla_ev_preserve(opt_module):
    """S4: ``_last_executed_action == "no_discharge"`` (Tesla EV-preserve,
    no ``set_no_discharge_mode`` primitive) with a pending elevated reserve
    — ``disable()`` must still restore it. Pre-fix this was gated on
    ``== "idle"`` and never ran, stranding the reserve at ~SOC."""

    async def _run():
        coordinator = _disable_coordinator(
            opt_module,
            last_executed_action="no_discharge",
            pre_idle_reserve=78,
            ev_active=True,
        )
        call_order = []

        async def set_backup_reserve(pct):
            call_order.append(("reserve_restore", pct))
            return True

        async def restore_no_discharge_mode():
            call_order.append(("ev_release",))
            return True

        coordinator.battery_controller = SimpleNamespace(
            set_backup_reserve=set_backup_reserve
        )
        coordinator.energy_coordinator = SimpleNamespace(
            restore_no_discharge_mode=restore_no_discharge_mode
        )

        await coordinator.disable()

        assert ("reserve_restore", 78) in call_order
        assert coordinator._pre_idle_backup_reserve is None
        # Reserve restore must run BEFORE the EV no-discharge release —
        # the release only restores work mode, not reserve, so it must
        # not be assumed to have already handled it.
        assert call_order.index(("reserve_restore", 78)) < call_order.index(
            ("ev_release",)
        )

    asyncio.run(_run())


def test_disable_reserve_restore_skipped_under_monitoring_but_stays_pending(opt_module):
    """Monitoring mode must still block the reserve write (RC-5's one
    surviving sub-case), but the pending reserve must NOT be dropped —
    it must remain available for a later restore (e.g. the OB-8 fix, or
    ``_should_restore_pre_idle_backup_reserve_from_polling``)."""

    async def _run():
        coordinator = _disable_coordinator(
            opt_module,
            last_executed_action="no_discharge",
            pre_idle_reserve=78,
            ev_active=False,
        )
        coordinator._monitoring_mode_active = lambda: True

        calls = []

        async def set_backup_reserve(pct):
            calls.append(pct)
            return True

        coordinator.battery_controller = SimpleNamespace(
            set_backup_reserve=set_backup_reserve
        )
        coordinator.energy_coordinator = None

        await coordinator.disable()

        assert calls == []
        assert coordinator._pre_idle_backup_reserve == 78

    asyncio.run(_run())


def test_disable_stops_executor_without_restore_writes_under_monitoring(opt_module):
    """An ordinary reload while already monitoring must perform zero restores."""

    async def _run():
        coordinator = _disable_coordinator(
            opt_module,
            last_executed_action=None,
            pre_idle_reserve=None,
            ev_active=False,
        )
        coordinator._monitoring_mode_active = lambda: True
        restore_flags = []

        async def stop(*, restore_normal):
            restore_flags.append(restore_normal)

        coordinator._executor = SimpleNamespace(stop=stop)
        coordinator.battery_controller = None
        coordinator.energy_coordinator = None

        await coordinator.disable()

        assert restore_flags == [False]

    asyncio.run(_run())


def test_disable_preserves_explicit_monitoring_enable_handoff(opt_module):
    """A real off-to-on transition keeps the established one-time cleanup."""

    async def _run():
        coordinator = _disable_coordinator(
            opt_module,
            last_executed_action=None,
            pre_idle_reserve=None,
            ev_active=False,
        )
        coordinator._monitoring_mode_active = lambda: True
        coordinator.hass.data["power_sync"]["entry-1"][
            "_monitoring_enable_restore_pending"
        ] = True
        restore_flags = []

        async def stop(*, restore_normal):
            restore_flags.append(restore_normal)

        coordinator._executor = SimpleNamespace(stop=stop)
        coordinator.battery_controller = None
        coordinator.energy_coordinator = None

        await coordinator.disable()

        assert restore_flags == [True]
        assert (
            "_monitoring_enable_restore_pending"
            not in coordinator.hass.data["power_sync"]["entry-1"]
        )

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# S6: the FoxESS/Sungrow work-mode restore must stay gated on
# _last_executed_action == "idle" only, so it never double-fires alongside
# the EV no-discharge release's own work-mode restore.
# ---------------------------------------------------------------------------


def test_s6_no_double_fire_of_restore_work_mode_from_idle(opt_module):
    """With ``_last_executed_action == "no_discharge"`` (not "idle") and the
    EV no-discharge release falling back to ``restore_work_mode_from_idle``,
    that method must be called at most once per disable() — never once from
    a (hypothetically widened) idle-gated block AND again from the EV
    release path."""

    async def _run():
        coordinator = _disable_coordinator(
            opt_module,
            last_executed_action="no_discharge",
            pre_idle_reserve=None,
            ev_active=True,
        )
        calls = []

        async def restore_work_mode_from_idle():
            calls.append(True)
            return True

        coordinator.battery_controller = None
        coordinator.energy_coordinator = SimpleNamespace(
            restore_work_mode_from_idle=restore_work_mode_from_idle
        )

        await coordinator.disable()

        assert len(calls) == 1

    asyncio.run(_run())


def test_disable_idle_path_unchanged(opt_module):
    """Sanity: the ordinary IDLE disable path (no EV preserve involved)
    still restores both reserve and work mode exactly once."""

    async def _run():
        coordinator = _disable_coordinator(
            opt_module,
            last_executed_action="idle",
            pre_idle_reserve=55,
            ev_active=False,
        )
        reserve_calls = []
        work_mode_calls = []

        async def set_backup_reserve(pct):
            reserve_calls.append(pct)
            return True

        async def restore_work_mode_from_idle():
            work_mode_calls.append(True)
            return True

        coordinator.battery_controller = SimpleNamespace(
            set_backup_reserve=set_backup_reserve
        )
        coordinator.energy_coordinator = SimpleNamespace(
            restore_work_mode_from_idle=restore_work_mode_from_idle
        )

        await coordinator.disable()

        assert reserve_calls == [55]
        assert work_mode_calls == [True]
        assert coordinator._pre_idle_backup_reserve is None

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# bypass_monitoring: _restore_pre_idle_backup_reserve
# ---------------------------------------------------------------------------


def _restore_coordinator(opt_module, *, pre_idle_reserve, monitoring_active):
    coordinator = object.__new__(opt_module.OptimizationCoordinator)
    coordinator._pre_idle_backup_reserve = pre_idle_reserve
    coordinator._idle_hold_reserve = pre_idle_reserve
    coordinator._monitoring_mode_active = lambda: monitoring_active
    return coordinator


def test_bypass_monitoring_true_skips_monitoring_block(opt_module):
    """OB-8: the one sanctioned bypass caller must be able to force the
    restore through even while monitoring mode is active."""

    async def _run():
        coordinator = _restore_coordinator(
            opt_module, pre_idle_reserve=45, monitoring_active=True
        )
        calls = []

        async def set_backup_reserve(pct):
            calls.append(pct)
            return True

        battery = SimpleNamespace(set_backup_reserve=set_backup_reserve)

        result = await coordinator._restore_pre_idle_backup_reserve(
            battery, "monitoring enabled", bypass_monitoring=True
        )

        assert result is True
        assert calls == [45]
        assert coordinator._pre_idle_backup_reserve is None

    asyncio.run(_run())


def test_bypass_monitoring_default_false_preserves_existing_block(opt_module):
    """Default behavior (no bypass) must be unchanged: monitoring mode
    still blocks the write and leaves the reserve pending."""

    async def _run():
        coordinator = _restore_coordinator(
            opt_module, pre_idle_reserve=45, monitoring_active=True
        )
        calls = []

        async def set_backup_reserve(pct):
            calls.append(pct)
            return True

        battery = SimpleNamespace(set_backup_reserve=set_backup_reserve)

        result = await coordinator._restore_pre_idle_backup_reserve(
            battery, "some other context"
        )

        assert result is False
        assert calls == []
        assert coordinator._pre_idle_backup_reserve == 45

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# S11 (OB-8): __init__.py ProviderConfigView.post monitoring-enable branch.
# The handler needs a live aiohttp request/hass to execute end-to-end, so
# (mirroring tests/test_sungrow_curtailment_runtime.py) this asserts the
# fix structurally via AST source extraction: the bypass restore call must
# exist, and must run AFTER the SERVICE_RESTORE_NORMAL call.
# ---------------------------------------------------------------------------


def _class_method_source(path: Path, class_name: str, method_name: str) -> str:
    source = path.read_text()
    module = ast.parse(source)
    for node in module.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for child in node.body:
                if (
                    isinstance(child, ast.AsyncFunctionDef)
                    and child.name == method_name
                ):
                    segment = ast.get_source_segment(source, child)
                    assert segment is not None
                    return segment
    raise AssertionError(f"{class_name}.{method_name} not found")


def test_s11_provider_config_post_restores_pre_idle_reserve_after_monitoring_enable():
    source = _class_method_source(INIT_PATH, "ProviderConfigView", "post")

    idx_restore_normal_call = source.index("SERVICE_RESTORE_NORMAL")
    assert "_restore_pre_idle_backup_reserve" in source
    idx_bypass_call = source.index("_restore_pre_idle_backup_reserve")

    assert idx_restore_normal_call < idx_bypass_call, (
        "the pre-IDLE reserve bypass restore must run AFTER "
        "SERVICE_RESTORE_NORMAL, not before/instead of it"
    )
    assert "bypass_monitoring=True" in source
    assert "_pre_idle_backup_reserve" in source


def test_bypass_monitoring_true_has_exactly_one_caller():
    """The coordinator notes are explicit: bypass_monitoring=True must
    never gain a second call site."""

    coordinator_source = COORDINATOR_PATH.read_text()
    init_source = INIT_PATH.read_text()

    total = coordinator_source.count("bypass_monitoring=True") + init_source.count(
        "bypass_monitoring=True"
    )
    assert total == 1
