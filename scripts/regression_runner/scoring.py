"""Independent accounting and invariants. This code never selects a plan."""
from __future__ import annotations

from datetime import datetime
import math

from .trusted_contract import ContractError


def primitive(value):
    if type(value) is list:
        return [primitive(v) for v in value]
    if type(value) is dict:
        if "$datetime" in value:
            return datetime.fromisoformat(value["$datetime"])
        if "$tuple" in value:
            return [primitive(v) for v in value["$tuple"]]
        if "$class" in value:
            return primitive(value["fields"])
        return {k: primitive(v) for k, v in value.items()}
    return value


def score(bundle, schedule, *, terminal_price=0.0):
    """Common tariff cashflow, terminal energy and independent limit checks.

    Complex settlement/EV policy metrics are explicitly unsupported in v1,
    rather than borrowing the candidate's own objective or reporting a pass.
    """
    inputs, state = primitive(bundle["inputs"]), primitive(bundle["state"])
    dt = float(state["dt_hours"])
    capacity = float(state["capacity_kwh"])
    opening_soc = float(inputs["current_soc"])
    if not math.isfinite(terminal_price) or capacity <= 0 or dt <= 0:
        raise ContractError("unsupported_physical_inputs")
    rows = primitive(schedule)
    length = min(max(len(inputs[k]) for k in ("import_prices", "export_prices", "solar_forecast", "load_forecast")),
                 int(state["horizon_hours"] * 60 / state["interval_minutes"]))
    if len(rows) != length:
        raise ContractError("schedule_length_mismatch")
    def series(key, default):
        values = inputs[key]
        if type(values) is bool or values is None:
            return [default if values is None else values] * length
        return list(values[:length]) + [default] * max(0, length - len(values))
    prices_in, prices_out = series("import_prices", 0.30), series("export_prices", 0.08)
    solar, load = series("solar_forecast", 0.0), series("load_forecast", 0.0)
    can_charge = series("grid_charge_allowed", True)
    valid = series("price_valid_slots", True)
    blocked = series("block_battery_charge", False)
    export_allowed = series("allow_battery_export", False)
    caps = series("grid_export_limits_w", state["max_grid_export_w"])
    timestamps = inputs["schedule_timestamps"]
    soc = opening_soc
    import_cost = export_revenue = import_kwh = export_kwh = throughput = 0.0
    violations = []
    unsupported = []
    if inputs["ev_plan"] is not None:
        unsupported.append("ev_policy_and_deadlines")
    if inputs["manual_control"] is not None:
        unsupported.append("manual_control")
    if inputs["cost_neutral_plan"] is not None or inputs["cost_neutral_earnings_cap"] is not None:
        unsupported.append("cost_neutral_settlement")
    if inputs["export_bonus_prices"] is not None or inputs["import_bonus_prices"] is not None:
        unsupported.append("settlement_bonuses")
    for index, row in enumerate(rows):
        charge, discharge, ev = (float(row[k]) / 1000 for k in ("battery_charge_w", "battery_discharge_w", "ev_charge_w"))
        reported = float(row["soc"]) if row["soc"] is not None else None
        if any(not math.isfinite(x) for x in (charge, discharge, ev)) or min(charge, discharge, ev) < -1e-6:
            violations.append({"slot": index, "rule": "invalid_power"})
            continue
        if timestamps and row["timestamp"] != timestamps[index]:
            violations.append({"slot": index, "rule": "timestamp_alignment"})
        if index and (row["timestamp"] - rows[index - 1]["timestamp"]).total_seconds() != dt * 3600:
            violations.append({"slot": index, "rule": "slot_duration"})
        if charge * 1000 > state["max_charge_w"] + 1 or discharge * 1000 > state["max_discharge_w"] + 1:
            violations.append({"slot": index, "rule": "battery_power_limit"})
        soc += (charge * state["charge_efficiency"] - discharge / state["discharge_efficiency"]) * dt / capacity
        floor = min(opening_soc, state["backup_reserve"])
        if soc < floor - 0.0002 or soc > 1.0002:
            violations.append({"slot": index, "rule": "soc_limit"})
        if reported is not None and abs(reported - soc) > 0.0002:
            violations.append({"slot": index, "rule": "soc_energy_balance"})
        net = load[index] + ev + charge - solar[index] - discharge
        importing, exporting = max(0.0, net), max(0.0, -net)
        if state["max_grid_import_w"] is not None and importing * 1000 > state["max_grid_import_w"] + 1:
            violations.append({"slot": index, "rule": "grid_import_limit"})
        if caps[index] is not None and exporting * 1000 > caps[index] + 1:
            # Solar curtailment isn't represented by ScheduleAction alone.
            unsupported.append("solar_curtailment")
        if row["action"] == "charge" and (not inputs["allow_grid_charge"] or not can_charge[index] or blocked[index] or not valid[index]):
            violations.append({"slot": index, "rule": "forbidden_grid_charge"})
        if row["action"] in ("export", "discharge") and (not export_allowed[index] or not valid[index]):
            violations.append({"slot": index, "rule": "forbidden_battery_export"})
        if not unsupported or "solar_curtailment" not in unsupported:
            import_cost += importing * dt * prices_in[index]
            export_revenue += exporting * dt * prices_out[index]
            import_kwh += importing * dt
            export_kwh += exporting * dt
        throughput += (charge + discharge) * dt
    terminal_delta = (soc - opening_soc) * capacity
    return {"scorer_version": 1, "currency": bundle["currency"], "violations": violations,
            "unsupported_metrics": sorted(set(unsupported)), "import_cost": import_cost,
            "export_revenue": export_revenue, "tariff_net_cost": import_cost - export_revenue,
            "import_kwh": import_kwh, "export_kwh": export_kwh, "throughput_kwh": throughput,
            "terminal_soc": soc, "terminal_energy_delta_kwh": terminal_delta,
            "terminal_price": terminal_price,
            "common_score": None if unsupported else import_cost - export_revenue - terminal_delta * terminal_price,
            "first_action": {"action": rows[0]["action"], "duration_seconds": dt * 3600} if rows else None,
            "mode": "identical_input", "counterfactual_daily_savings": None}
