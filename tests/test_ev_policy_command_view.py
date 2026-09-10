"""Regression tests for dashboard EV policy command wiring."""

from __future__ import annotations

import ast
import asyncio
import copy
from pathlib import Path


INIT_PATH = (
    Path(__file__).resolve().parent.parent
    / "custom_components"
    / "power_sync"
    / "__init__.py"
)
ACTIONS_PATH = (
    Path(__file__).resolve().parent.parent
    / "custom_components"
    / "power_sync"
    / "automations"
    / "actions.py"
)
FRONTEND_PATH = (
    Path(__file__).resolve().parent.parent
    / "custom_components"
    / "power_sync"
    / "frontend"
    / "power-sync-strategy.js"
)


def test_vehicle_command_view_accepts_start_policy_charging():
    source = INIT_PATH.read_text()
    command_start = source.index("class EVVehicleCommandView")
    command_source = source[command_start:source.index("class SolarSurplusStatusView", command_start)]

    assert '"start_policy_charging"' in command_source
    assert "elif command == \"start_policy_charging\":" in command_source
    assert "await self._start_policy_charging(" in command_source
    assert "except ValueError as err:" in command_source


def test_start_policy_charging_uses_mapping_and_owner_guard():
    source = INIT_PATH.read_text()
    method_start = source.index("    async def _start_policy_charging(")
    method_source = source[method_start:source.index("    async def _start_charging(", method_start)]

    assert "from .ev_policy import build_ev_policy_action" in method_source
    assert "build_ev_policy_action(policy, duration_minutes)" in method_source
    assert "if action.action_type == \"start_ev_charging\":" in method_source
    assert "self._active_non_manual_owner_message(vehicle_vin)" in method_source
    assert "\"start_ev_charging_dynamic\"" not in method_source
    assert "\"Manual EV policy start from HA dashboard\"" in method_source


def _command_view_method(name: str, namespace: dict):
    """Compile one command-view method with its dependencies supplied by a test."""
    tree = ast.parse(INIT_PATH.read_text())
    command_view = next(
        node for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "EVVehicleCommandView"
    )
    method = copy.deepcopy(next(
        node for node in command_view.body
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and node.name == name
    ))
    method.decorator_list = []
    method.returns = None
    for arg in [*method.args.args, *method.args.kwonlyargs]:
        arg.annotation = None
    method.body = [
        node for node in method.body
        if not isinstance(node, ast.ImportFrom)
    ]
    compiled = ast.Module(body=[method], type_ignores=[])
    ast.fix_missing_locations(compiled)
    exec(compile(compiled, str(INIT_PATH), "exec"), namespace)
    return namespace[name]


def test_manual_generic_service_failure_reaches_command_response_safely():
    async def failed_action(_hass, _entry, _action, params):
        params["_manual_command_failure"].update({
            "stage": "switch_service",
            "entity_id": "switch.garage_ev",
        })
        return False

    execute = _command_view_method(
        "_execute_manual_ev_action_for_entry",
        {"_execute_single_action": failed_action},
    )

    class _View:
        _hass = object()

        @staticmethod
        def _manual_action_params(_vehicle):
            return {"charger_type": "generic"}

    success, message = asyncio.run(
        execute(_View(), object(), "start_ev_charging", None, {}, "manual test")
    )

    assert success is False
    assert message == "Generic Charger switch service for switch.garage_ev failed"


def test_manual_generic_options_override_a_stale_app_profile():
    manual_params = {
        "charger_type": "generic",
        "charger_switch_entity": "switch.replacement",
        "charger_amps_entity": "number.replacement_amps",
        "charger_status_entity": "sensor.replacement_status",
        "charger_power_entity": "sensor.replacement_power",
    }
    method = _command_view_method("_manual_action_params", {})

    class _View:
        @staticmethod
        def _manual_session_identity(_vehicle):
            return "generic_ev", manual_params

        @staticmethod
        def _get_vehicle_charging_config(*_vehicle_ids):
            return {
                "vehicle_id": "generic_ev",
                "charger_type": "generic",
                "charger_switch_entity": "input_boolean.deleted",
                "charger_amps_entity": "number.old_amps",
            }

    params = method(_View(), "generic_ev")

    assert params["charger_switch_entity"] == "switch.replacement"
    assert params["charger_amps_entity"] == "number.replacement_amps"
    assert params["vehicle_id"] == "generic_ev"
    assert params["vehicle_vin"] is None


def test_manual_start_returns_the_safe_generic_failure_to_the_ui():
    start = _command_view_method("_start_charging", {})

    class _View:
        @staticmethod
        def _manual_action_params(_vehicle):
            return {"charger_type": "generic"}

        @staticmethod
        def _active_non_manual_owner_message(_vehicle):
            return None

        async def _loadpoint_ready_for_manual_start(self, _vehicle, _params):
            return True, ""

        async def _execute_manual_ev_action(self, *_args):
            return False, "Generic Charger switch service for switch.garage_ev failed"

    success, message = asyncio.run(start(_View(), None, None, "grid_allowed"))

    assert success is False
    assert message == "Generic Charger switch service for switch.garage_ev failed"


