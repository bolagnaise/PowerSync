"""Regression tests for Solax Force Time (Gen2/Gen3) restore-baseline capture.

Bug OB-17: `_save_force_time_states` overwrote `self._saved_force_time_states`
unconditionally on every call, with no re-capture guard. The optimizer
re-issues force_charge/force_discharge every cycle to keep the hardware
timeout alive, so cycle 2 would read back the already-force-modified
entities (grid_export_limit, charge/discharge currents, charge window,
allow_grid_charge) and clobber the real restore baseline — `restore_normal`
would then "restore" to the force-modified values instead of the pre-force
state.

Fix: `_save_force_time_states` only captures a key the first time it is
seen (per force session); a key already present in the saved snapshot is
left untouched on re-entry, mirroring the SAJ (`_cached_discharge_enable`)
and Neovolt (`preserve_restore_modes`) re-capture guards.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
import sys
import types


ROOT = Path(__file__).resolve().parent.parent
COMPONENT_ROOT = ROOT / "custom_components" / "power_sync"


def _install_stubs() -> None:
    ha_root = types.ModuleType("homeassistant")
    ha_helpers = types.ModuleType("homeassistant.helpers")
    ha_entity_registry = types.ModuleType("homeassistant.helpers.entity_registry")
    ha_event = types.ModuleType("homeassistant.helpers.event")
    ha_util = types.ModuleType("homeassistant.util")
    ha_dt = types.ModuleType("homeassistant.util.dt")

    ha_entity_registry.async_get = lambda hass: hass.entity_registry
    ha_entity_registry.async_entries_for_config_entry = (
        lambda registry, entry_id: registry.entries_for(entry_id)
    )
    ha_event.async_call_later = lambda *args, **kwargs: (lambda: None)
    ha_dt.now = lambda *args, **kwargs: datetime(2026, 5, 3, tzinfo=timezone.utc)
    ha_dt.utcnow = lambda *args, **kwargs: datetime(2026, 5, 3, tzinfo=timezone.utc)
    ha_dt.UTC = timezone.utc

    ha_helpers.entity_registry = ha_entity_registry
    ha_helpers.event = ha_event
    ha_util.dt = ha_dt
    ha_root.helpers = ha_helpers
    ha_root.util = ha_util

    sys.modules["homeassistant"] = ha_root
    sys.modules["homeassistant.helpers"] = ha_helpers
    sys.modules["homeassistant.helpers.entity_registry"] = ha_entity_registry
    sys.modules["homeassistant.helpers.event"] = ha_event
    sys.modules["homeassistant.util"] = ha_util
    sys.modules["homeassistant.util.dt"] = ha_dt

    power_sync = types.ModuleType("power_sync")
    power_sync.__path__ = [str(COMPONENT_ROOT)]
    sys.modules["power_sync"] = power_sync

    inverters = types.ModuleType("power_sync.inverters")
    inverters.__path__ = [str(COMPONENT_ROOT / "inverters")]
    sys.modules["power_sync.inverters"] = inverters


_install_stubs()

from power_sync.inverters.solax_battery import SolaxBatteryController  # noqa: E402


class _FakeState:
    def __init__(self, entity_id: str, state: str = "0", options: list[str] | None = None):
        self.entity_id = entity_id
        self.state = state
        self.attributes = {"options": options or []}


class _FakeStates:
    def __init__(self, states: list[_FakeState]):
        self._states = {state.entity_id: state for state in states}

    def get(self, entity_id: str | None):
        return self._states.get(entity_id or "")

    def async_all(self, domain: str | None = None):
        if domain is None:
            return list(self._states.values())
        prefix = f"{domain}."
        return [state for state in self._states.values() if state.entity_id.startswith(prefix)]

    def set_state(self, entity_id: str, value: str) -> None:
        """Test helper: mutate an existing fake state's value in place."""
        self._states[entity_id].state = value


class _FakeServices:
    def __init__(self):
        self.calls: list[tuple[str, str, dict]] = []

    async def async_call(self, domain: str, service: str, data: dict, blocking: bool = True):
        self.calls.append((domain, service, dict(data)))


class _FakeRegistry:
    def __init__(self, entries: dict[str, list[str]] | None = None):
        self._entries = entries or {}

    def entries_for(self, entry_id: str):
        return [
            SimpleNamespace(entity_id=entity_id)
            for entity_id in self._entries.get(entry_id, [])
        ]


class _FakeHass:
    def __init__(
        self,
        states: list[_FakeState],
        registry_entries: dict[str, list[str]] | None = None,
    ):
        self.states = _FakeStates(states)
        self.services = _FakeServices()
        self.entity_registry = _FakeRegistry(registry_entries)


