"""Regression tests for dashboard EV policy command wiring."""

from __future__ import annotations

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
