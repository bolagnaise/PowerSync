"""Read-only Charge on Solar pairing status: bridge records, key matching, steps."""
import asyncio
import base64
import hashlib
import importlib.util
import json
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

ROOT = Path(__file__).parents[1] / "custom_components/power_sync"
PKG = "ps_cos_pairing_probe"
VIN = "5YJ3E1EA7KF000001"
DIN = "1232100-00-E--TG123456789ABC"
NONCE = "a" * 32


def _load(name):
    spec = importlib.util.spec_from_file_location(f"{PKG}.{name}", ROOT / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def mod(monkeypatch):
    package = types.ModuleType(PKG)
    package.__path__ = []
    monkeypatch.setitem(sys.modules, PKG, package)
    recovery = types.ModuleType(f"{PKG}.tesla_ev_recovery")
    recovery.identities = {}
    recovery._identity = lambda hass, entity_id: recovery.identities.get(entity_id)
    monkeypatch.setitem(sys.modules, recovery.__name__, recovery)
    _load("teslemetry_solar")
    module = _load("charge_on_solar_pairing")
    module.recovery = recovery
    yield module
    for name in ("teslemetry_solar", "charge_on_solar_pairing"):
        sys.modules.pop(f"{PKG}.{name}", None)


def point():
    key = ec.generate_private_key(ec.SECP256R1()).public_key()
    return key, key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)


def record(**fields):
    return json.dumps({"v": 1, "i": VIN, "n": NONCE, "m": "k", **fields}, separators=(",", ":"))


def test_fingerprint_matches_the_bridge_definition(mod):
    # Firmware: first 8 bytes of SHA-256 over the key string's exact bytes.
    assert mod.fingerprint("04ab") == hashlib.sha256(b"04ab").hexdigest()[:16]


def test_gateway_key_accepts_raw_point_or_der(mod):
    key, raw = point()
    der = key.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    assert mod.gateway_key_point(raw) == raw
    assert mod.gateway_key_point(der) == raw
    encodings = mod.gateway_key_encodings(raw)
    assert encodings["hex"] == raw.hex() and encodings["hex_upper"] == raw.hex().upper()
    assert base64.b64decode(encodings["base64_raw"]) == raw
    assert base64.b64decode(encodings["base64_der"]) == der
    p384 = ec.generate_private_key(ec.SECP384R1()).public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    with pytest.raises(mod.SolarError):
        mod.gateway_key_point(p384)


@pytest.mark.parametrize("fields", [
    {"r": "s", "c": 1, "s": [["h", "0" * 16, DIN, -3386880, 15120930]]},
    {"r": "s", "c": 0, "s": []},
    {"r": "s", "c": 5, "t": 1, "s": [["r", "1" * 16, DIN]]},
    {"r": "k", "d": "i", "k": "04" + "ab" * 64},
    {"m": "f", "r": "s", "err": "ble_disconnected"},
])
def test_valid_records_decode(mod, fields):
    assert mod.decode_state(record(**fields))["i"] == VIN


@pytest.mark.parametrize("fields", [
    {"r": "s", "c": 2, "s": [["h", "0" * 16, DIN]]},           # incomplete list without "t"
    {"r": "s", "c": 1, "s": [["x", "0" * 16, DIN]]},           # unknown key format
    {"r": "s", "c": 1, "s": [["h", "0" * 15, DIN]]},           # short fingerprint
    {"r": "s", "c": 1, "s": [["h", "0" * 16, 'a"b']]},         # quote in DIN
    {"r": "s", "c": 1, "s": [["h", "0" * 16, DIN, 9000001, 0]]},  # latitude above 90
    {"r": "s", "c": 1, "s": [["h", "0" * 16, DIN, 1.5, 0]]},   # coordinates are integers
    {"r": "k", "d": "x", "k": "04" + "ab" * 64},
    {"r": "k", "d": "v", "k": "03" + "ab" * 32},               # compressed point
    {"r": "z", "c": 0, "s": []},                               # unknown record kind
    {"m": "f", "r": "s", "err": "bad\nerror"},
])
def test_invalid_records_are_rejected(mod, fields):
    with pytest.raises(mod.SolarError):
        mod.decode_state(record(**fields))