def _force_time_states() -> list[_FakeState]:
    """Entity set matching a Gen2/Gen3 Force Time Use profile (no Mode1/manual entities)."""
    return [
        _FakeState("sensor.solax_battery_capacity", "55"),
        _FakeState("sensor.solax_total_battery_power_charge", "0"),
        _FakeState("sensor.solax_measured_power", "0"),
        _FakeState(
            "select.solax_charger_use_mode",
            "Self Use Mode",
            ["Self Use Mode", "Force Time Use"],
        ),
        _FakeState("number.solax_battery_charge_max_current", "10"),
        _FakeState("number.solax_battery_discharge_max_current", "10"),
        _FakeState(
            "select.solax_allow_grid_charge",
            "Period 1 Disabled",
            ["Period 1 Disabled", "Period 1 Allowed"],
        ),
        _FakeState("time.solax_charge_start_1", "00:00:00"),
        _FakeState("time.solax_charge_end_1", "00:00:00"),
        _FakeState(
            "select.solax_export_duration",
            "Default",
            ["Default", "30 Minutes", "60 Minutes"],
        ),
        _FakeState("button.solax_grid_export", "unknown"),
        _FakeState("number.solax_grid_export_limit", "0"),
        _FakeState("number.solax_battery_minimum_capacity_grid_tied", "20"),
        _FakeState("number.solax_forcetime_period_1_max_capacity", "100"),
        _FakeState("number.solax_selfuse_discharge_min_soc", "20"),
    ]


async def _connect_force_time_controller():
    hass = _FakeHass(_force_time_states())
    controller = SolaxBatteryController(hass, entity_prefix="solax")
    assert await controller.connect()
    assert controller._control_profile == "force_time"
    return hass, controller


def test_save_force_time_states_does_not_reclobber_on_second_call():
    """Direct unit test: a second _save_force_time_states call for the same
    keys must not overwrite the values captured on the first call."""
    hass, controller = asyncio.run(_connect_force_time_controller())

    keys = (
        "charger_use_mode",
        "allow_grid_charge",
        "charge_start_1",
        "charge_end_1",
        "charge_current",
        "forcetime_period_1_max_capacity",
    )

    # Cycle 1: capture the genuine pre-force baseline.
    controller._save_force_time_states(keys)
    baseline = dict(controller._saved_force_time_states)
    assert baseline["charger_use_mode"] == "Self Use Mode"
    assert baseline["allow_grid_charge"] == "Period 1 Disabled"
    assert baseline["charge_current"] == "10"

    # Simulate the optimizer's per-cycle re-issue: by now these entities
    # have been force-modified (mode switched, grid charge enabled, current
    # bumped up).
    hass.states.set_state("select.solax_charger_use_mode", "Force Time Use")
    hass.states.set_state("select.solax_allow_grid_charge", "Period 1 Allowed")
    hass.states.set_state("number.solax_battery_charge_max_current", "25")

    # Cycle 2: re-issue captures again with the same key set.
    controller._save_force_time_states(keys)

    assert controller._saved_force_time_states == baseline
    assert controller._saved_force_time_states["charger_use_mode"] == "Self Use Mode"
    assert controller._saved_force_time_states["allow_grid_charge"] == "Period 1 Disabled"
    assert controller._saved_force_time_states["charge_current"] == "10"


def test_force_time_charge_reissue_preserves_restore_baseline():
    """End-to-end: two _force_time_charge calls (simulating the optimizer's
    per-cycle re-issue) must leave restore_normal able to unwind back to the
    genuine pre-force state, not the force-modified state from cycle 1."""
    hass, controller = asyncio.run(_connect_force_time_controller())

    asyncio.run(controller._force_time_charge(duration_minutes=30, amps=10.0))
    assert controller._saved_force_time_states["charger_use_mode"] == "Self Use Mode"
    assert controller._saved_force_time_states["charge_current"] == "10"

    # Simulate the force-modified entity states that would be live by the
    # time the optimizer re-issues force_charge next cycle.
    hass.states.set_state("select.solax_charger_use_mode", "Force Time Use")
    hass.states.set_state("select.solax_allow_grid_charge", "Period 1 Allowed")
    hass.states.set_state("number.solax_battery_charge_max_current", "25")
    hass.states.set_state("time.solax_charge_start_1", "10:00:00")
    hass.states.set_state("time.solax_charge_end_1", "10:30:00")

    # Cycle 2 re-issue uses a different amp value than cycle 1 — the
    # restored baseline must still be cycle 1's ORIGINAL pre-force reading
    # (10 A from _force_time_states()), not this cycle's force setpoint.
    asyncio.run(controller._force_time_charge(duration_minutes=30, amps=18.0))

    # The restore baseline must still reflect the ORIGINAL pre-force state.
    assert controller._saved_force_time_states["charger_use_mode"] == "Self Use Mode"
    assert controller._saved_force_time_states["allow_grid_charge"] == "Period 1 Disabled"
    assert controller._saved_force_time_states["charge_current"] == "10"
    assert controller._saved_force_time_states["charge_start_1"] == "00:00:00"
    assert controller._saved_force_time_states["charge_end_1"] == "00:00:00"

    pre_restore_call_count = len(hass.services.calls)
    asyncio.run(controller.restore_normal())
    calls_during_restore = hass.services.calls[pre_restore_call_count:]

    restore_calls = [
        call for call in calls_during_restore
        if call[2].get("entity_id") == "select.solax_charger_use_mode"
    ]
    # restore_normal replays the saved charger_use_mode first (Self Use Mode),
    # then force-sets it to Self Use Mode again as the final step — either
    # way it must never target the stale "Force Time Use" value.
    assert all(call[2].get("option") != "Force Time Use" for call in restore_calls)
    assert restore_calls
    assert restore_calls[0][2].get("option") == "Self Use Mode"

    current_calls = [
        call for call in calls_during_restore
        if call[2].get("entity_id") == "number.solax_battery_charge_max_current"
    ]
    assert current_calls
    assert current_calls[-1][2].get("value") == 10.0
