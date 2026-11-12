"""Closed solver replay v1 contract and bounded, non-executable value codec.

This contract describes resolved optimizer inputs, not provider payloads. Only
three reviewed dataclasses may cross the boundary; decoding never imports code
named by a document. Changing a dependency requires an explicit contract update.
"""
from __future__ import annotations

from dataclasses import fields, is_dataclass
from datetime import datetime
import hashlib
import json
import math
import re
from zoneinfo import ZoneInfo

VERSION = 1
MAX_BYTES = 4 * 1024 * 1024
MAX_ITEMS = 150_000
MAX_SLOTS = 576
INPUT_FIELDS = (
    "import_prices", "export_prices", "solar_forecast", "load_forecast",
    "current_soc", "cost_function", "acquisition_cost_kwh", "allow_battery_export",
    "block_battery_charge", "allow_grid_charge", "grid_charge_allowed",
    "export_bonus_prices", "export_bonus_cap_kwh", "import_bonus_prices",
    "import_bonus_cap_kwh", "export_reserve_floor", "schedule_timestamps",
    "priority_export_slots", "priority_export_enabled", "disable_idle",
    "grid_export_limits_w", "prevent_simultaneous_grid_flow",
    "cost_neutral_earnings_cap", "cost_neutral_slots",
    "cost_neutral_forecast_import_cost", "cost_neutral_fixed_cost_allowance",
    "cost_neutral_plan", "profit_max_solar_export_slots", "manual_control",
    "ev_plan", "price_valid_slots", "acquisition_cost_known_zero",
    "initial_solar_energy_kwh",
)
STATE_FIELDS = (
    "_acquisition_cost_known_zero", "_active_grid_export_limits_w",
    "_below_reserve_recovery_target", "_initial_solar_energy_kwh",
    "_last_grid_export_limits_w", "_prevent_simultaneous_grid_flow",
    "_quota_export_caps_by_group", "_quota_export_group_ids",
    "_quota_import_caps_by_group", "_quota_import_group_ids", "_relaxing",
    "backup_reserve", "capacity_kwh", "capacity_wh", "charge_efficiency",
    "discharge_efficiency", "dt_hours", "economic_round_trip_efficiency",
    "efficiency", "export_reserve_floor", "export_reserve_floor_slots",
    "grid_charge_soc_cap", "hardware_reserve", "hardware_reserve_known",
    "horizon_hours", "interval_minutes", "max_battery_export_kw",
    "max_battery_export_w", "max_charge_kw", "max_charge_w", "max_discharge_kw",
    "max_discharge_w", "max_grid_export_w", "max_grid_import_kw",
    "max_grid_import_w", "physical_round_trip_efficiency", "pre_window_slot",
    "pre_window_soc_target", "pre_window_solar_buffer_soc",
    "pre_window_solar_credit_factor", "pre_window_solar_error_margin_kwh",
    "pre_window_solar_learning_confidence", "resolved_parameters",
    "suppress_reserve_warning", "target_charge_power_supported", "terminal_weight",
    "use_per_kwh_terminal",
)
OPTIONAL_STATE = frozenset(("_acquisition_cost_known_zero", "_initial_solar_energy_kwh", "_relaxing"))
# These are intentionally mutated by optimize(), not concurrent configuration.
SOLVE_MUTATIONS = OPTIONAL_STATE | {"_last_grid_export_limits_w", "_below_reserve_recovery_target"}
CLASS_FIELDS = {
    "EVChargePlan": (
        "vehicle_id", "max_power_kw", "energy_needed_kwh", "charge_efficiency",
        "min_power_kw", "allow_grid", "allow_solar", "allow_battery", "min_start_soc",
        "battery_floor_soc", "stop_at_battery_floor", "preserve_home_battery",
        "limit_grid_import", "allow_min_start_solar_exception",
        "solar_battery_reserve_kw", "window_source", "initially_charging",
        "deadline_requirements",
    ),
    "CostNeutralPlan": (
        "day_ids", "earnings_caps_by_day", "forecast_import_costs_by_day",
        "fixed_cost_allowances_by_day", "current_day", "timezone",
        "settlement_import_prices", "settlement_export_prices",
        "forecast_natural_export_earnings_by_day",
    ),
    "ResolvedOptimizerParameters": (
        "charge_efficiency", "discharge_efficiency", "physical_round_trip_efficiency",
        "economic_round_trip_efficiency", "battery_efficiency_confidence",
        "battery_efficiency_source", "battery_efficiency_candidate_rte",
    ),
}
MANUAL_FIELDS = frozenset(("control_type", "mode_slots", "required_charge_kw", "required_discharge_kw"))
ENUMS = frozenset((
    "", "cost", "idle", "charge", "discharge", "export", "consume",
    "self_consumption", "self_use", "solar_export", "off_grid", "hold_soc",
    "legacy", "disabled", "learned", "learning", "cold_start", "unknown", "insufficient",
    "grid", "solar", "battery", "combined", "manual", "smart_schedule",
))
SCHEDULE_FIELDS = (
    "timestamp", "action", "power_w", "soc", "battery_charge_w",
    "battery_discharge_w", "ev_charge_w", "grid_import_target_w",
)


