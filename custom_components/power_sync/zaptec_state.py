"""Conservative evidence shared by Zaptec command and preservation consumers."""

from __future__ import annotations

import math
from collections.abc import Mapping


def _nonnegative_number(value):
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def zaptec_power_draw(state: Mapping) -> bool | None:
    """Return measured draw independently of the charger's session mode."""
    power = _nonnegative_number(state.get("total_charge_power_w"))
    if state.get("total_charge_power_w_valid") is False:
        power = None
    phases = []
    invalid_phase = False
    for phase in ("a", "b", "c"):
        key = f"phase_{phase}_current"
        if key not in state:
            continue
        current = _nonnegative_number(state[key])
        if current is None or state.get(f"{key}_valid") is False:
            invalid_phase = True
        else:
            phases.append(current)
    if (power is not None and power > 50) or any(
        current > 0.5 for current in phases
    ):
        return True
    if power is not None and not invalid_phase:
        return False
    return None


def zaptec_charge_state(state: Mapping) -> bool | None:
    """Keep stop and preservation obligations while a session may be active."""
    mode = str(state.get("charger_operation_mode") or "").lower()
    draw = zaptec_power_draw(state)
    if mode == "charging" or draw is True:
        return True
    if mode in ("connected_waiting", "disconnected", "connected_finished", "charging_paused"):
        return draw
    return None


def cached_zaptec_charge_state(
    entry_data: Mapping, *, now: float, after: float = 0, require_power_draw: bool = False,
) -> bool | None:
    """Use a completed recent poll, and a post-command poll when required."""
    poll = entry_data.get("zaptec_state_poll") or {}
    completed = _nonnegative_number(poll.get("completed_at"))
    started = _nonnegative_number(poll.get("started_at"))
    if completed is None or not math.isfinite(now) or not 0 <= now - completed <= 90:
        return None
    if started is not None and started > completed:
        return None
    if after and (_nonnegative_number(after) is None or started is None or started < after):
        return None
    state = entry_data.get("zaptec_cached_state") or {}
    return zaptec_power_draw(state) if require_power_draw else zaptec_charge_state(state)
