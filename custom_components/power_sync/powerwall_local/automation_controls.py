"""Versioned automation actions for the two explicit Tesla control APIs."""
from __future__ import annotations

from copy import deepcopy
import math
import time

from .battery_output import digest

ADVANCED_ACTIONS = {"battery_output", "native_site_limits"}


def validate_actions(actions):
    advanced = [item for item in actions if item.get("action_type") in ADVANCED_ACTIONS]
    if not advanced:
        return None
    if len(advanced) != 1 or any(item.get("action_type") not in ADVANCED_ACTIONS | {"send_notification"} for item in actions):
        return "An advanced battery action can only be combined with notifications"
    action = advanced[0]
    params = action.get("parameters")
    if not isinstance(params, dict) or params.get("schema_version") != 1:
        return "Unsupported battery action version"
    if action["action_type"] == "battery_output":
        if (set(params) - {"schema_version", "power_w", "duration_seconds", "target_soc"}
                or type(params.get("power_w")) is not int or not 0 <= abs(params["power_w"]) <= 10000
                or type(params.get("duration_seconds")) is not int or not 1 <= params["duration_seconds"] <= 86400):
            return "Invalid fixed battery output action"
        if "target_soc" in params and (type(params["target_soc"]) is not int or not 1 <= params["target_soc"] <= 100 or params["power_w"] == 0):
            return "Invalid battery target percentage"
    else:
        revision = params.get("approval_revision")
        values = [params[key] for key in ("import_limit_w", "export_limit_w") if key in params]
        if (set(params) - {"schema_version", "approval_revision", "import_limit_w", "export_limit_w"}
                or revision is not None and (not isinstance(revision, str) or len(revision) != 64 or any(c not in "0123456789abcdef" for c in revision))
                or not values or any(type(value) is not int or not 0 <= value <= 1000000 for value in values)):
            return "Invalid native site limits action"
    return None


async def execute(hass, entry, action_type, params, context):
    action = {"action_type": action_type, "parameters": params}
    if validate_actions([action]) or not context or not context.get("battery_run_id"):
        return False
    runtime = hass.data.get("power_sync", {}).get(entry.entry_id, {})
    control = runtime.get("battery_output_control" if action_type == "battery_output" else "native_site_limits_control")
    if control is None:
        return False  # Never route a new action through legacy Force.
    request_id = "auto:" + digest({"run": context["battery_run_id"], "index": context.get("action_index", 0), "action": action})
    receipt = (await control.load() or {}).get("requests", {}).get(request_id)
    if receipt:
        return receipt.get("phase") in ("active", "completed", "readback_confirmed")
    journal = context["battery_request_journal"]
    payload = journal.get(request_id)
    if payload is None:
        status = await control.read()
        if action_type == "battery_output":
            seconds = math.floor(context["battery_run_started"] + params["duration_seconds"] - time.time())
            if seconds < 1:
                return False
            payload = {"request_id": request_id, "expected_control_revision": status["control_revision"],
                       "power_w": params["power_w"], "duration_seconds": seconds,
                       **({"target_soc": params["target_soc"]} if "target_soc" in params else {})}
        else:
            if "approval_revision" in params and status["approval_revision"] != params["approval_revision"]:
                return False
            payload = {"request_id": request_id, "expected_config_revision": status["configuration_revision"],
                       "expected_approval_revision": status["approval_revision"],
                       **{key: params[key] for key in ("import_limit_w", "export_limit_w") if key in params}}
        journal[request_id] = deepcopy(payload)
        await context["save_battery_request_journal"]()
    result = await (control.dispatch(deepcopy(payload)) if action_type == "battery_output" else control.execute(deepcopy(payload)))
    return result.get("success") is True


async def run_has_unresolved_request(hass, entry, run):
    """Keep failed runs only while an accepted request may still control hardware."""
    runtime = hass.data.get("power_sync", {}).get(entry.entry_id, {})
    try:
        journals = {}
        for request_id, payload in run.get("requests", {}).items():
            key = "battery_output_control" if "power_w" in payload else "native_site_limits_control"
            control = runtime.get(key)
            if control is None:
                return True
            if key not in journals:
                journals[key] = (await control.load() or {}).get("requests", {})
            if journals[key].get(request_id, {}).get("phase") in ("prepared", "submitted", "active", "recovery_required"):
                return True
    except Exception:
        return True  # Unreadable journal is not proof that a request was refused.
    return False