class ContractError(ValueError):
    """Unsupported/incomplete input; messages never contain source payloads."""


def canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(value) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


class Codec:
    """Make independent primitive copies with case-local identity aliases."""

    def __init__(self):
        self.aliases = {}
        self.items = 0
        self.bytes = 0

    def alias(self, value):
        if type(value) is not str or len(value) > 4096:
            raise ContractError("unsupported_identity")
        return self.aliases.setdefault(value, f"ref_{len(self.aliases) + 1}")

    def encode(self, value, field="", depth=0):
        self.items += 1
        self.bytes += 32
        if self.items > MAX_ITEMS or self.bytes > MAX_BYTES or depth > 24:
            raise ContractError("capture_limit")
        if value is None or type(value) is bool:
            return value
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if not math.isfinite(value):
                raise ContractError("nonfinite_value")
            return int(value) if isinstance(value, int) else float(value)
        if type(value) is str:
            self.bytes += len(value.encode())
            if field == "vehicle_id" or field.startswith("_quota_"):
                return self.alias(value)
            if field == "timezone":
                try:
                    ZoneInfo(value)
                except (ValueError, KeyError):
                    raise ContractError("unsupported_timezone") from None
                return value
            if value not in ENUMS and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                raise ContractError("unsupported_text")
            return value
        if isinstance(value, datetime):
            if value.tzinfo is None or value.utcoffset() is None:
                raise ContractError("naive_timestamp")
            return {"$datetime": value.isoformat(), "zone": getattr(value.tzinfo, "key", None)}
        if type(value) in (list, tuple):
            if len(value) > MAX_SLOTS:
                raise ContractError("array_limit")
            result = [self.encode(v, field, depth + 1) for v in value]
            return {"$tuple": result} if type(value) is tuple else result
        if is_dataclass(value) and not isinstance(value, type):
            name = type(value).__name__
            if name not in CLASS_FIELDS or tuple(f.name for f in fields(value)) != CLASS_FIELDS[name]:
                raise ContractError("unsupported_dataclass")
            if not type(value).__module__.endswith((".ev_load_plan", ".cost_neutral", ".battery_efficiency")):
                raise ContractError("unsupported_dataclass")
            return {"$class": name, "fields": {
                key: self.encode(getattr(value, key), key, depth + 1) for key in CLASS_FIELDS[name]
            }}
        if type(value) is dict:
            if field == "manual_control" and not set(value) <= MANUAL_FIELDS:
                raise ContractError("unsupported_manual_fields")
            result = {}
            for key, item in value.items():
                if type(key) is not str:
                    raise ContractError("unsupported_key")
                if field.startswith("_quota_"):
                    public_key = self.alias(key)
                elif field == "manual_control":
                    public_key = key
                elif re.fullmatch(r"\d{4}-\d{2}-\d{2}", key):
                    public_key = key
                else:
                    raise ContractError("unsupported_key")
                result[public_key] = self.encode(item, key if field == "manual_control" else field, depth + 1)
            return result
        raise ContractError("unsupported_value")


