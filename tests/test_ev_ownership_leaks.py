"""Regression tests for two EV ownership/loadpoint leak bugs.

OB-18: ``_clear_ble_dynamic_session_if_unplugged`` — the ONLY unplug detector
in the dynamic charging loops (``_dynamic_ev_update`` /
``_dynamic_ev_update_surplus``) — used to early-return for any vehicle whose
VIN didn't start with "ble_". Fleet/Teslemetry/OCPP/generic loadpoints never
had a mid-session unplug detected: the ev_ownership lease stayed held
(blocking the other vehicle's cross-family start) and the timer kept issuing
amp commands against the unplugged car. Fixed by generalizing the plug-state
check to all providers via ``is_ev_plugged_in``.

OB-19: a no-VIN manual ``stop_ev_charging`` (Tesla/unset charger type)
resolves to DEFAULT_VEHICLE_ID ("_default") via ``_ev_action_loadpoint_id``,
so the 15-minute restart-suppression hold used to always get recorded under
"_default". ``manual_stop_hold_reason`` treats "_default" as a fallback
candidate for EVERY vin, so stopping car A without a VIN blocked car B's
scheduled/price start for 15 minutes too. Fixed by resolving the actually-
charging VIN before recording the hold, and skipping the hold entirely (never
falling back to "_default") when it can't be resolved and more than one
vehicle is configured.

HD-18: ``release_ev_ownership``/``clear_ev_ownerships`` used to pop only the
exact resolved key from the leases dict, while ``get_ev_ownership`` (and
``claim_ev_ownership``) resolve/evict through the "_default" overlap. A lease
claimed under "_default" (e.g. a no-VIN start) but later released/cleared
under a resolved VIN left the "_default" entry in place, so
``get_ev_ownership`` kept finding it via the default-candidate fallback and
reported the loadpoint owned forever — blocking a second vehicle's
cross-family start. Fixed by mirroring ``claim_ev_ownership``'s guarded
"_default" eviction (pop the exact key, then also pop "_default" only when
the exact key wasn't itself "_default") in both ``release_ev_ownership`` and
``clear_ev_ownerships``, so a falsy/"_default" id never resolves onto an
unrelated sibling VIN lease.
"""

from __future__ import annotations

import asyncio
import importlib
import sys
import types
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock


ROOT = Path(__file__).resolve().parent.parent / "custom_components" / "power_sync"


def _install_ha_stubs() -> None:
    ha_root = sys.modules.setdefault("homeassistant", types.ModuleType("homeassistant"))
    ha_config_entries = sys.modules.setdefault(
        "homeassistant.config_entries", types.ModuleType("homeassistant.config_entries")
    )
    ha_core = sys.modules.setdefault("homeassistant.core", types.ModuleType("homeassistant.core"))
    ha_exceptions = sys.modules.setdefault(
        "homeassistant.exceptions", types.ModuleType("homeassistant.exceptions")
    )
    ha_helpers = sys.modules.setdefault(
        "homeassistant.helpers", types.ModuleType("homeassistant.helpers")
    )
    ha_storage = sys.modules.setdefault(
        "homeassistant.helpers.storage", types.ModuleType("homeassistant.helpers.storage")
    )
    ha_update = sys.modules.setdefault(
        "homeassistant.helpers.update_coordinator",
        types.ModuleType("homeassistant.helpers.update_coordinator"),
    )
    ha_er = sys.modules.setdefault(
        "homeassistant.helpers.entity_registry",
        types.ModuleType("homeassistant.helpers.entity_registry"),
    )
    ha_dr = sys.modules.setdefault(
        "homeassistant.helpers.device_registry",
        types.ModuleType("homeassistant.helpers.device_registry"),
    )
    ha_event = sys.modules.setdefault(
        "homeassistant.helpers.event", types.ModuleType("homeassistant.helpers.event")
    )
    ha_util = sys.modules.setdefault("homeassistant.util", types.ModuleType("homeassistant.util"))
    ha_dt = sys.modules.setdefault("homeassistant.util.dt", types.ModuleType("homeassistant.util.dt"))

    ha_core.HomeAssistant = type("HomeAssistant", (), {})
    ha_config_entries.ConfigEntry = type("ConfigEntry", (), {})
    ha_exceptions.ConfigEntryNotReady = type("ConfigEntryNotReady", (Exception,), {})
    ha_er.async_get = lambda hass: getattr(hass, "entity_registry", SimpleNamespace(entities={}))
    ha_dr.async_get = lambda hass: getattr(hass, "device_registry", SimpleNamespace(devices={}))
    ha_storage.Store = type("Store", (), {"__init__": lambda self, *args, **kwargs: None})
    ha_update.DataUpdateCoordinator = type(
        "DataUpdateCoordinator",
        (),
        {
            "__class_getitem__": classmethod(lambda cls, item: cls),
            "__init__": lambda self, *args, **kwargs: None,
        },
    )
    ha_event.async_track_time_interval = lambda *args, **kwargs: (lambda: None)
    ha_event.async_track_time_change = lambda *args, **kwargs: (lambda: None)
    ha_event.async_track_point_in_time = lambda *args, **kwargs: (lambda: None)
    ha_dt.now = getattr(ha_dt, "now", lambda *args, **kwargs: None)
    ha_dt.utcnow = getattr(ha_dt, "utcnow", lambda *args, **kwargs: None)

    ha_helpers.entity_registry = ha_er
    ha_helpers.device_registry = ha_dr
    ha_helpers.storage = ha_storage
    ha_helpers.update_coordinator = ha_update
    ha_helpers.event = ha_event
    ha_util.dt = ha_dt
    ha_root.helpers = ha_helpers
    ha_root.util = ha_util


_install_ha_stubs()

_ps = types.ModuleType("power_sync")
_ps.__path__ = [str(ROOT)]
sys.modules["power_sync"] = _ps

_automations = types.ModuleType("power_sync.automations")
_automations.__path__ = [str(ROOT / "automations")]
sys.modules["power_sync.automations"] = _automations

if not hasattr(sys.modules.get("power_sync.const"), "CONF_EV_PROVIDER"):
    sys.modules.pop("power_sync.const", None)
sys.modules.pop("power_sync.automations.actions", None)
actions = importlib.import_module("power_sync.automations.actions")
ev_ownership = importlib.import_module("power_sync.automations.ev_ownership")


class _State:
    def __init__(self, entity_id: str, state: str, attributes: dict | None = None) -> None:
        self.entity_id = entity_id
        self.state = state
        self.attributes = attributes or {}


