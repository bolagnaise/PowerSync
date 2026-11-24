"""Read-only Tesla native Charge on Solar pairing status.

Charge on Solar needs four things in place (Teslemetry's manual setup guide,
python-tesla-fleet-api PR #221):

1. the gateway's signed-commands key on the vehicle as ROLE_CHARGING_MANAGER;
2. the vehicle's key registered on the gateway as a VERIFIED VEHICLE client;
3. a managed charging site on the vehicle naming the gateway's key and DIN;
4. the Charge on Solar preference itself (``get_teslemetry_charge_on_solar``).

This module only reads. It asks the gateway over the LAN for its key and its
authorized clients, and asks the car's PowerSync BLE bridge for the vehicle
keys it learned from session info and for the car's managed charging sites.
It never adds keys, clients or sites, and it never reads or changes the
Charge on Solar preference, so it cannot affect charging ownership.

Step 1 is reported as ``not_checked``: confirming it needs a VCSEC whitelist
query the bridge does not make yet.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import math
import re
import time
from typing import Any
from uuid import uuid4

from .teslemetry_solar import SolarError

REQUEST = "text.{prefix}_powersync_charge_on_solar_pairing_request"
STATE = "sensor.{prefix}_powersync_charge_on_solar_pairing"
TIMEOUT_SECONDS = 90  # the bridge gives up at 75 s and answers "f"
POLL_SECONDS = 0.5
_VIN = re.compile(r"[A-HJ-NPR-Z0-9]{17}")
_NONCE = re.compile(r"[0-9a-f]{32}")
_ERROR = re.compile(r"[A-Za-z0-9_:. -]{1,80}")
_FINGERPRINT = re.compile(r"[0-9a-f]{16}")
_DIN = re.compile(r"[!#-\[\]-~]{0,40}")
_RAW_KEY_HEX = re.compile(r"04[0-9a-f]{128}")
KEY_FORMATS = {"h": "hex", "H": "hex_upper", "r": "base64_raw", "d": "base64_der", "e": "empty", "o": "other"}
DOMAINS = {"v": "vehicle_security", "i": "infotainment"}
SITE_MATCH_METRES = 500


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate field")
        result[key] = value
    return result


def decode_state(value: Any) -> dict[str, Any]:
    """Strictly decode the bridge's compact pairing record."""
    try:
        if not isinstance(value, str) or len(value.encode()) > 255:
            raise ValueError
        state = json.loads(value, object_pairs_hook=_unique_object)
        if not isinstance(state, dict) or state.get("v") != 1 or type(state.get("v")) is not int:
            raise ValueError
        if not isinstance(state.get("i"), str) or not _VIN.fullmatch(state["i"]):
            raise ValueError
        if state.get("m") not in {"i", "w", "k", "f"}:
            raise ValueError
        nonce = state.get("n")
        if not isinstance(nonce, str) or (nonce != "" and not _NONCE.fullmatch(nonce)):
            raise ValueError
        if state["m"] != "i" and (nonce == "" or state.get("r") not in {"s", "k"}):
            raise ValueError
        if state["m"] == "f" and not (isinstance(state.get("err"), str) and _ERROR.fullmatch(state["err"])):
            raise ValueError
        if state["m"] == "k" and state["r"] == "k":
            if state.get("d") not in DOMAINS or not (isinstance(state.get("k"), str)
                                                       and _RAW_KEY_HEX.fullmatch(state["k"])):
                raise ValueError
        if state["m"] == "k" and state["r"] == "s":
            if type(state.get("c")) is not int or state["c"] < 0 or state.get("t", 0) not in (0, 1):
                raise ValueError
            sites = state.get("s")
            if not isinstance(sites, list) or len(sites) > state["c"]:
                raise ValueError
            if state.get("t", 0) == 0 and len(sites) != state["c"]:
                raise ValueError
            for site in sites:
                if not isinstance(site, list) or len(site) not in (3, 5):
                    raise ValueError
                fmt, fingerprint, din = site[:3]
                if fmt not in KEY_FORMATS or not isinstance(fingerprint, str) \
                        or not _FINGERPRINT.fullmatch(fingerprint) \
                        or not isinstance(din, str) or not _DIN.fullmatch(din):
                    raise ValueError
                if len(site) == 5:
                    lat, lon = site[3:]
                    if type(lat) is not int or type(lon) is not int \
                            or abs(lat) > 9_000_000 or abs(lon) > 18_000_000:
                        raise ValueError
        return state
    except (ValueError, TypeError) as error:
        raise SolarError("Tesla BLE bridge reported an invalid Charge on Solar pairing state") from error


