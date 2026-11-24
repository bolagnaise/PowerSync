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
    {"r": "q", "c": 0, "s": []},
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


def test_module_never_encodes_a_write():
    source = (ROOT / "charge_on_solar_pairing.py").read_text()
    for verb in ("set:", "add_authorized_client", "add_managed_charging_site", "WhiteList"):
        assert verb not in source.split('"""', 2)[2]  # outside the module docstring