def test_duplicate_fields_and_oversize_records_are_rejected(mod):
    with pytest.raises(mod.SolarError):
        mod.decode_state('{"v":1,"v":1,"i":"%s","n":"","m":"i"}' % VIN)
    with pytest.raises(mod.SolarError):
        mod.decode_state(record(r="k", d="i", k="04" + "ab" * 64, pad="x" * 200))


def gateway(mod, raw):
    return {"din": DIN, "encodings": mod.gateway_key_encodings(raw)}


def site(mod, text, din=DIN, **location):
    return {"key_format": "hex", "key_fingerprint": mod.fingerprint(text), "din": din, **location}


@pytest.mark.parametrize("encoding", ["hex", "hex_upper", "base64_raw", "base64_der"])
def test_site_step_identifies_the_gateway_key_in_any_encoding(mod, encoding):
    _, raw = point()
    gw = gateway(mod, raw)
    sites = {"count": 1, "complete": True,
             "sites": [site(mod, gw["encodings"][encoding], latitude=-33.8688, longitude=151.2093)]}
    result = mod.site_step(sites, gw, home=(-33.8690, 151.2093))
    assert result["status"] == "present" and result["key_format"] == encoding
    assert result["din_matches"] and result["near_home"]


def test_site_step_mismatch_missing_and_unknown(mod):
    _, raw = point()
    _, other = point()
    gw = gateway(mod, raw)
    other_site = {"count": 1, "complete": True, "sites": [site(mod, other.hex())]}
    assert mod.site_step(other_site, gw, None)["status"] == "key_mismatch"   # same DIN, other key
    elsewhere = {"count": 1, "complete": True, "sites": [site(mod, other.hex(), din="OTHER")]}
    assert mod.site_step(elsewhere, gw, None)["status"] == "missing"
    assert mod.site_step({"count": 0, "complete": True, "sites": []}, gw, None)["status"] == "missing"
    # The bridge dropped sites to fit its record: an absent match proves nothing.
    partial = {"count": 3, "complete": False, "sites": [site(mod, other.hex(), din="OTHER")]}
    assert mod.site_step(partial, gw, None)["status"] == "unknown"
    assert mod.site_step(None, gw, None) == {"status": "unknown"}
    assert mod.site_step(other_site, None, None)["status"] == "unknown"
    far = {"count": 1, "complete": True,
           "sites": [site(mod, raw.hex(), latitude=-33.0, longitude=151.2093)]}
    assert mod.site_step(far, gw, (-33.8688, 151.2093))["near_home"] is False


def test_vehicle_client_step(mod):
    _, vcsec = point()
    _, infotainment = point()
    keys = {"v": vcsec, "i": infotainment}

    def client(key, state="VERIFIED", vin=VIN, kind="VEHICLE"):
        return {"type": kind, "identifier": vin, "state": state,
                "public_key": base64.b64encode(key).decode()}

    verified = mod.vehicle_client_step({"clients": [client(infotainment)]}, VIN, keys)
    assert verified["status"] == "verified" and verified["matches_vehicle_key"] == "infotainment"
    pending = mod.vehicle_client_step({"clients": [client(vcsec, "PENDING_VERIFICATION")]}, VIN, keys)
    assert pending["status"] == "not_verified" and pending["matches_vehicle_key"] == "vehicle_security"
    _, stranger = point()
    assert mod.vehicle_client_step({"clients": [client(stranger)]}, VIN, keys)["matches_vehicle_key"] == "neither"
    # A verified record wins over an older pending one.
    both = mod.vehicle_client_step({"clients": [client(vcsec, "REMOVED"), client(infotainment)]}, VIN, keys)
    assert both["status"] == "verified" and both["client_count"] == 2
    others = {"clients": [client(vcsec, vin="5YJ3E1EA7KF000002"), client(vcsec, kind="CUSTOMER_MOBILE_APP")]}
    assert mod.vehicle_client_step(others, VIN, keys) == {"status": "missing"}
    assert mod.vehicle_client_step(None, VIN, keys) == {"status": "unknown"}
    assert "matches_vehicle_key" not in mod.vehicle_client_step({"clients": [client(vcsec)]}, VIN, {})