def fingerprint(text: str) -> str:
    """The bridge's fingerprint: SHA-256 of the key string's exact bytes."""
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def gateway_key_point(key: bytes) -> bytes:
    """Return the gateway key as a raw uncompressed P-256 point."""
    if len(key) == 65 and key[0] == 0x04:
        return bytes(key)
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    loaded = serialization.load_der_public_key(bytes(key))
    if not isinstance(loaded, ec.EllipticCurvePublicKey) or loaded.curve.name != "secp256r1":
        raise SolarError("The gateway's signed-commands key is not a P-256 key")
    return loaded.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)


def gateway_key_encodings(point: bytes) -> dict[str, str]:
    """Every text encoding a site's public_key string might use, by format."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    der = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), point).public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    return {
        "hex": point.hex(),
        "hex_upper": point.hex().upper(),
        "base64_raw": base64.b64encode(point).decode(),
        "base64_der": base64.b64encode(der).decode(),
    }


def _identity(hass, entity_id):
    from .tesla_ev_recovery import _identity as identity

    return identity(hass, entity_id)


class PairingBridge:
    """The pairing entities on the BLE bridge already bound to ``vin``."""

    def __init__(self, hass, solar_bridge):
        self.hass, self.prefix, self.vin = hass, solar_bridge.prefix, solar_bridge.vin
        self.solar_bridge = solar_bridge
        self.request_id = REQUEST.format(prefix=self.prefix)
        self.state_id = STATE.format(prefix=self.prefix)

    def _state(self) -> dict[str, Any] | None:
        state = self.hass.states.get(self.state_id)
        if state is None:
            return None
        try:
            return decode_state(state.state)
        except SolarError:
            return None

    def available(self) -> bool:
        """Both pairing entities sit on the same ESPHome device as the solar ones."""
        state = self._state()
        if not (state and state["i"] == self.vin and self.hass.states.get(self.request_id) is not None):
            return False
        ids = [self.request_id, self.state_id, self.solar_bridge.state_id]
        identities = [_identity(self.hass, entity_id) for entity_id in ids]
        if any(value is None or value.get("platform") != "esphome" for value in identities):
            return False
        first = identities[0]
        return all(value.get(key) == first.get(key) for value in identities
                   for key in ("device_id", "config_entry_id"))

    async def _request(self, text: str, kind: str, nonce: str) -> dict[str, Any]:
        await self.hass.services.async_call(
            "text", "set_value", {"entity_id": self.request_id, "value": text}, blocking=True)
        deadline = time.monotonic() + TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            state = self._state()
            if state is not None and state["n"] == nonce:
                if state["i"] != self.vin or state["r"] != kind:
                    raise SolarError("Tesla BLE bridge answered for a different vehicle or request")
                if state["m"] == "k":
                    return state
                if state["m"] == "f":
                    raise SolarError(f"Tesla BLE pairing read failed ({state['err']})")
            await asyncio.sleep(POLL_SECONDS)
        raise SolarError("Tesla BLE pairing read timed out")

    async def vehicle_key(self, domain: str) -> bytes:
        """The car's public key for one domain, from the bridge's session info."""
        nonce = uuid4().hex
        state = await self._request(f"key:{nonce}:{domain}", "k", nonce)
        if state["d"] != domain:
            raise SolarError("Tesla BLE bridge answered for a different domain")
        return bytes.fromhex(state["k"])

    async def managed_sites(self) -> dict[str, Any]:
        nonce = uuid4().hex
        state = await self._request(f"sit:{nonce}", "s", nonce)
        sites = []
        for site in state["s"]:
            entry = {"key_format": KEY_FORMATS[site[0]], "key_fingerprint": site[1], "din": site[2]}
            if len(site) == 5:
                entry["latitude"], entry["longitude"] = site[3] / 1e5, site[4] / 1e5
            sites.append(entry)
        return {"count": state["c"], "complete": state.get("t", 0) == 0, "sites": sites}


def _distance_m(lat1, lon1, lat2, lon2) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6_371_000 * math.asin(math.sqrt(a))


def site_step(sites: dict[str, Any] | None, gateway: dict[str, Any] | None,
              home: tuple[float, float] | None) -> dict[str, Any]:
    """Classify the car's managed charging sites against this gateway."""
    if sites is None:
        return {"status": "unknown"}
    result: dict[str, Any] = {"site_count": sites["count"], "complete": sites["complete"],
                              "sites": sites["sites"]}
    if gateway is None:
        result["status"] = "unknown" if sites["count"] else "missing"
        return result
    by_print = {fingerprint(text): name for name, text in gateway["encodings"].items()}
    match = None
    for site in sites["sites"]:
        if site["key_fingerprint"] in by_print:
            match = site
            site["matches_gateway_key"] = by_print[site["key_fingerprint"]]
        if home is not None and "latitude" in site:
            site["distance_from_home_m"] = round(_distance_m(home[0], home[1], site["latitude"], site["longitude"]))
    if match is not None:
        result["status"] = "present"
        result["key_format"] = match["matches_gateway_key"]
        result["din_matches"] = match["din"] == gateway["din"]
        if "distance_from_home_m" in match:
            result["near_home"] = match["distance_from_home_m"] <= SITE_MATCH_METRES
    elif sites["count"] and not sites["complete"]:
        result["status"] = "unknown"  # the bridge could not report every site
    else:
        # Sites naming this DIN with a different key are worth surfacing.
        same_din = any(site["din"] == gateway["din"] for site in sites["sites"])
        result["status"] = "key_mismatch" if same_din else "missing"
    return result