class _States:
    def __init__(self, states: list[_State]) -> None:
        self._states = {state.entity_id: state for state in states}

    def get(self, entity_id: str):
        return self._states.get(entity_id)

    def async_entity_ids(self, domain: str | None = None):
        if domain is None:
            return list(self._states)
        return [entity_id for entity_id in self._states if entity_id.startswith(f"{domain}.")]

    def async_all(self, domain: str | None = None):
        if domain is None:
            return list(self._states.values())
        return [
            state for entity_id, state in self._states.items()
            if entity_id.startswith(f"{domain}.")
        ]


class _Services:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict]] = []

    async def async_call(self, domain: str, service: str, data: dict, blocking: bool = True):
        self.calls.append((domain, service, data))


class _Hass:
    def __init__(self, states: list[_State] | None = None) -> None:
        self.data = {"power_sync": {"entry-1": {}}}
        self.states = _States(states or [])
        self.services = _Services()
        self.entity_registry = SimpleNamespace(entities={})
        self.device_registry = SimpleNamespace(devices={})


class _Entry:
    entry_id = "entry-1"
    data: dict = {}
    options: dict = {}


def _install_ev_planner_stub(
    monkeypatch,
    *,
    plugged_in=None,
    plugged_in_fn=None,
    configured_vehicles: list | None = None,
):
    """Replace power_sync.automations.ev_charging_planner with a lightweight
    stub exposing only what the dynamic-EV loop / stop-hold resolver need,
    mirroring the module-replacement pattern already used for solar-surplus
    tests in tests/test_ev_ocpp_actions.py."""
    ev_planner = types.ModuleType("power_sync.automations.ev_charging_planner")

    async def is_ev_plugged_in(hass, config_entry, vehicle_vin=None):
        if plugged_in_fn is not None:
            return plugged_in_fn(vehicle_vin)
        return bool(plugged_in)

    async def discover_all_tesla_vehicles(hass, config_entry):
        return list(configured_vehicles or [])

    ev_planner.is_ev_plugged_in = is_ev_plugged_in
    ev_planner.discover_all_tesla_vehicles = discover_all_tesla_vehicles
    monkeypatch.setitem(sys.modules, "power_sync.automations.ev_charging_planner", ev_planner)

    ev_session = types.ModuleType("power_sync.automations.ev_charging_session")
    ev_session.get_session_manager = lambda: None
    monkeypatch.setitem(sys.modules, "power_sync.automations.ev_charging_session", ev_session)

    return ev_planner


# ---------------------------------------------------------------------------
# OB-18: non-BLE dynamic EV sessions must detect unplug too
# ---------------------------------------------------------------------------

FLEET_VIN = "5YJ3E1EA7NF000001"


def _fleet_dynamic_state(active: bool = True) -> dict:
    return {
        "active": active,
        "current_amps": 16,
        "target_amps": 16,
        "params": {
            "dynamic_mode": "battery_target",
            "charger_type": "tesla",
            "vehicle_vin": FLEET_VIN,
            "target_battery_charge_kw": 5.0,
            "max_grid_import_kw": 12.5,
            "min_charge_amps": 5,
            "max_charge_amps": 32,
            "voltage": 240,
            "phases": 1,
        },
    }


def _clear_once(hass, entry, vehicle_id=FLEET_VIN):
    return asyncio.run(
        actions._clear_ble_dynamic_session_if_unplugged(
            hass,
            entry,
            vehicle_id,
            actions._dynamic_ev_state["entry-1"][vehicle_id]["params"],
        )
    )


def test_single_unplugged_read_keeps_non_ble_session(monkeypatch):
    """A SINGLE 'not plugged in' read must NOT clear the session — it may just
    be a transient telemetry gap (integration down, empty registry, OCPP 'no
    connector present'). The debounce counter advances to 1 but the session
    and lease survive. Clearing on the first False would falsely stop an
    actively-charging car."""
    _install_ev_planner_stub(monkeypatch, plugged_in=False)

    hass = _Hass()
    entry = _Entry()
    actions._dynamic_ev_state.clear()
    actions._dynamic_ev_state["entry-1"] = {FLEET_VIN: _fleet_dynamic_state()}
    ev_ownership.claim_ev_ownership(hass, entry, FLEET_VIN, owner_mode="dynamic", command="start")

    cleared = _clear_once(hass, entry)

    assert cleared is False
    assert FLEET_VIN in actions._dynamic_ev_state["entry-1"]
    assert actions._dynamic_ev_state["entry-1"][FLEET_VIN]["consecutive_unplugged"] == 1
    _lease_id, lease = ev_ownership.get_ev_ownership(hass, entry, FLEET_VIN)
    assert lease is not None


def test_two_consecutive_unplugged_reads_clear_and_release(monkeypatch):
    """A Fleet/Teslemetry VIN (no 'ble_' prefix) must have its stale dynamic
    session cleared and ev_ownership lease released after TWO consecutive
    unplugged reads (the debounce threshold). Pre-fix,
    _clear_ble_dynamic_session_if_unplugged returned False immediately for any
    non-'ble_' VIN without even checking plug state, so the lease leaked
    forever."""
    _install_ev_planner_stub(monkeypatch, plugged_in=False)

    hass = _Hass()
    entry = _Entry()
    actions._dynamic_ev_state.clear()
    actions._dynamic_ev_state["entry-1"] = {FLEET_VIN: _fleet_dynamic_state()}
    ev_ownership.claim_ev_ownership(hass, entry, FLEET_VIN, owner_mode="dynamic", command="start")

    # 1st unplugged read: debounced, session kept.
    assert _clear_once(hass, entry) is False
    assert FLEET_VIN in actions._dynamic_ev_state["entry-1"]

    # 2nd consecutive unplugged read: threshold reached → clear + release.
    cleared = _clear_once(hass, entry)

    assert cleared is True
    assert FLEET_VIN not in actions._dynamic_ev_state.get("entry-1", {})
    _lease_id, lease = ev_ownership.get_ev_ownership(hass, entry, FLEET_VIN)
    assert lease is None


