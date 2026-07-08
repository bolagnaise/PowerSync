"""Regression test for OB-29 (physical safety).

If HA reloads/restarts while a Powerwall is off-grid due to the curtailment
fallback, ``PowerwallCurtailmentFallback`` is rebuilt fresh with
``_active=False`` even though the grid contactor may still be physically
open. The startup orphan-cleanup path in
``optimization/coordinator.py`` detects this (grid_status contains
"island" with no active session) and calls ``fallback.release(...)`` to
reconnect — but the old ``release()`` began with ``if not self._active:
return True`` and never issued the real ``reconnect_grid()`` call, so the
house stayed stranded off-grid until the battery hit 0% and blacked out.

The fix adds a ``force: bool = False`` parameter to ``release()`` that
bypasses *only* the "not active" early-return, while leaving default
(``force=False``) behavior byte-identical for every other caller.
"""

from __future__ import annotations

import asyncio
import importlib
import sys
import types
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parent.parent
COMPONENT_ROOT = ROOT / "custom_components" / "power_sync"
POWERWALL_LOCAL_ROOT = COMPONENT_ROOT / "powerwall_local"


def _load_curtailment_fallback_module():
    """Import ``power_sync.powerwall_local.curtailment_fallback`` with a
    minimal stub environment, following the pattern used by
    tests/test_tesla_local_readback_overlay.py.

    We stub the top-level ``power_sync`` package and ``power_sync.const``
    (so we don't drag in unrelated constants), but let
    ``power_sync.powerwall_local`` resolve to the *real* directory on
    disk so ``curtailment_fallback.py`` and its lightweight sibling
    ``exceptions.py`` are imported for real. We avoid importing the real
    ``power_sync/powerwall_local/__init__.py`` (which pulls in the full
    LAN client + pairing manager) by pre-seeding a fake package module in
    sys.modules with ``__path__`` pointing at the real directory.
    """
    saved = {
        name: sys.modules.get(name)
        for name in (
            "homeassistant",
            "homeassistant.config_entries",
            "homeassistant.core",
            "homeassistant.util",
            "homeassistant.util.dt",
            "power_sync",
            "power_sync.const",
            "power_sync.powerwall_local",
            "power_sync.powerwall_local.exceptions",
            "power_sync.powerwall_local.curtailment_fallback",
        )
    }

    ha_root = types.ModuleType("homeassistant")
    ha_config_entries = types.ModuleType("homeassistant.config_entries")
    ha_core = types.ModuleType("homeassistant.core")
    ha_util = types.ModuleType("homeassistant.util")
    ha_util_dt = types.ModuleType("homeassistant.util.dt")

    ha_config_entries.ConfigEntry = type("ConfigEntry", (), {})
    ha_core.HomeAssistant = type("HomeAssistant", (), {})
    ha_util_dt.now = lambda: SimpleNamespace(date=lambda: None)
    ha_util.dt = ha_util_dt

    ps_module = types.ModuleType("power_sync")
    ps_module.__path__ = [str(COMPONENT_ROOT)]

    const_module = types.ModuleType("power_sync.const")
    const_module.DOMAIN = "power_sync"
    const_module.CONF_POWERWALL_LOCAL_PAIRED = "powerwall_local_paired"
    const_module.CONF_POWERWALL_OFFGRID_AS_CURTAILMENT = (
        "powerwall_offgrid_as_curtailment"
    )
    const_module.DEFAULT_POWERWALL_OFFGRID_AS_CURTAILMENT = False
    const_module.CONF_POWERWALL_OFFGRID_CURTAILMENT_MIN_SOC = (
        "powerwall_offgrid_curtailment_min_soc"
    )
    const_module.DEFAULT_POWERWALL_OFFGRID_CURTAILMENT_MIN_SOC = 40
    const_module.CONF_POWERWALL_OFFGRID_CURTAILMENT_MAX_SECONDS = (
        "powerwall_offgrid_curtailment_max_seconds"
    )
    const_module.DEFAULT_POWERWALL_OFFGRID_CURTAILMENT_MAX_SECONDS = 6 * 60 * 60

    pw_local_module = types.ModuleType("power_sync.powerwall_local")
    pw_local_module.__path__ = [str(POWERWALL_LOCAL_ROOT)]

    sys.modules["homeassistant"] = ha_root
    sys.modules["homeassistant.config_entries"] = ha_config_entries
    sys.modules["homeassistant.core"] = ha_core
    sys.modules["homeassistant.util"] = ha_util
    sys.modules["homeassistant.util.dt"] = ha_util_dt
    sys.modules["power_sync"] = ps_module
    sys.modules["power_sync.const"] = const_module
    sys.modules["power_sync.powerwall_local"] = pw_local_module
    sys.modules.pop("power_sync.powerwall_local.exceptions", None)
    sys.modules.pop("power_sync.powerwall_local.curtailment_fallback", None)

    module = importlib.import_module("power_sync.powerwall_local.curtailment_fallback")

    def restore() -> None:
        for name, module_obj in saved.items():
            if module_obj is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module_obj

    return module, restore


