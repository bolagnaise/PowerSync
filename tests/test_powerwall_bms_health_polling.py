"""Tests for periodic Powerwall BMS health polling."""

from __future__ import annotations

import ast
import asyncio
import importlib
import sys
import types
from pathlib import Path
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parent.parent / "custom_components" / "power_sync"
_SENTINEL = object()
_STUB_MODULE_NAMES = (
    "homeassistant.core",
    "homeassistant.helpers.event",
    "power_sync",
    "power_sync.powerwall_local",
    "power_sync.powerwall_local.bms_health_polling",
)


@pytest.fixture(autouse=True)
def _restore_stubbed_modules():
    saved_modules = {
        name: sys.modules.get(name, _SENTINEL)
        for name in _STUB_MODULE_NAMES
    }
    try:
        yield
    finally:
        for name in _STUB_MODULE_NAMES:
            if saved_modules[name] is _SENTINEL:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = saved_modules[name]


def _install_stubs() -> None:
    ha_core = types.ModuleType("homeassistant.core")
    ha_core.HomeAssistant = object
    sys.modules["homeassistant.core"] = ha_core

    ha_event = types.ModuleType("homeassistant.helpers.event")

    def async_track_time_interval(hass, action, interval):
        hass.interval_action = action
        hass.interval = interval

        def _cancel():
            hass.cancelled = True

        return _cancel

    def async_call_later(hass, delay, action):
        hass.initial_action = action
        hass.initial_delay = delay

        def _cancel():
            hass.initial_cancelled = True

        return _cancel

    ha_event.async_track_time_interval = async_track_time_interval
    ha_event.async_call_later = async_call_later
    sys.modules["homeassistant.helpers.event"] = ha_event

    ps_module = types.ModuleType("power_sync")
    ps_module.__path__ = [str(ROOT)]
    sys.modules["power_sync"] = ps_module

    local_module = types.ModuleType("power_sync.powerwall_local")
    local_module.__path__ = [str(ROOT / "powerwall_local")]
    sys.modules["power_sync.powerwall_local"] = local_module


def _polling_module():
    _install_stubs()
    sys.modules.pop("power_sync.powerwall_local.bms_health_polling", None)
    return importlib.import_module("power_sync.powerwall_local.bms_health_polling")


class _Hass:
    cancelled = False
    initial_cancelled = False


def test_powerwall_bms_health_polling_runs_every_five_minutes():
    polling = _polling_module()
    hass = _Hass()
    synced = []
    payload = {"available": True, "individual_batteries": []}

    async def fetch():
        return payload

    async def sync(data):
        synced.append(data)

    cancel = polling.async_start_powerwall_bms_health_polling(
        hass,
        "entry-1",
        fetch,
        sync,
    )

    assert hass.interval == polling.POWERWALL_BMS_HEALTH_POLL_INTERVAL
    assert hass.interval.total_seconds() == 300

    asyncio.run(hass.interval_action(None))

    assert synced == [payload]

    cancel()
    assert hass.cancelled is True
    assert hass.initial_cancelled is True


def test_powerwall_bms_health_polling_runs_promptly_after_arming():
    """Ticket 64: the interval helper does not fire when it is armed.

    Without a prompt first poll the sensor republished whatever the restore
    path put there -- a stale mobile WiFi-scan capacity -- for a full five
    minutes after every integration reload.
    """
    polling = _polling_module()
    hass = _Hass()
    synced = []
    payload = {"available": True, "individual_batteries": []}

    async def fetch():
        return payload

    async def sync(data):
        synced.append(data)

    polling.async_start_powerwall_bms_health_polling(hass, "entry-1", fetch, sync)

    assert hass.initial_delay == polling.POWERWALL_BMS_HEALTH_INITIAL_POLL_DELAY
    assert hass.initial_delay.total_seconds() <= 60
    assert hass.initial_delay < hass.interval

    asyncio.run(hass.initial_action(None))

    assert synced == [payload]


