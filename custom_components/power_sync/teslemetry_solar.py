"""Explicit native Tesla Charge on Solar control, with persistent EV ownership.

No background polling: both provider reads and writes can spend credits and
wake a vehicle. The hold is intentionally retained after an uncertain command.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from inspect import signature
import re
import time

_EV_SUBMISSION_CONTEXT = ContextVar("power_sync_ev_submission", default=None)


def ev_owner_snapshot(hass, entry, vin):
    from .automations.ev_ownership import get_ev_ownership
    owners = []
    entries = {entry.entry_id: entry}
    entries.update({key: runtime["entry"] for key, runtime in hass.data.get("power_sync", {}).items()
                    if isinstance(runtime, dict) and runtime.get("entry") is not None})
    for key, value in entries.items():
        _, lease = get_ev_ownership(hass, value, vin)
        if lease is not None:
            owners.append((key, id(lease), lease.get("owner"), lease.get("owner_mode"), lease.get("session_id")))
    return tuple(sorted(owners))


@contextmanager
def _ev_submission_scope(hass, entry, vin, params):
    data = getattr(hass, "data", {})
    runtime = data.get("power_sync", {}).get(entry.entry_id)
    snapshot = ev_owner_snapshot(hass, entry, vin) if data.get("power_sync_native_rate_budget") is not None else None
    token = _EV_SUBMISSION_CONTEXT.set({"hass": hass, "entry": entry,
                                       "vin": vin, "params": params, "runtime": runtime,
                                       "owner_snapshot": snapshot})
    try:
        yield
    finally:
        _EV_SUBMISSION_CONTEXT.reset(token)


class SolarError(ValueError):
    """A user-safe native solar failure."""


def validate_settings(value):
    if not isinstance(value, dict) or type(value.get("enabled")) is not bool:
        raise SolarError("enabled must be a boolean")
    for key in ("lowerChargeLimit", "upperChargeLimit"):
        if type(value.get(key)) is not int or not 0 <= value[key] <= 100:
            raise SolarError("Charge limits must be whole percentages from 0 to 100")
    if value["lowerChargeLimit"] > value["upperChargeLimit"]:
        raise SolarError("The lower charge limit must not exceed the upper limit")
    return {key: value[key] for key in ("enabled", "lowerChargeLimit", "upperChargeLimit")}


def native_solar_connection(hass, entry):
    """Prefer the selected Teslemetry provider, otherwise PowerSync's Fleet key.

    HA's separate Tesla Fleet integration cannot sign our custom command.
    PowerSync Fleet uses the existing PowerSync energy-account credentials.
    """
    from . import get_tesla_vehicle_api_token, get_tesla_api_token
    token, provider = get_tesla_vehicle_api_token(hass, entry)
    if token and provider == "teslemetry":
        return token, provider
    token, provider = get_tesla_api_token(hass, entry)
    if token and provider == "powersync":
        return token, provider
    return None, "none"


async def request_settings(session, token, vin, settings=None, *, provider="teslemetry"):
    if not re.fullmatch(r"[A-HJ-NPR-Z0-9]{17}", vin):
        raise SolarError("A valid vehicle VIN is required")
    if provider not in {"teslemetry", "powersync"}:
        raise SolarError("Select PowerSync Fleet or Teslemetry for native Charge on Solar")
    fleet = provider == "powersync"
    label = "PowerSync Fleet" if fleet else "Teslemetry"
    url = (f"https://api.powersync.cc/v1/vehicles/{vin}/native-solar" if fleet else
           f"https://api.teslemetry.com/api/1/vehicles/{vin}/custom_command/charge_on_solar")
    payload = {"action": "set" if settings is not None else "refresh", "provider": "fleet"} if fleet else None
    if settings is not None:
        validated = validate_settings(settings)
        if fleet:
            payload.update(settings=validated, confirm_eligible=validated["enabled"])
        else:
            payload = validated
    try:
        async with session.request(
            "POST" if fleet or settings is not None else "GET", url,
            headers={"Authorization": f"Bearer {token}"},
            **({"json": payload} if payload is not None else {}),
            timeout=100 if fleet else 45, allow_redirects=False,
        ) as response:
            if response.status != 200:
                raise SolarError(f"{label} request failed (HTTP {response.status}); check vehicle registration, connection, virtual-key pairing and compatibility")
            body = await response.json()
        if fleet:
            # PowerSync serializes write + readback in one DO request. Do not
            # issue a second read: it would hit the shared paid-request cooldown.
            if not isinstance(body, dict):
                raise SolarError("PowerSync Fleet readback could not be confirmed")
            return validate_settings(body.get("settings"))
        result = body.get("response") if isinstance(body, dict) else None
        if not isinstance(result, dict) or result.get("result") is False:
            raise SolarError("Teslemetry did not accept the request")
        if settings is not None:
            if result.get("result") is not True:
                raise SolarError("Teslemetry did not acknowledge the command")
            return None
        return validate_settings(result)
    except SolarError:
        raise
    except Exception as err:
        raise SolarError(f"{label} request could not be confirmed; refresh before changing controller ownership") from err


class SolarOwnership:
    def __init__(self, store):
        self.store = store
        self.lock = asyncio.Lock()
        self.owner = None
        self.holds = None
        self.requests = {}

    async def load(self):
        if self.holds is None:
            data = await self.store.async_load()
            self.holds = dict((data or {}).get("vehicles", {}))
            self.requests = dict((data or {}).get("requests", {}))

    async def hold(self, vin, value):
        self.holds[vin] = value
        await self.store.async_save({"vehicles": self.holds, "requests": self.requests})

    async def release(self, vin):
        # Persist before releasing the in-memory interlock.
        remaining = {key: val for key, val in self.holds.items() if key != vin}
        await self.store.async_save({"vehicles": remaining, "requests": self.requests})
        self.holds = remaining

    def blocked(self, vin):
        return bool(self.holds) if not vin or vin == "_default" else vin.upper() in self.holds


async def ownership(hass, entry_id):
    from homeassistant.helpers.storage import Store
    runtime = hass.data.setdefault("power_sync_native_solar", {})
    if entry_id not in runtime:
        runtime[entry_id] = SolarOwnership(Store(hass, 1, f"power_sync.native_solar.{entry_id}"))
    return runtime[entry_id]


def guard_ev_control(func):
    """Serialize Tesla writes and ownership changes, including nested actions."""
    sig = signature(func)

    @wraps(func)
    async def guarded(*args, **kwargs):
        bound = sig.bind(*args, **kwargs).arguments
        hass = bound["hass"]
        entry = bound.get("config_entry")
        if entry is None:
            return await func(*args, **kwargs)
        params = bound.get("params") or {}
        if (str(params.get("charger_type") or "tesla").lower() != "tesla"
                and func.__name__ in {"_action_start_ev_charging", "_action_start_ev_charging_dynamic",
                    "_action_set_ev_charging_amps", "_set_vehicle_amps", "_dynamic_ev_update"}):
            budget = hass.data.get("power_sync_native_rate_budget")
            if budget is not None:
                from .native_rate_budget import site_identity
                site = site_identity(entry)
                if any(record.get("site") == site and record.get("sessions")
                        for record in budget.entries.values()):
                    # Native rate cleanup must settle before an unsupported
                    # charger can acquire this site's unreserved capacity.
                    # Explicit stop actions remain available throughout.
                    return False
        gate = await ownership(hass, entry.entry_id)
        if gate.owner is asyncio.current_task():
            params = bound.get("params") or {}
            vin = params.get("vehicle_vin") or params.get("vehicle_id") or bound.get("vehicle_id")
            cleanup_only = params.get("stop_charging") is False and func.__name__ == "_action_stop_ev_charging_dynamic"
            if gate.blocked(vin) and not cleanup_only:
                return False
            with _ev_submission_scope(hass, entry, vin, params):
                return await func(*args, **kwargs)
        async with gate.lock:
            await gate.load()
            params = bound.get("params") or {}
            vin = params.get("vehicle_vin") or params.get("vehicle_id") or bound.get("vehicle_id")
            cleanup_only = params.get("stop_charging") is False and func.__name__ == "_action_stop_ev_charging_dynamic"
            if gate.blocked(vin) and not cleanup_only:
                return False
            gate.owner = asyncio.current_task()
            try:
                with _ev_submission_scope(hass, entry, vin, params):
                    return await func(*args, **kwargs)
            except Exception as err:
                if getattr(err, "site_import_admission_denied", False):
                    return False
                raise
            finally:
                gate.owner = None
    return guarded


def register_services(hass):
    import voluptuous as vol
    from homeassistant.core import SupportsResponse
    from homeassistant.exceptions import HomeAssistantError
    from homeassistant.helpers.aiohttp_client import async_get_clientsession
    from .const import DOMAIN, CONF_MONITORING_MODE

    async def handle(call):
        from .automations.actions import _dynamic_ev_state
        entries = [e for e in hass.config_entries.async_entries(DOMAIN)
                   if e.entry_id == call.data["entry_id"] and e.entry_id in hass.data.get(DOMAIN, {})]
        if len(entries) != 1:
            raise HomeAssistantError("Select a loaded PowerSync entry")
        entry = entries[0]
        token, provider = native_solar_connection(hass, entry)
        if not token:
            raise HomeAssistantError("Connect PowerSync Fleet or select Teslemetry as the vehicle API provider in PowerSync settings")
        vin = call.data["vin"].upper()
        if not re.fullmatch(r"[A-HJ-NPR-Z0-9]{17}", vin):
            raise HomeAssistantError("A valid vehicle VIN is required")
        gate = await ownership(hass, entry.entry_id)
        try:
            async with gate.lock:
                await gate.load()
                if any(state.get("active") for state in _dynamic_ev_state.get(entry.entry_id, {}).values()):
                    raise SolarError("Turn off PowerSync EV charging sessions before reading or changing native Charge on Solar")
                native_rate = hass.data.get(DOMAIN, {}).get(entry.entry_id, {}).get("tesla_native_rate")
                budget = hass.data.get("power_sync_native_rate_budget")
                if (vin in getattr(native_rate, "sessions", {})
                        or (budget is not None and budget.native_claimed(vin))):
                    raise SolarError("Turn off native-window rate management and wait for current restoration before reading or changing Charge on Solar")
                settings = None
                if call.service == "set_teslemetry_charge_on_solar":
                    if entry.options.get(CONF_MONITORING_MODE, entry.data.get(CONF_MONITORING_MODE, False)):
                        raise SolarError("Vehicle commands are blocked in monitoring mode")
                    if call.data["enabled"] and entry.options.get(
                        "no_solar_installed", entry.data.get("no_solar_installed", False)
                    ):
                        raise SolarError("Charge on Solar cannot be enabled while No solar installed is selected")
                    if call.data["enabled"] and not call.data.get("confirm_eligible"):
                        raise SolarError("Confirm Tesla setup at a Powerwall site without export/site limits; turn off all other charging controllers first")
                    settings = validate_settings({"enabled": call.data["enabled"],
                        "lowerChargeLimit": call.data["lower_charge_limit"],
                        "upperChargeLimit": call.data["upper_charge_limit"]})
                if time.time() - gate.requests.get(vin, 0) < 15:
                    raise SolarError("Wait 15 seconds before another native solar request")
                gate.requests[vin] = time.time()
                await gate.hold(vin, {"status": "unconfirmed"})
                if provider == "powersync":
                    observed = await request_settings(async_get_clientsession(hass), token, vin, settings, provider=provider)
                else:
                    if settings is not None:
                        await request_settings(async_get_clientsession(hass), token, vin, settings)
                    observed = await request_settings(async_get_clientsession(hass), token, vin)
                if observed["enabled"]:
                    await gate.hold(vin, observed)
                else:
                    await gate.release(vin)
                matches = settings is None or settings == observed
                return {"settings": observed, "readback_confirmed": matches,
                        "powersync_control_blocked": gate.blocked(vin)}
        except SolarError as err:
            raise HomeAssistantError(str(err)) from err

    schema = {vol.Required("entry_id"): str, vol.Required("vin"): str}
    hass.services.async_register("power_sync", "get_teslemetry_charge_on_solar", handle,
        schema=vol.Schema(schema), supports_response=SupportsResponse.ONLY)
    hass.services.async_register("power_sync", "set_teslemetry_charge_on_solar", handle,
        schema=vol.Schema({**schema, vol.Required("enabled"): bool,
            vol.Optional("confirm_eligible", default=False): bool,
            vol.Required("lower_charge_limit"): vol.All(int, vol.Range(min=0, max=100)),
            vol.Required("upper_charge_limit"): vol.All(int, vol.Range(min=0, max=100))}),
        supports_response=SupportsResponse.OPTIONAL)
