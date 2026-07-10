"""Regression tests for RSV-2 (PW-5 completion): trusted-only reserve
adoption/persist.

Background (reserve-cluster-design.md Step 2): RSV-1 added a trust-tagged
accessor, ``read_backup_reserve()`` -> ``ReserveReading(percent, trust,
source)``, to ``optimization/battery_controller.py`` as a behavioral no-op —
the bare ``get_backup_reserve()`` wrapper still returns ``.percent``
byte-identically and nothing yet consumed the trust tag. That left four
consumer sites in ``optimization/coordinator.py`` still adopting/persisting
whatever a stale ``CLOUD_STALE``/``ENTITY`` reading reported into
``_startup_backup_reserve`` or the persisted ``_user_backup_reserve`` option
(PW-5's "silent reader divergence").

This fix gates the four sites on ``reading.trust in TRUSTED_FOR_PERSIST``
(LIVE or CLOUD_FRESH), while preserving byte-identical behavior for the
bare ``get_backup_reserve()`` fallback (pre-RSV-1-shaped test doubles /
callers that don't expose ``read_backup_reserve`` at all):

1. ``_resolve_startup_backup_reserve`` — startup self-heal of a stale
   persisted reserve; only self-heals/persists on a trusted reading.
2. Self-consumption adoption elif (inside ``_execute_optimizer_action``) —
   only adopts a hardware reserve raise into ``_startup_backup_reserve`` on
   a trusted reading.
3. ``_deferred_enable_restore`` no-config fallback — only captures the live
   reserve as ``_startup_backup_reserve`` on a trusted reading; otherwise
   leaves it ``None`` (the pre-existing safe state).
4. ``_set_idle_hold_mode`` read fallback — only adopts the read into
   ``_pre_idle_backup_reserve`` on a trusted reading; otherwise falls
   through to the existing ``energy_coordinator.data`` fallback.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone
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

_BC_MODULE_NAME = "power_sync.optimization.battery_controller"


@pytest.fixture()
def opt_module():
    saved_modules = {
        name: sys.modules.get(name, _SENTINEL) for name in _STUB_MODULE_NAMES
    }
    saved_bc = sys.modules.get(_BC_MODULE_NAME, _SENTINEL)
    for name in _STUB_MODULE_NAMES:
        sys.modules.pop(name, None)
    # Drop any cached battery_controller module so it reloads fresh from
    # disk (the real file — it isn't stubbed) against the freshly-stubbed
    # homeassistant.core.
    sys.modules.pop(_BC_MODULE_NAME, None)

    _install_ha_stubs()
    _install_power_sync_stubs()
    import importlib

    module = importlib.import_module("power_sync.optimization.coordinator")
    try:
        yield module
    finally:
        for name in _STUB_MODULE_NAMES:
            if saved_modules[name] is _SENTINEL:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = saved_modules[name]
        if saved_bc is _SENTINEL:
            sys.modules.pop(_BC_MODULE_NAME, None)
        else:
            sys.modules[_BC_MODULE_NAME] = saved_bc


def _bc_module():
    if _BC_MODULE_NAME not in sys.modules:
        import importlib

        importlib.import_module(_BC_MODULE_NAME)
    return sys.modules[_BC_MODULE_NAME]


class _FakeBatteryWithTrust:
    """Mirrors ``BatteryControllerWrapper`` post-RSV-1: exposes both the
    trust-tagged accessor and the bare byte-identical wrapper."""

    def __init__(self, percent, trust, source="fake"):
        self.percent = percent
        self.trust = trust
        self.source = source
        self.get_backup_reserve_calls = 0

    async def read_backup_reserve(self):
        bc = _bc_module()
        return bc.ReserveReading(self.percent, self.trust, self.source)

    async def get_backup_reserve(self):
        self.get_backup_reserve_calls += 1
        return self.percent


class _FakeBatteryNoTrust:
    """Mirrors a pre-RSV-1 test double: only the bare wrapper, no
    provenance. Legacy callers/tests using this shape must see unchanged
    (ungated) behavior."""

    def __init__(self, percent):
        self.percent = percent

    async def get_backup_reserve(self):
        return self.percent


# ---------------------------------------------------------------------------
# Site 2: _resolve_startup_backup_reserve
# ---------------------------------------------------------------------------


def _resolve_coordinator(opt_module, *, persisted_reserve: int):
    coordinator = object.__new__(opt_module.OptimizationCoordinator)
    coordinator.battery_system = "tesla"
    coordinator.entry_id = "entry-1"
    coordinator._entry = SimpleNamespace(
        options={"_user_backup_reserve": persisted_reserve}, data={}
    )
    updates = []

    class _ConfigEntries:
        def async_update_entry(self, entry, **kwargs):
            updates.append(kwargs)
            if "options" in kwargs:
                entry.options = kwargs["options"]

    coordinator.hass = SimpleNamespace(
        data={"power_sync": {"entry-1": {}}},
        config_entries=_ConfigEntries(),
    )
    coordinator._resolve_updates = updates
    return coordinator


def test_s1_cloud_fresh_self_heals_stale_persisted_reserve(opt_module):
    """S1: CLOUD_FRESH during startup — self-heal DOES fire (must not
    over-refuse a genuinely fresh cloud read)."""

    bc = _bc_module()
    coordinator = _resolve_coordinator(opt_module, persisted_reserve=52)
    battery = _FakeBatteryWithTrust(20, bc.ReserveTrust.CLOUD_FRESH)

    result = asyncio.run(
        coordinator._resolve_startup_backup_reserve(
            battery, 52, "persisted user backup reserve"
        )
    )

    assert result == (20, "live Tesla backup reserve")
    assert coordinator._entry.options["_user_backup_reserve"] == 20
    assert coordinator._resolve_updates[-1]["options"]["_user_backup_reserve"] == 20


def test_s1_prime_entity_trust_refuses_self_heal(opt_module):
    """S1': ENTITY trust — self-heal refused; persisted reserve retained."""

    bc = _bc_module()
    coordinator = _resolve_coordinator(opt_module, persisted_reserve=52)
    battery = _FakeBatteryWithTrust(20, bc.ReserveTrust.ENTITY)

    result = asyncio.run(
        coordinator._resolve_startup_backup_reserve(
            battery, 52, "persisted user backup reserve"
        )
    )

    assert result == (52, "persisted user backup reserve")
    assert coordinator._entry.options["_user_backup_reserve"] == 52
    assert coordinator._resolve_updates == []