def test_plugged_read_between_unplugged_reads_resets_counter(monkeypatch):
    """A confirmed-plugged read must reset the debounce counter, so an
    unplugged / plugged / unplugged sequence never reaches two CONSECUTIVE
    unplugged reads and the session is preserved."""
    reads = iter([False, True, False])

    def plug_seq(_vehicle_vin):
        return next(reads)

    _install_ev_planner_stub(monkeypatch, plugged_in_fn=plug_seq)

    hass = _Hass()
    entry = _Entry()
    actions._dynamic_ev_state.clear()
    actions._dynamic_ev_state["entry-1"] = {FLEET_VIN: _fleet_dynamic_state()}
    ev_ownership.claim_ev_ownership(hass, entry, FLEET_VIN, owner_mode="dynamic", command="start")

    assert _clear_once(hass, entry) is False  # unplugged → counter 1
    assert actions._dynamic_ev_state["entry-1"][FLEET_VIN]["consecutive_unplugged"] == 1
    assert _clear_once(hass, entry) is False  # plugged → counter reset to 0
    assert actions._dynamic_ev_state["entry-1"][FLEET_VIN]["consecutive_unplugged"] == 0
    assert _clear_once(hass, entry) is False  # unplugged again → counter 1, still kept

    assert FLEET_VIN in actions._dynamic_ev_state["entry-1"]
    assert actions._dynamic_ev_state["entry-1"][FLEET_VIN]["consecutive_unplugged"] == 1
    _lease_id, lease = ev_ownership.get_ev_ownership(hass, entry, FLEET_VIN)
    assert lease is not None


def test_non_ble_dynamic_update_tears_down_and_stops_amps_after_debounce(monkeypatch):
    """End-to-end through the real dynamic-loop call site (_dynamic_ev_update):
    the first unplugged cycle still runs normally (could be a blip — amps may
    be issued), but the second consecutive unplugged cycle tears the session
    down and releases the lease. Once torn down, no further amp commands are
    issued against the now-unplugged vehicle (Fleet rate-limit pressure)."""
    _install_ev_planner_stub(monkeypatch, plugged_in=False)

    set_amps_calls: list[tuple[str, int]] = []

    async def fake_set_vehicle_amps(hass, config_entry, vehicle_id, amps, params):
        set_amps_calls.append((vehicle_id, amps))
        return True

    async def fake_live_status(hass, config_entry):
        return {"battery_power": -5000, "grid_power": 3000, "battery_soc": 55}

    monkeypatch.setattr(actions, "_set_vehicle_amps", fake_set_vehicle_amps)
    monkeypatch.setattr(actions, "_get_tesla_live_status", fake_live_status)

    hass = _Hass()
    entry = _Entry()
    actions._dynamic_ev_state.clear()
    actions._dynamic_ev_state["entry-1"] = {FLEET_VIN: _fleet_dynamic_state()}
    ev_ownership.claim_ev_ownership(hass, entry, FLEET_VIN, owner_mode="dynamic", command="start")

    # Cycle 1: debounced — session kept (telemetry could be momentarily gone).
    asyncio.run(actions._dynamic_ev_update(hass, entry, "entry-1", FLEET_VIN))
    assert FLEET_VIN in actions._dynamic_ev_state["entry-1"]
    _lease_id, lease = ev_ownership.get_ev_ownership(hass, entry, FLEET_VIN)
    assert lease is not None

    # Cycle 2: second consecutive unplugged read → tear down + release lease.
    asyncio.run(actions._dynamic_ev_update(hass, entry, "entry-1", FLEET_VIN))
    assert FLEET_VIN not in actions._dynamic_ev_state.get("entry-1", {})
    _lease_id, lease = ev_ownership.get_ev_ownership(hass, entry, FLEET_VIN)
    assert lease is None

    # Cycle 3: session is gone → the update early-returns, no further amps.
    amps_before = len(set_amps_calls)
    asyncio.run(actions._dynamic_ev_update(hass, entry, "entry-1", FLEET_VIN))
    assert len(set_amps_calls) == amps_before


def test_still_plugged_non_ble_session_is_not_falsely_released(monkeypatch):
    _install_ev_planner_stub(monkeypatch, plugged_in=True)

    hass = _Hass()
    entry = _Entry()
    actions._dynamic_ev_state.clear()
    actions._dynamic_ev_state["entry-1"] = {FLEET_VIN: _fleet_dynamic_state()}
    ev_ownership.claim_ev_ownership(hass, entry, FLEET_VIN, owner_mode="dynamic", command="start")

    cleared = asyncio.run(
        actions._clear_ble_dynamic_session_if_unplugged(
            hass, entry, FLEET_VIN, actions._dynamic_ev_state["entry-1"][FLEET_VIN]["params"]
        )
    )

    assert cleared is False
    assert FLEET_VIN in actions._dynamic_ev_state["entry-1"]
    _lease_id, lease = ev_ownership.get_ev_ownership(hass, entry, FLEET_VIN)
    assert lease is not None


def test_stale_unplug_result_cannot_clear_replacement_session(monkeypatch):
    plug_check_started = asyncio.Event()
    release_plug_check = asyncio.Event()
    ev_planner = types.ModuleType("power_sync.automations.ev_charging_planner")

    async def is_ev_plugged_in(hass, config_entry, vehicle_vin=None):
        plug_check_started.set()
        await release_plug_check.wait()
        return False

    ev_planner.is_ev_plugged_in = is_ev_plugged_in
    monkeypatch.setitem(
        sys.modules,
        "power_sync.automations.ev_charging_planner",
        ev_planner,
    )

    hass = _Hass()
    entry = _Entry()
    original_state = _fleet_dynamic_state()
    replacement_state = _fleet_dynamic_state()
    replacement_state["params"]["owner_mode"] = "scheduled"
    replacement_state["consecutive_unplugged"] = 1
    actions._dynamic_ev_state.clear()
    actions._dynamic_ev_state["entry-1"] = {FLEET_VIN: original_state}

    async def run_replacement():
        stale_check = asyncio.create_task(
            actions._clear_ble_dynamic_session_if_unplugged(
                hass,
                entry,
                FLEET_VIN,
                original_state["params"],
                expected_state=original_state,
            )
        )
        await plug_check_started.wait()
        actions._dynamic_ev_state["entry-1"][FLEET_VIN] = replacement_state
        release_plug_check.set()
        return await stale_check

    cleared = asyncio.run(run_replacement())

    assert cleared is False
    assert actions._dynamic_ev_state["entry-1"][FLEET_VIN] is replacement_state
    assert replacement_state["consecutive_unplugged"] == 1