def test_known_manual_command_rejection_is_a_resolved_application_response():
    """Keep a safe action failure visible to the dashboard's response branch."""
    class _Logger:
        @staticmethod
        def info(*_args, **_kwargs):
            pass

    post = _command_view_method(
        "post", {"web": type("Web", (), {}), "_LOGGER": _Logger()}
    )

    class _Request:
        async def json(self):
            return {
                "command": "start_policy_charging",
                "policy": "full_grid_solar",
                "duration_minutes": 60,
            }

    class _Web:
        @staticmethod
        def json_response(payload, **kwargs):
            return {"payload": payload, "status": kwargs.get("status", 200)}

    post.__globals__["web"] = _Web

    class _View:
        @staticmethod
        def _get_vin_from_vehicle_id(_vehicle_id):
            return "generic_ev"

        async def _start_policy_charging(self, _policy, _vehicle, _duration):
            return False, "Generic Charger switch service for switch.garage_ev failed"

    response = asyncio.run(post(_View(), _Request(), "generic_ev"))

    assert response == {
        "payload": {
            "success": False,
            "error": "Generic Charger switch service for switch.garage_ev failed",
        },
        "status": 200,
    }


def test_manual_owner_guard_uses_manual_takeover_policy():
    source = INIT_PATH.read_text()
    method_start = source.index(
        "    def _active_non_manual_owner_message("
    )
    method_source = source[
        method_start:source.index(
            "    async def _loadpoint_ready_for_manual_start(",
            method_start,
        )
    ]

    assert "can_claim_ev_ownership(" in method_source
    assert 'owner_mode="manual"' in method_source
    assert "return None if allowed else reason" in method_source
    assert "owner_family(" not in method_source


def test_dashboard_manual_start_does_not_block_automated_owner_takeover():
    source = FRONTEND_PATH.read_text()
    method_start = source.index("  _canStart(loadpoint) {")
    method_source = source[
        method_start:source.index("  _canStop(loadpoint) {", method_start)
    ]

    assert "loadpoint.connected" in method_source
    assert "!this._ownerConflict(loadpoint)" not in method_source


def test_manual_vehicle_config_lookup_filters_ambiguous_ble_only_vins():
    source = INIT_PATH.read_text()
    method_start = source.index("    def _get_vehicle_charging_config(")
    method_source = source[
        method_start:source.index("    def _manual_session_identity(", method_start)
    ]

    assert "_safe_vehicle_charging_configs" in method_source
    assert "safe_configs = _safe_vehicle_charging_configs(" in method_source
    assert "for config in safe_configs:" in method_source


def test_manual_action_params_carries_stored_vehicle_display_name():
    source = INIT_PATH.read_text()
    method_start = source.index("    def _manual_action_params(")
    method_source = source[
        method_start:source.index("    def _generic_charger_ready_for_start(", method_start)
    ]

    assert 'if stored_config.get("display_name"):' in method_source
    assert 'params["vehicle_name"] = stored_config["display_name"]' in method_source


def test_widget_data_uses_canonical_display_snapshot():
    source = INIT_PATH.read_text()
    view_start = source.index("class EVWidgetDataView")
    view_source = source[
        view_start:source.index("class EVLoadpointStatusView", view_start)
    ]

    assert "_get_ev_display_coordinator(" in view_source
    assert "display_snapshot_to_widgets(snapshot)" in view_source
    assert "_get_ev_vehicles_status" not in view_source


def test_disabling_solar_surplus_awaits_immediate_runtime_teardown():
    source = INIT_PATH.read_text()
    view_start = source.index("class SolarSurplusConfigView")
    view_source = source[
        view_start:source.index("class ChargingSessionsView", view_start)
    ]

    assert "current_enabled = normalize_solar_surplus_config(" in view_source
    assert (
        'if current_enabled and not updated_config.get("enabled", False):'
        in view_source
    )
    assert "await stop_solar_surplus_ev_charging(" in view_source
    save_index = view_source.index("await store.async_save()")
    teardown_index = view_source.index(
        "await stop_solar_surplus_ev_charging("
    )
    assert save_index < teardown_index


def test_policy_quick_stop_does_not_replace_dynamic_controller_timer():
    source = INIT_PATH.read_text()
    method_start = source.index("    def _schedule_policy_quick_stop(")
    method_source = source[method_start:source.index("    async def _start_policy_charging(", method_start)]

    assert "quick_stop_timer" in method_source
    assert "state[\"quick_stop_timer\"]" in method_source
    assert "stop_ev_charging_dynamic" in method_source
    assert "state[\"cancel_timer\"]" not in method_source


def test_dynamic_stop_cancels_policy_quick_stop_timer():
    source = ACTIONS_PATH.read_text()
    method_start = source.index("async def _action_stop_ev_charging_dynamic(")
    method_source = source[method_start:]

    assert "quick_stop_timer = state.get(\"quick_stop_timer\")" in method_source
    assert "quick_stop_timer()" in method_source


def test_manual_quick_session_restart_resumes_deadline_without_start_resend():
    source = INIT_PATH.read_text()
    method_start = source.index("    async def restore_manual_quick_sessions(")
    method_source = source[
        method_start:source.index("    def _schedule_policy_quick_stop(", method_start)
    ]

    assert '"start_ev_charging"' not in method_source
    assert '"stop_ev_charging"' in method_source
    assert 'if loadpoint_id in expired:' in method_source
    assert 'switch_state.state == "off"' in method_source
    assert "await record_manual_ev_charging_session(" in method_source
    assert "self._arm_manual_quick_stop(" in method_source
    assert "stops_at.astimezone(dt_util.UTC)" in method_source
    assert "dt_util.utcnow()" not in method_source


def test_unload_does_not_drop_nonmanual_dynamic_ev_sessions():
    source = INIT_PATH.read_text()
    unload_start = source.index("async def async_unload_entry(")
    unload_source = source[unload_start:]

    assert "_dynamic_ev_state.pop(entry.entry_id" not in unload_source
