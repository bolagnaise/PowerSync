"""Adapter-level regressions for qualified stock Tesla BLE recovery."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
import importlib
import sys
import time
import types

import pytest


ROOT = Path(__file__).resolve().parents[1] / "custom_components" / "power_sync"
package = sys.modules.setdefault("power_sync", types.ModuleType("power_sync"))
package.__path__ = [str(ROOT)]

from power_sync import tesla_ev_recovery as recovery  # noqa: E402
from power_sync import tesla_ev_stock_recovery as stock  # noqa: E402
from power_sync.tesla_ev_observation import TeslaVehicleObservation  # noqa: E402


VIN_A = "5YJ3E1EA7NF0000A1"
VIN_B = "5YJ3E1EA7NF0000B2"
PROJECT = "PedroKTFC.esphome-tesla-ble"


class _Entity:
    def __init__(self, entity_id, *, device, config="ble-config", unique_id=None):
        self.entity_id = entity_id
        self.platform = "esphome"
        self.config_entry_id = config
        self.device_id = device
        self.unique_id = unique_id or entity_id
        self.disabled_by = None


class _State:
    def __init__(self, entity_id, value, stamp):
        self.entity_id = entity_id
        self.state = str(value)
        self.last_reported = stamp
        self.last_updated = stamp


class _Registry:
    def __init__(self, entities):
        self.entities = entities

    def async_get(self, entity_id):
        return self.entities.get(entity_id)


class _States:
    def __init__(self, states):
        self.states = states

    def get(self, entity_id):
        return self.states.get(entity_id)

    def async_all(self):
        return list(self.states.values())


class _RuntimeData:
    def __init__(self, device):
        self.device_info = types.SimpleNamespace(
            project_name=PROJECT, project_version="2026.8.0",
            esphome_version="2026.6.5",
        )
        self.client = object()
        self.available = True
        self.device = device


class _ConfigEntry:
    def __init__(self, device):
        self.runtime_data = _RuntimeData(device)


class _ConfigEntries:
    def __init__(self, entries):
        self.entries = entries

    def async_get_entry(self, entry_id):
        return self.entries[entry_id]


class _Services:
    def __init__(self):
        self.calls = []

    async def async_call(self, domain, service, data, *, blocking):
        self.calls.append((domain, service, data, blocking))


class _Store:
    def __init__(self):
        self.value = None

    async def async_save(self, value):
        self.value = deepcopy(value)
        return None


class _Entry:
    entry_id = "power-entry"

    def __init__(self):
        self.data = {
            "ev_provider": "both",
            "tesla_ble_entity_prefix": "bridge_a,bridge_b",
            "tesla_ble_vehicle_mapping": f"{VIN_A}=bridge_a,{VIN_B}=bridge_b",
        }
        self.options = {}


class _Hass:
    def __init__(self):
        self.data = {"power_sync": {"power-entry": {}}}
        self.entities = {}
        self.states_by_id = {}
        for prefix, device in (("bridge_a", "device-a"), ("bridge_b", "device-b")):
            for kind, suffix in (
                ("charger", "charger"), ("restart", "restart"),
                ("uptime", "uptime"), ("radio", "ble_connection"),
            ):
                domain = "button" if kind == "restart" else "switch" if kind in {"charger", "radio"} else "sensor"
                entity_id = f"{domain}.{prefix}_{suffix}"
                self.entities[entity_id] = _Entity(
                    entity_id, device=device, unique_id=f"{prefix}_{suffix}_uid"
                )
            status_id = f"binary_sensor.{prefix}_ble_status"
            self.entities[status_id] = _Entity(status_id, device=device,
                                                unique_id=f"{prefix}_status_uid")
            now = datetime.now(timezone.utc)
            self.states_by_id[status_id] = _State(status_id, "on", now)
            self.states_by_id[f"sensor.{prefix}_uptime"] = _State(
                f"sensor.{prefix}_uptime", 100, now
            )
        self.entity_registry = _Registry(self.entities)
        self.states = _States(self.states_by_id)
        self.services = _Services()
        self.config_entries = _ConfigEntries({
            "ble-config": _ConfigEntry("shared-device-config"),
        })
        runtime = self.data["power_sync"]["power-entry"]
        runtime["_tesla_ev_recovery_sidecar"] = recovery._new_sidecar()
        runtime[recovery.RECOVERY_KEY] = deepcopy(
            runtime["_tesla_ev_recovery_sidecar"]["recovery"]
        )
        runtime["_tesla_ev_recovery_sidecar_loaded"] = True
        runtime["_tesla_ev_recovery_sidecar_store"] = _Store()


@pytest.fixture
def harness(monkeypatch):
    hass = _Hass()
    entry = _Entry()
    # Other collected HA-style tests install module-level helper stubs.  The
    # production resolver imports this registry lazily, so an earlier stub can
    # otherwise return a detached registry and make every exact bridge look
    # missing.  Bind the complete lazy import path to this harness.
    ha = types.ModuleType("homeassistant")
    helpers = types.ModuleType("homeassistant.helpers")
    entity_registry = types.ModuleType("homeassistant.helpers.entity_registry")
    entity_registry.async_get = lambda value: value.entity_registry
    helpers.entity_registry = entity_registry
    ha.helpers = helpers
    monkeypatch.setitem(sys.modules, "homeassistant", ha)
    monkeypatch.setitem(sys.modules, "homeassistant.helpers", helpers)
    monkeypatch.setitem(sys.modules, "homeassistant.helpers.entity_registry", entity_registry)
    const = types.ModuleType("homeassistant.const")
    const.__version__ = "2026.10.4"
    monkeypatch.setitem(sys.modules, "homeassistant.const", const)
    metadata = importlib.import_module("importlib.metadata")
    monkeypatch.setattr(metadata, "version", lambda name: "46.6.0")
    return hass, entry


def _bridge(hass, entry, vin):
    bridge = stock.resolve_stock_bridge(hass, entry, vin)
    assert bridge is not None
    return bridge


def _observation(vin, *, domain="teslemetry", stamp=100.0, config="ble-config",
                 device="native-device", producer="native:producer", requested=10):
    return TeslaVehicleObservation(
        vin=vin, domain=domain, config_entry_id=config, device_id=device,
        producer_identity=producer, source_charge_timestamp=stamp,
        charge_state="Charging", charger_actual_current=9.5,
        charger_power=2.1, charge_limit_soc=80, requested_current=requested,
        origin="native_cloud", field_provenance={
            "requested_current": "charge_state", "charge_limit_soc": "charge_state",
            "charge_state": "charge_state", "charger_actual_current": "charge_state",
            "charger_power": "charge_state",
        },
    )


def test_resolve_stock_bridge_requires_exact_current_profile_metadata(harness):
    hass, entry = harness
    bridge = _bridge(hass, entry, VIN_A)
    assert bridge.metadata == (PROJECT, "2026.8.0", "2026.6.5")
    assert bridge.profile["queue_persistence"] == "ram_only"
    assert bridge.profile["host_replay"] == "none"
    assert bridge.profile["source"] == stock.STOCK_PROFILES[bridge.metadata]
    hass.entities["switch.bridge_a_charger"].platform = "mqtt"
    assert stock.resolve_stock_bridge(hass, entry, VIN_A) is None


@pytest.mark.asyncio
async def test_disconnect_with_missing_uptime_keeps_identity_fence_distinct(harness, monkeypatch):
    hass, entry = harness
    bridge = _bridge(hass, entry, VIN_A)
    transaction = stock.StockBridgeTransaction(hass, entry, bridge)
    transaction.reset_at = stock.time.time()
    transaction.deadline = stock.time.monotonic() + 0.01
    hass.states_by_id.pop("sensor.bridge_a_uptime")
    monkeypatch.setattr(stock, "POLL_SECONDS", 0.001)
    assert transaction.current()
    with pytest.raises(TimeoutError, match="observation"):
        await transaction.uptime()


@pytest.mark.asyncio
async def test_pre_reset_uptime_waits_for_fresh_sample_before_continuing(harness, monkeypatch):
    hass, entry = harness
    bridge = _bridge(hass, entry, VIN_A)
    transaction = stock.StockBridgeTransaction(hass, entry, bridge)
    monkeypatch.setattr(stock, "POLL_SECONDS", 0.001)
    uptime_id = "sensor.bridge_a_uptime"
    hass.states_by_id[uptime_id] = _State(
        uptime_id, 0.562, datetime.fromtimestamp(time.time() - 10, timezone.utc)
    )

    pending = asyncio.create_task(transaction.uptime())
    await asyncio.sleep(0)
    fresh_stamp = datetime.now(timezone.utc)
    hass.states_by_id[uptime_id] = _State(uptime_id, 62.815, fresh_stamp)

    result = await asyncio.wait_for(pending, 1)
    assert result["uptime"] == pytest.approx(62.815)
    assert result["observed_at"] == pytest.approx(fresh_stamp.timestamp())
    assert transaction.last_uptime == result


@pytest.mark.asyncio
async def test_uptime_requires_fresh_low_sample_then_advancing_sample(harness):
    hass, entry = harness
    bridge = _bridge(hass, entry, VIN_A)
    transaction = stock.StockBridgeTransaction(hass, entry, bridge)
    transaction.last_uptime = {"uptime": 100, "observed_at": 10}
    transaction.reset_at = 20
    hass.states_by_id["sensor.bridge_a_uptime"] = _State(
        "sensor.bridge_a_uptime", 2, datetime.fromtimestamp(21, timezone.utc)
    )
    assert (await transaction.uptime())["uptime"] == 2
    hass.states_by_id["sensor.bridge_a_uptime"] = _State(
        "sensor.bridge_a_uptime", 3, datetime.fromtimestamp(22, timezone.utc)
    )
    assert (await transaction.uptime())["uptime"] == 3


def test_generation_or_registry_rebind_invalidates_transaction(harness):
    hass, entry = harness
    bridge = _bridge(hass, entry, VIN_A)
    transaction = stock.StockBridgeTransaction(hass, entry, bridge)
    transaction.generation = 1
    hass.data["power_sync"][entry.entry_id]["_tesla_ev_recovery_sidecar"]["generations"] = {VIN_A: 1}
    assert transaction.current()
    hass.data["power_sync"][entry.entry_id]["_tesla_ev_recovery_sidecar"]["generations"][VIN_A] = 2
    assert not transaction.current()
    hass.data["power_sync"][entry.entry_id]["_tesla_ev_recovery_sidecar"]["generations"][VIN_A] = 1
    hass.entities["switch.bridge_a_charger"].device_id = "rebound-device"
    assert not transaction.current()


@pytest.mark.asyncio
async def test_containment_failure_does_not_claim_recovery(harness):
    hass, entry = harness
    bridge = _bridge(hass, entry, VIN_A)
    transaction = stock.StockBridgeTransaction(hass, entry, bridge)

    async def fail(*_args, **_kwargs):
        raise RuntimeError("radio write failed")

    hass.services.async_call = fail
    with pytest.raises(RuntimeError, match="radio write failed"):
        await transaction.contain()
    assert hass.data["power_sync"][entry.entry_id]["_tesla_ev_recovery_sidecar"]["incidents"] == {}


@pytest.mark.asyncio
async def test_two_independent_native_observations_converge_requested_state(harness, monkeypatch):
    hass, entry = harness
    bridges = {vin: _bridge(hass, entry, vin) for vin in (VIN_A, VIN_B)}
    snapshot = hass.data["power_sync"][entry.entry_id]["_tesla_ev_recovery_sidecar"]
    snapshot["incidents"] = {
        f"incident-{vin}": {
            "incident_id": f"incident-{vin}",
            "phase": "held", "current_candidate_vehicles": [vin],
            "current_candidate_producers": [bridges[vin].identity],
        } for vin in (VIN_A, VIN_B)
    }
    observation_module = importlib.import_module("power_sync.tesla_ev_observation")
    reads = {VIN_A: 0, VIN_B: 0}
    bindings = {
        VIN_A: {"vin": VIN_A, "domain": "tesla_fleet", "config_entry_id": "ble-config",
                "device_id": "native-a", "producer_identity": "native:a"},
        VIN_B: {"vin": VIN_B, "domain": "teslemetry", "config_entry_id": "ble-config",
                "device_id": "native-b", "producer_identity": "native:b"},
    }

    async def read(_hass, vin, *, updated_after=None, **_kwargs):
        reads[vin] += 1
        return _observation(
            vin, domain="tesla_fleet" if vin == VIN_A else "teslemetry",
            stamp=time.time() + reads[vin], producer=bindings[vin]["producer_identity"],
            device=bindings[vin]["device_id"],
        )

    # The adapter's callback closes over VIN through the actual recovery call;
    # use one fake per call so both exact VINs are exercised independently.
    async def run(vin):
        async def recover(_hass, _entry, *, observe, reconcile, **_kwargs):
            first = await observe(vin)
            return first is not None and await reconcile({vin: first})
        monkeypatch.setattr(observation_module, "async_read_independent_tesla_vehicle", read)
        monkeypatch.setattr(
            observation_module, "independent_tesla_source_binding",
            lambda _hass, selected_vin: bindings[selected_vin],
        )
        monkeypatch.setattr(recovery, "async_recover_stock", recover)
        monkeypatch.setattr(stock.asyncio, "sleep", _no_sleep)
        return await stock.async_recover_vehicle_transport(hass, entry, vin)

    assert await run(VIN_A)
    assert await run(VIN_B)
    assert reads[VIN_A] >= 2 and reads[VIN_B] >= 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider", "paid_expected"),
    [("both", True), ("tesla_ble", False)],
)
async def test_recovery_reader_paid_refresh_follows_provider_policy(
    harness, monkeypatch, provider, paid_expected,
):
    hass, entry = harness
    entry.data["ev_provider"] = provider
    if provider == "tesla_ble":
        entry.data["tesla_ble_entity_prefix"] = "bridge_a"
        entry.data["tesla_ble_vehicle_mapping"] = f"{VIN_A}=bridge_a"
    bridge = _bridge(hass, entry, VIN_A)
    snapshot = hass.data["power_sync"][entry.entry_id]["_tesla_ev_recovery_sidecar"]
    snapshot["incidents"] = {"incident": {
        "incident_id": "incident", "phase": "held",
        "current_candidate_vehicles": [VIN_A],
        "current_candidate_producers": [bridge.identity],
    }}
    observation_module = importlib.import_module("power_sync.tesla_ev_observation")
    actions_module = types.ModuleType("power_sync.automations.actions")
    binding = {"vin": VIN_A, "domain": "teslemetry",
               "config_entry_id": "native-entry", "device_id": "native-device",
               "producer_identity": "native:producer"}
    read_requests = []

    async def read(*_args, **kwargs):
        read_requests.append(kwargs)
        assert kwargs["authorize"]() is True
        return None

    async def ble_wake(*_args, **_kwargs):
        return False

    async def cloud_wake(*_args, **_kwargs):
        return False

    actions_module._wake_tesla_ble = ble_wake
    monkeypatch.setitem(sys.modules, "power_sync.automations.actions", actions_module)
    monkeypatch.setattr(observation_module, "async_read_independent_tesla_vehicle", read)
    monkeypatch.setattr(observation_module, "async_wake_independent_tesla_vehicle", cloud_wake)
    monkeypatch.setattr(observation_module, "independent_tesla_source_binding",
                        lambda _hass, _vin: binding)

    async def fake_recover(_hass, _entry, *, observe, **_kwargs):
        return await observe(VIN_A) is not None

    monkeypatch.setattr(recovery, "async_recover_stock", fake_recover)
    assert not await stock.async_recover_vehicle_transport(hass, entry, VIN_A)
    assert read_requests
    assert all(request["allow_paid_refresh"] is paid_expected
               for request in read_requests)


@pytest.mark.asyncio
async def test_recovery_reader_paid_authorize_rejects_policy_revocation(
    harness, monkeypatch,
):
    hass, entry = harness
    entry.data["ev_provider"] = "both"
    bridge = _bridge(hass, entry, VIN_A)
    snapshot = hass.data["power_sync"][entry.entry_id]["_tesla_ev_recovery_sidecar"]
    snapshot["incidents"] = {"incident": {
        "incident_id": "incident", "phase": "held",
        "current_candidate_vehicles": [VIN_A],
        "current_candidate_producers": [bridge.identity],
    }}
    observation_module = importlib.import_module("power_sync.tesla_ev_observation")
    actions_module = types.ModuleType("power_sync.automations.actions")
    binding = {"vin": VIN_A, "domain": "teslemetry",
               "config_entry_id": "native-entry", "device_id": "native-device",
               "producer_identity": "native:producer"}
    seen = []

    async def read(*_args, **kwargs):
        seen.append(kwargs)
        entry.data["ev_provider"] = "tesla_ble"
        assert kwargs["allow_paid_refresh"] is True
        assert kwargs["authorize"]() is False
        return None

    async def ble_wake(*_args, **_kwargs):
        return False

    async def cloud_wake(*_args, **kwargs):
        assert kwargs["authorize"]() is False
        return False

    actions_module._wake_tesla_ble = ble_wake
    monkeypatch.setitem(sys.modules, "power_sync.automations.actions", actions_module)
    monkeypatch.setattr(observation_module, "async_read_independent_tesla_vehicle", read)
    monkeypatch.setattr(observation_module, "async_wake_independent_tesla_vehicle", cloud_wake)
    monkeypatch.setattr(observation_module, "independent_tesla_source_binding",
                        lambda _hass, _vin: binding)

    async def fake_recover(_hass, _entry, *, observe, **_kwargs):
        return await observe(VIN_A) is not None

    monkeypatch.setattr(recovery, "async_recover_stock", fake_recover)
    assert not await stock.async_recover_vehicle_transport(hass, entry, VIN_A)
    assert seen


@pytest.mark.asyncio
async def test_recovery_uses_ble_wake_before_one_exact_native_cloud_wake(
    harness, monkeypatch,
):
    hass, entry = harness
    bridge = _bridge(hass, entry, VIN_A)
    snapshot = hass.data["power_sync"][entry.entry_id]["_tesla_ev_recovery_sidecar"]
    snapshot["incidents"] = {"incident": {
        "incident_id": "incident", "phase": "held",
        "current_candidate_vehicles": [VIN_A],
        "current_candidate_producers": [bridge.identity],
    }}
    observation_module = importlib.import_module("power_sync.tesla_ev_observation")
    actions_module = types.ModuleType("power_sync.automations.actions")
    calls = []
    binding = {"vin": VIN_A, "domain": "teslemetry",
               "config_entry_id": "native-entry", "device_id": "native-device",
               "producer_identity": "native:producer"}
    reads = iter((None, None, _observation(
        VIN_A, stamp=time.time(), config="native-entry", device="native-device",
    )))

    async def read(*_args, **_kwargs):
        calls.append("read")
        return next(reads)

    async def ble_wake(*_args, **kwargs):
        calls.append("ble")
        assert kwargs["wait_timeout"] == 120
        assert kwargs["authorize"]()
        return False

    async def cloud_wake(*_args, **kwargs):
        calls.append("cloud")
        assert kwargs["expected_binding"] == binding
        assert kwargs["timeout"] == 60
        assert kwargs["authorize"]()
        return True

    actions_module._wake_tesla_ble = ble_wake
    monkeypatch.setitem(sys.modules, "power_sync.automations.actions", actions_module)
    monkeypatch.setattr(observation_module, "async_read_independent_tesla_vehicle", read)
    monkeypatch.setattr(observation_module, "async_wake_independent_tesla_vehicle", cloud_wake)
    monkeypatch.setattr(observation_module, "independent_tesla_source_binding",
                        lambda _hass, _vin: binding)

    async def fake_recover(_hass, _entry, *, observe, **_kwargs):
        return await observe(VIN_A) is not None

    monkeypatch.setattr(recovery, "async_recover_stock", fake_recover)
    assert await stock.async_recover_vehicle_transport(hass, entry, VIN_A)
    assert calls == ["read", "ble", "read", "cloud", "read"]


@pytest.mark.asyncio
async def test_native_cloud_wake_rechecks_paid_fallback_policy_after_wait(
    harness, monkeypatch,
):
    hass, entry = harness
    bridge = _bridge(hass, entry, VIN_A)
    snapshot = hass.data["power_sync"][entry.entry_id]["_tesla_ev_recovery_sidecar"]
    snapshot["incidents"] = {"incident": {
        "incident_id": "incident", "phase": "held",
        "current_candidate_vehicles": [VIN_A],
        "current_candidate_producers": [bridge.identity],
    }}
    observation_module = importlib.import_module("power_sync.tesla_ev_observation")
    actions_module = types.ModuleType("power_sync.automations.actions")
    calls = []
    binding = {"vin": VIN_A, "domain": "teslemetry",
               "config_entry_id": "native-entry", "device_id": "native-device",
               "producer_identity": "native:producer"}

    async def read(*_args, **_kwargs):
        calls.append("read")
        return None

    async def ble_wake(*_args, **_kwargs):
        calls.append("ble")
        return False

    async def cloud_wake(*_args, **kwargs):
        calls.append("cloud")
        entry.data["ev_provider"] = "tesla_ble"
        assert kwargs["authorize"]() is False
        return False

    actions_module._wake_tesla_ble = ble_wake
    monkeypatch.setitem(sys.modules, "power_sync.automations.actions", actions_module)
    monkeypatch.setattr(observation_module, "async_read_independent_tesla_vehicle", read)
    monkeypatch.setattr(observation_module, "async_wake_independent_tesla_vehicle", cloud_wake)
    monkeypatch.setattr(observation_module, "independent_tesla_source_binding",
                        lambda _hass, _vin: binding)

    async def fake_recover(_hass, _entry, *, observe, **_kwargs):
        return await observe(VIN_A) is not None

    monkeypatch.setattr(recovery, "async_recover_stock", fake_recover)
    assert not await stock.async_recover_vehicle_transport(hass, entry, VIN_A)
    assert calls == ["read", "ble", "read", "cloud"]
    assert snapshot["incidents"]["incident"]["phase"] == "held"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider", "cloud_wake_expected"),
    [("both", True), ("tesla_ble", False)],
)
async def test_failed_or_disallowed_native_cloud_wake_keeps_recovery_incident_held(
    harness, monkeypatch, provider, cloud_wake_expected,
):
    hass, entry = harness
    entry.data["ev_provider"] = provider
    if provider == "tesla_ble":
        entry.data["tesla_ble_entity_prefix"] = "bridge_a"
        entry.data["tesla_ble_vehicle_mapping"] = f"{VIN_A}=bridge_a"
    bridge = _bridge(hass, entry, VIN_A)
    snapshot = hass.data["power_sync"][entry.entry_id]["_tesla_ev_recovery_sidecar"]
    snapshot["incidents"] = {"incident": {
        "incident_id": "incident", "phase": "held",
        "current_candidate_vehicles": [VIN_A],
        "current_candidate_producers": [bridge.identity],
    }}
    observation_module = importlib.import_module("power_sync.tesla_ev_observation")
    actions_module = types.ModuleType("power_sync.automations.actions")
    cloud_wake_calls = []
    binding = {"vin": VIN_A, "domain": "teslemetry",
               "config_entry_id": "native-entry", "device_id": "native-device",
               "producer_identity": "native:producer"}

    async def read(*_args, **_kwargs):
        return None

    async def ble_wake(*_args, **_kwargs):
        return False

    async def cloud_wake(*_args, **_kwargs):
        cloud_wake_calls.append(True)
        return False

    actions_module._wake_tesla_ble = ble_wake
    monkeypatch.setitem(sys.modules, "power_sync.automations.actions", actions_module)
    monkeypatch.setattr(observation_module, "async_read_independent_tesla_vehicle", read)
    monkeypatch.setattr(observation_module, "async_wake_independent_tesla_vehicle", cloud_wake)
    monkeypatch.setattr(observation_module, "independent_tesla_source_binding",
                        lambda _hass, _vin: binding)

    async def fake_recover(_hass, _entry, *, observe, **_kwargs):
        return await observe(VIN_A) is not None

    monkeypatch.setattr(recovery, "async_recover_stock", fake_recover)
    assert not await stock.async_recover_vehicle_transport(hass, entry, VIN_A)
    assert snapshot["incidents"]["incident"]["phase"] == "held"
    assert bool(cloud_wake_calls) is cloud_wake_expected


async def _no_sleep(_delay):
    return None


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["vin", "domain", "config_entry_id", "device_id"])
async def test_native_source_identity_mismatch_refuses_reconciliation(harness, monkeypatch, field):
    hass, entry = harness
    bridge = _bridge(hass, entry, VIN_A)
    snapshot = hass.data["power_sync"][entry.entry_id]["_tesla_ev_recovery_sidecar"]
    snapshot["incidents"] = {"incident": {
        "incident_id": "incident",
        "phase": "held", "current_candidate_vehicles": [VIN_A],
        "current_candidate_producers": [bridge.identity],
    }}
    observation_module = importlib.import_module("power_sync.tesla_ev_observation")
    source_binding = {"vin": VIN_A, "domain": "teslemetry", "config_entry_id": "ble-config",
                      "device_id": "native-device", "producer_identity": "native:producer"}
    monkeypatch.setattr(
        observation_module, "independent_tesla_source_binding",
        lambda _hass, _vin: source_binding,
    )
    value = _observation(VIN_A, config="ble-config", device="native-device",
                         producer="native:producer")
    if field == "vin":
        value = _observation(VIN_B, config="ble-config", device="native-device",
                              producer="native:producer")
    elif field == "domain":
        value = _observation(VIN_A, domain="tesla_cloud", config="ble-config",
                              device="native-device", producer="native:producer")
    elif field == "config_entry_id":
        value = _observation(VIN_A, config="other-config", device="native-device",
                              producer="native:producer")
    else:
        value = _observation(VIN_A, config="ble-config", device="other-device",
                              producer="native:producer")

    async def read(*_args, **_kwargs):
        return value

    async def fake_recover(_hass, _entry, *, observe, **_kwargs):
        return await observe(VIN_A) is not None

    monkeypatch.setattr(observation_module, "async_read_independent_tesla_vehicle", read)
    monkeypatch.setattr(recovery, "async_recover_stock", fake_recover)
    assert not await stock.async_recover_vehicle_transport(hass, entry, VIN_A)


@pytest.mark.asyncio
async def test_known_command_uncertainty_selects_real_refs_and_uses_automatic_scope(harness, monkeypatch):
    hass, entry = harness
    bridge = _bridge(hass, entry, VIN_A)
    state = hass.data["power_sync"][entry.entry_id]["_tesla_ev_recovery_sidecar"]
    state["recovery"]["vehicles"] = {VIN_A: {
        "route": None,
        "attempts": {"attempt-1": {
            "outcome": "sent_unknown", "route": {
                "kind": "ble", "prefix": bridge.prefix,
                "entity_identity": bridge.identities["charger"],
            },
        }},
        "hazards": {},
    }}
    hass.data["power_sync"][entry.entry_id][recovery.RECOVERY_KEY] = deepcopy(
        state["recovery"]
    )
    calls = {}
    original_recover = recovery.async_recover_stock

    async def fake_recover(*args, **kwargs):
        calls["recover"] = kwargs
        return True

    observation_module = importlib.import_module("power_sync.tesla_ev_observation")
    monkeypatch.setattr(
        observation_module, "independent_tesla_source_binding",
        lambda _hass, _vin: {"vin": VIN_A, "domain": "teslemetry",
                             "config_entry_id": "native-config", "device_id": "native-device",
                             "producer_identity": "native:producer"},
    )
    monkeypatch.setattr(recovery, "async_recover_stock", fake_recover)
    assert await stock.async_recover_vehicle_transport(hass, entry, VIN_A)
    incident = next(iter(state["incidents"].values()))
    assert incident["kind"] == "command_uncertainty"
    assert incident["covered_attempts"] == [{"vehicle_id": VIN_A, "attempt_id": "attempt-1", "outcome": "sent_unknown", "operation": None, "route": {
        "kind": "ble", "prefix": bridge.prefix, "entity_identity": bridge.identities["charger"]
    }}]
    assert incident["scope_basis"] == "known_journal_and_registry"
    assert incident["operator_scope_confirmation"]["confirmed_by"] == "recorded_binding"
    assert calls["recover"]["incident_id"] == incident["incident_id"]
    assert original_recover is not recovery.async_recover_stock


def test_rate_entity_unique_id_can_differ_from_charger_on_same_physical_node(harness):
    hass, entry = harness
    bridge = _bridge(hass, entry, VIN_A)
    rate_identity = {
        "platform": bridge.identities["charger"]["platform"],
        "config_entry_id": bridge.identities["charger"]["config_entry_id"],
        "device_id": bridge.identities["charger"]["device_id"],
        "unique_id": "number.bridge_a_charge_current_uid",
    }
    ref = {"vehicle_id": VIN_A, "route": {
        "kind": "ble", "prefix": bridge.prefix, "entity_identity": rate_identity,
    }}
    assert recovery._liability_matches_stock_scope(ref, bridge.identity)


@pytest.mark.asyncio
async def test_scope_confirmation_cannot_drop_recorded_candidate_producers_or_vins(harness):
    hass, entry = harness
    bridges = [_bridge(hass, entry, VIN_A), _bridge(hass, entry, VIN_B)]
    incident = await recovery.async_record_evidence_loss(
        hass, entry, candidate_producers=[bridge.identity for bridge in bridges],
        candidate_vehicles=[VIN_A, VIN_B], provenance=["operator_inventory"],
    )
    assert incident is not None
    state = recovery.recovery_sidecar_snapshot(hass, entry)
    assert not await recovery.async_confirm_recovery_scope(
        hass, entry, incident["incident_id"], expected_revision=state["revision"],
        candidate_producers=[bridges[0].identity], candidate_vehicles=[VIN_A],
        operator_confirmation=True,
    )
    selected = recovery.recovery_sidecar_snapshot(hass, entry)["incidents"][incident["incident_id"]]
    assert selected["scope_closed"] is False
    assert selected["current_candidate_vehicles"] == [VIN_A, VIN_B]


def test_stale_observation_cannot_be_accepted_by_fresh_flag():
    now = time.time()
    observation = {
        "requested_current": 10, "charge_limit": 80, "charging": True, "power": 2,
        "fresh": True, "observed_at": now - 60,
        "provenance": {"requested_current": "native", "charge_limit": "native",
                        "charging": "vehicle", "power": "vehicle"},
    }
    assert not recovery._valid_reconciliation_observation(observation, observed_after=now - 1)


@pytest.mark.asyncio
async def test_final_release_save_failure_keeps_unknown_hazard_and_positive_hold(harness, monkeypatch):
    hass, entry = harness
    bridge = _bridge(hass, entry, VIN_A)
    runtime = hass.data["power_sync"][entry.entry_id]
    state = runtime["_tesla_ev_recovery_sidecar"]
    route = {
        "kind": "ble", "prefix": bridge.prefix,
        "entity_id": bridge.identities["charger"]["entity_id"],
        "entity_identity": bridge.identities["charger"],
        "producer_identity": bridge.producer,
    }
    state["recovery"]["vehicles"] = {VIN_A: {
        "route": route,
        "attempts": {"attempt-1": {
            "attempt_id": "attempt-1", "vehicle_id": VIN_A,
            "operation": "rate", "entity_id": route["entity_id"],
            "route": route, "outcome": "sent_unknown",
        }},
        "hazards": {},
    }}
    runtime[recovery.RECOVERY_KEY] = deepcopy(state["recovery"])
    incident = await recovery.async_record_evidence_loss(
        hass, entry, kind="command_uncertainty",
        covered_attempts=[{"vehicle_id": VIN_A, "attempt_id": "attempt-1"}],
        candidate_producers=[bridge.identity], candidate_vehicles=[VIN_A],
    )
    assert incident is not None
    snapshot = recovery.recovery_sidecar_snapshot(hass, entry)
    assert await recovery.async_confirm_recovery_scope(
        hass, entry, incident["incident_id"], expected_revision=snapshot["revision"],
        automatic=True,
    )
    original_save = recovery._save_sidecar
    failed_release_save = []

    async def fail_released_save(_hass, _entry, _runtime, candidate):
        if candidate["incidents"][incident["incident_id"]]["phase"] == "released":
            failed_release_save.append(True)
            return False
        return await original_save(_hass, _entry, _runtime, candidate)

    monkeypatch.setattr(recovery, "_save_sidecar", fail_released_save)
    uptimes = iter((100.0, 1.0, 2.0))
    identity = bridge.identity

    async def read_uptime():
        value = next(uptimes)
        return {"uptime": value, "observed_at": time.time() + value, "fresh": True}

    async def observe(_vin):
        return {
            "requested_current": 10, "charge_limit": 80, "charging": True,
            "power": 2.0, "fresh": True, "observed_at": time.time() + 5,
            "provenance": {"requested_current": "native", "charge_limit": "native",
                           "charging": "vehicle", "power": "vehicle"},
        }

    assert not await recovery.async_recover_stock(
        hass, entry, incident_id=incident["incident_id"],
        profile={"queue_persistence": "ram_only", "host_replay": "none", "exercised": True},
        identity=identity, restart=lambda: True,
        wait_disconnected=lambda: True, wait_connected=lambda: True,
        read_identity=lambda: identity, read_uptime=read_uptime, observe=observe,
    )
    assert failed_release_save
    current = runtime[recovery.RECOVERY_KEY]["vehicles"][VIN_A]["attempts"]["attempt-1"]
    assert current["outcome"] == "sent_unknown"
    assert "recovery_retired" not in current
    released = runtime["_tesla_ev_recovery_sidecar"]["incidents"][incident["incident_id"]]
    assert released["phase"] != "released"
    assert not recovery.recovery_control_allowed(
        hass, entry, VIN_A, recovery.current_vehicle_generation(hass, entry, VIN_A), "rate"
    )


@pytest.mark.asyncio
async def test_evidence_loss_two_incidents_preserves_inventory_and_cleanup_obligation(harness):
    hass, entry = harness
    runtime = hass.data["power_sync"][entry.entry_id]
    runtime["_tesla_ev_recovery_sidecar"]["obligations"] = {
        "cleanup": {"kind": "ev_quick_control", "expires_at": 123.0}
    }
    bridges = [_bridge(hass, entry, VIN_A), _bridge(hass, entry, VIN_B)]
    ids = await stock.async_register_evidence_loss(
        hass, entry, vins=[VIN_A, VIN_B], operator_confirmation=True,
        provenance=["operator_inventory"], surviving_facts=[{"charging": True}],
    )
    assert len(ids) == 2
    incidents = runtime["_tesla_ev_recovery_sidecar"]["incidents"]
    for incident_id in ids:
        incident = incidents[incident_id]
        assert incident["phase"] == "held"
        assert "historical_attempt_id" in incident["missing_fields"]
        assert incident["scope_basis"]["historical_identity_reconstructed"] is False
        assert incident["operator_scope_confirmation"]["confirmed"] is True
        assert incident["operator_scope_confirmation"]["automatic"] is False
    assert runtime["_tesla_ev_recovery_sidecar"]["obligations"]["cleanup"]["expires_at"] == 123.0
    assert {bridge.producer for bridge in bridges} == {
        item["producers"][0]["producer_identity"]
        for item in (incidents[ids[0]]["operator_scope_confirmation"],
                     incidents[ids[1]]["operator_scope_confirmation"])
    }
