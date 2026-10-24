"""Actuator capabilities and a one-shot startup pulse for native tariff control."""
from __future__ import annotations

import math
import time

TIMED_DISPATCH_MAX_W = 10_000


def native_capacity_w(site_info):
    """Use reported site watts, never a battery-count or configuration guess."""
    value = site_info.get("nameplate_power") if isinstance(site_info, dict) else None
    return value if type(value) in (int, float) and math.isfinite(value) and 0 < value <= 1_000_000 else None


def prefers_native_tariff(site_info):
    capacity = native_capacity_w(site_info)
    return capacity is not None and capacity > TIMED_DISPATCH_MAX_W


def routing_site_info(energy):
    """Use only a successful site_info cache within its normal six-hour TTL.

    Unknown capacity retains bounded local control, without inventing a site
    rating. Native takeover and a larger planning envelope need reported data.
    """
    at = getattr(energy, "_site_info_last_fetch", None)
    if type(at) not in (int, float) or not math.isfinite(at) or at <= 0:
        return None
    age = time.monotonic() - at
    return getattr(energy, "_site_info_cache", None) if 0 <= age <= 21600 else None


def startup_watts(direction, snapshot, config):
    """A pulse must not downgrade an already-busy inverter to 10 kW.

    Missing/bad power evidence merely skips the optional pulse. The native
    actuator still has its own admission and mutation checks.
    """
    values = [getattr(snapshot, key, None) for key in ("battery_w", "load_w", "solar_w", "grid_w")]
    if any(type(v) not in (int, float) or not math.isfinite(v) for v in values):
        return None
    battery, load, solar, grid = values
    if (load < 0 or max(abs(battery), abs(solar), max(0, load - solar)) >= TIMED_DISPATCH_MAX_W
            or abs(grid + battery + solar - load) > max(250, .05 * max(abs(grid), abs(battery), load))):
        return None
    # An existing same-direction native flow needs no startup command.
    if (direction == "charge" and battery < -250) or (direction == "discharge" and battery > 250):
        return None
    from .battery_dispatch import battery_watts
    bounded = dict(config)
    key = f"optimization_max_{direction}_w"
    maximum = bounded.get(key, 5000)
    if type(maximum) not in (int, float) or not math.isfinite(maximum) or maximum <= 0:
        return None
    bounded[key] = min(maximum, TIMED_DISPATCH_MAX_W)
    try:
        watts = battery_watts(direction, bounded[key], snapshot, bounded)
    except (ValueError, TypeError, AttributeError):
        return None
    learned = config.get(f"gateway_{direction}_limit_w")
    if type(learned) in (int, float) and math.isfinite(learned) and learned > 0:
        watts = math.copysign(min(abs(watts), learned), watts)
    return watts if watts else None
