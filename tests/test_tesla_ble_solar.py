"""BLE-provider native Charge on Solar: bridge protocol, discovery and ownership."""
import importlib.util
import json
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).parents[1] / "custom_components/power_sync"
PKG = "ps_ble_solar_probe"
VIN = "5YJ3E1EA7KF000001"
OTHER_VIN = "5YJ3E1EA7KF000002"
NONCE = "a" * 32


def _load(name):
    spec = importlib.util.spec_from_file_location(f"{PKG}.{name}", ROOT / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def mods(monkeypatch):
    package = types.ModuleType(PKG)
    package.__path__ = []
    monkeypatch.setitem(sys.modules, PKG, package)
    mapping = types.ModuleType(f"{PKG}.tesla_ble_mapping")
    mapping.resolve_ble_prefixes = lambda hass, config: list(config.get("prefixes", []))
    monkeypatch.setitem(sys.modules, mapping.__name__, mapping)
    recovery = types.ModuleType(f"{PKG}.tesla_ev_recovery")
    recovery.identities = {}
    recovery._identity = lambda hass, entity_id: recovery.identities.get(entity_id)
    monkeypatch.setitem(sys.modules, recovery.__name__, recovery)
    solar = _load("teslemetry_solar")
    ble = _load("tesla_ble_solar")
    yield SimpleNamespace(solar=solar, ble=ble, recovery=recovery)
    for name in ("teslemetry_solar", "tesla_ble_solar"):
        sys.modules.pop(f"{PKG}.{name}", None)


def record(**fields):
    return json.dumps({"v": 1, "i": VIN, "n": "", "m": "i", **fields}, separators=(",", ":"))


class Hass:
    """States plus a fake bridge that answers the request text entity."""

    def __init__(self, recovery, prefixes=("tesla_flinn",), vin=VIN):
        self.states_map, self.sent, self.answer = {}, [], None
        self.states = SimpleNamespace(get=self.states_map.get)
        self.services = SimpleNamespace(async_call=self.async_call)
        for index, prefix in enumerate(prefixes):
            self.add_bridge(recovery, prefix, vin, device=f"device-{index}")

    def add_bridge(self, recovery, prefix, vin, device):
        request = f"text.{prefix}_powersync_charge_on_solar_request"
        state = f"sensor.{prefix}_powersync_charge_on_solar"
        self.states_map[request] = SimpleNamespace(state="")
        self.states_map[state] = SimpleNamespace(state=record(i=vin))
        for entity_id in (request, state):
            recovery.identities[entity_id] = {"platform": "esphome", "device_id": device,
                                              "config_entry_id": f"entry-{device}"}

    async def async_call(self, domain, service, data, blocking=False):
        assert (domain, service, blocking) == ("text", "set_value", True)
        self.sent.append(data)
        if self.answer is not None:
            nonce = data["value"].split(":")[1]
            prefix = data["entity_id"][len("text."):-len("_powersync_charge_on_solar_request")]
            self.states_map[f"sensor.{prefix}_powersync_charge_on_solar"] = SimpleNamespace(
                state=self.answer(nonce, data["value"]))


def entry(**options):
    return SimpleNamespace(entry_id="one", data={}, options={"ev_provider": "tesla_ble",
                                                             "prefixes": ["tesla_flinn"], **options})


@pytest.mark.parametrize("value", [
    record(),
    record(m="k", n=NONCE, w=0, e=1, lo=20, up=80),
    record(m="f", n=NONCE, w=1, err="write_rejected:not_supported"),
    record(m="u", n=NONCE, w=1, a=1, err="timeout"),
])
def test_decode_state_accepts_firmware_records(mods, value):
    assert mods.ble.decode_state(value)["i"] == VIN


@pytest.mark.parametrize("value", [
    None, "", "{", record(v=True), record(v=2), record(i="SHORT"), record(m="x"),
    record(m="k", n=NONCE, w=0, e=1, lo=20),                  # missing upper limit
    record(m="k", n=NONCE, w=0, e=True, lo=20, up=80),        # bool is not the int flag
    record(m="k", n="", w=0, e=1, lo=20, up=80),              # result without a nonce
    record(m="k", n=NONCE, e=1, lo=20, up=80),                # missing request kind
    record(m="f", n=NONCE, w=1, err='"quoted"'),
    record(m="f", n=NONCE, w=1),
    record(n="A" * 32),
    '{"v":1,"v":1,"i":"%s","n":"","m":"i"}' % VIN,             # duplicate key
    record(err="x" * 300),                                    # over HA's 255-byte state limit
])
def test_decode_state_rejects_invalid_records(mods, value):
    with pytest.raises(mods.solar.SolarError):
        mods.ble.decode_state(value)


def test_find_bridge_requires_one_bound_bridge_reporting_the_vin(mods):
    hass = Hass(mods.recovery)
    bridge = mods.ble.find_bridge(hass, entry(), VIN.lower())
    assert bridge is not None and bridge.prefix == "tesla_flinn" and bridge.alias == "BLE_TESLA_FLINN"
    assert mods.ble.find_bridge(hass, entry(), OTHER_VIN) is None            # bridge reports another car
    assert mods.ble.find_bridge(hass, entry(), None) is None
    assert mods.ble.find_bridge(hass, entry(ev_provider="teslemetry"), VIN) is None
    assert mods.ble.find_bridge(hass, entry(prefixes=[]), VIN) is None
    # Entities split across two ESPHome devices are not one bridge.
    mods.recovery.identities["sensor.tesla_flinn_powersync_charge_on_solar"]["device_id"] = "elsewhere"
    assert mods.ble.find_bridge(hass, entry(), VIN) is None
    mods.recovery.identities["sensor.tesla_flinn_powersync_charge_on_solar"]["device_id"] = "device-0"
    # Two bridges claiming one VIN are ambiguous; never guess.
    hass.add_bridge(mods.recovery, "tesla_spare", VIN, device="device-9")
    assert mods.ble.find_bridge(hass, entry(prefixes=["tesla_flinn", "tesla_spare"]), VIN) is None
    assert mods.ble.find_bridge(hass, entry(ev_provider="both", prefixes=["tesla_flinn"]), VIN) is not None


@pytest.mark.asyncio
async def test_read_and_write_use_one_nonce_correlated_transaction(mods):
    hass = Hass(mods.recovery)
    bridge = mods.ble.find_bridge(hass, entry(), VIN)
    hass.answer = lambda nonce, text: record(m="k", n=nonce, w=0, e=1, lo=20, up=80)
    assert await bridge.request() == {"enabled": True, "lowerChargeLimit": 20, "upperChargeLimit": 80}
    assert hass.sent[-1]["entity_id"] == "text.tesla_flinn_powersync_charge_on_solar_request"
    get_text = hass.sent[-1]["value"]
    assert get_text.startswith("get:") and len(get_text) == 36

    hass.answer = lambda nonce, text: record(m="k", n=nonce, w=1, a=1, e=0, lo=0, up=100)
    result = await bridge.request({"enabled": False, "lowerChargeLimit": 0, "upperChargeLimit": 100})
    assert result == {"enabled": False, "lowerChargeLimit": 0, "upperChargeLimit": 100}
    set_text = hass.sent[-1]["value"]
    assert set_text.startswith("set:") and set_text.endswith(":0:0:100")
    assert set_text.split(":")[1] != get_text.split(":")[1]   # fresh nonce per request
    assert len(hass.sent) == 2                                # a write's readback needs no extra read


@pytest.mark.asyncio
@pytest.mark.parametrize("answer, match", [
    (lambda n, t: record(m="f", n=n, w=1, err="write_rejected:busy"), r"failed \(write_rejected:busy\)"),
    (lambda n, t: record(m="u", n=n, w=1, a=1, err="timeout"), "could not be confirmed"),
    (lambda n, t: record(m="k", n=n, w=1, e=1, lo=20, up=80, i=OTHER_VIN), "different vehicle"),
    (lambda n, t: record(m="k", n=n, w=0, e=1, lo=20, up=80), "different vehicle or request"),
    (lambda n, t: record(m="k", n=n, w=1, e=1, lo=90, up=80), "lower charge limit"),
])
async def test_failed_or_mismatched_answers_are_errors(mods, answer, match):
    hass = Hass(mods.recovery)
    bridge = mods.ble.find_bridge(hass, entry(), VIN)
    hass.answer = answer
    with pytest.raises(mods.solar.SolarError, match=match):
        await bridge.request({"enabled": True, "lowerChargeLimit": 20, "upperChargeLimit": 80})


@pytest.mark.asyncio
async def test_a_stale_answer_is_never_accepted(mods, monkeypatch):
    monkeypatch.setattr(mods.ble, "TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(mods.ble, "POLL_SECONDS", 0.01)
    hass = Hass(mods.recovery)
    bridge = mods.ble.find_bridge(hass, entry(), VIN)
    # The bridge still shows a previous request's confirmed result.
    hass.answer = lambda nonce, text: record(m="k", n="b" * 32, w=0, e=1, lo=20, up=80)
    with pytest.raises(mods.solar.SolarError, match="could not be confirmed"):
        await bridge.request()


@pytest.mark.asyncio
async def test_ble_hold_fences_the_standalone_ble_vehicle_id(mods):
    class Store:
        value = None
        async def async_load(self): return self.value
        async def async_save(self, value): self.value = value

    gate = mods.solar.SolarOwnership(Store()); await gate.load()
    await gate.hold(VIN, {"status": "unconfirmed", "aliases": ["BLE_TESLA_FLINN"]})
    assert gate.blocked(VIN) and gate.blocked("ble_tesla_flinn") and gate.blocked("BLE_TESLA_FLINN")
    assert not gate.blocked("ble_tesla_other") and not gate.blocked(OTHER_VIN)
    restarted = mods.solar.SolarOwnership(gate.store); await restarted.load()
    assert restarted.blocked("ble_tesla_flinn")              # survives restart
    await restarted.release(VIN)
    assert not restarted.blocked("ble_tesla_flinn")


@pytest.mark.asyncio
async def test_service_uses_ble_provider_with_single_readback_and_aliased_hold(mods, monkeypatch):
    solar = mods.solar
    vol = types.ModuleType("voluptuous")
    vol.Required = vol.Optional = lambda key, **kwargs: key
    vol.Schema = lambda value: value
    vol.All = lambda *args: args
    vol.Range = lambda **kwargs: kwargs
    monkeypatch.setitem(sys.modules, "voluptuous", vol)
    const = types.ModuleType(f"{PKG}.const")
    const.DOMAIN, const.CONF_MONITORING_MODE = "power_sync", "monitoring_mode"
    monkeypatch.setitem(sys.modules, const.__name__, const)
    actions = types.ModuleType(f"{PKG}.automations.actions")
    actions._dynamic_ev_state = {}
    monkeypatch.setitem(sys.modules, actions.__name__, actions)
    ha_core = types.ModuleType("homeassistant.core")
    ha_core.SupportsResponse = SimpleNamespace(ONLY="only", OPTIONAL="optional")
    monkeypatch.setitem(sys.modules, ha_core.__name__, ha_core)
    ha_ex = types.ModuleType("homeassistant.exceptions")
    ha_ex.HomeAssistantError = solar.SolarError
    monkeypatch.setitem(sys.modules, ha_ex.__name__, ha_ex)
    ha_client = types.ModuleType("homeassistant.helpers.aiohttp_client")
    ha_client.async_get_clientsession = lambda hass: None
    monkeypatch.setitem(sys.modules, ha_client.__name__, ha_client)

    class Store:
        value = None
        async def async_load(self): return self.value
        async def async_save(self, value): self.value = value
    gate = solar.SolarOwnership(Store()); await gate.load()

    async def ownership(*args): return gate
    monkeypatch.setattr(solar, "ownership", ownership)

    async def no_cloud(*args, **kwargs):
        raise AssertionError("a BLE-provider request must not reach a cloud provider")
    monkeypatch.setattr(solar, "request_settings", no_cloud)

    calls, answers = [], []

    class Bridge:
        provider, alias = "ble", "BLE_TESLA_FLINN"
        async def request(self, settings=None):
            calls.append(settings)
            answer = answers.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return answer
    seen_vins = []

    def connection(hass, config_entry, vin=None):
        seen_vins.append(vin)
        return Bridge(), "ble"
    monkeypatch.setattr(solar, "native_solar_connection", connection)

    services = {}
    config_entry = SimpleNamespace(entry_id="one", data={}, options={})
    hass = SimpleNamespace(data={"power_sync": {"one": {}}},
                           config_entries=SimpleNamespace(async_entries=lambda _: [config_entry]),
                           services=SimpleNamespace(async_register=lambda d, n, h, **k: services.__setitem__(n, h)))
    solar.register_services(hass)

    async def call(service, **extra):
        gate.requests.clear()
        return await services[service](SimpleNamespace(service=service, data={
            "entry_id": "one", "vin": VIN.lower(), "enabled": True, "confirm_eligible": True,
            "lower_charge_limit": 20, "upper_charge_limit": 80, **extra}))
    on = {"enabled": True, "lowerChargeLimit": 20, "upperChargeLimit": 80}
    off = {"enabled": False, "lowerChargeLimit": 20, "upperChargeLimit": 80}

    answers.append(on)
    result = await call("set_teslemetry_charge_on_solar")
    assert seen_vins[-1] == VIN                              # provider chosen for this exact car
    assert calls == [on]                                     # one transaction, readback included
    assert result == {"settings": on, "readback_confirmed": True, "powersync_control_blocked": True}
    assert gate.blocked("ble_tesla_flinn")                   # BLE charging paths are fenced too
    assert gate.store.value["vehicles"][VIN]["aliases"] == ["BLE_TESLA_FLINN"]

    # The bridge goes offline and a cloud provider answers: the BLE fence stays.
    async def cloud(session, token, vin, settings=None, *, provider="teslemetry"):
        return on
    monkeypatch.setattr(solar, "request_settings", cloud)
    monkeypatch.setattr(solar, "native_solar_connection", lambda *args: ("secret", "powersync"))
    await call("get_teslemetry_charge_on_solar")
    assert gate.blocked("ble_tesla_flinn")
    assert gate.store.value["vehicles"][VIN]["aliases"] == ["BLE_TESLA_FLINN"]
    monkeypatch.setattr(solar, "request_settings", no_cloud)
    monkeypatch.setattr(solar, "native_solar_connection", connection)

    answers.append(solar.SolarError("Tesla BLE Charge on Solar write could not be confirmed"))
    with pytest.raises(solar.SolarError):
        await call("set_teslemetry_charge_on_solar", enabled=False)
    assert gate.blocked("ble_tesla_flinn")                   # an uncertain write keeps the fence

    answers.append(solar.SolarError("Tesla BLE Charge on Solar read failed (timeout)"))
    with pytest.raises(solar.SolarError):
        await call("get_teslemetry_charge_on_solar")
    assert gate.blocked(VIN)                                 # a failed read cannot release a hold

    answers.append(off)
    result = await call("get_teslemetry_charge_on_solar")
    assert calls[-1] is None and not result["powersync_control_blocked"]
    assert not gate.blocked("ble_tesla_flinn") and VIN not in gate.store.value["vehicles"]

    answers.append(solar.SolarError("Tesla BLE Charge on Solar read failed (ble_disconnected)"))
    with pytest.raises(solar.SolarError):
        await call("get_teslemetry_charge_on_solar")
    assert not gate.blocked(VIN)                             # a failed read with no prior hold leaves control

    config_entry.options["monitoring_mode"] = True
    before = len(calls)
    with pytest.raises(solar.SolarError, match="monitoring mode"):
        await call("set_teslemetry_charge_on_solar")
    assert len(calls) == before
