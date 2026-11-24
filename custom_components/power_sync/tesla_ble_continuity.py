"""Authenticate a re-added ESPHome producer for an explicitly held Stop."""
from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass, field
import hmac
import json
import re
import time
from uuid import uuid4

from .tesla_ble_stop_recovery import StopRecoveryRefused

CHALLENGE_KEY = "_ble_stop_continuity_challenge"
CHALLENGE_SECONDS = 20 * 60
PURPOSE = b"PowerSync ESPHome Stop continuity v1\0"
BINDING_PURPOSE = b"PowerSync ESPHome Stop continuity binding v1\0"


def _decode_key(value):
    if not isinstance(value, str) or len(value) != 44:
        return None
    try:
        key = base64.b64decode(value, validate=True)
    except ValueError:
        return None
    return key if len(key) == 32 else None


def _mac(value):
    normalized = re.sub(r"[:-]", "", str(value or "")).lower()
    return normalized if re.fullmatch(r"[0-9a-f]{12}", normalized) else None


def _device_key(hass, config_entry):
    key = _decode_key(config_entry.data.get("noise_psk"))
    if key is None or any(
        other is not config_entry and _decode_key(other.data.get("noise_psk")) == key
        for other in hass.config_entries.async_entries("esphome")
    ):
        raise StopRecoveryRefused("bridge_requires_unique_encryption_key")
    return key


def _binding(key, salt):
    return hmac.digest(key, BINDING_PURPOSE + salt.encode("ascii"), "sha256").hex()


def released_binding_matches(bridge, proof):
    """Require the same configured key when resuming a durable release."""
    if not isinstance(proof, dict) or proof.get("current") != bridge.identity:
        return False
    original = proof.get("original")
    if (not isinstance(original, dict) or not isinstance(original.get("config_entry_id"), str)
            or bridge.hass.config_entries.async_get_entry(original["config_entry_id"]) is not None
            or _mac(getattr(bridge.config_entry, "unique_id", None)) != _mac(bridge.identity["unique_id"].split("/", 1)[0])):
        return False
    salt, binding = proof.get("salt"), proof.get("key_binding")
    if not isinstance(salt, str) or not re.fullmatch(r"[0-9a-f]{32}", salt) or not isinstance(binding, str):
        return False
    try:
        return hmac.compare_digest(_binding(_device_key(bridge.hass, bridge.config_entry), salt), binding)
    except StopRecoveryRefused:
        return False


@dataclass(frozen=True)
class Continuity:
    bridge: object
    original: dict
    refs: list
    key: bytes = field(repr=False)
    mac: str

    def current(self):
        hass = self.bridge.hass
        if hass.config_entries.async_get_entry(self.original["config_entry_id"]) is not None:
            return False
        data = self.bridge.config_entry.data
        try:
            return (
                _device_key(hass, self.bridge.config_entry) == self.key
                and _mac(self.bridge.config_entry.unique_id) == self.mac
                and self.bridge.continuity_endpoint == (data.get("host"), data.get("port", 6053))
            )
        except StopRecoveryRefused:
            return False

    def record_matches(self, entry, record):
        from . import tesla_ev_recovery as manager

        route = record.get("route") or {}
        return (
            route.get("kind") == "ble"
            and route.get("entity_id") == self.original["entity_id"]
            and route.get("entity_identity") == self.original
            and route.get("producer_identity") == manager._producer_identity(None, self.original)
            and route.get("session_anchor") is None
            and route.get("session_scope") is None
            and route.get("pairing_digest") == manager._pairing_digest(entry, route, identity=self.original)
        )

    async def authenticate(self):
        """Prove the original key works against the live node on a fresh API connection."""
        from aioesphomeapi import APIClient, APIConnectionError

        host, port = self.bridge.continuity_endpoint
        if not isinstance(host, str) or not host or type(port) is not int or not 1 <= port <= 65535:
            raise StopRecoveryRefused("bridge_api_unavailable")
        client = APIClient(host, port, None, noise_psk=base64.b64encode(self.key).decode("ascii"))
        try:
            async with asyncio.timeout(12):
                await client.connect(login=True)
                info = await client.device_info()
            if _mac(info.mac_address) != self.mac:
                raise StopRecoveryRefused("authenticated_bridge_mismatch")
        except (APIConnectionError, OSError, TimeoutError):
            raise StopRecoveryRefused("original_bridge_authentication_failed") from None
        finally:
            await client.disconnect(force=True)