def test_cloud_stale_refuses_self_heal(opt_module):
    """CLOUD_STALE (past the freshness window) must not self-heal either."""

    bc = _bc_module()
    coordinator = _resolve_coordinator(opt_module, persisted_reserve=52)
    battery = _FakeBatteryWithTrust(20, bc.ReserveTrust.CLOUD_STALE)

    result = asyncio.run(
        coordinator._resolve_startup_backup_reserve(
            battery, 52, "persisted user backup reserve"
        )
    )

    assert result == (52, "persisted user backup reserve")
    assert coordinator._resolve_updates == []


def test_resolve_startup_reserve_no_op_persist_skips_skip_reload(opt_module):
    """persisted_changed guard: a same-value write must not set
    ``_skip_reload`` or call ``async_update_entry`` (OB-39-site no-op)."""

    bc = _bc_module()
    coordinator = _resolve_coordinator(opt_module, persisted_reserve=20)
    battery = _FakeBatteryWithTrust(20, bc.ReserveTrust.LIVE)

    # live_reserve (20) == startup_reserve (20) -> early-return path, no
    # persist attempted at all (covers the >= early return, not just the
    # no-op-persist branch, since equal values never reach the persist
    # block in the first place).
    result = asyncio.run(
        coordinator._resolve_startup_backup_reserve(
            battery, 20, "persisted user backup reserve"
        )
    )

    assert result == (20, "persisted user backup reserve")
    assert coordinator._resolve_updates == []
    assert "_skip_reload" not in coordinator.hass.data["power_sync"]["entry-1"]


def test_resolve_startup_reserve_legacy_fallback_unchanged(opt_module):
    """Byte-identical behavior for a battery object without
    ``read_backup_reserve`` (pre-RSV-1-shaped double) — must still self-heal
    exactly as before RSV-2."""

    coordinator = _resolve_coordinator(opt_module, persisted_reserve=52)
    battery = _FakeBatteryNoTrust(30)

    result = asyncio.run(
        coordinator._resolve_startup_backup_reserve(
            battery, 52, "persisted user backup reserve"
        )
    )

    assert result == (30, "live Tesla backup reserve")
    assert coordinator._entry.options["_user_backup_reserve"] == 30


