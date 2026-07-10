"""Regression tests for RSV-3 (PW-6): schedule_max_backup must not snapshot
its restore target from the stale Tesla cloud site_info cache.

Background: ``handle_schedule_max_backup`` (``__init__.py``) captured
``saved_reserve`` from ``coord._site_info_cache.get("backup_reserve_percent")``
with zero freshness/trust check -- the same untrusted-cloud-cache read that
RSV-1/RSV-2 gated everywhere else in ``optimization/coordinator.py``, but
this call site was untouched by either commit. ``_max_backup_restore`` then
force-persists that snapshotted value with ``source="user"``, so a stale or
garbage cloud value clobbers the user's real reserve when the window ends.

Fix (this change):
1. ``OptimizationCoordinator.resolve_restore_target()`` (new method in
   ``optimization/coordinator.py``) returns a trustworthy reserve value:
   the optimizer's ``_startup_backup_reserve`` (provenance-clean after
   RSV-2), else the persisted ``_user_backup_reserve`` option, else a
   freshly-read, trust-tagged reading (LIVE/CLOUD_FRESH via
   ``read_backup_reserve()``), else ``None``. The clean sources are
   deliberately preferred over even a trusted live read (design §2 PW-6 /
   S3): a LIVE tag certifies freshness, not overlay integrity, and a
   PW-3/PW-4 offset-corrupted local snapshot must not become the
   ``source="user"`` restore value that clobbers the persisted user
   reserve. An untrusted reading (CLOUD_STALE/ENTITY) is never returned
   directly, and a legacy battery with no ``read_backup_reserve`` accessor
   is *not* trusted via a raw ``get_backup_reserve()`` read either.
2. ``handle_schedule_max_backup`` now snapshots via
   ``opt_coord.resolve_restore_target()`` when the optimization coordinator
   is available, keeping the original ``_site_info_cache`` read only as a
   fallback for the (rare) case the optimizer isn't set up.

Explicitly OUT of scope / left unchanged (per the RSV-3 design, coordinator
notes S3): the two force-save "last-resort" branches (~L25693-25710 and
~L27327-27343) already prioritize ``_startup_backup_reserve`` /
``_pre_idle_backup_reserve`` over a live (but provenance-untagged) API read
-- that shape is intentionally NOT touched here. This is a documented
residual exposure (PW-4/S3), not a regression introduced by this fix; a
later RSV step gates it at persist time.
"""

from __future__ import annotations

import ast
import asyncio
import sys
import textwrap
import types
from pathlib import Path
from types import SimpleNamespace

# Reuse the fixture/fake scaffolding already built for the RSV-1/RSV-2
# regression suite (pytest prepends this directory to sys.path).
from test_reserve_source_of_truth import (
    _FakeBatteryNoTrust,
    _FakeBatteryWithTrust,
    _bc_module,
    opt_module,
)

ROOT = Path(__file__).resolve().parent.parent
INIT_PATH = ROOT / "custom_components" / "power_sync" / "__init__.py"


def _function_source(name: str) -> str:
    """Extract a function nested directly inside async_setup_entry."""
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


def _find_all(haystack: str, needle: str) -> list[int]:
    positions = []
    start = 0
    while True:
        idx = haystack.find(needle, start)
        if idx == -1:
            return positions
        positions.append(idx)
        start = idx + 1


# ---------------------------------------------------------------------------
# (1) OptimizationCoordinator.resolve_restore_target
# ---------------------------------------------------------------------------


def _make_coordinator(opt_module, *, persisted_reserve=None, startup_reserve=None, battery=None):
    coordinator = object.__new__(opt_module.OptimizationCoordinator)
    options = {} if persisted_reserve is None else {"_user_backup_reserve": persisted_reserve}
    coordinator._entry = SimpleNamespace(options=options)
    coordinator._startup_backup_reserve = startup_reserve
    coordinator._executor = SimpleNamespace(battery_controller=battery) if battery is not None else None
    return coordinator