def resolve_continuity(bridge, refs):
    """Only one exact historical VIN and one unknown Stop can use this action."""
    from . import tesla_ev_recovery as manager

    attempts = [ref for ref in refs if ref.get("hazard_id") is None]
    if len(attempts) != 1:
        raise StopRecoveryRefused("one_retained_ble_stop_required")
    attempt = attempts[0]
    route = attempt.get("route")
    if (attempt.get("vehicle_id") != bridge.vin or attempt.get("operation") != "stop"
            or attempt.get("outcome") != "sent_unknown" or not isinstance(route, dict)
            or route.get("kind") != "ble" or route.get("prefix") != bridge.prefix):
        raise StopRecoveryRefused("one_retained_ble_stop_required")
    original = route.get("entity_identity")
    live = bridge.identity
    if (not isinstance(original, dict) or not isinstance(original.get("config_entry_id"), str)
            or not original["config_entry_id"]
            or original["config_entry_id"] == live["config_entry_id"]
            or original != (live | {"config_entry_id": original["config_entry_id"]})
            or bridge.hass.config_entries.async_get_entry(original["config_entry_id"]) is not None):
        raise StopRecoveryRefused("original_bridge_registry_mismatch")
    for ref in refs:
        if ref is attempt:
            continue
        old_charger = (ref.get("binding") or {}).get("entities", {}).get("charger")
        if (ref.get("kind") != "ble_quarantine" or ref.get("vehicle_id") != bridge.vin
                or ref.get("attempt_id") != attempt.get("attempt_id") or ref.get("prefix") != bridge.prefix
                or old_charger != original or not manager._liability_matches_stock_scope(ref, original)):
            raise StopRecoveryRefused("related_ble_quarantine_mismatch")
    active = bridge.config_entry
    mac = _mac(getattr(active, "unique_id", None))
    if (getattr(active, "domain", None) != "esphome" or mac is None
            or _mac(live["unique_id"].split("/", 1)[0]) != mac):
        raise StopRecoveryRefused("current_bridge_registry_mismatch")
    key = _device_key(bridge.hass, active)
    bridge.continuity_endpoint = (active.data.get("host"), active.data.get("port", 6053))
    return Continuity(bridge, original, refs, key, mac)


def prepare_challenge(runtime, continuity, digest):
    """Create one in-memory challenge for the exact original and current bindings."""
    bridge = continuity.bridge
    created_at = getattr(bridge.config_entry, "created_at", None)
    if created_at is None or created_at.tzinfo is None:
        raise StopRecoveryRefused("current_bridge_creation_date_unavailable")
    salt = uuid4().hex
    payload = {
        "version": 1,
        "nonce": salt,
        "original": continuity.original,
        "current_entry_id": bridge.identity["config_entry_id"],
        "current_created_at": created_at.isoformat(),
        "mac": continuity.mac,
        "scope_digest": digest,
    }
    challenge = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode("ascii")
    runtime[CHALLENGE_KEY] = {
        "challenge": challenge, "original": continuity.original, "current": bridge.identity,
        "salt": salt, "scope_digest": digest, "expiry": time.monotonic() + CHALLENGE_SECONDS,
    }
    return {"success": True, "challenge": challenge, "expires_in_seconds": CHALLENGE_SECONDS,
            "controls_issued": False}


async def verify_challenge(runtime, continuity, digest, response):
    pending = runtime.pop(CHALLENGE_KEY, None)
    if (not pending or pending["expiry"] <= time.monotonic()
            or pending["original"] != continuity.original or pending["current"] != continuity.bridge.identity
            or pending["scope_digest"] != digest):
        raise StopRecoveryRefused("bridge_continuity_challenge_missing_or_changed")
    expected = hmac.digest(continuity.key, PURPOSE + pending["challenge"].encode("ascii"), "sha256").hex()
    if not isinstance(response, str) or not re.fullmatch(r"[0-9a-f]{64}", response) or not hmac.compare_digest(expected, response):
        raise StopRecoveryRefused("historical_bridge_key_proof_mismatch")
    await continuity.authenticate()
    if not continuity.bridge.current():
        raise StopRecoveryRefused("recovery_scope_changed")
    return {"original": continuity.original, "current": continuity.bridge.identity,
            "salt": pending["salt"], "key_binding": _binding(continuity.key, pending["salt"])}