# ---------------------------------------------------------------------------
# Site 3: _deferred_enable_restore no-config fallback
# ---------------------------------------------------------------------------


def _deferred_restore_coordinator(opt_module, battery):
    coordinator = object.__new__(opt_module.OptimizationCoordinator)
    coordinator._enabled = True
    coordinator._executor = SimpleNamespace(battery_controller=battery)
    coordinator._optimizer = None
    coordinator._startup_backup_reserve = None
    coordinator.hass = SimpleNamespace(data={})
    coordinator.entry_id = "entry-1"
    coordinator._entry = SimpleNamespace(options={}, data={})
    coordinator.energy_coordinator = None
    # No reserve config at all -> _resolve_startup_backup_reserve's
    # precondition (reserve_source == "persisted user backup reserve")
    # never matches, so it returns (None, ...) unchanged and execution
    # falls into the no-config fallback branch under test.
    coordinator._configured_startup_backup_reserve = lambda: (None, "no reserve configured")
    coordinator._should_apply_offgrid_overlay = lambda: False
    return coordinator


def test_s5_no_config_fallback_untrusted_reading_leaves_startup_reserve_none(opt_module):
    """S5: reload mid-force-like state, elevated reserve, no reserve config
    at all, and an untrusted reading — ``_startup_backup_reserve`` stays
    ``None`` rather than adopting the elevated live read (LP self-corrects
    next cycle)."""

    bc = _bc_module()
    battery = _FakeBatteryWithTrust(80, bc.ReserveTrust.CLOUD_STALE)
    coordinator = _deferred_restore_coordinator(opt_module, battery)

    asyncio.run(coordinator._deferred_enable_restore())

    assert coordinator._startup_backup_reserve is None


def test_no_config_fallback_trusted_reading_is_captured(opt_module):
    """A trusted (LIVE) reading in the no-config fallback IS captured as the
    startup reserve — the gate must not over-refuse a genuinely live read."""

    bc = _bc_module()
    battery = _FakeBatteryWithTrust(45, bc.ReserveTrust.LIVE)
    coordinator = _deferred_restore_coordinator(opt_module, battery)

    asyncio.run(coordinator._deferred_enable_restore())

    assert coordinator._startup_backup_reserve == 45


def test_no_config_fallback_legacy_battery_unchanged(opt_module):
    """Byte-identical behavior for a battery object without
    ``read_backup_reserve`` — still captures the bare read as before."""

    battery = _FakeBatteryNoTrust(37)
    coordinator = _deferred_restore_coordinator(opt_module, battery)

    asyncio.run(coordinator._deferred_enable_restore())

    assert coordinator._startup_backup_reserve == 37


# ---------------------------------------------------------------------------
# Site 4: self-consumption adoption elif (inside _execute_optimizer_action)
# ---------------------------------------------------------------------------


def _execute_coordinator(opt_module, battery):
    coordinator = object.__new__(opt_module.OptimizationCoordinator)
    coordinator._executor = SimpleNamespace(battery_controller=battery)
    coordinator.hass = SimpleNamespace(data={})
    coordinator.entry_id = "entry-1"
    coordinator.battery_system = "tesla"
    coordinator.energy_coordinator = None
    coordinator._config = opt_module.OptimizationConfig(
        interval_minutes=5,
        horizon_hours=24,
        backup_reserve=0.20,
    )
    coordinator._entry = SimpleNamespace(options={}, data={})
    coordinator._monitoring_mode_active = lambda: False
    coordinator._get_active_force_state = lambda: None
    coordinator._scheduled_ev_preserve_active = lambda: False
    coordinator._scheduled_ev_no_discharge_active = False
    coordinator._last_executed_action = "self_consumption"
    coordinator._last_executed_planned_action = "self_consumption"
    coordinator._pre_idle_backup_reserve = None
    coordinator._idle_hold_reserve = None
    coordinator._startup_backup_reserve = None
    coordinator._optimizer = None
    coordinator._enabled = True

    async def _battery_state():
        return 0.50, 13500

    coordinator._get_battery_state = _battery_state
    return coordinator


