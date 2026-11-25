"""Authenticated mobile access to the existing native-solar service gates."""

from __future__ import annotations

import re
import time

from .teslemetry_solar import SolarError, ownership, validate_settings, native_solar_connection
from .charge_on_solar_pairing import pairing_supported


def register_http_view(hass):
    from homeassistant.components.http import HomeAssistantView
    from homeassistant.exceptions import HomeAssistantError

    class NativeSolarView(HomeAssistantView):
        url = "/api/power_sync/ev/native_solar"
        name = "api:power_sync:ev:native_solar"
        requires_auth = True

        def entries(self):
            return [entry for entry in hass.config_entries.async_entries("power_sync")
                    if entry.entry_id in hass.data.get("power_sync", {})]

        async def get(self, request):
            """Return provider/ownership metadata; never request Tesla settings."""
            vin = str(request.query.get("vin", "")).upper()
            if not re.fullmatch(r"[A-HJ-NPR-Z0-9]{17}", vin):
                return self.json({"success": False, "error": "A valid vehicle VIN is required"}, status_code=400)
            entries = []
            for entry in self.entries():
                token, provider = native_solar_connection(hass, entry, vin)
                gate = await ownership(hass, entry.entry_id)
                async with gate.lock:
                    await gate.load()
                    hold = (gate.holds or {}).get(vin)
                    options = {**entry.data, **entry.options}
                    entries.append({
                        "entry_id": entry.entry_id, "name": entry.title,
                        # Local evidence only: no car or gateway request.
                        "pairing_supported": pairing_supported(hass, entry, vin),
                        "available": bool(token), "provider": provider,
                        "monitoring_mode": bool(options.get("monitoring_mode", False)),
                        "no_solar_installed": bool(options.get("no_solar_installed", False)),
                        "powersync_control_blocked": gate.blocked(vin),
                        "cached_settings": validate_settings(hold) if hold and "enabled" in hold else None,
                        "status": "unconfirmed" if hold and "enabled" not in hold else "cached" if hold else "unknown",
                        "retry_after_seconds": max(0, int(15 - (time.time() - gate.requests.get(vin, 0))) + 1),
                    })
            return self.json({"success": True, "entries": entries})

        async def post(self, request):
            """Only an explicit button action may incur a native Tesla request."""
            try:
                body = await request.json()
                if not isinstance(body, dict):
                    raise SolarError("A request object is required")
                matches = [entry for entry in self.entries() if entry.entry_id == body.get("entry_id")]
                if len(matches) != 1:
                    raise SolarError("Select a loaded PowerSync entry")
                action = body.get("action")
                if action not in {"read", "write"}:
                    raise SolarError("Select read or write")
                data = {"entry_id": matches[0].entry_id, "vin": str(body.get("vin", "")).upper()}
                if not re.fullmatch(r"[A-HJ-NPR-Z0-9]{17}", data["vin"]):
                    raise SolarError("A valid vehicle VIN is required")
                if action == "write":
                    settings = validate_settings(body.get("settings"))
                    data.update(enabled=settings["enabled"], lower_charge_limit=settings["lowerChargeLimit"],
                                upper_charge_limit=settings["upperChargeLimit"],
                                confirm_eligible=body.get("confirm_eligible") is True)
                result = await hass.services.async_call(
                    "power_sync", "get_teslemetry_charge_on_solar" if action == "read" else "set_teslemetry_charge_on_solar",
                    data, blocking=True, return_response=True,
                )
                return self.json({"success": True, **result})
            except (SolarError, HomeAssistantError, ValueError, TypeError) as err:
                return self.json({"success": False, "error": str(err)}, status_code=400)

    hass.http.register_view(NativeSolarView())