class Hass:
    """A pairing-capable bridge answering its request text entity."""

    def __init__(self, recovery, answer):
        prefix = "tesla_flinn"
        self.request = f"text.{prefix}_powersync_charge_on_solar_pairing_request"
        self.state = f"sensor.{prefix}_powersync_charge_on_solar_pairing"
        self.solar_state = f"sensor.{prefix}_powersync_charge_on_solar"
        self.states_map = {self.request: SimpleNamespace(state=""),
                           self.state: SimpleNamespace(state=record(m="i", n=""))}
        self.states = SimpleNamespace(get=self.states_map.get)
        self.services = SimpleNamespace(async_call=self.async_call)
        self.sent, self.answer = [], answer
        for entity_id in (self.request, self.state, self.solar_state):
            recovery.identities[entity_id] = {"platform": "esphome", "device_id": "d", "config_entry_id": "e"}
        self.bridge = SimpleNamespace(prefix=prefix, vin=VIN, state_id=self.solar_state)

    async def async_call(self, domain, service, data, blocking=False):
        assert (domain, service, blocking) == ("text", "set_value", True)
        self.sent.append(data["value"])
        nonce = data["value"].split(":")[1]
        self.states_map[self.state] = SimpleNamespace(state=self.answer(nonce, data["value"]))


def test_bridge_reads_sites_and_keys(mod):
    _, key = point()

    def answer(nonce, text):
        if text.startswith("sit:"):
            return record(n=nonce, r="s", c=1, s=[["h", "0" * 16, DIN, -3386880, 15120930]])
        return record(n=nonce, r="k", d=text[-1], k=key.hex())

    hass = Hass(mod.recovery, answer)
    bridge = mod.PairingBridge(hass, hass.bridge)
    assert bridge.available()
    sites = asyncio.run(bridge.managed_sites())
    assert sites == {"count": 1, "complete": True, "sites": [
        {"key_format": "hex", "key_fingerprint": "0" * 16, "din": DIN,
         "latitude": -33.8688, "longitude": 151.2093}]}
    assert asyncio.run(bridge.vehicle_key("i")) == key
    assert hass.sent[0].startswith("sit:") and hass.sent[1].endswith(":i")
    # Only read verbs ever reach the bridge.
    assert all(text.split(":")[0] in {"sit", "key"} for text in hass.sent)


def test_bridge_rejects_answers_for_another_request(mod):
    hass = Hass(mod.recovery, lambda nonce, text: record(n=nonce, r="k", d="v", k="04" + "ab" * 64))
    with pytest.raises(mod.SolarError, match="different vehicle or request"):
        asyncio.run(mod.PairingBridge(hass, hass.bridge).managed_sites())
    hass.answer = lambda nonce, text: record(n=nonce, m="f", r="s", err="recovery_locked")
    with pytest.raises(mod.SolarError, match="recovery_locked"):
        asyncio.run(mod.PairingBridge(hass, hass.bridge).managed_sites())


def test_bridge_unavailable_on_another_device(mod):
    hass = Hass(mod.recovery, lambda nonce, text: "")
    mod.recovery.identities[hass.request] = {"platform": "esphome", "device_id": "other", "config_entry_id": "e"}
    assert not mod.PairingBridge(hass, hass.bridge).available()


