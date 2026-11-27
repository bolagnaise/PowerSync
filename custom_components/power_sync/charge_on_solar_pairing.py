"""Tesla native Charge on Solar pairing: status reads and explicit repairs.

Charge on Solar needs four things in place (Teslemetry's manual setup guide,
python-tesla-fleet-api PR #221):

1. the gateway's signed-commands key on the vehicle as ROLE_CHARGING_MANAGER;
2. the vehicle's key registered on the gateway as a VERIFIED VEHICLE client;
3. a managed charging site on the vehicle naming the gateway's key and DIN;
4. the Charge on Solar preference itself (``get_teslemetry_charge_on_solar``).

``get_charge_on_solar_pairing_status`` only reads: the gateway's key and
authorized clients over the LAN, and through the car's PowerSync BLE bridge
the vehicle keys it learned from session info, the car's managed charging
sites and its whitelist entry for the gateway's key.

``repair_charge_on_solar_pairing`` performs one explicitly confirmed step and
returns its readback. Step 1 is an unsigned key add the car only accepts after
a key-card tap, so no owner key is ever held by PowerSync or the bridge. Step 2
is signed with this installation's verified gateway RSA key. Step 3 is a BLE
CarServer action. Nothing touches the Charge on Solar preference or
PowerSync's EV control holds, and nothing is retried after a final result.
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


class PairingUncertain(SolarError):
    """A pairing write may have been applied but was not confirmed."""

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
CHARGING_MANAGER_ROLE = 6  # Keys.Role.ROLE_CHARGING_MANAGER
# Phases each record kind may report: s sites, k vehicle key, e whitelist
# entry (reads); a key add, t site add, q site remove (writes). "p" means a key
# add left the bridge and the car is waiting for a key-card tap. "u" means a
# write may have been applied but was not confirmed.
PHASES = {"s": {"w", "k", "f"}, "k": {"w", "k", "f"}, "e": {"w", "k", "f"},
          "a": {"w", "p", "f"}, "t": {"w", "k", "f", "u"}, "q": {"w", "k", "f", "u"}}
SITE_OUTCOMES = {0: None, 1: "added_or_updated", 2: "existing_site_nearby", 3: "acknowledged"}


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
        if state.get("m") not in {"i", "w", "k", "f", "u", "p"}:
            raise ValueError
        nonce = state.get("n")
        if not isinstance(nonce, str) or (nonce != "" and not _NONCE.fullmatch(nonce)):
            raise ValueError
        kind = state.get("r")
        if state["m"] != "i" and (nonce == "" or state["m"] not in PHASES.get(kind, ())):
            raise ValueError
        if state["m"] in {"f", "u"} and not (isinstance(state.get("err"), str) and _ERROR.fullmatch(state["err"])):
            raise ValueError
        if state["m"] == "u" and state.get("a", 0) not in (0, 1):
            raise ValueError
        if state["m"] == "k" and kind == "k":
            if state.get("d") not in DOMAINS or not (isinstance(state.get("k"), str)
                                                       and _RAW_KEY_HEX.fullmatch(state["k"])):
                raise ValueError
        if state["m"] == "k" and kind == "e":
            if state.get("p") not in (0, 1) or type(state["p"]) is not int:
                raise ValueError
            if state["p"] == 1 and (type(state.get("o")) is not int or not 0 <= state["o"] <= 255):
                raise ValueError
        if state["m"] == "k" and kind == "t" and state.get("x") not in (0, 1, 2, 3):
            raise ValueError
        if state["m"] == "k" and kind in {"s", "t", "q"}:
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

    async def _request(self, text: str, kind: str, nonce: str, *, write: bool = False,
                       done: frozenset = frozenset({"k"})) -> dict[str, Any]:
        label = "write" if write else "read"
        try:
            await self.hass.services.async_call(
                "text", "set_value", {"entity_id": self.request_id, "value": text}, blocking=True)
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - normalised for the request gate
            # A write the bridge never received changed nothing.
            raise SolarError(f"Tesla BLE pairing {label} could not be sent to the bridge") from error
        deadline = time.monotonic() + TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            state = self._state()
            if state is not None and state["n"] == nonce:
                if state["i"] != self.vin or state["r"] != kind:
                    raise SolarError("Tesla BLE bridge answered for a different vehicle or request")
                if state["m"] in done:
                    return state
                if state["m"] == "f":
                    suffix = "; nothing was changed" if write else ""
                    raise SolarError(f"Tesla BLE pairing {label} failed ({state['err']}){suffix}")
                if state["m"] == "u":
                    raise PairingUncertain(f"Tesla BLE pairing write could not be confirmed ({state['err']})")
            await asyncio.sleep(POLL_SECONDS)
        if write:
            raise PairingUncertain("Tesla BLE pairing write could not be confirmed (timeout)")
        raise SolarError("Tesla BLE pairing read timed out")

    async def vehicle_key(self, domain: str) -> bytes:
        """The car's public key for one domain, from the bridge's session info."""
        nonce = uuid4().hex
        state = await self._request(f"key:{nonce}:{domain}", "k", nonce)
        if state["d"] != domain:
            raise SolarError("Tesla BLE bridge answered for a different domain")
        return bytes.fromhex(state["k"])

    @staticmethod
    def _sites(state: dict[str, Any]) -> dict[str, Any]:
        sites = []
        for site in state["s"]:
            entry = {"key_format": KEY_FORMATS[site[0]], "key_fingerprint": site[1], "din": site[2]}
            if len(site) == 5:
                entry["latitude"], entry["longitude"] = site[3] / 1e5, site[4] / 1e5
            sites.append(entry)
        return {"count": state["c"], "complete": state.get("t", 0) == 0, "sites": sites}

    async def managed_sites(self) -> dict[str, Any]:
        nonce = uuid4().hex
        return self._sites(await self._request(f"sit:{nonce}", "s", nonce))

    async def whitelist_role(self, key: bytes) -> int | None:
        """The car's role for ``key``, or None if the car did not return the entry.

        None is not proof of absence: the car only says it returned no entry.
        """
        nonce = uuid4().hex
        try:
            state = await self._request(f"ent:{nonce}:{_point_hex(key)}", "e", nonce)
        except SolarError as err:
            if "entry_not_returned" in str(err):
                return None
            raise
        return state["o"] if state["p"] == 1 else None

    async def add_gateway_key(self, key: bytes) -> None:
        """Send the one-shot charging-manager key add; the car then awaits a card tap."""
        nonce = uuid4().hex
        await self._request(f"wla:{nonce}:{_point_hex(key)}", "a", nonce, write=True, done=frozenset({"p"}))

    async def add_site(self, key_text: str, din: str, latitude: float, longitude: float) -> dict[str, Any]:
        """Add a managed charging site; returns the car's own readback."""
        lat, lon = round(latitude * 1e5), round(longitude * 1e5)
        if abs(lat) > 9_000_000 or abs(lon) > 18_000_000:
            raise SolarError("Site coordinates are out of range")
        if not _DIN.fullmatch(din) or not din or ":" in key_text or not key_text:
            raise SolarError("Invalid gateway DIN or key")
        nonce = uuid4().hex
        text = f"ste:{nonce}:{lat}:{lon}:{din}:{key_text}"
        if len(text) > 255:
            raise SolarError("The site request is too long for the bridge")
        state = await self._request(text, "t", nonce, write=True)
        return {**self._sites(state), "outcome": SITE_OUTCOMES[state["x"]]}

    async def remove_site(self, key_text: str) -> dict[str, Any]:
        """Remove the managed charging site with this key; returns the readback."""
        if ":" in key_text or not key_text:
            raise SolarError("Invalid site key")
        nonce = uuid4().hex
        return self._sites(await self._request(f"rms:{nonce}:{key_text}", "q", nonce, write=True))


