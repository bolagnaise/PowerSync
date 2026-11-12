"""Shared Ed25519 vectors and closed cross-language request data."""
import copy
import importlib.util
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("diagnostics_wire_test", ROOT / "custom_components/power_sync/diagnostics_access/wire.py")
wire = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = wire
spec.loader.exec_module(wire)
VECTOR = json.loads((ROOT / "tests/fixtures/diagnostics/wire-v1.json").read_text())


def test_python_matches_independent_wire_golden_and_verifies_exact_bindings():
    private, public = wire.keypair(bytes(range(32)))
    packet = wire.packet(private, "client", "0" * 64, "1" * 32, VECTOR["payload"], now=1700000000, nonce=wire.b64(bytes(16)))
    assert public == VECTOR["public_key"] and packet == VECTOR["packet"]
    assert wire.verify(packet, public, "client", "0" * 64, "1" * 32, now=1700000001) == VECTOR["payload"]
    for purpose, root, grant, now in [("boot", "0" * 64, "1" * 32, 1700000001),
        ("client", "2" * 64, "1" * 32, 1700000001), ("client", "0" * 64, "2" * 32, 1700000001),
        ("client", "0" * 64, "1" * 32, 1700000061)]:
        with pytest.raises(wire.WireError):
            wire.verify(packet, public, purpose, root, grant, now=now)
    changed = copy.deepcopy(packet)
    changed["payload"] = wire.b64(wire.json_bytes({"operation": "ha_get_system"}))
    with pytest.raises(wire.WireError, match="payload_changed"):
        wire.verify(changed, public, "client", "0" * 64, "1" * 32, now=1700000001)


@pytest.mark.parametrize("raw", [b'{"key":1,"key":2}', b'{"value":NaN}', b'{"value":1e309}',
    b'{"value":9007199254740992}', b'{"value":"\\ud800"}', b'[]', b'false', b'\xff'])
def test_wire_rejects_ambiguous_or_unrepresentable_json(raw):
    with pytest.raises(wire.WireError):
        wire.loads(raw)


def test_payload_limits_reject_cycles_opaque_objects_and_conversion_hooks():
    class Opaque:
        def __str__(self):
            raise AssertionError("must not format")
    recursive = {}; recursive["again"] = recursive
    for value in [{"opaque": Opaque()}, recursive, {"too_big": "x" * wire.MAX_PAYLOAD}, {"unsafe_float": 1e20}]:
        with pytest.raises(wire.WireError):
            wire.json_bytes(value)