def test_resolve_restore_target_startup_reserve_beats_trusted_live(opt_module):
    """S3/PW-4 ordering guard (design §2 PW-6): a LIVE tag certifies
    freshness, not overlay integrity -- a fresh-but-offset-corrupted local
    snapshot must not become the max-backup restore target while a
    provenance-clean _startup_backup_reserve exists."""
    bc = _bc_module()
    battery = _FakeBatteryWithTrust(8, bc.ReserveTrust.LIVE)
    coordinator = _make_coordinator(opt_module, persisted_reserve=40, startup_reserve=20, battery=battery)

    result = asyncio.run(coordinator.resolve_restore_target())

    assert result == 20
    assert result != 8


def test_resolve_restore_target_persisted_beats_trusted_live(opt_module):
    bc = _bc_module()
    battery = _FakeBatteryWithTrust(15, bc.ReserveTrust.LIVE)
    coordinator = _make_coordinator(opt_module, persisted_reserve=40, startup_reserve=None, battery=battery)

    result = asyncio.run(coordinator.resolve_restore_target())

    assert result == 40


def test_resolve_restore_target_trusted_live_is_final_fallback(opt_module):
    bc = _bc_module()
    battery = _FakeBatteryWithTrust(15, bc.ReserveTrust.LIVE)
    coordinator = _make_coordinator(opt_module, persisted_reserve=None, startup_reserve=None, battery=battery)

    result = asyncio.run(coordinator.resolve_restore_target())

    assert result == 15


def test_resolve_restore_target_cloud_fresh_reading_used_when_no_clean_source(opt_module):
    bc = _bc_module()
    battery = _FakeBatteryWithTrust(22, bc.ReserveTrust.CLOUD_FRESH)
    coordinator = _make_coordinator(opt_module, persisted_reserve=None, battery=battery)

    result = asyncio.run(coordinator.resolve_restore_target())

    assert result == 22


def test_resolve_restore_target_cloud_stale_falls_back_to_persisted_not_stale_cache(opt_module):
    """Core PW-6 regression: an untrusted (stale-cache) reading must never
    be used as the restore target -- fall back to the persisted user
    reserve instead of clobbering it with the stale value."""
    bc = _bc_module()
    battery = _FakeBatteryWithTrust(5, bc.ReserveTrust.CLOUD_STALE)
    coordinator = _make_coordinator(opt_module, persisted_reserve=40, startup_reserve=None, battery=battery)

    result = asyncio.run(coordinator.resolve_restore_target())

    assert result == 40
    assert result != 5


def test_resolve_restore_target_entity_trust_falls_back_to_persisted(opt_module):
    bc = _bc_module()
    battery = _FakeBatteryWithTrust(5, bc.ReserveTrust.ENTITY)
    coordinator = _make_coordinator(opt_module, persisted_reserve=40, battery=battery)

    result = asyncio.run(coordinator.resolve_restore_target())

    assert result == 40


def test_resolve_restore_target_legacy_battery_no_trust_accessor_skips_raw_read(opt_module):
    """Legacy batteries without read_backup_reserve() must not have their
    raw get_backup_reserve() value trusted either -- they fall through the
    same persisted/startup chain (design step 1)."""
    battery = _FakeBatteryNoTrust(77)
    coordinator = _make_coordinator(opt_module, persisted_reserve=40, battery=battery)

    result = asyncio.run(coordinator.resolve_restore_target())

    assert result == 40
    assert result != 77
    assert battery.get_backup_reserve_calls == 0 if hasattr(battery, "get_backup_reserve_calls") else True


def test_resolve_restore_target_no_persisted_falls_back_to_startup_reserve(opt_module):
    bc = _bc_module()
    battery = _FakeBatteryWithTrust(5, bc.ReserveTrust.CLOUD_STALE)
    coordinator = _make_coordinator(opt_module, persisted_reserve=None, startup_reserve=60, battery=battery)

    result = asyncio.run(coordinator.resolve_restore_target())

    assert result == 60