def test_asleep_or_unavailable_non_ble_session_is_not_falsely_released(monkeypatch):
    """is_ev_plugged_in already treats an asleep/unavailable vehicle as
    "assume plugged in" (see ev_charging_planner.is_ev_plugged_in). Verify
    the generalized unplug check preserves that convention instead of
    releasing a sleeping car's session."""

    async def fake_is_ev_plugged_in(hass, config_entry, vehicle_vin=None):
        # Represents the asleep/unavailable-but-assume-plugged branch inside
        # the real is_ev_plugged_in.
        return True

    ev_planner = types.ModuleType("power_sync.automations.ev_charging_planner")
    ev_planner.is_ev_plugged_in = fake_is_ev_plugged_in
    monkeypatch.setitem(sys.modules, "power_sync.automations.ev_charging_planner", ev_planner)

    hass = _Hass()
    entry = _Entry()
    actions._dynamic_ev_state.clear()
    actions._dynamic_ev_state["entry-1"] = {FLEET_VIN: _fleet_dynamic_state()}
    ev_ownership.claim_ev_ownership(hass, entry, FLEET_VIN, owner_mode="dynamic", command="start")

    cleared = asyncio.run(
        actions._clear_ble_dynamic_session_if_unplugged(
            hass, entry, FLEET_VIN, actions._dynamic_ev_state["entry-1"][FLEET_VIN]["params"]
        )
    )

    assert cleared is False
    assert FLEET_VIN in actions._dynamic_ev_state["entry-1"]
    _lease_id, lease = ev_ownership.get_ev_ownership(hass, entry, FLEET_VIN)
    assert lease is not None


# ---------------------------------------------------------------------------
# OB-19: no-VIN manual stop must not record a "_default" hold that blocks
# every other configured vehicle
# ---------------------------------------------------------------------------

VIN_A = "5YJ3E1EA7NF0000A1"
VIN_B = "5YJ3E1EA7NF0000B2"


def _both_provider_entry(prefixes: str, vehicle_mapping: str = ""):
    return SimpleNamespace(
        entry_id="entry-1",
        data={},
        options={
            "ev_provider": "both",
            "tesla_ble_entity_prefix": prefixes,
            "tesla_ble_vehicle_mapping": vehicle_mapping,
        },
    )


def _two_fleet_vehicle_hass() -> _Hass:
    hass = _Hass()
    hass.device_registry.devices = {
        "fleet-a": SimpleNamespace(
            identifiers={("tesla_fleet", VIN_A)},
        ),
        "teslemetry-a": SimpleNamespace(
            identifiers={("teslemetry", VIN_A)},
        ),
        "fleet-b": SimpleNamespace(
            identifiers={("tesla_fleet", VIN_B)},
        ),
    }
    return hass


def test_unmapped_multi_vehicle_setup_never_guesses_a_ble_bridge():
    hass = _two_fleet_vehicle_hass()
    entry = _both_provider_entry("primary_ev_bridge")

    assert actions._resolve_ble_prefix_for_vehicle(hass, entry, VIN_A) == ""
    assert actions._resolve_ble_prefix_for_vehicle(hass, entry, VIN_B) == ""


def test_single_vehicle_command_uses_unambiguous_autodetected_ble_bridge(monkeypatch):
    monkeypatch.setattr(actions, "TESLA_EV_INTEGRATIONS", {"tesla_fleet"})
    monkeypatch.setattr(actions.dr, "async_get", lambda hass: hass.device_registry)
    hass = _Hass(
        [
            _State("sensor.garage_ble_charging_state", "Stopped"),
            _State("binary_sensor.garage_ble_ble_status", "on"),
        ]
    )
    hass.device_registry.devices = {
        "fleet-a": SimpleNamespace(identifiers={("tesla_fleet", VIN_A)})
    }
    entry = _both_provider_entry("tesla_ble")

    assert (
        actions._resolve_ble_prefix_for_vehicle(hass, entry, VIN_A)
        == "garage_ble"
    )


def test_explicit_ble_mapping_is_independent_of_registry_and_prefix_order():
    hass = _two_fleet_vehicle_hass()
    entry = _both_provider_entry(
        "bridge_alpha,bridge_beta",
        f"{VIN_A}=bridge_beta,{VIN_B}=bridge_alpha",
    )

    assert actions._resolve_ble_prefix_for_vehicle(hass, entry, VIN_A) == "bridge_beta"
    assert actions._resolve_ble_prefix_for_vehicle(hass, entry, VIN_B) == "bridge_alpha"


def test_ev_action_config_merges_legacy_data_with_options():
    entry = SimpleNamespace(
        data={
            "ev_provider": "both",
            "tesla_ble_entity_prefix": "legacy_bridge",
        },
        options={},
    )

    assert actions._get_ev_config(entry) == {
        "ev_provider": "both",
        "ble_prefix": "legacy_bridge",
    }


def test_ble_availability_distinguishes_sleeping_from_unavailable_bridge():
    sleeping = _Hass([_State("switch.bridge_alpha_charger", "unknown")])
    unavailable = _Hass([_State("switch.bridge_alpha_charger", "unavailable")])

    assert actions._is_ble_available(sleeping, "bridge_alpha") is True
    assert actions._is_ble_available(unavailable, "bridge_alpha") is False


def test_both_provider_start_uses_fleet_when_paired_ble_is_unavailable(monkeypatch):
    hass = _two_fleet_vehicle_hass()
    hass.states = _States([_State("switch.bridge_alpha_charger", "unavailable")])
    entry = _both_provider_entry(
        "bridge_alpha,bridge_beta",
        f"{VIN_A}=bridge_beta,{VIN_B}=bridge_alpha",
    )
    ble_start = AsyncMock(return_value=True)
    monkeypatch.setattr(actions, "_start_ev_charging_ble", ble_start)
    monkeypatch.setattr(actions, "_resolve_teslemetry_bt_prefix", lambda *args: "")
    monkeypatch.setattr(actions, "_is_teslemetry_bt_available", lambda *args: False)
    monkeypatch.setattr(
        actions,
        "_get_tesla_ev_entity",
        AsyncMock(return_value="switch.w3rt1e_charge"),
    )
    monkeypatch.setattr(actions, "_wake_tesla_ev", AsyncMock(return_value=True))
    monkeypatch.setattr(actions, "_is_api_credit_available", lambda *args: True)

    result = asyncio.run(
        actions._action_start_ev_charging(
            hass,
            entry,
            {"vehicle_vin": VIN_B, "charger_type": "tesla"},
            {},
        )
    )

    assert result is True
    ble_start.assert_not_awaited()
    assert hass.services.calls == [
        ("switch", "turn_on", {"entity_id": "switch.w3rt1e_charge"})
    ]