def _point_hex(key: bytes) -> str:
    if len(key) != 65 or key[0] != 0x04:
        raise SolarError("Expected an uncompressed P-256 public key")
    return bytes(key).hex()


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
    runtime = hass.data.get("power_sync", {}).get(entry.entry_id, {})
    local = runtime.get("powerwall_local") or {}
    client = local.get("client") or getattr(local.get("coordinator"), "client", None)
    if client is None or getattr(client, "local_access_enabled", True) is False:
        return None
    from .powerwall_local.client import is_loopback_host

    # A loopback host is a cloud-relay shim, not the gateway on the LAN.
    if is_loopback_host(getattr(client, "host", None)):
        return None
    if getattr(client, "_transport", None) is None or not getattr(client, "_din", None):
        return None
    return client


def gateway_key_step(role: int | None, checked: bool) -> dict[str, Any]:
    """Classify the car's whitelist entry for the gateway's key."""
    if not checked:
        return {"status": "unknown"}
    if role is None:
        # The car returned no entry, which is not proof the key is absent.
        return {"status": "not_confirmed"}
    if role == CHARGING_MANAGER_ROLE:
        return {"status": "present", "role": role}
    return {"status": "wrong_role", "role": role}


async def _gateway(client, errors: list[str]) -> dict[str, Any] | None:
    try:
        key = await client.get_signed_commands_public_key()
    except (SolarError, ValueError) as err:
        errors.append(str(err) or "The gateway's signed-commands key could not be read")
        return None
    if key is None:
        errors.append("The gateway did not return its signed-commands key")
        return None
    try:
        point = gateway_key_point(key)
    except (SolarError, ValueError) as err:
        errors.append(str(err) or "The gateway's signed-commands key is not usable")
        return None
    return {"din": client._din, "point": point, "key_fingerprint": fingerprint(point.hex()),
            "encodings": gateway_key_encodings(point)}