def _self_consumption_action():
    return SimpleNamespace(
        timestamp=datetime(2026, 7, 10, 12, 0, tzinfo=timezone.utc),
        action="self_consumption",
        power_w=0.0,
        soc=0.50,
        battery_charge_w=0.0,
        battery_discharge_w=0.0,
    )


def test_s10_cloud_fresh_reserve_raise_is_adopted(opt_module):
    """S10: user raises reserve in the Tesla app, only cloud available
    (CLOUD_FRESH) — the adoption elif ADOPTS the fresh-cloud raise rather
    than fighting it by reapplying the lower cached target."""

    bc = _bc_module()
    battery = _FakeBatteryWithTrust(30, bc.ReserveTrust.CLOUD_FRESH)
    coordinator = _execute_coordinator(opt_module, battery)
    coordinator._startup_backup_reserve = 20

    asyncio.run(coordinator._execute_optimizer_action(_self_consumption_action()))

    assert coordinator._startup_backup_reserve == 30


def test_cloud_stale_reserve_raise_is_not_adopted(opt_module):
    """A CLOUD_STALE raise must NOT be adopted into _startup_backup_reserve
    — falls to the else/reapply branch instead."""

    bc = _bc_module()
    battery = _FakeBatteryWithTrust(30, bc.ReserveTrust.CLOUD_STALE)
    coordinator = _execute_coordinator(opt_module, battery)
    coordinator._startup_backup_reserve = 20

    asyncio.run(coordinator._execute_optimizer_action(_self_consumption_action()))

    assert coordinator._startup_backup_reserve == 20


def test_s11_pre_idle_guard_still_wins_regardless_of_trust(opt_module):
    """S11 (498e2f98 interaction): the adoption elif keeps the pre-existing
    ``_pre_idle_backup_reserve``/``_idle_hold_reserve`` guards AND the new
    trust guard, compounded — a pending idle hold refuses adoption even on
    an otherwise-trusted CLOUD_FRESH reading."""

    bc = _bc_module()
    battery = _FakeBatteryWithTrust(30, bc.ReserveTrust.CLOUD_FRESH)
    coordinator = _execute_coordinator(opt_module, battery)
    coordinator._startup_backup_reserve = 20
    coordinator._pre_idle_backup_reserve = 5
    coordinator._idle_hold_reserve = 30

    asyncio.run(coordinator._execute_optimizer_action(_self_consumption_action()))

    assert coordinator._startup_backup_reserve == 20


def test_self_consumption_legacy_battery_adoption_unchanged(opt_module):
    """Byte-identical behavior for a battery object without
    ``read_backup_reserve`` — the adoption elif still fires unconditionally
    (matches pre-RSV-2 behavior for legacy callers with no trust info)."""

    battery = _FakeBatteryNoTrust(30)
    coordinator = _execute_coordinator(opt_module, battery)
    coordinator._startup_backup_reserve = 20

    asyncio.run(coordinator._execute_optimizer_action(_self_consumption_action()))

    assert coordinator._startup_backup_reserve == 30


# ---------------------------------------------------------------------------
# Site 1: _set_idle_hold_mode read fallback
# ---------------------------------------------------------------------------


def _idle_hold_coordinator(opt_module, battery, *, soc: float = 0.30):
    coordinator = object.__new__(opt_module.OptimizationCoordinator)
    coordinator.battery_system = "tesla"
    coordinator._config = opt_module.OptimizationConfig(
        interval_minutes=5,
        horizon_hours=24,
        backup_reserve=0.20,
    )
    coordinator._pre_idle_backup_reserve = None
    coordinator._idle_hold_reserve = None
    coordinator._idle_reserve_adjustment = False
    coordinator._startup_backup_reserve = None
    coordinator.energy_coordinator = None
    coordinator._entry = SimpleNamespace(options={}, data={})

    async def _battery_state():
        return soc, 13500

    coordinator._get_battery_state = _battery_state
    return coordinator