def test_both_provider_start_uses_matching_teslemetry_bt_when_ble_is_unavailable(
    monkeypatch,
):
    hass = _two_fleet_vehicle_hass()
    hass.states = _States([_State("switch.bridge_alpha_charger", "unavailable")])
    entry = _both_provider_entry(
        "bridge_alpha,bridge_beta",
        f"{VIN_A}=bridge_beta,{VIN_B}=bridge_alpha",
    )
    ble_start = AsyncMock(return_value=True)
    tbt_start = AsyncMock(return_value=True)
    monkeypatch.setattr(actions, "_start_ev_charging_ble", ble_start)
    monkeypatch.setattr(actions, "_resolve_teslemetry_bt_prefix", lambda *args: VIN_B)
    monkeypatch.setattr(actions, "_is_teslemetry_bt_available", lambda *args: True)
    monkeypatch.setattr(actions, "_start_ev_charging_teslemetry_bt", tbt_start)
    fleet_entity = AsyncMock(
        side_effect=AssertionError("Fleet fallback was not expected")
    )
    monkeypatch.setattr(actions, "_get_tesla_ev_entity", fleet_entity)

    result = asyncio.run(
        actions._action_start_ev_charging(
            hass,
            entry,
            {"vehicle_vin": VIN_B, "charger_type": "tesla"},
            {},
        )
    )

    assert result is True
    ble_start.assert_not_awaited()
    tbt_start.assert_awaited_once_with(hass, VIN_B)
    fleet_entity.assert_not_awaited()


def test_both_provider_start_prefers_paired_esphome_ble(monkeypatch):
    hass = _two_fleet_vehicle_hass()
    entry = _both_provider_entry(
        "bridge_alpha,bridge_beta",
        f"{VIN_A}=bridge_beta,{VIN_B}=bridge_alpha",
    )
    ble_start = AsyncMock(return_value=True)
    tbt_start = AsyncMock(return_value=True)
    monkeypatch.setattr(actions, "_is_ble_available", lambda *args: True)
    monkeypatch.setattr(actions, "_start_ev_charging_ble", ble_start)
    monkeypatch.setattr(actions, "_resolve_teslemetry_bt_prefix", lambda *args: VIN_B)
    monkeypatch.setattr(actions, "_is_teslemetry_bt_available", lambda *args: True)
    monkeypatch.setattr(actions, "_start_ev_charging_teslemetry_bt", tbt_start)

    result = asyncio.run(
        actions._action_start_ev_charging(
            hass,
            entry,
            {"vehicle_vin": VIN_B, "charger_type": "tesla"},
            {},
        )
    )

    assert result is True
    ble_start.assert_awaited_once_with(hass, "bridge_alpha")
    tbt_start.assert_not_awaited()


def test_both_provider_amp_command_uses_paired_ble_and_vehicle_cap(monkeypatch):
    hass = _two_fleet_vehicle_hass()
    entry = _both_provider_entry(
        "bridge_alpha,bridge_beta",
        f"{VIN_A}=bridge_beta,{VIN_B}=bridge_alpha",
    )
    ble_amps = AsyncMock(return_value=True)
    tbt_amps = AsyncMock(return_value=True)
    monkeypatch.setattr(actions, "_is_ble_available", lambda *args: True)
    monkeypatch.setattr(actions, "_set_ev_charging_amps_ble", ble_amps)
    monkeypatch.setattr(actions, "_resolve_teslemetry_bt_prefix", lambda *args: VIN_B)
    monkeypatch.setattr(actions, "_is_teslemetry_bt_available", lambda *args: True)
    monkeypatch.setattr(actions, "_set_ev_charging_amps_teslemetry_bt", tbt_amps)

    result = asyncio.run(
        actions._set_vehicle_amps(
            hass,
            entry,
            VIN_B,
            16,
            {
                "vehicle_vin": VIN_B,
                "charger_type": "tesla",
                "max_charge_amps": 10,
            },
        )
    )

    assert result is True
    assert ble_amps.await_args.args[:3] == (hass, "bridge_alpha", 10)
    tbt_amps.assert_not_awaited()


def _active_tesla_state(vin: str) -> dict:
    return {
        "active": True,
        "current_amps": 16,
        "target_amps": 16,
        "params": {
            "dynamic_mode": "battery_target",
            "charger_type": "tesla",
            "vehicle_vin": vin,
        },
    }


def test_no_vin_stop_resolves_active_vehicle_and_does_not_block_other_vin(monkeypatch):
    """Two VINs configured; car A is the one actively charging. Stopping with
    no vehicle_id/vin must scope the restart hold to car A (the vehicle that
    was actually charging), NOT "_default" — so car B's scheduled/price start
    is not blocked. Pre-fix, the hold was recorded under "_default" and
    manual_stop_hold_reason(VIN_B) returned a blocking reason too."""
    _install_ev_planner_stub(monkeypatch, configured_vehicles=[
        {"vin": VIN_A, "name": "Car A"},
        {"vin": VIN_B, "name": "Car B"},
    ])
    monkeypatch.setattr(actions, "_action_stop_ev_charging", AsyncMock(return_value=True))

    hass = _Hass()
    entry = _Entry()
    actions._dynamic_ev_state.clear()
    actions._dynamic_ev_state["entry-1"] = {VIN_A: _active_tesla_state(VIN_A)}

    result = asyncio.run(
        actions._execute_single_action(hass, entry, "stop_ev_charging", {})
    )

    assert result is True
    # Car B's restart is not suppressed by car A's stop.
    assert ev_ownership.manual_stop_hold_reason(hass, entry, VIN_B) is None
    # Car A (the one actually stopped) does get the hold.
    assert ev_ownership.manual_stop_hold_reason(hass, entry, VIN_A) is not None
    # "_default" itself was never used as a hold key.
    assert "_default" not in hass.data["power_sync"]["entry-1"].get("ev_manual_stop_holds", {})


def test_single_vehicle_install_still_gets_default_hold(monkeypatch):
    """A single-vehicle (or undetectable-fleet) install with no VIN supplied
    must keep falling back to the "_default" hold exactly as before."""
    _install_ev_planner_stub(monkeypatch, configured_vehicles=[{"vin": VIN_A, "name": "Car A"}])
    monkeypatch.setattr(actions, "_action_stop_ev_charging", AsyncMock(return_value=True))

    hass = _Hass()
    entry = _Entry()
    actions._dynamic_ev_state.clear()

    result = asyncio.run(
        actions._execute_single_action(hass, entry, "stop_ev_charging", {})
    )

    assert result is True
    assert ev_ownership.manual_stop_hold_reason(hass, entry, None) is not None
    assert ev_ownership.manual_stop_hold_reason(hass, entry, VIN_A) is not None


