"""Optional whole-site solar sensor for hybrid inverters.

A hybrid inverter only measures the panels wired to it.  When a second,
AC-coupled array (for example Enphase microinverters) feeds the same house,
the inverter's own PV figure under-reports total production, and its
house-consumption figure under-reports load by the same amount.

Users who already publish a whole-site solar sensor can point PowerSync at it
through ``custom_solar_power_entity``.  This module holds the pure helpers that
turn that sensor into corrected telemetry so the logic can be unit tested
without Home Assistant.
"""

from __future__ import annotations

import math
from typing import Any

_UNAVAILABLE = {"", "unknown", "unavailable", "none", "None"}


def read_power_entity_kw(hass: Any, entity_id: str | None) -> float | None:
    """Return a power sensor's state in kW, or ``None`` when it is unusable.

    ``None`` is returned for a missing entity, a non-numeric or non-finite
    state, or a negative reading.  Units follow the rest of the GoodWe code:
    ``W`` and ``MW`` are converted, ``kW`` or a missing unit is taken as kW.
    """
    entity_id = (entity_id or "").strip()
    if not entity_id:
        return None
    state = hass.states.get(entity_id)
    if state is None or str(state.state).strip() in _UNAVAILABLE:
        return None
    try:
        value = float(state.state)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value) or value < 0:
        return None
    unit = str((getattr(state, "attributes", {}) or {}).get("unit_of_measurement", "")).lower()
    if unit == "w":
        return value / 1000.0
    if unit == "mw":
        return value * 1000.0
    return value


def apply_site_solar_override(
    inverter_solar_kw: float,
    inverter_load_kw: float,
    override_solar_kw: float | None,
) -> tuple[float, float, bool]:
    """Correct inverter solar and load using a whole-site solar reading.

    Returns ``(solar_kw, load_kw, solar_valid)``.

    * ``override_solar_kw is None`` (sensor unavailable): the inverter-only
      figures are returned unchanged and ``solar_valid`` is ``False`` so the
      optimiser ignores the sample instead of treating a half-blind reading as
      real production.
    * Otherwise the site figure is used for solar, but never below what the
      inverter itself measures.  The surplus over the inverter's own PV is the
      AC-coupled production the inverter cannot see; the inverter's load figure
      excludes it too, so it is added back to load.
    """
    if override_solar_kw is None:
        return inverter_solar_kw, inverter_load_kw, False
    solar_kw = max(override_solar_kw, inverter_solar_kw)
    ac_coupled_kw = solar_kw - inverter_solar_kw
    return solar_kw, inverter_load_kw + ac_coupled_kw, True