def decode(value, module, depth=0):
    """Reconstruct only reviewed data types from an already validated bundle."""
    if depth > 24:
        raise ContractError("capture_limit")
    if type(value) is list:
        return [decode(v, module, depth + 1) for v in value]
    if type(value) is not dict:
        return value
    if "$datetime" in value:
        if set(value) != {"$datetime", "zone"}:
            raise ContractError("invalid_timestamp")
        date = datetime.fromisoformat(value["$datetime"])
        if date.tzinfo is None:
            raise ContractError("naive_timestamp")
        return date.astimezone(ZoneInfo(value["zone"])) if value["zone"] else date
    if "$tuple" in value:
        if set(value) != {"$tuple"} or type(value["$tuple"]) is not list:
            raise ContractError("invalid_tuple")
        return tuple(decode(v, module, depth + 1) for v in value["$tuple"])
    if "$class" in value:
        name = value["$class"]
        if set(value) != {"$class", "fields"} or name not in CLASS_FIELDS or set(value["fields"]) != set(CLASS_FIELDS[name]):
            raise ContractError("unsupported_dataclass")
        return getattr(module, name)(**{k: decode(v, module, depth + 1) for k, v in value["fields"].items()})
    return {key: decode(v, module, depth + 1) for key, v in value.items()}


def state_snapshot(optimizer, codec):
    missing = [name for name in STATE_FIELDS if not hasattr(optimizer, name)]
    if set(missing) - OPTIONAL_STATE:
        raise ContractError("missing_optimizer_state")
    return {name: codec.encode(getattr(optimizer, name), name) for name in STATE_FIELDS if name not in missing}


def schedule_snapshot(schedule, codec):
    if len(schedule.actions) > MAX_SLOTS:
        raise ContractError("array_limit")
    return [{key: codec.encode(getattr(action, key), key) for key in SCHEDULE_FIELDS} for action in schedule.actions]