def test_idle_hold_untrusted_reading_falls_through_to_configured_reserve(opt_module):
    """Untrusted read fallback: falls through to the configured-reserve
    fallback (no energy_coordinator present) rather than adopting the
    untrusted percent into ``_pre_idle_backup_reserve``."""

    bc = _bc_module()
    battery = _FakeBatteryWithTrust(80, bc.ReserveTrust.ENTITY)
    coordinator = _idle_hold_coordinator(opt_module, battery, soc=0.30)

    asyncio.run(coordinator._set_idle_hold_mode(battery))

    # Configured optimizer floor (20%) is used, not the untrusted 80% read.
    assert coordinator._pre_idle_backup_reserve == 20


def test_idle_hold_trusted_reading_is_adopted(opt_module):
    """A trusted (LIVE) reading IS adopted into _pre_idle_backup_reserve."""

    bc = _bc_module()
    battery = _FakeBatteryWithTrust(55, bc.ReserveTrust.LIVE)
    coordinator = _idle_hold_coordinator(opt_module, battery, soc=0.30)

    asyncio.run(coordinator._set_idle_hold_mode(battery))

    assert coordinator._pre_idle_backup_reserve == 55


def test_idle_hold_legacy_battery_unchanged(opt_module):
    """Byte-identical behavior for a battery object without
    ``read_backup_reserve``."""

    battery = _FakeBatteryNoTrust(55)
    coordinator = _idle_hold_coordinator(opt_module, battery, soc=0.30)

    asyncio.run(coordinator._set_idle_hold_mode(battery))

    assert coordinator._pre_idle_backup_reserve == 55


# ---------------------------------------------------------------------------
# Site 5: _sync_brand_restore_targets (OB-22)
#
# Sigenergy's restore_normal() writes hardware from a separate
# SigenergyController instance's `_restore_backup_reserve_pct`
# (custom_components/power_sync/inverters/sigenergy.py), which lives on
# `entry_data["sigenergy_coordinator"]._controller` — NOT on
# `self._executor.battery_controller` (a BatteryControllerWrapper with no
# such attribute). Before this fix, a live reserve change (20 -> 10) made
# without a reload never reached that controller: every subsequent
# force/restore cycle wrote hardware back to the stale value.
# ---------------------------------------------------------------------------


def _sigenergy_coordinator_with_target(opt_module, *, initial_pct: int):
    coordinator = object.__new__(opt_module.OptimizationCoordinator)
    coordinator.battery_system = "sigenergy"
    coordinator.entry_id = "entry-1"
    sigenergy_controller = SimpleNamespace(_restore_backup_reserve_pct=initial_pct)
    sigenergy_coordinator = SimpleNamespace(_controller=sigenergy_controller)
    coordinator.hass = SimpleNamespace(
        data={"power_sync": {"entry-1": {"sigenergy_coordinator": sigenergy_coordinator}}}
    )
    return coordinator, sigenergy_controller


def test_sync_brand_restore_targets_sigenergy_updates_persistent_controller(opt_module):
    coordinator, ctrl = _sigenergy_coordinator_with_target(opt_module, initial_pct=20)

    coordinator._sync_brand_restore_targets(10)

    assert ctrl._restore_backup_reserve_pct == 10
    assert isinstance(ctrl._restore_backup_reserve_pct, int)


def test_sync_brand_restore_targets_non_sigenergy_is_noop(opt_module):
    coordinator, ctrl = _sigenergy_coordinator_with_target(opt_module, initial_pct=20)
    coordinator.battery_system = "tesla"

    coordinator._sync_brand_restore_targets(10)

    assert ctrl._restore_backup_reserve_pct == 20


def test_sync_brand_restore_targets_no_sigenergy_coordinator_is_safe_noop(opt_module):
    """No sigenergy_coordinator in hass.data (not yet set up) must not raise."""

    coordinator = object.__new__(opt_module.OptimizationCoordinator)
    coordinator.battery_system = "sigenergy"
    coordinator.entry_id = "entry-1"
    coordinator.hass = SimpleNamespace(data={"power_sync": {"entry-1": {}}})

    coordinator._sync_brand_restore_targets(10)  # must not raise


