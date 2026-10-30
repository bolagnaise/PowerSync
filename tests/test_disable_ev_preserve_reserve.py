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
shared monitoring transition helper, fired only for a false-to-true change.
"""

from __future__ import annotations

import ast
import asyncio
import importlib
import logging
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

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
MONITORING_PATH = ROOT / "custom_components" / "power_sync" / "monitoring.py"


def _tesla_handoff_runtime(opt_module, *, confirmed=True):
    """Join the real coordinator, wrapper, monitoring gate and reserve handler."""
    from conftest import load_power_sync_function
    from power_sync.optimization.battery_controller import BatteryControllerWrapper

    entry = SimpleNamespace(entry_id="entry-1", data={}, options={})
    runtime = {}
    hass = SimpleNamespace(data={"power_sync": {entry.entry_id: runtime}})
    coordinator = object.__new__(opt_module.OptimizationCoordinator)
    coordinator.hass = hass
    coordinator.entry_id = entry.entry_id
    coordinator.battery_system = "tesla"
    coordinator._entry = entry
    coordinator._execute_lock = asyncio.Lock()
    coordinator._pre_idle_backup_reserve = 30
    coordinator._idle_hold_reserve = 78
    coordinator._startup_backup_reserve = 30
    coordinator._idle_reserve_adjustment = False
    coordinator.battery_controller = BatteryControllerWrapper(hass, "tesla")
    coordinator.battery_controller.entry_id = entry.entry_id
    runtime["optimization_coordinator"] = coordinator
    provider = AsyncMock(return_value={"confirmed": confirmed})
    if confirmed == "exception":
        provider.side_effect = OSError("provider unavailable")
    namespace = dict(
        hass=hass, entry=entry, DOMAIN="power_sync",
        _LOGGER=logging.getLogger(__name__),
        battery_connection_profile=SimpleNamespace(monitoring_only=False),
        alphaess_coordinator=None,
        hold_soc_state={}, _tesla_reserve_generation=[0],
        _tesla_charge_kick_generation=[0], _command_generation=[0],
        _supersede_tesla_charge_kick=lambda reason: 0,
        _stop_native_max_charge=AsyncMock(),
        _get_neovolt_entry_ids=lambda *args: [],
        _get_tesla_site_configs=lambda *args: [("site-1", "test", "fleet")],
        _tesla_force_apply_backup_reserve=provider,
        _tesla_force_result_all_confirmed=lambda result, sites: result["confirmed"],
        refresh_powerwall_local_after_settings_write=AsyncMock(),
    )
    # Use the actual constants even though this test's HA module tree is stubbed.
    constants = {}
    for node in ast.parse((ROOT / "custom_components/power_sync/const.py").read_text()).body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            try:
                constants[node.targets[0].id] = ast.literal_eval(node.value)
            except (ValueError, TypeError):
                pass
    namespace.update({key: value for key, value in constants.items()
                      if key.startswith(("CONF_", "BATTERY_SYSTEM_", "SUNGROW_CONNECTION_"))})
    for name in ("_is_monitoring_mode", "_control_call_source",
                 "_monitoring_mode_should_block_control", "handle_set_backup_reserve"):
        load_power_sync_function(name, namespace)
    calls = []

    async def async_call(domain, service, data, **kwargs):
        calls.append((service, dict(data)))
        if service == "restore_normal":
            return None
        assert service == "set_backup_reserve"
        call = SimpleNamespace(data=data, context=SimpleNamespace(user_id=None))
        return await namespace["handle_set_backup_reserve"](call)

    hass.services = SimpleNamespace(async_call=async_call)
    sys.modules["power_sync.const"].SERVICE_RESTORE_NORMAL = "restore_normal"
    monitoring = importlib.import_module("power_sync.monitoring")
    return coordinator, entry, runtime, provider, calls, namespace, monitoring


@pytest.mark.parametrize("confirmed", [True, False, "exception"])
def test_monitoring_handoff_restores_through_real_tesla_service_gate(opt_module, confirmed):
    """An available provider must be reached; failed readback keeps cleanup pending."""
    async def run():
        c, entry, runtime, provider, calls, env, monitoring = _tesla_handoff_runtime(
            opt_module, confirmed=confirmed
        )
        try:
            if confirmed is True:
                await monitoring.async_prepare_monitoring_handoff(c.hass, entry)
                assert c._pre_idle_backup_reserve is None
                assert c._idle_hold_reserve is None
            else:
                with pytest.raises(RuntimeError, match="pre-IDLE backup reserve restore failed"):
                    await monitoring.async_prepare_monitoring_handoff(c.hass, entry)
                assert c._pre_idle_backup_reserve == 30
                assert c._idle_hold_reserve == 78
            provider.assert_awaited_once_with(
                [("site-1", "test", "fleet")], 30, reason="set backup reserve"
            )
            assert calls[-1][1]["source"] == "optimizer"
            assert c._startup_backup_reserve == 30
            assert entry.options == {}
            assert not c._execute_lock.locked()
        finally:
            monitoring.finish_monitoring_handoff(c.hass, entry)
        assert "_monitoring_handoff_active" not in runtime

    asyncio.run(run())


@pytest.mark.parametrize("handoff,monitoring,marker,target,monitoring_only", [
    (True, False, False, 30, False),  # Ordinary optimizer write during cleanup.
    (True, False, True, 40, False),   # A different reserve is not restoration.
    (False, True, True, 30, False),   # A marker cannot bypass persisted monitoring.
    (True, True, True, 30, False),
    (True, False, True, 30, True),    # A telemetry-only connection cannot write.
])
def test_monitoring_reserve_restore_does_not_authorize_other_writes(
    opt_module, handoff, monitoring, marker, target, monitoring_only
):
    async def run():
        c, entry, runtime, provider, calls, env, _ = _tesla_handoff_runtime(opt_module)
        runtime["_monitoring_handoff_active"] = handoff
        entry.options["monitoring_mode"] = monitoring
        env["battery_connection_profile"].monitoring_only = monitoring_only
        call = SimpleNamespace(data={
            "percent": target, "source": "optimizer",
            "_monitoring_reserve_restore": marker,
        }, context=SimpleNamespace(user_id=None))
        result = await env["handle_set_backup_reserve"](call)
        assert result["success"] is False
        provider.assert_not_awaited()
        assert c._pre_idle_backup_reserve == 30

    asyncio.run(run())


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
    coordinator.battery_system = "sigenergy"
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


def test_disable_defers_solar_export_hold_cleanup_under_monitoring(opt_module):
    async def _run():
        coordinator = _disable_coordinator(
            opt_module,
            last_executed_action=None,
            pre_idle_reserve=None,
            ev_active=False,
        )
        coordinator._enabled = False
        coordinator._monitoring_mode_active = lambda: True
        clear_calls = []

        async def clear(reason):
            clear_calls.append(reason)
            return True

        coordinator._solar_export_hold = SimpleNamespace(
            active=True,
            clear=clear,
        )

        await coordinator.disable()

        assert clear_calls == []

    asyncio.run(_run())


def test_monitoring_handoff_drains_write_then_restores_control_and_reserve(opt_module):
    """False-to-true handoff drains writes and performs one complete cleanup."""
    sys.modules["power_sync.const"].SERVICE_RESTORE_NORMAL = "restore_normal"
    monitoring = importlib.import_module("power_sync.monitoring")

    async def _run():
        execute_lock = asyncio.Lock()
        await execute_lock.acquire()
        reserve_calls = []

        async def restore_pre_idle(battery, context, *, bypass_monitoring):
            reserve_calls.append((battery, context, bypass_monitoring))
            return True

        hold_calls = []

        async def clear_hold(reason):
            hold_calls.append(reason)
            return True

        battery = object()
        coordinator = SimpleNamespace(
            _execute_lock=execute_lock,
            _pre_idle_backup_reserve=78,
            battery_controller=battery,
            _restore_pre_idle_backup_reserve=restore_pre_idle,
            _solar_export_hold=SimpleNamespace(
                active=True,
                clear=clear_hold,
            ),
        )
        entry_data = {
            "optimization_coordinator": coordinator,
            "force_charge_state": {"active": True},
        }
        service_calls = []

        async def async_call(*args, **kwargs):
            assert entry_data["_monitoring_handoff_active"] is True
            assert execute_lock.locked() is True
            service_calls.append((args, kwargs))
            entry_data["force_charge_state"]["active"] = False

        hass = SimpleNamespace(
            data={"power_sync": {"entry-1": entry_data}},
            services=SimpleNamespace(async_call=async_call),
        )
        entry = SimpleNamespace(entry_id="entry-1")

        handoff = asyncio.create_task(
            monitoring.async_prepare_monitoring_handoff(hass, entry)
        )
        await asyncio.sleep(0)
        assert service_calls == []

        execute_lock.release()
        await handoff

        assert len(service_calls) == 1
        assert service_calls[0][0][2] == {
            "source": "manual",
            "_force_restore": True,
        }
        assert hold_calls == ["monitoring_handoff"]
        assert reserve_calls == [(battery, "monitoring enabled", True)]
        monitoring.finish_monitoring_handoff(hass, entry)
        assert "_monitoring_handoff_active" not in entry_data

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
    source = MONITORING_PATH.read_text()

    idx_restore_normal_call = source.index("SERVICE_RESTORE_NORMAL")
    assert "_restore_pre_idle_backup_reserve" in source
    idx_bypass_call = source.index("_restore_pre_idle_backup_reserve")

    assert idx_restore_normal_call < idx_bypass_call, (
        "the pre-IDLE reserve bypass restore must run AFTER "
        "SERVICE_RESTORE_NORMAL, not before/instead of it"
    )
    assert "bypass_monitoring=True" in source
    assert "_pre_idle_backup_reserve" in source
    assert "if not restored:" in source
    assert 'raise RuntimeError("pre-IDLE backup reserve restore failed")' in source


def test_bypass_monitoring_true_has_exactly_one_caller():
    """The coordinator notes are explicit: bypass_monitoring=True must
    never gain a second call site."""

    coordinator_source = COORDINATOR_PATH.read_text()
    monitoring_source = MONITORING_PATH.read_text()

    total = coordinator_source.count("bypass_monitoring=True") + monitoring_source.count(
        "bypass_monitoring=True"
    )
    assert total == 1

def test_licence_shutdown_restores_after_enabled_flag_is_closed_and_retries(opt_module):
    async def run():
        c=_disable_coordinator(opt_module,last_executed_action='no_discharge',pre_idle_reserve=35,ev_active=False)
        c._enabled=False
        calls=[]
        async def restore(value):calls.append(value);return True
        c.battery_controller=SimpleNamespace(set_backup_reserve=restore)
        attempts=0
        async def stop(**kwargs):
            nonlocal attempts
            attempts+=1
            assert not c._enabled and kwargs=={'restore_normal':True,'retry_restore':True}
            return attempts>1
        c._executor=SimpleNamespace(stop=stop)
        with pytest.raises(RuntimeError,match='restore_pending'):await c.disable(licensing=True)
        await c.disable(licensing=True)
        assert calls==[35] and attempts==2
    asyncio.run(run())


def test_licence_shutdown_preserves_newer_manual_force(opt_module):
    async def run():
        c=_disable_coordinator(opt_module,last_executed_action='no_discharge',pre_idle_reserve=35,ev_active=False)
        c._get_active_force_state=lambda:{'active':True,'source':'user'}
        writes=[]
        async def restore(value):writes.append(value);return True
        c.battery_controller=SimpleNamespace(set_backup_reserve=restore)
        async def stop(**kwargs):assert kwargs['restore_normal'] is False;return True
        c._executor=SimpleNamespace(stop=stop)
        await c.disable(licensing=True)
        assert writes==[] and c._pre_idle_backup_reserve==35
    asyncio.run(run())