def test_status_reads_never_send_a_write_verb(mod):
    source = (ROOT / "charge_on_solar_pairing.py").read_text()
    status = source[source.index("async def pairing_status"):source.index("# ---- repair (writes)")]
    for call in ("add_gateway_key", "add_site", "remove_site", "add_vehicle_client"):
        assert call not in status


@pytest.mark.parametrize("fields", [
    {"r": "e", "p": 1, "o": 6}, {"r": "e", "p": 0},
    {"m": "p", "r": "a"},
    {"r": "t", "x": 1, "c": 1, "s": [["h", "0" * 16, DIN, -3386880, 15120930]]},
    {"r": "q", "c": 0, "s": []},
    {"m": "u", "r": "t", "err": "timeout", "a": 1},
])
def test_write_records_decode(mod, fields):
    assert mod.decode_state(record(**fields))["r"] == fields["r"]


@pytest.mark.parametrize("fields", [
    {"r": "e", "p": 1},                    # present without a role
    {"r": "e", "p": True, "o": 6},         # boolean is not the integer flag
    {"m": "k", "r": "a"},                  # a key add is never "confirmed" by the bridge
    {"m": "u", "r": "s", "err": "x"},      # reads are never uncertain
    {"m": "p", "r": "t"},                  # only key adds await a tap
    {"r": "t", "x": 4, "c": 0, "s": []},
    {"m": "u", "r": "q", "err": "x", "a": 2},
])
def test_invalid_write_records_are_rejected(mod, fields):
    with pytest.raises(mod.SolarError):
        mod.decode_state(record(**fields))


class FakeBridge:
    def __init__(self, mod, gateway_raw, roles=(), sites=None, keys=None, site_after=None, outcome="added_or_updated"):
        self.mod, self.calls = mod, []
        self.roles = list(roles)
        self.sites = sites or {"count": 0, "complete": True, "sites": []}
        self.keys = keys or {}
        self.site_after, self.outcome, self.gateway_raw = site_after, outcome, gateway_raw

    async def whitelist_role(self, key):
        self.calls.append(("role", key))
        return self.roles.pop(0) if self.roles else None

    async def add_gateway_key(self, key):
        self.calls.append(("add_key", key))

    async def vehicle_key(self, domain):
        if domain not in self.keys:
            raise self.mod.SolarError("no_session_key")
        return self.keys[domain]

    async def managed_sites(self):
        return self.sites

    async def add_site(self, text, din, lat, lon):
        self.calls.append(("add_site", text, din, lat, lon))
        return {**(self.site_after or self.sites), "outcome": self.outcome}

    async def remove_site(self, text):
        self.calls.append(("remove_site", text))
        return self.site_after


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def fast(mod, monkeypatch):
    async def no_sleep(_):
        return None
    monkeypatch.setattr(mod.asyncio, "sleep", no_sleep)
    return mod


def test_gateway_key_add_waits_for_the_card_tap(fast):
    mod = fast
    _, raw = point()
    gw = gateway(mod, raw) | {"point": raw}
    bridge = FakeBridge(mod, raw, roles=[None, None, 6])
    result = run(mod._repair_gateway_key(bridge, gw))
    assert result["result"] == "confirmed" and result["status"]["status"] == "present"
    assert [c[0] for c in bridge.calls] == ["role", "add_key", "role", "role"]
    assert sum(c[0] == "add_key" for c in bridge.calls) == 1  # sent exactly once


def test_gateway_key_already_present_or_wrong_role_sends_nothing(fast):
    mod = fast
    _, raw = point()
    gw = gateway(mod, raw) | {"point": raw}
    bridge = FakeBridge(mod, raw, roles=[6])
    assert run(mod._repair_gateway_key(bridge, gw))["result"] == "already_present"
    bridge = FakeBridge(mod, raw, roles=[3])
    with pytest.raises(mod.SolarError, match="role 3"):
        run(mod._repair_gateway_key(bridge, gw))
    assert all(c[0] != "add_key" for c in bridge.calls)