def _entry():
    return SimpleNamespace(
        entry_id="entry-1",
        data={"powerwall_local_paired": True},
        options={},
    )


class _FakeClient:
    """Records reconnect_grid() calls; go_off_grid() is unused by these tests."""

    def __init__(self) -> None:
        self.reconnect_calls = 0

    async def go_off_grid(self, *args, **kwargs) -> bool:
        raise AssertionError("go_off_grid should not be called by release()")

    async def reconnect_grid(self) -> bool:
        self.reconnect_calls += 1
        return True


def _coordinator(client: _FakeClient):
    async def _noop_refresh():
        return None

    return SimpleNamespace(client=client, async_request_refresh=_noop_refresh)


def test_orphan_cleanup_forces_reconnect_when_stale_active_flag_is_false():
    """Post-reload: _active=False but the contactor may still be open.

    The startup orphan-cleanup path must call reconnect_grid() anyway via
    force=True. This is the core OB-29 regression check — it fails against
    pre-fix code because the old release() returned True immediately on
    ``if not self._active`` without ever touching the client.
    """
    module, restore = _load_curtailment_fallback_module()
    try:
        fallback = module.PowerwallCurtailmentFallback(
            hass=SimpleNamespace(states=SimpleNamespace(get=lambda *_: None)),
            entry=_entry(),
            coordinator_getter=lambda: coord,
        )
        client = _FakeClient()
        coord = _coordinator(client)

        # Simulate the state after a HA reload: no in-memory record of the
        # off-grid session that was active before restart.
        assert fallback._active is False

        result = asyncio.run(
            fallback.release(trigger_reason="startup_orphan_cleanup", force=True)
        )

        assert result is True
        assert client.reconnect_calls == 1, (
            "release(force=True) must call reconnect_grid() even when "
            "_active is False, or a reload-orphaned Powerwall stays "
            "physically off-grid until it blacks out"
        )
        # Forced teardown must not crash and must leave a clean idle state.
        assert fallback._active is False
        assert fallback._started_at is None
        assert fallback._reason is None
    finally:
        restore()


def test_release_without_force_is_unchanged_noop_when_not_active():
    """Every other caller uses the default force=False — must stay a
    true no-op (no reconnect_grid call) when there is no active session,
    exactly like before this fix.
    """
    module, restore = _load_curtailment_fallback_module()
    try:
        fallback = module.PowerwallCurtailmentFallback(
            hass=SimpleNamespace(states=SimpleNamespace(get=lambda *_: None)),
            entry=_entry(),
            coordinator_getter=lambda: coord,
        )
        client = _FakeClient()
        coord = _coordinator(client)

        assert fallback._active is False

        result = asyncio.run(fallback.release(trigger_reason="optimizer_reconnect"))

        assert result is True
        assert client.reconnect_calls == 0
    finally:
        restore()
