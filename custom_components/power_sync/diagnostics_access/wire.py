"""Python/Worker signed wire contract. Pure crypto/data; no Home Assistant APIs."""
from __future__ import annotations

import base64
import hashlib
import json
import math
import re
import secrets
import time

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import Encoding, PrivateFormat, PublicFormat, NoEncryption

ORIGIN = "https://diagnostics.powersync.cc"
PURPOSES = frozenset({"client", "installation", "boot", "authority", "runner"})
MAX_PAYLOAD = 300 * 1024
ID = re.compile(r"^[a-f0-9]{32,64}$")


class WireError(ValueError):
    pass


def b64(raw):
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def unb64(value, *, size=None, maximum=MAX_PAYLOAD):
    try:
        if type(value) is not str or not re.fullmatch(r"[A-Za-z0-9_-]+", value) or len(value) > math.ceil(maximum * 4 / 3):
            raise ValueError
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        if b64(raw) != value or len(raw) > maximum or size is not None and len(raw) != size:
            raise ValueError
        return raw
    except (ValueError, TypeError):
        raise WireError("invalid_encoding") from None


def loads(raw, *, maximum=MAX_PAYLOAD):
    if type(raw) is not bytes or len(raw) > maximum:
        raise WireError("payload_limit")
    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                raise WireError("invalid_json")
            result[key] = value
        return result
    try:
        result = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs,
                            parse_constant=lambda _: (_ for _ in ()).throw(WireError("invalid_json")))
        if type(result) is not dict:
            raise WireError("invalid_json")
        _validate(result)
        return result
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise WireError("invalid_json") from None


def json_bytes(value):
    _validate(value)
    try:
        raw = json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")).encode()
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise WireError("invalid_json") from None
    if len(raw) > MAX_PAYLOAD:
        raise WireError("payload_limit")
    return raw


def _validate(value):
    count = [0]
    def visit(item, depth=0):
        count[0] += 1
        if depth > 24 or count[0] > 100000:
            raise WireError("payload_limit")
        if item is None or type(item) is bool:
            return
        if type(item) is int:
            if abs(item) > 2**53 - 1:
                raise WireError("invalid_json")
            return
        if type(item) is float:
            if not math.isfinite(item) or abs(item) > 2**53 - 1:
                raise WireError("invalid_json")
            return
        if type(item) is str:
            try:
                item.encode()
            except UnicodeError:
                raise WireError("invalid_json") from None
            return
        if type(item) is list:
            for child in item:
                visit(child, depth + 1)
            return
        if type(item) is dict:
            for key, child in item.items():
                if type(key) is not str:
                    raise WireError("invalid_json")
                visit(key, depth + 1)
                visit(child, depth + 1)
            return
        raise WireError("invalid_json")
    visit(value)


def keypair(seed=None):
    key = Ed25519PrivateKey.from_private_bytes(seed) if seed is not None else Ed25519PrivateKey.generate()
    return (b64(key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())),
            b64(key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)))


def key_id(public_key):
    return hashlib.sha256(unb64(public_key, size=32)).hexdigest()


def secret_hash(value):
    if type(value) is not str:
        raise WireError("invalid_secret")
    return b64(hashlib.sha256(value.encode()).digest())


def canonical(metadata):
    expected = {"version", "root_id", "grant_id", "purpose", "issued_at", "nonce", "payload_sha256"}
    if type(metadata) is not dict or set(metadata) != expected or type(metadata["version"]) is not int or metadata["version"] != 1:
        raise WireError("invalid_metadata")
    if not all(type(metadata[key]) is str and ID.fullmatch(metadata[key]) for key in ("root_id", "grant_id")):
        raise WireError("invalid_metadata")
    if type(metadata["purpose"]) is not str or metadata["purpose"] not in PURPOSES or type(metadata["issued_at"]) is not int or not 0 <= metadata["issued_at"] <= 2**53 - 1:
        raise WireError("invalid_metadata")
    unb64(metadata["nonce"], size=16)
    unb64(metadata["payload_sha256"], size=32)
    return json_bytes(metadata)


def signed_bytes(metadata):
    return ("powersync-diagnostics-wire:" + metadata["purpose"] + ":v1\n").encode() + canonical(metadata)


def packet(private_key, purpose, root_id, grant_id, payload, *, now=None, nonce=None):
    if type(payload) is not dict:
        raise WireError("invalid_json")
    raw = json_bytes(payload)
    metadata = {"version": 1, "root_id": root_id, "grant_id": grant_id, "purpose": purpose,
                "issued_at": int(time.time() if now is None else now), "nonce": nonce or b64(secrets.token_bytes(16)),
                "payload_sha256": b64(hashlib.sha256(raw).digest())}
    signature = Ed25519PrivateKey.from_private_bytes(unb64(private_key, size=32)).sign(signed_bytes(metadata))
    return {"metadata": metadata, "signature": b64(signature), "payload": b64(raw)}


def verify(value, public_key, purpose, root_id, grant_id, *, now=None, max_age=60):
    try:
        if type(value) is not dict or set(value) != {"metadata", "signature", "payload"}:
            raise WireError("invalid_envelope")
        metadata = value["metadata"]
        encoded = signed_bytes(metadata)
        current = time.time() if now is None else now
        if metadata["purpose"] != purpose or metadata["root_id"] != root_id or metadata["grant_id"] != grant_id or abs(current - metadata["issued_at"]) > max_age:
            raise WireError("binding_denied")
        raw = unb64(value["payload"])
        if b64(hashlib.sha256(raw).digest()) != metadata["payload_sha256"]:
            raise WireError("payload_changed")
        Ed25519PublicKey.from_public_bytes(unb64(public_key, size=32)).verify(unb64(value["signature"], size=64), encoded)
        return loads(raw)
    except WireError:
        raise
    except Exception:
        raise WireError("signature_denied") from None