def test_gateway_key_without_tap_is_uncertain(fast, monkeypatch):
    mod = fast
    clock = iter(range(0, 1000, 10))
    monkeypatch.setattr(mod.time, "monotonic", lambda: next(clock))
    _, raw = point()
    gw = gateway(mod, raw) | {"point": raw}
    with pytest.raises(mod.PairingUncertain, match="Tap a key card"):
        run(mod._repair_gateway_key(FakeBridge(mod, raw), gw))


class FakeClient:
    def __init__(self, before, after, answer=True):
        self.before, self.after, self.answer, self.added = before, after, answer, []
        self.reads = 0

    async def list_authorized_clients(self):
        self.reads += 1
        return self.before if self.reads == 1 else self.after

    async def add_vehicle_client(self, vin, key):
        self.added.append((vin, key))
        return self.answer


def vehicle_client(key, state="VERIFIED"):
    return {"type": "VEHICLE", "identifier": VIN, "state": state, "public_key": base64.b64encode(key).decode()}


def test_vehicle_registration_confirms_by_readback(fast):
    mod = fast
    _, key = point()
    bridge = FakeBridge(mod, None, keys={"v": key, "i": key})
    client = FakeClient({"clients": []}, {"clients": [vehicle_client(key)]})
    result = run(mod._repair_vehicle_client(client, bridge, VIN, None))
    assert result["result"] == "confirmed" and client.added == [(VIN, key)]


def test_vehicle_registration_refusals(fast):
    mod = fast
    _, key = point()
    _, other = point()
    # Different keys per domain need an explicit choice.
    with pytest.raises(mod.SolarError, match="vehicle_key_domain"):
        run(mod._repair_vehicle_client(FakeClient({"clients": []}, None), FakeBridge(mod, None, keys={"v": key, "i": other}), VIN, None))
    # An existing record for this car is never overwritten or duplicated.
    client = FakeClient({"clients": [vehicle_client(other, "PENDING_VERIFICATION")]}, None)
    with pytest.raises(mod.SolarError, match="Tesla app"):
        run(mod._repair_vehicle_client(client, FakeBridge(mod, None, keys={"v": key, "i": key}), VIN, None))
    assert client.added == []
    # Already verified with this key: nothing is sent.
    client = FakeClient({"clients": [vehicle_client(key)]}, None)
    assert run(mod._repair_vehicle_client(client, FakeBridge(mod, None, keys={"v": key, "i": key}), VIN, None))["result"] == "already_present"
    assert client.added == []
    # A refusal changed nothing; an unconfirmed answer is uncertain.
    with pytest.raises(mod.SolarError, match="nothing was changed"):
        run(mod._repair_vehicle_client(FakeClient({"clients": []}, None, answer=False), FakeBridge(mod, None, keys={"v": key, "i": key}), VIN, None))
    with pytest.raises(mod.PairingUncertain):
        run(mod._repair_vehicle_client(FakeClient({"clients": []}, {"clients": []}, answer=None), FakeBridge(mod, None, keys={"v": key, "i": key}), VIN, "i"))


def test_site_add_uses_the_chosen_encoding_and_confirms(fast):
    mod = fast
    _, raw = point()
    gw = gateway(mod, raw) | {"point": raw}
    text = gw["encodings"]["base64_raw"]
    after = {"count": 1, "complete": True, "sites": [site(mod, text, latitude=-33.8688, longitude=151.2093)]}
    bridge = FakeBridge(mod, raw, site_after=after)
    result = run(mod._repair_site(bridge, gw, "base64_raw", (-33.8688, 151.2093)))
    assert result["result"] == "confirmed" and result["status"]["key_format"] == "base64_raw"
    assert bridge.calls == [("add_site", text, DIN, -33.8688, 151.2093)]