def test_resolve_restore_target_nothing_available_returns_none(opt_module):
    coordinator = _make_coordinator(opt_module, persisted_reserve=None, startup_reserve=None, battery=None)

    result = asyncio.run(coordinator.resolve_restore_target())

    assert result is None


# ---------------------------------------------------------------------------
# (2) handle_schedule_max_backup call-site: structural
# ---------------------------------------------------------------------------


def test_handle_schedule_max_backup_snapshots_via_resolve_restore_target():
    source = _function_source("handle_schedule_max_backup")

    assert (
        'opt_coord = hass.data.get(DOMAIN, {}).get(entry.entry_id, {}).get("optimization_coordinator")'
        in source
    )
    assert "await opt_coord.resolve_restore_target()" in source

    # The original _site_info_cache read must survive as the fallback for
    # the (rare) case the optimization coordinator isn't available.
    assert 'site_info = getattr(coord, "_site_info_cache", None) or {}' in source
    assert 'site_info.get("backup_reserve_percent")' in source

    resolve_index = source.index("await opt_coord.resolve_restore_target()")
    fallback_index = source.index('site_info = getattr(coord, "_site_info_cache", None)')
    assert resolve_index < fallback_index


# ---------------------------------------------------------------------------
# (3) handle_schedule_max_backup: behavioral (exec round-trip)
# ---------------------------------------------------------------------------


def _install_call_later_stub():
    """Install a minimal homeassistant.helpers.event stub so the function's
    local ``from homeassistant.helpers.event import async_call_later``
    import resolves; returns the saved sys.modules state for teardown."""
    ha_event = types.ModuleType("homeassistant.helpers.event")
    call_later_calls: list = []

    def _fake_async_call_later(hass_arg, delay, cb):
        call_later_calls.append((delay, cb))
        return lambda: None

    ha_event.async_call_later = _fake_async_call_later
    ha_helpers = types.ModuleType("homeassistant.helpers")
    ha_helpers.event = ha_event
    ha_root = types.ModuleType("homeassistant")
    ha_root.helpers = ha_helpers

    saved = {}
    for name, mod in (
        ("homeassistant", ha_root),
        ("homeassistant.helpers", ha_helpers),
        ("homeassistant.helpers.event", ha_event),
    ):
        saved[name] = sys.modules.get(name)
        sys.modules[name] = mod
    return saved, call_later_calls


def _restore_modules(saved: dict) -> None:
    for name, mod in saved.items():
        if mod is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = mod


def test_handle_schedule_max_backup_uses_resolve_restore_target_not_stale_cache():
    """Behavioral: when the optimization coordinator can resolve a trusted
    restore target, the raw (stale) _site_info_cache value must NOT be what
    gets snapshotted/persisted as saved_reserve -- the PW-6 clobber."""
    source = textwrap.dedent(_function_source("handle_schedule_max_backup"))

    saved_modules, call_later_calls = _install_call_later_stub()
    try:
        class _FakeCoord:
            _site_info_cache = {"backup_reserve_percent": 5}  # stale cloud value

        class _FakeOptCoord:
            async def resolve_restore_target(self):
                return 42  # trusted persisted/live value

        persisted_schedules: list = []

        async def _fake_persist(payload):
            persisted_schedules.append(payload)

        service_calls: list = []

        class _FakeServices:
            async def async_call(self, domain, service, data, blocking=True):
                service_calls.append((domain, service, data))

        entry_data: dict = {"optimization_coordinator": _FakeOptCoord()}
        hass = SimpleNamespace(
            data={"power_sync": {"entry-1": entry_data}},
            services=_FakeServices(),
        )
        entry = SimpleNamespace(entry_id="entry-1")

        namespace = {
            "hass": hass,
            "entry": entry,
            "DOMAIN": "power_sync",
            "ServiceCall": object,
            "_LOGGER": SimpleNamespace(
                error=lambda *a, **k: None,
                info=lambda *a, **k: None,
            ),
            "_get_tesla_coordinator_for_service": lambda name: _FakeCoord(),
            "_persist_max_backup_schedule": _fake_persist,
            "_max_backup_restore": lambda *_a, **_k: None,
        }
        exec(source, namespace)

        call = SimpleNamespace(data={"duration_minutes": 30})
        asyncio.run(namespace["handle_schedule_max_backup"](call))
    finally:
        _restore_modules(saved_modules)

    assert entry_data["max_backup_saved_reserve"] == 42
    assert entry_data["max_backup_saved_reserve"] != 5
    assert persisted_schedules[-1]["saved_reserve"] == 42
    assert service_calls[0] == (
        "power_sync",
        "set_backup_reserve",
        {"percent": 100, "source": "user"},
    )


