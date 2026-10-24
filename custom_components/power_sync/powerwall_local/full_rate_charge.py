"""Eligibility for explicit Tesla-managed Max charging, separate from watts.

The force-charge service defines zero/omitted power as rated/BMS maximum.
That request may exceed an optimiser planning rate; it does not promise a
battery-input watt ceiling. Numeric requests and Spread Import remain local.
Native hardware/site limits and all current control permissions still apply.
"""
from __future__ import annotations

from dataclasses import dataclass
import math


@dataclass(frozen=True)
class NativeMaxChargeEligibility:
    eligible: bool
    reason: str
    native_import_max_w: float | None = None


def _number(value):
    try:
        return (float(value) if isinstance(value, (int, float))
                and not isinstance(value, bool) and math.isfinite(value) else None)
    except (OverflowError, ValueError):
        return None


def native_max_charge_eligibility(*, source: str, requested_power_w,
        grid_import_target_w, planning_charge_max_w, software_import_max_w,
        native_import_max_w, native_import_known: bool, pending_ev_w,
        native_ev_active: bool, grid_charging_allowed: bool,
        grid_connected: bool, soc, soc_cap, evidence_fresh: bool,
        control_permitted: bool) -> NativeMaxChargeEligibility:
    """Select deliberate native Max; never convert a precise generation.

    A finite software site-import cap requires a fresh native cap no higher
    than it. No nominal output rating or inferred AC rating supplies a
    charge-power bound. The positive planning setting remains unchanged.
    """
    def denied(reason):
        return NativeMaxChargeEligibility(False, reason)
    requested = 0.0 if requested_power_w is None else _number(requested_power_w)
    if not isinstance(source, str) or source == "optimizer" or requested != 0:
        return denied("native_max_requires_explicit_max_request")
    if grid_import_target_w is not None:
        return denied("grid_target_requires_local_control")
    planned = _number(planning_charge_max_w)
    if planned is None or planned <= 0:
        return denied("configured_charge_disabled")
    if evidence_fresh is not True or native_import_known is not True:
        return denied("native_limit_evidence_unavailable")
    imported = _number(native_import_max_w)
    if native_import_max_w is not None and (imported is None or imported <= 0):
        return denied("native_import_limit_blocks_charge")
    configured = _number(software_import_max_w)
    if software_import_max_w is not None:
        if configured is None or configured < 0:
            return denied("software_import_limit_invalid")
        # Home Assistant's zero setting explicitly means no software cap.
        if configured > 0 and (imported is None or imported > configured):
            return denied("software_import_limit_requires_local_control")
    pending = _number(pending_ev_w)
    if pending is None or pending != 0 or native_ev_active is not False:
        return denied("ev_demand_requires_local_control")
    level, ceiling = _number(soc), _number(soc_cap)
    if level is None or ceiling is None or not 0 <= level < ceiling <= 100:
        return denied("charge_soc_limit_reached_or_unknown")
    if grid_connected is not True or grid_charging_allowed is not True:
        return denied("grid_charging_not_permitted")
    if control_permitted is not True:
        return denied("native_max_control_not_permitted")
    return NativeMaxChargeEligibility(True, "native_managed_max_charge", imported)
