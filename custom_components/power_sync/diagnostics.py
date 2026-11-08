"""Native HA config-entry diagnostics using the same typed local collector."""
from __future__ import annotations


async def async_get_config_entry_diagnostics(hass, entry):
    runtime = hass.data.get("power_sync_support", {}).get(entry.entry_id)
    if runtime is None:
        return {"schema_version": 1, "availability": "unavailable"}
    return runtime.capture()