def test_set_settings_hardware_reserve_syncs_sigenergy_restore_target(opt_module):
    """set_settings's hardware_backup_reserve block must push the new value
    to the persistent Sigenergy controller, not just `_startup_backup_reserve`."""

    from test_battery_export_allowed_slots import _coordinator

    coordinator = _coordinator(
        opt_module,
        "amber",
        hardware_backup_reserve=0.20,
    )
    coordinator.entry_id = "entry-1"
    coordinator.battery_system = "sigenergy"
    coordinator._startup_backup_reserve = 20
    coordinator._optimizer = SimpleNamespace(update_hardware_reserve=lambda reserve: None)

    sigenergy_controller = SimpleNamespace(_restore_backup_reserve_pct=20)
    sigenergy_coordinator = SimpleNamespace(_controller=sigenergy_controller)

    class _ConfigEntries:
        def async_update_entry(self, entry, **kwargs):
            if "data" in kwargs:
                entry.data = kwargs["data"]
            if "options" in kwargs:
                entry.options = kwargs["options"]

    coordinator.hass = SimpleNamespace(
        data={"power_sync": {"entry-1": {"sigenergy_coordinator": sigenergy_coordinator}}},
        config_entries=_ConfigEntries(),
    )

    result = asyncio.run(coordinator.set_settings({"hardware_backup_reserve": 10}))

    assert result["success"] is True
    assert coordinator._startup_backup_reserve == 10
    assert sigenergy_controller._restore_backup_reserve_pct == 10


def test_deferred_enable_restore_syncs_sigenergy_restore_target(opt_module):
    """_deferred_enable_restore's no-config trusted-reading fallback must
    also push the resolved reserve to the persistent Sigenergy controller."""

    bc = _bc_module()
    battery = _FakeBatteryWithTrust(45, bc.ReserveTrust.LIVE)
    coordinator = _deferred_restore_coordinator(opt_module, battery)
    coordinator.battery_system = "sigenergy"

    sigenergy_controller = SimpleNamespace(_restore_backup_reserve_pct=20)
    sigenergy_coordinator = SimpleNamespace(_controller=sigenergy_controller)
    coordinator.hass.data = {
        "power_sync": {"entry-1": {"sigenergy_coordinator": sigenergy_coordinator}}
    }

    asyncio.run(coordinator._deferred_enable_restore())

    assert coordinator._startup_backup_reserve == 45
    assert sigenergy_controller._restore_backup_reserve_pct == 45


# ---------------------------------------------------------------------------
# Site 5b: SigenergySettingsView.post (OB-22 fifth surface)
#
# /api/power_sync/sigenergy_settings writes `backup_reserve` straight to the
# PERSISTENT sigenergy_coordinator._controller via
# `controller.set_backup_reserve(val)`, but (before this fix) never updated
# that same controller's `_restore_backup_reserve_pct`. Because
# restore_normal() runs on that identical instance and writes hardware from
# `_restore_backup_reserve_pct`, a reserve change made through this endpoint
# with no reload was clobbered back to the stale value on the next
# force/restore cycle -- unlike the scratch-controller case in
# handle_set_backup_reserve, `controller` here already IS the persistent
# instance, so the fix is a direct assignment with no extra resolution.
# ---------------------------------------------------------------------------


def _sigenergy_settings_view_post_source():
    import ast

    init_path = (
        Path(__file__).resolve().parent.parent
        / "custom_components"
        / "power_sync"
        / "__init__.py"
    )
    source = init_path.read_text()
    module = ast.parse(source)
    for node in module.body:
        if isinstance(node, ast.ClassDef) and node.name == "SigenergySettingsView":
            for child in node.body:
                if isinstance(child, ast.AsyncFunctionDef) and child.name == "post":
                    segment = ast.get_source_segment(source, child)
                    assert segment is not None
                    return segment
    raise AssertionError("SigenergySettingsView.post not found")


def test_sigenergy_settings_view_post_syncs_restore_target_on_backup_reserve_change():
    """The `backup_reserve` branch of the HTTP handler must keep the
    persistent controller's `_restore_backup_reserve_pct` in sync with the
    value just written to hardware, gated on `success` (not unconditional)."""

    source = _sigenergy_settings_view_post_source()

    body_idx = source.index('"backup_reserve" in body')
    set_call_idx = source.index("controller.set_backup_reserve(val)", body_idx)
    results_idx = source.index('results["backup_reserve"] = success', set_call_idx)

    block = source[set_call_idx:results_idx]
    assert "controller._restore_backup_reserve_pct = val" in block
    assert "if success" in block
