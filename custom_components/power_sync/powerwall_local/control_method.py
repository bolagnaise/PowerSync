"""Tesla control preference and conservative actuator selection.

A preference applies to Max and optimiser actions. Explicit numeric requests
retain their local watt target. No preference overrides control admission.
"""
from __future__ import annotations

import math
import time

CONF_CONTROL_METHOD = "tesla_control_method"
METHODS = ("tariff", "local", "auto")
LABELS = {"tariff": "Tariff", "local": "Local V1R", "auto": "Automatic"}
DEFAULT_METHOD = "tariff"
LOCAL_MAX_W = 10000


def control_method(config):
    value = config.get(CONF_CONTROL_METHOD)
    return value if value in METHODS else DEFAULT_METHOD


def validate_method(value):
    if not isinstance(value, str) or value not in METHODS:
        raise ValueError("Choose tariff, local or auto")
    return value


def select_route(config, direction, snapshot=None, *, local_available=False, local_limit_w=None):
    """Select a pre-plan actuator; unknown Auto evidence uses tariff control."""
    method = control_method(config)
    if method != "auto":
        return method
    if not local_available or snapshot is None:
        return "tariff"
    try:
        from .battery_dispatch import site_grid_limits_w
        imported, exported = site_grid_limits_w(snapshot)
        load, solar, battery, grid = (getattr(snapshot, key) for key in
                                       ("load_w", "solar_w", "battery_w", "grid_w"))
        maximum = config.get(f"optimization_max_{direction}_w", 5000)
        values = (load, solar, battery, grid, maximum)
        if any(type(value) not in (int, float) or not math.isfinite(value) for value in values):
            return "tariff"
        if (snapshot.grid_status != "SystemGridConnected" or load < 0 or maximum <= 0
                or abs(grid + battery + solar - load) > max(250, .05 * max(abs(grid), abs(battery), load))):
            return "tariff"
        ceiling = LOCAL_MAX_W
        if type(local_limit_w) in (int, float) and math.isfinite(local_limit_w) and local_limit_w > 0:
            ceiling = min(ceiling, local_limit_w)
        # A site can be restricted by the timed inverter envelope even when
        # the battery component alone is below its limit.
        if abs(solar) > ceiling:
            return "tariff"
        if direction == "discharge":
            configured = config.get("optimization_max_grid_export_w")
            bound = min(exported if exported is not None else math.inf,
                        configured if configured is not None else math.inf)
            required = min(maximum, max(0, bound + load - solar))
            inverter_flow = min(bound + load, maximum + max(0, solar))
        elif direction == "charge":
            configured = config.get("optimization_max_grid_import_w")
            bound = min(imported if imported is not None else math.inf,
                        configured if configured not in (None, 0) else math.inf)
            required = min(maximum, max(0, bound - load + solar))
            inverter_flow = required
        else:
            raise ValueError("Unknown battery direction")
        return "local" if 0 < required <= ceiling and inverter_flow <= ceiling else "tariff"
    except (ValueError, TypeError, AttributeError):
        return "tariff"


def runtime_route(runtime, config, direction):
    """Use fresh per-entry telemetry; retain a running Auto tariff episode."""
    from ..powerwall_host import local_battery_dispatch_configured
    method = control_method(config)
    state = runtime.get(f"force_{direction}_state", {})
    if method == "auto" and state.get("active") and state.get("native_tariff_force"):
        return "tariff"
    local = runtime.get("powerwall_local", {})
    coordinator = local.get("coordinator")
    at = getattr(coordinator, "last_success_monotonic", None)
    fresh = type(at) in (int, float) and 0 <= time.monotonic() - at <= 90
    client = local.get("client") or getattr(runtime.get("powerwall_battery_dispatch"), "client", None)
    return select_route(config, direction, getattr(coordinator, "data", None) if fresh else None,
        local_available=local_battery_dispatch_configured(config),
        local_limit_w=getattr(client, f"dispatch_{direction}_limit_w", None))


async def set_control_method(hass, entry, method):
    """One mutation boundary shared by the entity and authenticated API."""
    from homeassistant.exceptions import HomeAssistantError
    from ..const import DOMAIN
    method = validate_method(method)
    runtime = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
    change = runtime.get("set_tesla_control_method")
    if change is None:
        raise HomeAssistantError("Tesla battery control is unavailable")
    await change(method)