def validate_bundle(bundle):
    required = {"schema_version", "layer", "source", "decision_time", "currency", "inputs", "state", "outcome", "result", "coverage", "digest"}
    if type(bundle) is not dict or set(bundle) != required or bundle["schema_version"] != VERSION or bundle["layer"] != "solver_v1":
        raise ContractError("unsupported_contract")
    if type(bundle["inputs"]) is not dict or type(bundle["state"]) is not dict:
        raise ContractError("incomplete_contract")
    if set(bundle["inputs"]) != set(INPUT_FIELDS) or not set(STATE_FIELDS) - OPTIONAL_STATE <= set(bundle["state"]) or set(bundle["state"]) - set(STATE_FIELDS):
        raise ContractError("incomplete_contract")
    if bundle["outcome"] not in ("feasible", "infeasible", "exception") or type(bundle["currency"]) is not str or not re.fullmatch(r"[A-Z]{3}", bundle["currency"]):
        raise ContractError("invalid_outcome")
    try:
        decision = datetime.fromisoformat(bundle["decision_time"])
        if decision.tzinfo is None:
            raise ValueError
    except (TypeError, ValueError):
        raise ContractError("invalid_decision_time") from None
    source = bundle["source"]
    if type(source) is not dict or set(source) != {"component_sha256", "requirements_sha256", "python", "platform", "solver", "solver_version"}:
        raise ContractError("invalid_source_binding")
    for key in ("component_sha256", "requirements_sha256"):
        if type(source[key]) is not str or not re.fullmatch(r"[a-f0-9]{64}", source[key]):
            raise ContractError("invalid_source_binding")
    if source["solver"] not in ("highs", "greedy") or not all(type(source[k]) is str and re.fullmatch(r"[a-zA-Z0-9_.+-]{1,64}", source[k]) for k in ("python", "platform", "solver_version")):
        raise ContractError("invalid_source_binding")
    count = [0]
    def encoded(value, field, depth=0):
        count[0] += 1
        if depth > 24 or count[0] > MAX_ITEMS:
            raise ContractError("capture_limit")
        if value is None or type(value) is bool:
            return
        if type(value) in (int, float):
            if not math.isfinite(value):
                raise ContractError("nonfinite_value")
            return
        if type(value) is str:
            if len(value) > 256:
                raise ContractError("text_limit")
            if field == "timezone":
                try:
                    ZoneInfo(value)
                except (ValueError, KeyError):
                    raise ContractError("unsupported_timezone") from None
            elif field == "vehicle_id" or field.startswith("_quota_"):
                if not re.fullmatch(r"ref_[1-9]\d*", value):
                    raise ContractError("invalid_alias")
            elif value not in ENUMS and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                raise ContractError("unsupported_text")
            return
        if type(value) is list:
            if len(value) > MAX_SLOTS:
                raise ContractError("array_limit")
            for item in value:
                encoded(item, field, depth + 1)
            return
        if type(value) is not dict:
            raise ContractError("unsupported_value")
        if "$datetime" in value:
            if set(value) != {"$datetime", "zone"} or type(value["$datetime"]) is not str or len(value["$datetime"]) > 80:
                raise ContractError("invalid_timestamp")
            try:
                if datetime.fromisoformat(value["$datetime"]).tzinfo is None:
                    raise ValueError
                if value["zone"] is not None:
                    ZoneInfo(value["zone"])
            except (ValueError, TypeError, KeyError):
                raise ContractError("invalid_timestamp") from None
        elif "$tuple" in value:
            if set(value) != {"$tuple"} or type(value["$tuple"]) is not list:
                raise ContractError("invalid_tuple")
            encoded(value["$tuple"], field, depth + 1)
        elif "$class" in value:
            name = value["$class"]
            if type(name) is not str or name not in CLASS_FIELDS or set(value) != {"$class", "fields"} or type(value["fields"]) is not dict or set(value["fields"]) != set(CLASS_FIELDS[name]):
                raise ContractError("unsupported_dataclass")
            for key, item in value["fields"].items():
                encoded(item, key, depth + 1)
        else:
            for key, item in value.items():
                if field == "manual_control":
                    allowed = key in MANUAL_FIELDS
                elif field.startswith("_quota_"):
                    allowed = bool(re.fullmatch(r"ref_[1-9]\d*", key))
                else:
                    allowed = bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", key))
                if not allowed:
                    raise ContractError("unsupported_key")
                encoded(item, key if field == "manual_control" else field, depth + 1)
    for name, value in (*bundle["inputs"].items(), *bundle["state"].items()):
        encoded(value, name)
    result = bundle["result"]
    if type(result) is not dict or set(result) != {"schedule", "feasible", "solver", "exception"} or type(result["schedule"]) is not list or len(result["schedule"]) > MAX_SLOTS:
        raise ContractError("invalid_result")
    if result["solver"] not in (None, "highs", "greedy") or result["exception"] not in (None, "ValueError", "RuntimeError", "TimeoutError", "CancelledError", "KeyboardInterrupt", "other") or result["feasible"] is not None and type(result["feasible"]) is not bool:
        raise ContractError("invalid_result")
    for row in result["schedule"]:
        if type(row) is not dict or set(row) != set(SCHEDULE_FIELDS):
            raise ContractError("invalid_schedule")
        if row["action"] not in {"idle", "charge", "discharge", "export", "consume", "self_consumption", "solar_export", "off_grid"}:
            raise ContractError("invalid_schedule_action")
        if type(row["timestamp"]) is not dict or "$datetime" not in row["timestamp"]:
            raise ContractError("invalid_timestamp")
        for key in ("power_w", "battery_charge_w", "battery_discharge_w", "ev_charge_w"):
            if type(row[key]) not in (int, float):
                raise ContractError("invalid_schedule_power")
        for key in ("soc", "grid_import_target_w"):
            if row[key] is not None and type(row[key]) not in (int, float):
                raise ContractError("invalid_schedule_power")
        for key, value in row.items():
            encoded(value, key)
    coverage = bundle["coverage"]
    if type(coverage) is not dict or set(coverage) != {"complete", "torn", "stages", "not_covered"} or type(coverage["complete"]) is not bool or type(coverage["torn"]) is not bool or coverage["stages"] != ["solver_raw"] or coverage["not_covered"] != ["coordinator", "hardware", "counterfactual"]:
        raise ContractError("invalid_coverage")
    raw = {k: v for k, v in bundle.items() if k != "digest"}
    if len(canonical(bundle)) > MAX_BYTES or digest(raw) != bundle["digest"]:
        raise ContractError("invalid_digest")
    if not coverage["complete"] or coverage["torn"]:
        raise ContractError("incomplete_capture")
    return bundle


def loads_bundle(raw):
    if len(raw) > MAX_BYTES:
        raise ContractError("capture_limit")
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ContractError("duplicate_key")
            result[key] = value
        return result
    try:
        return validate_bundle(json.loads(raw, object_pairs_hook=pairs, parse_constant=lambda _: (_ for _ in ()).throw(ContractError("nonfinite_value"))))
    except (RecursionError, json.JSONDecodeError, UnicodeError) as error:
        raise ContractError("invalid_json") from error
