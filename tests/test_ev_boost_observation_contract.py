"""Finite Boost retirement consumes the actual Fleet/BLE status producer."""
from datetime import datetime, timedelta, timezone
import sys
from types import SimpleNamespace

import pytest

from test_ev_boost_timers import BoostHarness, VIN_A, VIN_B, own
from test_ev_vehicle_status import _Hass, _State, _entity, _power_sync_module


@pytest.fixture(autouse=True)
def preserve_ha_and_product_module_bindings(monkeypatch):
    # The legacy whole-integration fixture replaces HA modules at call time.
    # Record every existing binding with pytest so teardown restores whichever
    # real/stub module the surrounding suite owns, independent of file order.
    for name, module in tuple(sys.modules.items()):
        if name == "homeassistant" or name.startswith("homeassistant.") or name == "power_sync" or name.startswith("power_sync."):
            monkeypatch.setitem(sys.modules, name, module)


def real_status_harness(monkeypatch, *, bridge=False):
    power_sync = _power_sync_module()
    h = BoostHarness()
    h.now = datetime.now(timezone.utc)
    monkeypatch.setattr(power_sync.dt_util, "utcnow", lambda: h.now)
    states = [
        _State("sensor.car_charging_state", "Charging", last_updated=h.now),
        _State("binary_sensor.car_charge_cable", "on", last_updated=h.now),
        _State("sensor.car_charger_power", "1.2", {"unit_of_measurement": "kW"}, last_updated=h.now),
    ]
    entities = {s.entity_id: _entity(s.entity_id, "car-device") for s in states}
    devices = {"car-device": SimpleNamespace(id="car-device", name="Test car",
                                             identifiers={("tesla_fleet", VIN_A)})}
    h.entry.options["ev_provider"] = "fleet_api"
    if bridge:
        h.entry.options.update(tesla_ble_entity_prefix="car_bridge",
                               tesla_ble_vehicle_mapping=f"{VIN_A}=car_bridge")
        states.extend([
            _State("binary_sensor.car_bridge_status", "on", last_updated=h.now),
            _State("binary_sensor.car_bridge_charge_flap", "on", last_updated=h.now),
            _State("sensor.car_bridge_charging_state", "Charging", last_updated=h.now),
            _State("sensor.car_bridge_charge_power", "1.2", {"unit_of_measurement": "kW"}, last_updated=h.now),
        ])
    producer_hass = _Hass(states, entities, devices)
    for attribute in ("states", "entity_registry", "device_registry", "config_entries"):
        setattr(h.hass, attribute, getattr(producer_hass, attribute))
    # Keep the real producer, its physical coalescer and real freshness checks;
    # no injected known-state flag or copied status dictionary is involved.
    h.view._boost_observation.__func__.__globals__["_get_ev_vehicles_status"] = power_sync._get_ev_vehicles_status
    return h, power_sync


def sample(h, entity_id, value, *, at=None, reported_at=None):
    state = h.hass.states.get(entity_id)
    state.state = value
    state.last_updated = at or h.now
    if reported_at is not None:
        state.last_reported = reported_at


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["Stopped", "Complete", "Disconnected"])
async def test_real_fresh_fleet_stopped_evidence_reaches_boost_consumer(monkeypatch, state):
    h, producer = real_status_harness(monkeypatch)
    await h.start(VIN_A)
    attempt = h.runtime["ev_boost_sessions"][VIN_A]["attempted_at"]
    h.now += timedelta(seconds=2)
    sample(h, "sensor.car_charging_state", state)
    sample(h, "sensor.car_charger_power", "0")
    default_rows = producer._get_ev_vehicles_status(h.hass, h.entry)
    assert len(default_rows) == 1 and default_rows[0]["vehicle_id"] == VIN_A
    assert "_charging_state_known" not in default_rows[0]
    assert "_boost_charging_state_evidence" not in default_rows[0]
    assert h.view._boost_observation(VIN_A, after=attempt) == "stopped"


@pytest.mark.asyncio
async def test_real_stopped_producer_retires_exact_owned_journal_without_another_command(monkeypatch):
    h, _ = real_status_harness(monkeypatch)
    await h.start(VIN_A)
    h.now += timedelta(seconds=2)
    sample(h, "sensor.car_charging_state", "Stopped")
    sample(h, "sensor.car_charger_power", "0")
    h.physical[VIN_A] = False
    await h.runtime["ev_boost_timers"][VIN_A]["callback"](h.now)
    assert h.stops() == [] and h.owner(VIN_A) is None
    assert h.runtime["ev_boost_sessions"] == {} and h.runtime["ev_boost_timers"] == {}
    assert h.store.saved["ev_runtime_state"]["boost_sessions"] == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["unknown", "unavailable", "unrecognized", "Starting", "Pending", "Connected", "No Power"])
