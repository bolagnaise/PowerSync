"""Read explicitly selected SolarEdge AC members for dashboard telemetry only."""

from datetime import datetime, timezone
import math
import re


def normalize_ac_power_entities(value: object) -> list[str]:
    """Validate and deduplicate explicit local AC sensor selections."""
    if not isinstance(value, list):
        raise ValueError("AC power members must be a list of sensor entity IDs")
    members = []
    for entity_id in value:
        if not isinstance(entity_id, str) or not re.fullmatch(r"sensor\.[a-z0-9_]+", entity_id):
            raise ValueError("Each AC power member must be a sensor entity ID")
        if entity_id not in members:
            members.append(entity_id)
    return members


def read_ac_power_members(hass, entity_ids: object, *, now: datetime | None = None) -> dict:
    """Sum fresh W/kW readings once, withholding totals if any member is missing.

    The list replaces the card's power input; the selected Modbus inverter is
    never implicitly added. Callers keep raw DC and control telemetry separate.
    """
    try:
        members = normalize_ac_power_entities(entity_ids)
    except ValueError:
        return {
            "power_output_w": None,
            "ac_power_complete": False,
            "ac_power_members": [],
            "ac_power_error": "invalid_member_configuration",
        }

    now = now or datetime.now(timezone.utc)
    readings = []
    powers = []
    for entity_id in members:
        member = {"entity_id": entity_id, "power_w": None}
        state = hass.states.get(entity_id)
        try:
            if state is None:
                raise ValueError("missing")
            reported = getattr(state, "last_reported", None) or getattr(state, "last_updated", None)
            if not isinstance(reported, datetime) or reported.tzinfo is None:
                raise ValueError("missing_timestamp")
            member["reported_at"] = reported.isoformat()
            if not 0 <= (now - reported).total_seconds() <= 120:
                raise ValueError("stale")
            value = float(state.state)
            unit = state.attributes.get("unit_of_measurement")
            if unit not in ("W", "kW"):
                raise ValueError("unsupported_unit")
            power = value * (1000 if unit == "kW" else 1)
            if not math.isfinite(power):
                raise ValueError("invalid_value")
            member["power_w"] = power
            powers.append(power)
        except (TypeError, ValueError) as err:
            member["error"] = str(err) if str(err) in {
                "missing", "missing_timestamp", "stale", "unsupported_unit", "invalid_value"
            } else "invalid_value"
        readings.append(member)

    complete = bool(members) and len(powers) == len(members)
    try:
        total = math.fsum(powers) if complete else None
    except OverflowError:
        total = None
    if total is not None and not math.isfinite(total):
        total = None
    return {
        "power_output_w": total,
        "ac_power_complete": complete and total is not None,
        "ac_power_members": readings,
    }