def test_powerwall_bms_health_polling_skips_overlapping_fetches():
    polling = _polling_module()
    hass = _Hass()
    started = asyncio.Event()
    release = asyncio.Event()
    fetch_count = 0
    synced = []

    async def fetch():
        nonlocal fetch_count
        fetch_count += 1
        started.set()
        await release.wait()
        return {"available": True}

    async def sync(data):
        synced.append(data)

    polling.async_start_powerwall_bms_health_polling(
        hass,
        "entry-1",
        fetch,
        sync,
    )

    async def run_test():
        first = asyncio.create_task(hass.interval_action(None))
        await started.wait()
        await hass.interval_action(None)
        release.set()
        await first

    asyncio.run(run_test())

    assert fetch_count == 1
    assert synced == [{"available": True}]


def _load_bms_health_syncer():
    """Extract the nested poll->sensor sync closure from ``__init__.py``.

    The AST source-extraction pattern (see ``tests/test_sungrow_curtailment_
    runtime.py``) lets a single nested function be driven in isolation without
    importing the 148k-line module.
    """
    init_path = ROOT / "__init__.py"
    tree = ast.parse(init_path.read_text())
    node = next(
        child
        for child in ast.walk(tree)
        if isinstance(child, ast.AsyncFunctionDef)
        and child.name == "_sync_powerwall_bms_health"
    )
    module = ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[]))
    namespace: dict[str, Any] = {"Any": Any}
    exec(compile(module, str(init_path), "exec"), namespace)
    return namespace["_sync_powerwall_bms_health"], namespace


class _RecordingBatteryHealthView:
    def __init__(self) -> None:
        self.calls: list[tuple[dict, bool]] = []

    async def _sync_live_battery_health_to_sensor(self, entry, payload, *, persist):
        self.calls.append((payload, persist))


def _run_sync(namespace, syncer, stored, payload):
    view = _RecordingBatteryHealthView()
    entry = types.SimpleNamespace(entry_id="entry-1")
    entry_data = {"battery_health": stored} if stored is not None else {}
    namespace["DOMAIN"] = "power_sync"
    namespace["entry"] = entry
    namespace["battery_health_view"] = view
    namespace["_time"] = types.SimpleNamespace(monotonic=lambda: 1000.0)
    namespace["hass"] = types.SimpleNamespace(
        data={"power_sync": {"entry-1": entry_data}}
    )
    asyncio.run(syncer(payload))
    return view, entry_data


_FLEET_PAYLOAD = {
    "available": True,
    "current_capacity_wh": 71800,
    "original_capacity_wh": 67500,
    "battery_count": 5,
    "source": "ha_local_tedapi",
}


def test_fleet_bms_poll_persists_a_corrected_capacity():
    """Ticket 64: the authoritative Fleet value never reached the Store.

    The poll synced with ``persist=False`` and cached into the in-memory
    ``battery_health_cloud`` only, so every reload restored the stale mobile
    WiFi-scan capacity (57.6 kWh = 4 of 5 packs) and republished it as the
    current total until the next poll landed.
    """
    syncer, namespace = _load_bms_health_syncer()
    stale_wifi_scan = {
        "current_capacity_wh": 57600,
        "original_capacity_wh": 67500,
        "battery_count": 5,
        "source": "mobile_app_wifi_scan",
    }

    view, entry_data = _run_sync(namespace, syncer, stale_wifi_scan, _FLEET_PAYLOAD)

    assert view.calls == [(_FLEET_PAYLOAD, True)]
    # The live cache is still populated for the app status payload.
    assert entry_data["battery_health_cloud"]["value"] is _FLEET_PAYLOAD


def test_fleet_bms_poll_does_not_rewrite_an_unchanged_capacity():
    """Persisting every five minutes forever would be a needless Store write."""
    syncer, namespace = _load_bms_health_syncer()
    already_current = {
        "current_capacity_wh": 71800,
        "original_capacity_wh": 67500,
        "battery_count": 5,
        "source": "ha_local_tedapi",
    }

    view, _ = _run_sync(namespace, syncer, already_current, _FLEET_PAYLOAD)

    assert view.calls == [(_FLEET_PAYLOAD, False)]


def test_fleet_bms_poll_persists_when_no_stored_snapshot_exists():
    syncer, namespace = _load_bms_health_syncer()

    view, _ = _run_sync(namespace, syncer, None, _FLEET_PAYLOAD)

    assert view.calls == [(_FLEET_PAYLOAD, True)]
