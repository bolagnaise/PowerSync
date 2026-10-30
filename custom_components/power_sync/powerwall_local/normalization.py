"""Normalization helpers for Tesla Powerwall local readback values."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any


# Powerwall keeps a hidden low-SOE reserve. Local hardware/config values include
# that reserve, while Tesla app/cloud UI presents the user-facing reserve target.
DEFAULT_LOW_SOE_RESERVE_PCT = 5.0
MAX_AUTO_DETECTED_LOW_SOE_RESERVE_PCT = 20.0


@dataclass(frozen=True)
class NativeGridChargingReading:
    """Native disallow value with its evidence source; never cloud permission."""

    disallowed: bool | None = None
    source: str = "unknown"


def native_grid_charging_reading(config, system_info=None, gateway_din=None):
    """Interpret the false-omitting config encoding only on the tested build.

    A signed Off/On experiment on 26.34.2 / 56d7ba06 observed true for Off
    and an omitted field for On. Cloud omission did not track either setting.
    Other builds, incomplete native envelopes and explicit malformed values
    remain unknown. Callers must supply a fresh same-gateway Common response.
    """
    info = config.get("site_info") if isinstance(config, dict) else None
    if not isinstance(info, dict):
        return NativeGridChargingReading()
    field = "disallow_charge_from_grid_with_solar_installed"
    if field in info:
        value = info[field]
        return (NativeGridChargingReading(value, "explicit") if type(value) is bool
                else NativeGridChargingReading())
    if (not isinstance(system_info, dict) or not isinstance(gateway_din, str) or not gateway_din
            or system_info.get("din") != gateway_din
            or system_info.get("firmware_version") != "26.34.2 56d7ba06"
            or system_info.get("firmware_githash") != "56d7ba064772ad6d740825723c7e9c8524cd31b1"
            or config.get("default_real_mode") not in ("autonomous", "self_consumption", "backup")
            or not isinstance(config.get("battery_blocks"), list) or not config["battery_blocks"]
            or not isinstance(config.get("meters"), list) or not config["meters"]
            or not isinstance(config.get("strategy"), dict)
            or info.get("customer_preferred_export_rule") not in ("battery_ok", "pv_only", "never")):
        return NativeGridChargingReading()
    for key, low, high in (("backup_reserve_percent", 0, 100),
                           ("max_site_meter_power_ac", 0, math.inf),
                           ("min_site_meter_power_ac", -math.inf, 0)):
        value = info.get(key)
        if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
            return NativeGridChargingReading()
    return NativeGridChargingReading(False, "attested_omitted")


def _coerce_percent(value: Any) -> float | None:
    try:
        return max(0.0, min(100.0, float(value)))
    except (TypeError, ValueError):
        return None


def _coerce_low_soe_reserve_percent(value: Any) -> float:
    reserve = _coerce_percent(value)
    if reserve is None:
        return DEFAULT_LOW_SOE_RESERVE_PCT
    return min(reserve, MAX_AUTO_DETECTED_LOW_SOE_RESERVE_PCT)


def normalize_local_soc_percent(value: Any) -> float | None:
    """Map raw full-pack SOE percent to Tesla app/cloud SOC percent."""
    try:
        raw_soc = float(value)
    except (TypeError, ValueError):
        return None
    raw_soc = max(0.0, min(100.0, raw_soc))
    return max(
        0.0,
        (raw_soc - DEFAULT_LOW_SOE_RESERVE_PCT)
        / (100.0 - DEFAULT_LOW_SOE_RESERVE_PCT)
        * 100.0,
    )


def detect_local_backup_reserve_offset(
    local_value: Any,
    user_facing_value: Any,
) -> float | None:
    """Infer the local hidden reserve offset from paired local/cloud readbacks."""
    local_reserve = _coerce_percent(local_value)
    user_reserve = _coerce_percent(user_facing_value)
    if local_reserve is None or user_reserve is None:
        return None
    if local_reserve >= 100 or user_reserve >= 100:
        return None
    offset = local_reserve - user_reserve
    if offset < 0 or offset > MAX_AUTO_DETECTED_LOW_SOE_RESERVE_PCT:
        return None
    return round(offset, 3)


def normalize_local_backup_reserve_percent(
    value: Any,
    low_soe_reserve_pct: Any = DEFAULT_LOW_SOE_RESERVE_PCT,
) -> int | None:
    """Map local Powerwall backup reserve readback to the user-facing target."""
    local_reserve = _coerce_percent(value)
    if local_reserve is None:
        return None
    low_soe_reserve = _coerce_low_soe_reserve_percent(low_soe_reserve_pct)
    if local_reserve >= 100:
        return 100
    if local_reserve <= low_soe_reserve:
        return 0
    return int(
        round(
            max(0.0, min(100.0, local_reserve - low_soe_reserve))
        )
    )


def local_backup_reserve_write_percent(
    value: Any,
    low_soe_reserve_pct: Any = DEFAULT_LOW_SOE_RESERVE_PCT,
) -> int | None:
    """Map a user-facing reserve target to the local Powerwall config value."""
    reserve = _coerce_percent(value)
    if reserve is None:
        return None
    low_soe_reserve = _coerce_low_soe_reserve_percent(low_soe_reserve_pct)
    if reserve >= 100:
        return 100
    return int(
        round(
            max(0.0, min(100.0, reserve + low_soe_reserve))
        )
    )