def test_ambiguous_multi_vehicle_no_vin_stop_skips_hold_entirely(monkeypatch):
    """Two vehicles configured, but no single active session to attribute the
    stop to (e.g. state already cleared, or two simultaneously "active"
    entries) — the resolver must skip the hold rather than guess "_default"
    and block a vehicle that was never touched."""
    _install_ev_planner_stub(monkeypatch, configured_vehicles=[
        {"vin": VIN_A, "name": "Car A"},
        {"vin": VIN_B, "name": "Car B"},
    ])
    monkeypatch.setattr(actions, "_action_stop_ev_charging", AsyncMock(return_value=True))

    hass = _Hass()
    entry = _Entry()
    actions._dynamic_ev_state.clear()  # nothing actively tracked

    result = asyncio.run(
        actions._execute_single_action(hass, entry, "stop_ev_charging", {})
    )

    assert result is True
    assert ev_ownership.manual_stop_hold_reason(hass, entry, VIN_A) is None
    assert ev_ownership.manual_stop_hold_reason(hass, entry, VIN_B) is None
    assert ev_ownership.manual_stop_hold_reason(hass, entry, None) is None
    assert hass.data["power_sync"]["entry-1"].get("ev_manual_stop_holds", {}) == {}


def test_explicit_vin_stop_is_unaffected_by_resolver(monkeypatch):
    """When a VIN IS supplied, the hold must be recorded under that VIN
    directly — the resolver should be a no-op passthrough."""
    _install_ev_planner_stub(monkeypatch)
    monkeypatch.setattr(actions, "_action_stop_ev_charging", AsyncMock(return_value=True))

    hass = _Hass()
    entry = _Entry()
    actions._dynamic_ev_state.clear()

    result = asyncio.run(
        actions._execute_single_action(
            hass, entry, "stop_ev_charging", {"vehicle_vin": VIN_A}
        )
    )

    assert result is True
    assert ev_ownership.manual_stop_hold_reason(hass, entry, VIN_A) is not None
    assert ev_ownership.manual_stop_hold_reason(hass, entry, VIN_B) is None


def test_automation_stop_uses_ble_vehicle_from_trigger_context(monkeypatch):
    """An EV trigger must stop the BLE vehicle it observed, not an unrelated
    Fleet/Teslemetry vehicle returned first by registry order."""
    stop = AsyncMock(return_value=True)
    monkeypatch.setattr(actions, "_action_stop_ev_charging", stop)

    hass = _Hass()
    entry = _Entry()
    actions._dynamic_ev_state.clear()

    result = asyncio.run(
        actions._execute_single_action(
            hass,
            entry,
            "stop_ev_charging",
            {},
            {"ev_vehicle_id": "ble_tesla_yf88"},
        )
    )

    assert result is True
    assert stop.await_args.args[2]["vehicle_vin"] == "ble_tesla_yf88"
    assert (
        ev_ownership.manual_stop_hold_reason(
            hass,
            entry,
            "ble_tesla_yf88",
        )
        is not None
    )


def test_ocpp_stop_canonicalizes_matching_generic_solar_session(monkeypatch):
    """An OCPP charger exposed through generic entities is one loadpoint.

    The mobile OCPP stop endpoint names it ``ocpp_charger`` while Solar Surplus
    tracks it as ``generic_ev``.  The stop must clear/hold the tracked generic
    controller without suppressing an unrelated vehicle.
    """
    monkeypatch.setattr(actions, "_action_stop_ev_charging", AsyncMock(return_value=True))

    hass = _Hass()
    entry = _Entry()
    actions._dynamic_ev_state.clear()
    actions._dynamic_ev_state["entry-1"] = {
        "generic_ev": {
            "active": True,
            "current_amps": 10,
            "target_amps": 10,
            "cancel_timer": lambda: None,
            "params": {
                "dynamic_mode": "solar_surplus",
                "owner_mode": "solar_surplus",
                "charger_type": "generic",
                "charger_switch_entity": "switch.charger_charge_control",
            },
        },
        VIN_B: _active_tesla_state(VIN_B),
    }

    result = asyncio.run(
        actions._execute_single_action(
            hass,
            entry,
            "stop_ev_charging",
            {
                "charger_type": "ocpp",
                "ocpp_charger_id": "charger",
                "vehicle_id": "ocpp_charger",
            },
        )
    )

    assert result is True
    assert set(actions._dynamic_ev_state["entry-1"]) == {VIN_B}
    holds = hass.data["power_sync"]["entry-1"]["ev_manual_stop_holds"]
    assert set(holds) == {"generic_ev"}
    assert ev_ownership.manual_stop_hold_reason(hass, entry, "generic_ev") is not None
    assert ev_ownership.manual_stop_hold_reason(hass, entry, VIN_B) is None


def test_solar_surplus_start_respects_matching_manual_stop_hold(monkeypatch):
    hass = _Hass()
    entry = _Entry()
    actions._dynamic_ev_state.clear()
    _install_ev_planner_stub(monkeypatch, plugged_in=True)
    ev_ownership.record_manual_stop_hold(
        hass,
        entry,
        "generic_ev",
        reason="Manual stop from mobile",
    )

    result = asyncio.run(
        actions._action_start_ev_charging_dynamic(
            hass,
            entry,
            {
                "dynamic_mode": "solar_surplus",
                "owner_mode": "solar_surplus",
                "vehicle_vin": "generic_ev",
                "charger_type": "generic",
            },
        )
    )

    assert result is False
    assert actions._dynamic_ev_state == {}

    hass.data["power_sync"]["entry-1"]["ev_manual_stop_holds"]["generic_ev"][
        "expires_at"
    ] = "2000-01-01T00:00:00+00:00"
    result = asyncio.run(
        actions._action_start_ev_charging_dynamic(
            hass,
            entry,
            {
                "dynamic_mode": "solar_surplus",
                "owner_mode": "solar_surplus",
                "vehicle_vin": "generic_ev",
                "charger_type": "generic",
            },
        )
    )

    assert result is True
    assert actions._dynamic_ev_state["entry-1"]["generic_ev"]["active"] is True


def test_manual_solar_surplus_start_bypasses_matching_manual_stop_hold(monkeypatch):
    """An explicit manual Solar Surplus start must not honor the restart hold.

    The hold suppresses automated restarts after a user stop.  A dashboard
    action with ``manual_solar_surplus`` is an explicit user request and must
    create the dynamic session even when the loadpoint has a matching hold.
    """
    hass = _Hass()
    entry = _Entry()
    actions._dynamic_ev_state.clear()
    _install_ev_planner_stub(monkeypatch, plugged_in=True)
    ev_ownership.record_manual_stop_hold(
        hass,
        entry,
        "generic_ev",
        reason="Manual stop from mobile",
    )

    result = asyncio.run(
        actions._action_start_ev_charging_dynamic(
            hass,
            entry,
            {
                "dynamic_mode": "solar_surplus",
                "owner_mode": "manual_solar_surplus",
                "vehicle_vin": "generic_ev",
                "charger_type": "generic",
            },
        )
    )

    assert result is True
    assert actions._dynamic_ev_state["entry-1"]["generic_ev"]["active"] is True