def _bridge(hass, entry, vin):
    from .tesla_ble_solar import find_bridge

    solar_bridge = find_bridge(hass, entry, vin)
    bridge = PairingBridge(hass, solar_bridge) if solar_bridge is not None else None
    return bridge if bridge is not None and bridge.available() else None


def _home(hass) -> tuple[float, float] | None:
    if hass.config.latitude is None or hass.config.longitude is None:
        return None
    return float(hass.config.latitude), float(hass.config.longitude)


async def pairing_status(hass, entry, vin: str) -> dict[str, Any]:
    """Collect read-only evidence for each Charge on Solar pairing step."""
    errors: list[str] = []
    gateway = None
    clients = None
    client = local_client(hass, entry)
    if client is None:
        errors.append("Local Powerwall signed access is not configured for this entry")
    else:
        gateway = await _gateway(client, errors)
        clients = await client.list_authorized_clients()
        if clients is None:
            errors.append("The gateway did not return its authorized clients")

    vehicle_keys: dict[str, bytes] = {}
    sites = None
    role, role_checked = None, False
    bridge = _bridge(hass, entry, vin)
    if bridge is None:
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
        if gateway is not None:
            try:
                role, role_checked = await bridge.whitelist_role(gateway["point"]), True
            except SolarError as err:
                errors.append(f"Gateway key on vehicle: {err}")

    keys = {DOMAINS[domain]: key.hex() for domain, key in vehicle_keys.items()}
    if len(vehicle_keys) == 2:
        keys["domains_share_key"] = vehicle_keys["v"] == vehicle_keys["i"]
    return {
        "vin": vin,
        "gateway": None if gateway is None else {"din": gateway["din"], "key_fingerprint": gateway["key_fingerprint"]},
        "vehicle_keys": keys,
        "steps": {
            "gateway_key_on_vehicle": gateway_key_step(role, role_checked),
            "vehicle_on_gateway": vehicle_client_step(clients, vin, vehicle_keys),
            "managed_charging_site": site_step(sites, gateway, _home(hass)),
            "charge_on_solar": {"status": "not_read",
                                "note": "Use power_sync.get_teslemetry_charge_on_solar to read the preference"},
        },
        "errors": errors,
    }


# ---- repair (writes) ---------------------------------------------------------

KEY_CARD_WAIT_SECONDS = 90
KEY_CARD_POLL_SECONDS = 5
REPAIR_STEPS = ("gateway_key_on_vehicle", "vehicle_on_gateway", "managed_charging_site",
                "remove_managed_charging_site")


def _require(value, message):
    if value is None:
        raise SolarError(message)
    return value