def test_site_add_refusals(fast):
    mod = fast
    _, raw = point()
    gw = gateway(mod, raw) | {"point": raw}
    with pytest.raises(mod.SolarError, match="key_format"):
        run(mod._repair_site(FakeBridge(mod, raw), gw, None, (0.0, 0.0)))
    partial = {"count": 5, "complete": False, "sites": []}
    with pytest.raises(mod.SolarError, match="every managed site"):
        run(mod._repair_site(FakeBridge(mod, raw, sites=partial), gw, "hex", (0.0, 0.0)))
    present = {"count": 1, "complete": True, "sites": [site(mod, raw.hex())]}
    bridge = FakeBridge(mod, raw, sites=present)
    assert run(mod._repair_site(bridge, gw, "hex", (0.0, 0.0)))["result"] == "already_present"
    assert bridge.calls == []
    nearby = FakeBridge(mod, raw, site_after={"count": 0, "complete": True, "sites": []}, outcome="existing_site_nearby")
    with pytest.raises(mod.SolarError, match="existing managed site"):
        run(mod._repair_site(nearby, gw, "hex", (0.0, 0.0)))
    silent = FakeBridge(mod, raw, site_after={"count": 0, "complete": True, "sites": []})
    with pytest.raises(mod.PairingUncertain):
        run(mod._repair_site(silent, gw, "hex", (0.0, 0.0)))


def test_site_removal_uses_the_cars_own_encoding(fast):
    mod = fast
    _, raw = point()
    gw = gateway(mod, raw) | {"point": raw}
    text = gw["encodings"]["hex_upper"]
    bridge = FakeBridge(mod, raw, sites={"count": 1, "complete": True, "sites": [site(mod, text)]},
                        site_after={"count": 0, "complete": True, "sites": []})
    assert run(mod._remove_site(bridge, gw, None))["result"] == "confirmed"
    assert bridge.calls == [("remove_site", text)]
    assert run(mod._remove_site(FakeBridge(mod, raw), gw, None))["result"] == "already_absent"


def test_bridge_write_requests_are_well_formed(mod):
    _, key = point()
    sent = []

    def answer(nonce, text):
        sent.append(text)
        verb = text[:3]
        if verb == "wla":
            return record(n=nonce, m="p", r="a")
        if verb == "ste":
            return record(n=nonce, r="t", x=1, c=0, s=[])
        return record(n=nonce, m="u", r="q", err="timeout", a=1)

    hass = Hass(mod.recovery, answer)
    bridge = mod.PairingBridge(hass, hass.bridge)
    run(bridge.add_gateway_key(key))
    assert sent[-1].endswith(":" + key.hex())
    assert run(bridge.add_site("04" + "ab" * 64, DIN, -33.8688, 151.2093))["outcome"] == "added_or_updated"
    assert sent[-1].split(":")[2:5] == ["-3386880", "15120930", DIN]
    with pytest.raises(mod.PairingUncertain):
        run(bridge.remove_site("04" + "ab" * 64))
    with pytest.raises(mod.SolarError):
        run(bridge.add_site("bad:key", DIN, 0, 0))
    assert all(len(text) <= 255 for text in sent)


class GateHass:
    """Just enough Home Assistant for the shared request gate."""

    def __init__(self, entry):
        self.data = {"power_sync": {entry.entry_id: {}}}
        self.config_entries = SimpleNamespace(async_entries=lambda domain: [entry])


def gate_entry(**options):
    return SimpleNamespace(entry_id="one", data={}, options=options)


@pytest.fixture
def gate(mod, monkeypatch):
    const = types.ModuleType(f"{PKG}.const")
    const.DOMAIN, const.CONF_MONITORING_MODE = "power_sync", "monitoring_mode"
    monkeypatch.setitem(sys.modules, const.__name__, const)
    calls = []

    async def status(hass, entry, vin):
        calls.append(("status", vin))
        return {"vin": vin}

    async def repair(hass, entry, vin, step, **options):
        calls.append(("repair", step, options))
        return {"vin": vin, "step": step, "result": "confirmed"}

    monkeypatch.setattr(mod, "pairing_status", status)
    monkeypatch.setattr(mod, "repair_pairing", repair)
    mod.calls = calls
    return mod


