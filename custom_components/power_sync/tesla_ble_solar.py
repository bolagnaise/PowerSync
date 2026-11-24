"""Native Tesla Charge on Solar through the PowerSync BLE companion firmware.

The optional ``esphome/charge-on-solar.yaml`` package adds two entities to a
Pedro-family bridge running the PowerSync ``tesla_ble_vehicle`` component:

* ``text.<prefix>_powersync_charge_on_solar_request`` accepts
  ``get:<nonce>`` or ``set:<nonce>:<0|1>:<lower>:<upper>``.
* ``sensor.<prefix>_powersync_charge_on_solar`` publishes the bridge's own VIN
  and the result for the latest nonce.

A write's result includes the car's own readback, so callers must not issue
a second read. The nonce is the freshness proof: a state that does not echo
this request's random nonce is never accepted as its answer.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any
from uuid import uuid4

from .teslemetry_solar import SolarError, validate_settings

REQUEST = "text.{prefix}_powersync_charge_on_solar_request"
STATE = "sensor.{prefix}_powersync_charge_on_solar"
TIMEOUT_SECONDS = 90  # firmware gives up at 75 s and answers "u" or "f"
POLL_SECONDS = 0.5
_VIN = re.compile(r"[A-HJ-NPR-Z0-9]{17}")
_NONCE = re.compile(r"[0-9a-f]{32}")
_ERROR = re.compile(r"[A-Za-z0-9_:. -]{1,80}")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate field")
        result[key] = value
    return result


def decode_state(value: Any) -> dict[str, Any]:
    """Strictly decode the bridge's compact result record."""
    try:
        if not isinstance(value, str) or len(value.encode()) > 255:
            raise ValueError
        state = json.loads(value, object_pairs_hook=_unique_object)
        if not isinstance(state, dict) or state.get("v") != 1 or type(state.get("v")) is not int:
            raise ValueError
        if not isinstance(state.get("i"), str) or not _VIN.fullmatch(state["i"]):
            raise ValueError
        if state.get("m") not in {"i", "w", "k", "f", "u"}:
            raise ValueError
        nonce = state.get("n")
        if not isinstance(nonce, str) or (nonce != "" and not _NONCE.fullmatch(nonce)):
            raise ValueError
        if state["m"] != "i" and (nonce == "" or state.get("w") not in (0, 1)):
            raise ValueError
        if state["m"] == "k":
            for key in ("e", "lo", "up"):
                if type(state.get(key)) is not int:
                    raise ValueError
            if state["e"] not in (0, 1):
                raise ValueError
        if state["m"] in {"f", "u"} and not (isinstance(state.get("err"), str) and _ERROR.fullmatch(state["err"])):
            raise ValueError
        return state
    except (ValueError, TypeError) as error:
        raise SolarError("Tesla BLE bridge reported an invalid Charge on Solar state") from error


def _identity(hass, entity_id):
    from .tesla_ev_recovery import _identity as identity

    return identity(hass, entity_id)


class BleSolarBridge:
    """One ESPHome bridge whose firmware reports it is paired to ``vin``."""

    provider = "ble"

    def __init__(self, hass, prefix: str, vin: str):
        self.hass, self.prefix, self.vin = hass, prefix, vin
        self.request_id = REQUEST.format(prefix=prefix)
        self.state_id = STATE.format(prefix=prefix)

    @property
    def alias(self) -> str:
        """The identifier PowerSync's BLE charging paths use for this car."""
        return f"BLE_{self.prefix.upper()}"

    def _bound(self) -> bool:
        """Both entities must be enabled and belong to one ESPHome device."""
        identities = [_identity(self.hass, self.request_id), _identity(self.hass, self.state_id)]
        if any(value is None or value.get("platform") != "esphome" for value in identities):
            return False
        first, second = identities
        return all(first.get(key) == second.get(key) for key in ("device_id", "config_entry_id"))

    def _state(self) -> dict[str, Any] | None:
        state = self.hass.states.get(self.state_id)
        if state is None:
            return None
        try:
            return decode_state(state.state)
        except SolarError:
            return None

    def available(self) -> bool:
        state = self._state()
        return bool(state and state["i"] == self.vin and self._bound()
                    and self.hass.states.get(self.request_id) is not None)

    async def request(self, settings: dict[str, Any] | None = None) -> dict[str, Any]:
        """Send one explicit read or write and return the car's readback."""
        if not self.available():
            raise SolarError("The Tesla BLE bridge for this vehicle is unavailable")
        nonce = uuid4().hex
        if settings is None:
            text = f"get:{nonce}"
        else:
            settings = validate_settings(settings)
            text = (f"set:{nonce}:{int(settings['enabled'])}:"
                    f"{settings['lowerChargeLimit']}:{settings['upperChargeLimit']}")
        await self.hass.services.async_call(
            "text", "set_value", {"entity_id": self.request_id, "value": text}, blocking=True)
        label = "read" if settings is None else "write"
        deadline = time.monotonic() + TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            state = self._state()
            if state is not None and state["n"] == nonce:
                if state["i"] != self.vin or state["w"] != int(settings is not None):
                    raise SolarError("Tesla BLE bridge answered for a different vehicle or request")
                if state["m"] == "k":
                    return validate_settings({"enabled": state["e"] == 1,
                                              "lowerChargeLimit": state["lo"],
                                              "upperChargeLimit": state["up"]})
                if state["m"] == "f":
                    raise SolarError(f"Tesla BLE Charge on Solar {label} failed ({state['err']})")
                if state["m"] == "u":
                    raise SolarError(f"Tesla BLE Charge on Solar {label} could not be confirmed "
                                     f"({state['err']}); read the settings before changing controller ownership")
            await asyncio.sleep(POLL_SECONDS)
        raise SolarError(f"Tesla BLE Charge on Solar {label} could not be confirmed; "
                         "read the settings before changing controller ownership")


def find_bridge(hass, entry, vin: str | None) -> BleSolarBridge | None:
    """Return the single companion bridge whose firmware reports ``vin``."""
    if not vin or not _VIN.fullmatch(str(vin).upper()):
        return None
    config = {**entry.data, **entry.options}
    if config.get("ev_provider") not in {"tesla_ble", "both"}:
        return None
    from .tesla_ble_mapping import resolve_ble_prefixes

    try:
        prefixes = resolve_ble_prefixes(hass, config)
    except Exception:  # noqa: BLE001 - discovery failure means no BLE provider
        return None
    vin = str(vin).upper()
    matches = [bridge for bridge in (BleSolarBridge(hass, prefix, vin) for prefix in dict.fromkeys(prefixes))
               if bridge.available()]
    # Two bridges claiming one VIN is ambiguous; never guess which one to use.
    return matches[0] if len(matches) == 1 else None