async def _repair_gateway_key(bridge, gateway) -> dict[str, Any]:
    point = gateway["point"]
    role = await bridge.whitelist_role(point)
    if role == CHARGING_MANAGER_ROLE:
        return {"result": "already_present", "status": gateway_key_step(role, True)}
    if role is not None:
        raise SolarError(f"The gateway's key is already on the car with role {role}. Remove it from the "
                         "car's Locks menu before adding it as a charging manager")
    await bridge.add_gateway_key(point)
    # The car holds the request until a key card is tapped on the reader.
    deadline = time.monotonic() + KEY_CARD_WAIT_SECONDS
    while time.monotonic() < deadline:
        await asyncio.sleep(KEY_CARD_POLL_SECONDS)
        try:
            role = await bridge.whitelist_role(point)
        except SolarError:
            continue  # e.g. the bridge was busy; the next read decides
        if role is not None:
            if role != CHARGING_MANAGER_ROLE:
                raise PairingUncertain(f"The car added the gateway's key with role {role}, not charging manager")
            return {"result": "confirmed", "status": gateway_key_step(role, True)}
    raise PairingUncertain("The add request was sent, but the car has not confirmed the key. Tap a key card "
                           "on the car's reader if you have not, then read the pairing status")


def _vehicle_key(vehicle_keys: dict[str, bytes], domain: str | None) -> bytes:
    if domain is not None:
        return _require(vehicle_keys.get(domain), f"The bridge has no {DOMAINS[domain]} key for this car")
    if len(vehicle_keys) == 2 and vehicle_keys["v"] == vehicle_keys["i"]:
        return vehicle_keys["v"]
    if len(vehicle_keys) == 2:
        raise SolarError("The car uses different vehicle-security and infotainment keys. "
                         "Choose vehicle_key_domain")
    raise SolarError("The bridge has not learned both of this car's keys yet; wake the car and try again")


async def _repair_vehicle_client(client, bridge, vin: str, domain: str | None) -> dict[str, Any]:
    vehicle_keys = {}
    for name in DOMAINS:
        try:
            vehicle_keys[name] = await bridge.vehicle_key(name)
        except SolarError:
            pass
    key = _vehicle_key(vehicle_keys, domain)
    encoded = base64.b64encode(key).decode()
    before = _require(await client.list_authorized_clients(), "The gateway did not return its authorized clients")
    records = [c for c in before.get("clients", []) if c.get("type") == "VEHICLE" and c.get("identifier") == vin]
    if any(c.get("public_key") == encoded and c.get("state") == "VERIFIED" for c in records):
        return {"result": "already_present", "status": vehicle_client_step(before, vin, vehicle_keys)}
    if records:
        raise SolarError("The gateway already lists this car with another key or state. Remove the car's "
                         "Powerwall pairing in the Tesla app before registering it again")
    answered = await client.add_vehicle_client(vin, key)
    if answered is False:
        raise SolarError("The gateway refused to register the car; nothing was changed")
    after = await client.list_authorized_clients()
    status = vehicle_client_step(after, vin, vehicle_keys)
    if status.get("status") == "verified" and status.get("matches_vehicle_key") not in (None, "neither"):
        return {"result": "confirmed", "status": status}
    raise PairingUncertain("The registration could not be confirmed on the gateway; read the pairing status "
                           "before trying again")


def site_in_place(status: dict[str, Any]) -> bool:
    """A site counts only if its key matches and its DIN and location are not known to be wrong."""
    return (status.get("status") == "present" and status.get("din_matches") is not False
            and status.get("near_home") is not False)


async def _repair_site(bridge, gateway, key_format: str | None, location) -> dict[str, Any]:
    sites = await bridge.managed_sites()
    current = site_step(sites, gateway, location)
    if site_in_place(current):
        return {"result": "already_present", "status": current}
    if current["status"] == "unknown":
        raise SolarError("The bridge could not report every managed site; nothing was changed")
    if current["status"] == "present":
        # This gateway's site has the wrong DIN or location: send it again
        # with the requested details, in the encoding the car already stores.
        key_format = current["key_format"]
    if key_format is None:
        raise SolarError("Choose key_format: the car's expected encoding of the gateway key is undocumented "
                         "and no existing site shows it")
    location = _require(location, "Site latitude and longitude are required (HA has no home location)")
    readback = await bridge.add_site(gateway["encodings"][key_format], gateway["din"], *location)
    status = site_step(readback, gateway, location)
    status["outcome"] = readback["outcome"]
    if site_in_place(status):
        return {"result": "confirmed", "status": status}
    if readback["outcome"] == "existing_site_nearby":
        raise SolarError("The car kept an existing managed site near this location instead of adding one. "
                         "Remove that site first")
    if status["status"] == "present":
        raise PairingUncertain("The car acknowledged the site, but its readback still shows the wrong DIN or "
                               "location. Remove the site and add it again")
    raise PairingUncertain("The car acknowledged the site but its readback does not show it")