async def test_real_unknown_state_with_fresh_zero_power_never_proves_stopped(monkeypatch, state):
    h, _ = real_status_harness(monkeypatch)
    await h.start(VIN_A)
    h.now += timedelta(seconds=2)
    sample(h, "sensor.car_charging_state", state)
    sample(h, "sensor.car_charger_power", "0")
    owner = h.owner(VIN_A)
    await h.runtime["ev_boost_timers"][VIN_A]["callback"](h.now)
    assert h.view._boost_observation(VIN_A) is None
    assert h.stops() == [] and h.owner(VIN_A) is owner
    assert VIN_A in h.runtime["ev_boost_sessions"]


@pytest.mark.asyncio
async def test_real_old_stopped_value_is_not_renewed_by_a_recent_ha_report(monkeypatch):
    h, _ = real_status_harness(monkeypatch)
    await h.start(VIN_A)
    h.now += timedelta(seconds=2)
    old = h.now - timedelta(minutes=5)
    sample(h, "sensor.car_charging_state", "Stopped", at=old, reported_at=h.now)
    sample(h, "sensor.car_charger_power", "0", at=old, reported_at=h.now)
    owner = h.owner(VIN_A)
    await h.runtime["ev_boost_timers"][VIN_A]["callback"](h.now)
    assert h.view._boost_observation(VIN_A) is None
    assert h.stops() == [] and h.owner(VIN_A) is owner
    assert VIN_A in h.runtime["ev_boost_sessions"]


@pytest.mark.asyncio
async def test_newer_unknown_ble_cannot_borrow_an_older_known_fleet_stopped_flag(monkeypatch):
    h, _ = real_status_harness(monkeypatch, bridge=True)
    await h.start(VIN_A)
    attempt = h.runtime["ev_boost_sessions"][VIN_A]["attempted_at"]
    h.now += timedelta(seconds=2)
    sample(h, "sensor.car_charging_state", "Stopped", at=h.now - timedelta(seconds=10))
    sample(h, "sensor.car_charger_power", "0", at=h.now - timedelta(seconds=10))
    sample(h, "sensor.car_bridge_charging_state", "unknown")
    sample(h, "sensor.car_bridge_charge_power", "unknown")
    assert h.view._boost_observation(VIN_A, after=attempt) is None
    await h.runtime["ev_boost_timers"][VIN_A]["callback"](h.now)
    assert h.stops() == [] and VIN_A in h.runtime["ev_boost_sessions"]


@pytest.mark.asyncio
async def test_real_charging_observation_cannot_reclaim_or_stop_a_superseding_owner(monkeypatch):
    h, _ = real_status_harness(monkeypatch)
    await h.start(VIN_A)
    new_owner = own.claim_ev_ownership(h.hass, h.entry, VIN_A, owner_mode="manual")
    h.now += timedelta(seconds=2)
    sample(h, "sensor.car_charging_state", "Charging")
    sample(h, "sensor.car_charger_power", "1.2")
    await h.runtime["ev_boost_timers"][VIN_A]["callback"](h.now)
    assert h.stops() == [] and h.owner(VIN_A) is new_owner
    assert h.runtime["ev_boost_sessions"][VIN_A]["superseded"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["Stopped", "unknown"])
async def test_fresh_wall_connector_zero_cannot_renew_old_or_unknown_fleet_state(monkeypatch, state):
    h, producer = real_status_harness(monkeypatch)
    await h.start(VIN_A)
    attempt = h.runtime["ev_boost_sessions"][VIN_A]["attempted_at"]
    h.now += timedelta(seconds=2)
    sample(h, "sensor.car_charging_state", state, at=h.now - timedelta(minutes=5))
    sample(h, "sensor.car_charger_power", "0", at=h.now - timedelta(minutes=5))
    h.runtime["tesla_coordinator"] = SimpleNamespace(data={
        "wall_connectors_raw": [{"vin": VIN_A, "wall_connector_state": 11, "wall_connector_power": 0}],
        "last_update": h.now,
    })
    rows = producer._get_ev_vehicles_status(h.hass, h.entry)
    assert rows[0]["_charging_observed_at"] == h.now
    assert h.view._boost_observation(VIN_A, after=attempt) is None
    await h.runtime["ev_boost_timers"][VIN_A]["callback"](h.now)
    assert h.stops() == [] and VIN_A in h.runtime["ev_boost_sessions"]