def test_handle_schedule_max_backup_falls_back_to_site_info_when_optimizer_unavailable():
    """When the optimization coordinator is not set up at all, the original
    _site_info_cache fallback must still work (no regression for that
    path)."""
    source = textwrap.dedent(_function_source("handle_schedule_max_backup"))

    saved_modules, _ = _install_call_later_stub()
    try:
        class _FakeCoord:
            _site_info_cache = {"backup_reserve_percent": 33}

        persisted_schedules: list = []

        async def _fake_persist(payload):
            persisted_schedules.append(payload)

        class _FakeServices:
            async def async_call(self, domain, service, data, blocking=True):
                pass

        entry_data: dict = {}  # no "optimization_coordinator" key at all
        hass = SimpleNamespace(
            data={"power_sync": {"entry-1": entry_data}},
            services=_FakeServices(),
        )
        entry = SimpleNamespace(entry_id="entry-1")

        namespace = {
            "hass": hass,
            "entry": entry,
            "DOMAIN": "power_sync",
            "ServiceCall": object,
            "_LOGGER": SimpleNamespace(
                error=lambda *a, **k: None,
                info=lambda *a, **k: None,
            ),
            "_get_tesla_coordinator_for_service": lambda name: _FakeCoord(),
            "_persist_max_backup_schedule": _fake_persist,
            "_max_backup_restore": lambda *_a, **_k: None,
        }
        exec(source, namespace)

        call = SimpleNamespace(data={"duration_minutes": 30})
        asyncio.run(namespace["handle_schedule_max_backup"](call))
    finally:
        _restore_modules(saved_modules)

    assert entry_data["max_backup_saved_reserve"] == 33


# ---------------------------------------------------------------------------
# (4) Force-save last-resort branches: documented S3 residual (unchanged)
# ---------------------------------------------------------------------------


def test_force_save_last_resort_branches_still_use_untagged_api_reserve_s3_residual():
    """PW-4/S3 residual (documented, NOT closed by this fix): the two
    force-save last-resort fallback branches still fold a live (but
    provenance-untagged) site_info API read into saved_backup_reserve when
    neither _startup_backup_reserve nor _pre_idle_backup_reserve is set.

    Per the RSV-3 design (coordinator notes S3 / registry PW-4), this
    interim behavior is intentionally left unchanged by this change --
    closing it is deferred to a later RSV step that gates persist-time
    writes, not just the schedule_max_backup snapshot fixed here. This test
    exists to document the residual exposure and catch it if the branch
    shape is ever silently touched without updating this note.
    """
    source = INIT_PATH.read_text()

    occurrences = _find_all(
        source, "elif api_reserve is not None and api_reserve < 100:"
    )
    assert len(occurrences) == 2, (
        "expected exactly the two known force-save last-resort branches "
        f"(found {len(occurrences)}); if this count changed, re-check "
        "whether they were folded into resolve_restore_target (would mean "
        "S3 is closed -- update this test and the docstring above)"
    )
    for idx in occurrences:
        window = source[idx : idx + 400]
        assert 'site_state["saved_backup_reserve"] = api_reserve' in window
        # Still gated only by startup_reserve/pre_idle priority above it --
        # not by any read-trust tag (that gate is the still-open part).
        assert "startup_reserve is not None" in source[max(0, idx - 400) : idx]