@pytest.mark.parametrize("kwargs,message", [
    ({"action": "repair", "step": "vehicle_on_gateway"}, "Confirm"),
    ({"action": "repair", "step": "vehicle_on_gateway", "confirm": "yes"}, "Confirm"),
    ({"action": "repair", "step": "delete_everything", "confirm": True}, "pairing step"),
    ({"action": "write"}, "status or repair"),
])
def test_request_gate_refuses_before_any_io(gate, kwargs, message):
    hass = GateHass(gate_entry())
    action = kwargs.pop("action")
    with pytest.raises(gate.SolarError, match=message):
        run(gate.run_request(hass, "one", VIN, action, **kwargs))
    assert gate.calls == []


def test_request_gate_blocks_monitoring_mode_bad_vin_and_concurrency(gate):
    hass = GateHass(gate_entry(monitoring_mode=True))
    with pytest.raises(gate.SolarError, match="monitoring mode"):
        run(gate.run_request(hass, "one", VIN, "repair", step="vehicle_on_gateway", confirm=True))
    # Reads stay available in monitoring mode.
    assert run(gate.run_request(hass, "one", VIN.lower(), "status")) == {"vin": VIN}
    with pytest.raises(gate.SolarError, match="VIN"):
        run(gate.run_request(hass, "one", "nope", "status"))
    with pytest.raises(gate.SolarError, match="loaded PowerSync entry"):
        run(gate.run_request(hass, "two", VIN, "status"))

    async def busy():
        async with gate.pairing_lock(hass):
            await gate.run_request(hass, "one", VIN, "status")
    with pytest.raises(gate.SolarError, match="already running"):
        run(busy())


def test_request_gate_passes_repair_options(gate):
    hass = GateHass(gate_entry())
    result = run(gate.run_request(hass, "one", VIN, "repair", step="managed_charging_site", confirm=True,
                                  key_format="hex", latitude=-33.0, longitude=151.0))
    assert result["result"] == "confirmed"
    assert gate.calls == [("repair", "managed_charging_site", {
        "key_format": "hex", "vehicle_key_domain": None, "latitude": -33.0, "longitude": 151.0})]


def test_mobile_view_reports_whole_errors_with_a_code(gate, monkeypatch):
    views = []
    http = types.ModuleType("homeassistant.components.http")

    class HomeAssistantView:
        def json(self, data, status_code=200):
            return status_code, data
    http.HomeAssistantView = HomeAssistantView
    for name in ("homeassistant", "homeassistant.components"):
        monkeypatch.setitem(sys.modules, name, sys.modules.get(name) or types.ModuleType(name))
    monkeypatch.setitem(sys.modules, "homeassistant.components.http", http)
    hass = GateHass(gate_entry())
    hass.http = SimpleNamespace(register_view=views.append)
    gate.register_http_view(hass)
    view = views[0]
    assert view.url == "/api/power_sync/ev/native_solar/pairing" and view.requires_auth

    def post(body):
        async def body_json():
            return body
        return run(view.post(SimpleNamespace(json=body_json)))

    status, data = post({"entry_id": "one", "vin": VIN, "action": "status"})
    assert status == 200 and data == {"success": True, "vin": VIN}
    status, data = post({"entry_id": "one", "vin": VIN, "action": "repair", "step": "vehicle_on_gateway"})
    assert status == 200 and data["success"] is False and data["code"] == "pairing_refused"
    assert "Confirm" in data["error"]
    for bad in ({"action": "repair", "step": "vehicle_on_gateway", "confirm": True, "latitude": "1"},
                {"action": "repair", "step": "vehicle_on_gateway", "confirm": True, "latitude": True},
                {"action": "repair", "step": "vehicle_on_gateway", "confirm": True, "key_format": "pem"},
                {"action": "repair", "step": "vehicle_on_gateway", "confirm": True, "vehicle_key_domain": "x"}):
        status, data = post({"entry_id": "one", "vin": VIN, **bad})
        assert data["success"] is False
    assert all(call[0] != "repair" for call in gate.calls)

    async def uncertain(*args, **kwargs):
        raise gate.PairingUncertain("could not be confirmed")
    monkeypatch.setattr(gate, "repair_pairing", uncertain)
    status, data = post({"entry_id": "one", "vin": VIN, "action": "repair", "step": "vehicle_on_gateway", "confirm": True})
    assert data == {"success": False, "error": "could not be confirmed", "code": "pairing_uncertain"}
    status, data = post(["not", "an", "object"])
    assert data["success"] is False