@pytest.mark.asyncio
async def test_real_two_vin_recovery_retires_fresh_b_and_retains_stale_a_without_commands(monkeypatch):
    h, _ = real_status_harness(monkeypatch)
    second = [
        _State("sensor.car_b_charging_state", "Charging", last_updated=h.now),
        _State("binary_sensor.car_b_charge_cable", "on", last_updated=h.now),
        _State("sensor.car_b_charger_power", "1.2", {"unit_of_measurement": "kW"}, last_updated=h.now),
    ]
    h.hass.states._states.update({state.entity_id: state for state in second})
    h.hass.entity_registry.entities.update({state.entity_id: _entity(state.entity_id, "car-b-device")
                                           for state in second})
    h.hass.device_registry.devices["car-b-device"] = SimpleNamespace(
        id="car-b-device", name="Second test car", identifiers={("tesla_fleet", VIN_B)})
    await h.start(VIN_A)
    await h.start(VIN_B)
    records = h.runtime["ev_boost_sessions"]
    old_a = dict(records[VIN_A])
    calls_before = list(h.calls)
    # Match restart recovery: durable finite obligations exist, but active
    # process-local controllers and ownership leases do not.
    for vin in (VIN_A, VIN_B):
        own.release_ev_ownership(h.hass, h.entry, vin)
    h.states["entry"].clear()
    h.unload_timers()
    h.now += timedelta(seconds=2)
    sample(h, "sensor.car_charging_state", "Stopped", at=h.now - timedelta(minutes=5))
    sample(h, "sensor.car_charger_power", "0", at=h.now - timedelta(minutes=5))
    sample(h, "sensor.car_b_charging_state", "Stopped")
    sample(h, "sensor.car_b_charger_power", "0")
    for vin in (VIN_A, VIN_B):
        callback = h.view._arm_boost_stop(vin, records[vin], at=h.now)
        await callback(h.now)
    assert h.calls == calls_before and h.owner(VIN_A) is None and h.owner(VIN_B) is None
    assert set(records) == {VIN_A} and set(h.runtime["ev_boost_timers"]) == {VIN_A}
    assert records[VIN_A]["generation"] == old_a["generation"]
    assert records[VIN_A]["expires_at"] == old_a["expires_at"]
    assert records[VIN_A]["status"] == "pending_fresh_observation"
    assert set(h.store.saved["ev_runtime_state"]["boost_sessions"]) == {VIN_A}


@pytest.mark.asyncio
@pytest.mark.parametrize("fleet_state,ble_state", [
    ("Stopped", "unknown"), ("unknown", "Stopped"),
    ("Stopped", "Charging"), ("Charging", "Stopped"),
])
async def test_equal_time_conflicting_provider_evidence_never_retires_boost(
    monkeypatch, fleet_state, ble_state,
):
    h, producer = real_status_harness(monkeypatch, bridge=True)
    await h.start(VIN_A)
    h.now += timedelta(seconds=2)
    sample(h, "sensor.car_charging_state", fleet_state)
    sample(h, "sensor.car_charger_power", "0")
    sample(h, "sensor.car_bridge_charging_state", ble_state)
    sample(h, "sensor.car_bridge_charge_power", "0")
    rows = producer._get_ev_vehicles_status(h.hass, h.entry, include_control_evidence=True)
    assert len(rows) == 1
    assert rows[0]["_boost_charging_state_evidence"] == {
        "known": False, "charging": None, "observed_at": h.now}
    assert h.view._boost_observation(VIN_A) != "stopped"
    await h.runtime["ev_boost_timers"][VIN_A]["callback"](h.now)
    assert h.stops() == [] and VIN_A in h.runtime["ev_boost_sessions"]


@pytest.mark.asyncio
async def test_equal_time_identical_explicit_stopped_providers_can_retire_boost(monkeypatch):
    h, _ = real_status_harness(monkeypatch, bridge=True)
    await h.start(VIN_A)
    h.now += timedelta(seconds=2)
    for prefix in ("car", "car_bridge"):
        sample(h, f"sensor.{prefix}_charging_state", "Stopped")
    sample(h, "sensor.car_charger_power", "0")
    sample(h, "sensor.car_bridge_charge_power", "0")
    assert h.view._boost_observation(VIN_A) == "stopped"
    await h.runtime["ev_boost_timers"][VIN_A]["callback"](h.now)
    assert h.stops() == [] and not h.runtime["ev_boost_sessions"]