def vehicle_client_step(clients: dict[str, Any] | None, vin: str,
                        vehicle_keys: dict[str, bytes]) -> dict[str, Any]:
    """Classify the gateway's authorized VEHICLE client for ``vin``."""
    if clients is None:
        return {"status": "unknown"}
    records = [client for client in clients.get("clients", [])
               if client.get("type") == "VEHICLE" and client.get("identifier") == vin]
    if not records:
        return {"status": "missing"}
    by_key = {base64.b64encode(key).decode(): DOMAINS[domain] for domain, key in vehicle_keys.items()}
    verified = [record for record in records if record.get("state") == "VERIFIED"]
    record = (verified or records)[0]
    result = {"status": "verified" if verified else "not_verified", "state": record.get("state"),
              "client_count": len(records)}
    if vehicle_keys:
        result["matches_vehicle_key"] = by_key.get(record.get("public_key"), "neither")
    return result


def local_client(hass, entry):
    """The entry's local Powerwall client, when local signed access is usable."""
    from .const import DOMAIN
    from .powerwall_local.client import is_loopback_host

    runtime = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
    local = runtime.get("powerwall_local") or {}
    client = local.get("client") or getattr(local.get("coordinator"), "client", None)
    if client is None or getattr(client, "local_access_enabled", True) is False:
        return None
    # A loopback host is a cloud-relay shim, not the gateway on the LAN.
    if is_loopback_host(getattr(client, "host", None)):
        return None
    if getattr(client, "_transport", None) is None or not getattr(client, "_din", None):
        return None
    return client