def test_site_with_wrong_location_or_din_is_corrected(fast):
    mod = fast
    _, raw = point()
    gw = gateway(mod, raw) | {"point": raw}
    text = gw["encodings"]["base64_der"]
    # The car has this gateway's site, but 90 km from where the car parks.
    misplaced = {"count": 1, "complete": True, "sites": [site(mod, text, latitude=-33.0, longitude=151.2093)]}
    fixed = {"count": 1, "complete": True, "sites": [site(mod, text, latitude=-33.8688, longitude=151.2093)]}
    bridge = FakeBridge(mod, raw, sites=misplaced, site_after=fixed)
    result = run(mod._repair_site(bridge, gw, None, (-33.8688, 151.2093)))
    # Re-sent in the encoding the car already stores, then confirmed in place.
    assert result["result"] == "confirmed"
    assert bridge.calls == [("add_site", text, DIN, -33.8688, 151.2093)]
    # A wrong DIN is corrected the same way.
    wrong_din = {"count": 1, "complete": True, "sites": [site(mod, text, din="OTHER-DIN")]}
    bridge = FakeBridge(mod, raw, sites=wrong_din, site_after={"count": 1, "complete": True, "sites": [site(mod, text)]})
    assert run(mod._repair_site(bridge, gw, None, (-33.8688, 151.2093)))["result"] == "confirmed"
    # If the readback still shows the wrong location, the result is uncertain.
    bridge = FakeBridge(mod, raw, sites=misplaced, site_after=misplaced)
    with pytest.raises(mod.PairingUncertain, match="wrong DIN or location"):
        run(mod._repair_site(bridge, gw, None, (-33.8688, 151.2093)))


def test_pairing_bridge_dispatch_failure_is_a_solar_error(mod):
    hass = Hass(mod.recovery, lambda nonce, text: "")

    async def offline(*args, **kwargs):
        raise RuntimeError("ESPHome device unavailable")
    hass.services.async_call = offline
    with pytest.raises(mod.SolarError, match="could not be sent"):
        run(mod.PairingBridge(hass, hass.bridge).managed_sites())
    with pytest.raises(mod.SolarError, match="write could not be sent"):
        run(mod.PairingBridge(hass, hass.bridge).add_gateway_key(point()[1]))


def test_firmware_holds_vcsec_commands_during_the_key_card_window():
    component = Path(__file__).parents[1] / "esphome/components/tesla_ble_vehicle"
    queue = (component / "tesla_ble_vehicle.cpp").read_text()
    process = queue[queue.index("void TeslaBLEVehicle::process_command_queue()"):]
    hold = process.index("key_add_pending(now)")
    # The hold comes before the timeout check, and refreshes the held command.
    assert hold < process.index("// Overall timeout check")
    assert "command_queue_.front().started_at = now;" in process[hold:process.index("// Overall timeout check")]
    glue = (component / "cos_pairing.cpp").read_text()
    assert 'refusal = "command_queue_busy"' in glue and "vcsec_command_in_flight()" in glue
    assert 'refusal = "key_add_pending"' in glue           # never two adds at once
    assert "pair_key_add_until_ = millis() + KEY_ADD_HOLD_MS;" in glue
    assert glue.count("pair_key_add_until_ = 0;") == 1     # cleared when the entry shows the key