# ---------------------------------------------------------------------------
# HD-18: release/clear must evict a "_default"-held lease when it is
# released/cleared under a resolved VIN, mirroring claim_ev_ownership's
# "_default" eviction, so it stops leaking and blocking cross-family starts.
# ---------------------------------------------------------------------------


def test_release_under_resolved_vin_evicts_leaked_default_lease():
    """A lease claimed under "_default" (no VIN known at claim time) must be
    evicted when it is later released under a resolved VIN — otherwise the
    stale "_default" lease keeps being found via the default-candidate
    overlap and blocks a second vehicle (VIN_B) from starting."""
    hass = _Hass()
    entry = _Entry()

    ev_ownership.claim_ev_ownership(hass, entry, None, owner_mode="scheduled", command="start")
    lease_id, lease = ev_ownership.get_ev_ownership(hass, entry, ev_ownership.DEFAULT_VEHICLE_ID)
    assert lease_id == ev_ownership.DEFAULT_VEHICLE_ID
    assert lease is not None

    # Released under a resolved VIN that was never the exact lease key.
    previous = ev_ownership.release_ev_ownership(hass, entry, VIN_A)
    assert previous is not None

    # The "_default" lease must be gone, not merely unreachable under VIN_A.
    assert ev_ownership.DEFAULT_VEHICLE_ID not in ev_ownership.get_ev_ownerships(hass, entry)
    # VIN_B must no longer see the loadpoint as owned via the default overlap.
    _lease_id, lease = ev_ownership.get_ev_ownership(hass, entry, VIN_B)
    assert lease is None


def test_clear_under_resolved_vin_evicts_leaked_default_lease():
    """Same leak class as above, but via clear_ev_ownerships' explicit-list
    branch: clearing a resolved VIN must also evict the "_default" lease it
    resolves to."""
    hass = _Hass()
    entry = _Entry()

    ev_ownership.claim_ev_ownership(hass, entry, None, owner_mode="scheduled", command="start")

    ev_ownership.clear_ev_ownerships(hass, entry, vehicle_ids=[VIN_A])

    assert ev_ownership.DEFAULT_VEHICLE_ID not in ev_ownership.get_ev_ownerships(hass, entry)
    _lease_id, lease = ev_ownership.get_ev_ownership(hass, entry, VIN_B)
    assert lease is None


def test_release_under_default_does_not_evict_unrelated_vin_lease():
    """Releasing/clearing under a falsy/"_default" id must never resolve onto
    an unrelated sibling VIN lease. Pre-fix, ``_default`` eviction resolved
    via the unguarded ``_candidate_vehicle_ids`` overlap, which for a falsy
    id returns every other lease key as a candidate — so releasing "_default"
    when no "_default" lease exists popped VIN_A's real lease instead."""
    hass = _Hass()
    entry = _Entry()

    ev_ownership.claim_ev_ownership(hass, entry, VIN_A, owner_mode="scheduled", command="start")

    ev_ownership.release_ev_ownership(hass, entry, None)

    _lease_id, lease = ev_ownership.get_ev_ownership(hass, entry, VIN_A)
    assert lease is not None


def test_clear_under_default_does_not_evict_unrelated_vin_lease():
    """Same guard as above, via ``clear_ev_ownerships``' explicit-list branch."""
    hass = _Hass()
    entry = _Entry()

    ev_ownership.claim_ev_ownership(hass, entry, VIN_A, owner_mode="scheduled", command="start")

    ev_ownership.clear_ev_ownerships(hass, entry, vehicle_ids=[None])

    _lease_id, lease = ev_ownership.get_ev_ownership(hass, entry, VIN_A)
    assert lease is not None


def test_dynamic_entry_cleanup_cancels_only_target_timers_and_is_command_neutral(
    monkeypatch,
):
    hass = _Hass()
    hass.data["power_sync"]["entry-2"] = {"dynamic_ev_state": {}}
    entry_id = "entry-1"
    other_id = "entry-2"
    cancelled: list[str] = []

    actions._dynamic_ev_state.clear()
    actions._dynamic_ev_update_locks.clear()
    actions._ev_scheduled_stop.clear()
    actions._dynamic_ev_state[entry_id] = {
        VIN_A: {
            "active": True,
            "cancel_timer": lambda: cancelled.append("a-cancel"),
            "quick_stop_timer": lambda: cancelled.append("a-quick"),
        },
    }
    actions._dynamic_ev_state[other_id] = {
        VIN_B: {
            "active": True,
            "cancel_timer": lambda: cancelled.append("b-cancel"),
            "quick_stop_timer": lambda: cancelled.append("b-quick"),
        },
    }
    actions._ev_scheduled_stop[entry_id] = {
        "cancel": lambda: cancelled.append("a-scheduled"),
    }
    actions._ev_scheduled_stop[other_id] = {
        "cancel": lambda: cancelled.append("b-scheduled"),
    }
    command_calls: list[str] = []
    monkeypatch.setattr(
        actions,
        "_action_stop_ev_charging",
        lambda *args, **kwargs: command_calls.append("stop"),
        raising=False,
    )
    monkeypatch.setattr(
        actions,
        "_action_start_ev_charging",
        lambda *args, **kwargs: command_calls.append("start"),
        raising=False,
    )

    actions.cleanup_dynamic_ev_entry(hass, entry_id)

    assert sorted(cancelled) == ["a-cancel", "a-quick", "a-scheduled"]
    assert command_calls == []
    assert entry_id not in actions._dynamic_ev_state
    assert other_id in actions._dynamic_ev_state
    assert entry_id not in actions._ev_scheduled_stop
    assert other_id in actions._ev_scheduled_stop
    assert hass.data["power_sync"][entry_id].get("dynamic_ev_state") is None
    assert other_id in hass.data["power_sync"]

    # Idempotent: a second unload cleanup neither raises nor touches the
    # surviving entry's timers.
    actions.cleanup_dynamic_ev_entry(hass, entry_id)
    assert sorted(cancelled) == ["a-cancel", "a-quick", "a-scheduled"]