async def pairing_status(hass, entry, vin: str) -> dict[str, Any]:
    """Collect read-only evidence for each Charge on Solar pairing step."""
    from .tesla_ble_solar import find_bridge

    errors: list[str] = []
    gateway = None
    clients = None
    client = local_client(hass, entry)
    if client is None:
        errors.append("Local Powerwall signed access is not configured for this entry")
    else:
        try:
            key = await client.get_signed_commands_public_key()
            if key is None:
                errors.append("The gateway did not return its signed-commands key")
            else:
                point = gateway_key_point(key)
                gateway = {"din": client._din, "key_fingerprint": fingerprint(point.hex()),
                           "encodings": gateway_key_encodings(point)}
        except (SolarError, ValueError) as err:
            errors.append(str(err) or "The gateway's signed-commands key could not be read")
        clients = await client.list_authorized_clients()
        if clients is None:
            errors.append("The gateway did not return its authorized clients")

    vehicle_keys: dict[str, bytes] = {}
    sites = None
    solar_bridge = find_bridge(hass, entry, vin)
    bridge = PairingBridge(hass, solar_bridge) if solar_bridge is not None else None
    if bridge is None or not bridge.available():
        errors.append("No PowerSync BLE bridge with the Charge on Solar pairing entities reports this VIN")
    else:
        for domain in DOMAINS:
            try:
                vehicle_keys[domain] = await bridge.vehicle_key(domain)
            except SolarError as err:
                errors.append(f"{DOMAINS[domain]} key: {err}")
        try:
            sites = await bridge.managed_sites()
        except SolarError as err:
            errors.append(f"Managed charging sites: {err}")

    home = None
    if hass.config.latitude is not None and hass.config.longitude is not None:
        home = (float(hass.config.latitude), float(hass.config.longitude))
    keys = {DOMAINS[domain]: key.hex() for domain, key in vehicle_keys.items()}
    if len(vehicle_keys) == 2:
        keys["domains_share_key"] = vehicle_keys["v"] == vehicle_keys["i"]
    return {
        "vin": vin,
        "gateway": None if gateway is None else {"din": gateway["din"], "key_fingerprint": gateway["key_fingerprint"]},
        "vehicle_keys": keys,
        "steps": {
            "gateway_key_on_vehicle": {"status": "not_checked"},
            "vehicle_on_gateway": vehicle_client_step(clients, vin, vehicle_keys),
            "managed_charging_site": site_step(sites, gateway, home),
            "charge_on_solar": {"status": "not_read",
                                "note": "Use power_sync.get_teslemetry_charge_on_solar to read the preference"},
        },
        "errors": errors,
    }


def register_services(hass):
    import voluptuous as vol
    from homeassistant.core import SupportsResponse
    from homeassistant.exceptions import HomeAssistantError
    from .const import DOMAIN

    lock = asyncio.Lock()

    async def handle(call):
        entries = [e for e in hass.config_entries.async_entries(DOMAIN)
                   if e.entry_id == call.data["entry_id"] and e.entry_id in hass.data.get(DOMAIN, {})]
        if len(entries) != 1:
            raise HomeAssistantError("Select a loaded PowerSync entry")
        vin = call.data["vin"].upper()
        if not _VIN.fullmatch(vin):
            raise HomeAssistantError("A valid vehicle VIN is required")
        if lock.locked():
            raise HomeAssistantError("A Charge on Solar pairing status read is already running")
        async with lock:
            return await pairing_status(hass, entries[0], vin)

    hass.services.async_register(
        "power_sync", "get_charge_on_solar_pairing_status", handle,
        schema=vol.Schema({vol.Required("entry_id"): str, vol.Required("vin"): str}),
        supports_response=SupportsResponse.ONLY)