async def _remove_site(bridge, gateway, location) -> dict[str, Any]:
    current = site_step(await bridge.managed_sites(), gateway, location)
    if current["status"] != "present":
        if current["status"] == "unknown":
            raise SolarError("The bridge could not report every managed site; nothing was changed")
        return {"result": "already_absent", "status": current}
    readback = await bridge.remove_site(gateway["encodings"][current["key_format"]])
    status = site_step(readback, gateway, location)
    if status["status"] in {"missing", "key_mismatch"}:
        return {"result": "confirmed", "status": status}
    raise PairingUncertain("The car acknowledged the removal but its readback still shows the site")


async def repair_pairing(hass, entry, vin: str, step: str, *, key_format: str | None = None,
                         vehicle_key_domain: str | None = None,
                         latitude: float | None = None, longitude: float | None = None) -> dict[str, Any]:
    """Perform one explicit pairing write and return its readback."""
    errors: list[str] = []
    client = _require(local_client(hass, entry), "Local Powerwall signed access is not configured for this entry")
    bridge = _require(_bridge(hass, entry, vin),
                      "No PowerSync BLE bridge with the Charge on Solar pairing entities reports this VIN")
    if step == "vehicle_on_gateway":
        result = await _repair_vehicle_client(client, bridge, vin, vehicle_key_domain)
    else:
        gateway = _require(await _gateway(client, errors), "; ".join(errors) or "The gateway key is unavailable")
        if (latitude is None) != (longitude is None):
            raise SolarError("Give both latitude and longitude, or neither")
        location = (latitude, longitude) if latitude is not None else _home(hass)
        if step == "gateway_key_on_vehicle":
            result = await _repair_gateway_key(bridge, gateway)
        elif step == "managed_charging_site":
            result = await _repair_site(bridge, gateway, key_format, location)
        elif step == "remove_managed_charging_site":
            result = await _remove_site(bridge, gateway, location)
        else:
            raise SolarError("Unknown pairing step")
    return {"vin": vin, "step": step, **result}


def pairing_lock(hass) -> asyncio.Lock:
    """One pairing request at a time, shared by the services and the mobile API."""
    return hass.data.setdefault("power_sync_cos_pairing_lock", asyncio.Lock())


def pairing_supported(hass, entry, vin: str) -> bool:
    """Whether status and repair can run, without contacting the car or gateway."""
    return local_client(hass, entry) is not None and _bridge(hass, entry, vin) is not None


def loaded_entry(hass, entry_id):
    from .const import DOMAIN

    entries = [e for e in hass.config_entries.async_entries(DOMAIN)
               if e.entry_id == entry_id and e.entry_id in hass.data.get(DOMAIN, {})]
    if len(entries) != 1:
        raise SolarError("Select a loaded PowerSync entry")
    return entries[0]


def monitoring_mode(entry) -> bool:
    from .const import CONF_MONITORING_MODE

    return bool(entry.options.get(CONF_MONITORING_MODE, entry.data.get(CONF_MONITORING_MODE, False)))