def test_dynamic_outside_window_stop_is_scoped_to_current_vehicle(monkeypatch):
    _install_ev_planner_stub(monkeypatch, plugged_in=True)
    stop = AsyncMock(return_value=True)
    monkeypatch.setattr(actions, "_action_stop_ev_charging_dynamic", stop)
    monkeypatch.setattr(actions, "_is_inside_time_window", lambda *args, **kwargs: False)

    actions._dynamic_ev_state.clear()
    actions._dynamic_ev_state["entry-1"] = {
        VIN_A: {
            "active": True,
            "params": {
                "dynamic_mode": "battery_target",
                "charger_type": "tesla",
                "vehicle_vin": VIN_A,
                "stop_outside_window": True,
                "time_window_start": "00:00",
                "time_window_end": "01:00",
            },
        },
        VIN_B: {
            "active": True,
            "params": {"dynamic_mode": "battery_target", "charger_type": "tesla"},
        },
    }

    asyncio.run(actions._dynamic_ev_update(_Hass(), _Entry(), "entry-1", VIN_A))

    stop.assert_awaited_once()
    stop_params = stop.await_args.args[2]
    assert stop_params["vehicle_id"] == VIN_A
    assert stop_params["vehicle_vin"] == VIN_A


def test_dynamic_update_becomes_noop_when_unload_invalidates_in_flight_callback(
    monkeypatch,
):
    hass = _Hass()
    stop = AsyncMock(return_value=True)
    monkeypatch.setattr(actions, "_action_stop_ev_charging_dynamic", stop)
    monkeypatch.setattr(actions, "_is_inside_time_window", lambda *args, **kwargs: False)
    actions._dynamic_ev_state.clear()
    actions._dynamic_ev_state["entry-1"] = {
        VIN_A: {
            "active": True,
            "params": {
                "dynamic_mode": "battery_target",
                "charger_type": "tesla",
                "stop_outside_window": True,
                "time_window_start": "00:00",
                "time_window_end": "01:00",
            },
            "cancel_timer": lambda: None,
        }
    }

    async def invalidate_then_continue(*_args, **_kwargs):
        actions.cleanup_dynamic_ev_entry(hass, "entry-1")
        return False

    monkeypatch.setattr(
        actions,
        "_clear_ble_dynamic_session_if_unplugged",
        invalidate_then_continue,
    )

    asyncio.run(actions._dynamic_ev_update(hass, _Entry(), "entry-1", VIN_A))
    stop.assert_not_awaited()


def test_dynamic_rate_update_becomes_noop_when_unload_invalidates_live_read(
    monkeypatch,
):
    hass = _Hass()
    set_amps = AsyncMock(return_value=True)
    monkeypatch.setattr(actions, "_set_vehicle_amps", set_amps)
    actions._dynamic_ev_state.clear()
    actions._dynamic_ev_state["entry-1"] = {
        VIN_A: {
            "active": True,
            "current_amps": 20,
            "params": {
                "dynamic_mode": "battery_target",
                "charger_type": "tesla",
                "vehicle_vin": VIN_A,
                "no_grid_import": True,
                "max_charge_amps": 32,
                "min_charge_amps": 5,
                "max_inverter_kw": 10,
            },
            "cancel_timer": lambda: None,
        }
    }

    async def invalidate_during_live_read(*_args, **_kwargs):
        actions.cleanup_dynamic_ev_entry(hass, "entry-1")
        return {
            "battery_power": 5_000,
            "grid_power": 2_000,
            "solar_power": 0,
            "battery_soc": 50,
        }

    monkeypatch.setattr(
        actions,
        "_get_tesla_live_status",
        invalidate_during_live_read,
    )

    asyncio.run(actions._dynamic_ev_update(hass, _Entry(), "entry-1", VIN_A))
    set_amps.assert_not_awaited()


def test_window_end_callback_becomes_noop_after_entry_cleanup(monkeypatch):
    hass = _Hass([_State("switch.car_charge", "off")])
    callbacks = []
    cancelled = []
    stop = AsyncMock(return_value=True)

    def capture_timer(_hass, callback, _when):
        callbacks.append(callback)
        return lambda: cancelled.append(True)

    monkeypatch.setattr(actions, "async_track_point_in_time", capture_timer)
    monkeypatch.setattr(
        actions,
        "_get_window_end_datetime",
        lambda *_args, **_kwargs: datetime(2026, 8, 9, 12, 0),
    )
    monkeypatch.setattr(
        actions,
        "_get_tesla_ev_entity",
        AsyncMock(return_value="switch.car_charge"),
    )
    monkeypatch.setattr(actions, "_wake_tesla_ev", AsyncMock(return_value=True))
    monkeypatch.setattr(actions, "_is_api_credit_available", lambda *_args: True)
    monkeypatch.setattr(actions, "_action_stop_ev_charging", stop)
    actions._ev_scheduled_stop.clear()

    assert asyncio.run(
        actions._action_start_ev_charging(
            hass,
            _Entry(),
            {
                "charger_type": "tesla",
                "vehicle_id": VIN_A,
                "vehicle_vin": VIN_A,
                "stop_outside_window": True,
            },
            context={
                "time_window_start": "11:00",
                "time_window_end": "12:00",
                "timezone": "UTC",
            },
        )
    ) is True
    assert len(callbacks) == 1

    actions.cleanup_dynamic_ev_entry(hass, "entry-1")
    assert cancelled == [True]
    asyncio.run(callbacks[0](None))

    stop.assert_not_awaited()


def test_anonymous_tesla_dynamic_start_fails_closed_before_state(monkeypatch):
    ev_planner = types.ModuleType("power_sync.automations.ev_charging_planner")

    async def unresolved(*_args, **_kwargs):
        return None

    ev_planner._resolve_unspecified_tesla_start_vin = unresolved
    monkeypatch.setitem(sys.modules, "power_sync.automations.ev_charging_planner", ev_planner)
    actions._dynamic_ev_state.clear()

    result = asyncio.run(
        actions._action_start_ev_charging_dynamic(
            _Hass(),
            _Entry(),
            {"charger_type": "tesla", "vehicle_id": actions.DEFAULT_VEHICLE_ID},
        )
    )

    assert result is False
    assert actions._dynamic_ev_state == {}


def test_anonymous_tesla_manual_start_fails_closed_before_claim(monkeypatch):
    ev_planner = types.ModuleType("power_sync.automations.ev_charging_planner")

    async def unresolved(*_args, **_kwargs):
        return None

    ev_planner._resolve_unspecified_tesla_start_vin = unresolved
    monkeypatch.setitem(sys.modules, "power_sync.automations.ev_charging_planner", ev_planner)
    hass = _Hass()
    entry = _Entry()
    actions._dynamic_ev_state.clear()

    result = asyncio.run(
        actions._start_manual_ev_charging(
            hass,
            entry,
            {"charger_type": "tesla", "vehicle_id": actions.DEFAULT_VEHICLE_ID},
        )
    )

    assert result is False
    assert ev_ownership.get_ev_ownerships(hass, entry) == {}
    assert actions._dynamic_ev_state == {}
