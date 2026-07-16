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


def test_solar_surplus_start_respects_matching_manual_stop_hold():
    hass = _Hass()
    entry = _Entry()
    actions._dynamic_ev_state.clear()
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