async def run_request(hass, entry_id, vin, action: str, **options) -> dict[str, Any]:
    """Validate and run one status read or repair; raises SolarError."""
    entry = loaded_entry(hass, entry_id)
    vin = str(vin or "").upper()
    if not _VIN.fullmatch(vin):
        raise SolarError("A valid vehicle VIN is required")
    if action not in ("status", "repair"):
        raise SolarError("Select status or repair")
    if action == "repair":
        if options.get("step") not in REPAIR_STEPS:
            raise SolarError("Select a pairing step")
        if options.get("confirm") is not True:
            raise SolarError("Confirm that PowerSync may change this car's and Powerwall's Charge on Solar pairing")
        if monitoring_mode(entry):
            raise SolarError("Pairing changes are blocked in monitoring mode")
    lock = pairing_lock(hass)
    if lock.locked():
        raise SolarError("A Charge on Solar pairing request is already running")
    async with lock:
        if action == "status":
            return await pairing_status(hass, entry, vin)
        return await repair_pairing(
            hass, entry, vin, options["step"], key_format=options.get("key_format"),
            vehicle_key_domain=options.get("vehicle_key_domain"),
            latitude=options.get("latitude"), longitude=options.get("longitude"))


KEY_FORMAT_CHOICES = ("hex", "hex_upper", "base64_raw", "base64_der")


def register_services(hass):
    import voluptuous as vol
    from homeassistant.core import SupportsResponse
    from homeassistant.exceptions import HomeAssistantError

    async def status(call):
        try:
            return await run_request(hass, call.data["entry_id"], call.data["vin"], "status")
        except SolarError as err:
            raise HomeAssistantError(str(err)) from err

    async def repair(call):
        try:
            return await run_request(hass, call.data["entry_id"], call.data["vin"], "repair", **{
                key: call.data.get(key) for key in
                ("step", "confirm", "key_format", "vehicle_key_domain", "latitude", "longitude")})
        except SolarError as err:
            raise HomeAssistantError(str(err)) from err

    base = {vol.Required("entry_id"): str, vol.Required("vin"): str}
    hass.services.async_register(
        "power_sync", "get_charge_on_solar_pairing_status", status,
        schema=vol.Schema(base), supports_response=SupportsResponse.ONLY)
    hass.services.async_register(
        "power_sync", "repair_charge_on_solar_pairing", repair,
        schema=vol.Schema({
            **base,
            vol.Required("step"): vol.In(REPAIR_STEPS),
            vol.Optional("confirm", default=False): bool,
            vol.Optional("key_format"): vol.In(KEY_FORMAT_CHOICES),
            vol.Optional("vehicle_key_domain"): vol.In(tuple(DOMAINS)),
            vol.Optional("latitude"): vol.All(vol.Coerce(float), vol.Range(min=-90, max=90)),
            vol.Optional("longitude"): vol.All(vol.Coerce(float), vol.Range(min=-180, max=180)),
        }),
        supports_response=SupportsResponse.OPTIONAL)


def _number(value, low, high, name):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high:
        raise SolarError(f"{name} must be a number from {low} to {high}")
    return float(value)


def register_http_view(hass):
    """Authenticated mobile access; every request is explicit and gated as above."""
    from homeassistant.components.http import HomeAssistantView

    class PairingView(HomeAssistantView):
        url = "/api/power_sync/ev/native_solar/pairing"
        name = "api:power_sync:ev:native_solar:pairing"
        requires_auth = True

        async def post(self, request):
            try:
                body = await request.json()
            except ValueError:
                body = None
            try:
                if not isinstance(body, dict):
                    raise SolarError("A request object is required")
                options = {}
                if body.get("action") == "repair":
                    key_format = body.get("key_format")
                    if key_format is not None and key_format not in KEY_FORMAT_CHOICES:
                        raise SolarError("Unknown key_format")
                    domain = body.get("vehicle_key_domain")
                    if domain is not None and domain not in DOMAINS:
                        raise SolarError("Unknown vehicle_key_domain")
                    options = {"step": body.get("step"), "confirm": body.get("confirm"),
                               "key_format": key_format, "vehicle_key_domain": domain,
                               "latitude": _number(body.get("latitude"), -90, 90, "latitude"),
                               "longitude": _number(body.get("longitude"), -180, 180, "longitude")}
                result = await run_request(hass, body.get("entry_id"), body.get("vin"),
                                           body.get("action"), **options)
                return self.json({"success": True, **result})
            except SolarError as err:
                # Answered with 200: these messages carry instructions the app
                # must show whole, and its client truncates non-2xx bodies.
                code = "pairing_uncertain" if isinstance(err, PairingUncertain) else "pairing_refused"
                return self.json({"success": False, "error": str(err), "code": code})

    hass.http.register_view(PairingView())
