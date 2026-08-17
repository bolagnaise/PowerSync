"""Source-level integration contracts for phase-aware EV command routing."""

from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def test_home_power_api_is_additive_default_off_and_merge_compatible():
    source = (ROOT / "custom_components/power_sync/__init__.py").read_text()

    assert '"phase_load_management_supported": True' in source
    assert '"phase_current_entity_l1"' in source
    assert '"phase_current_entity_l2"' in source
    assert '"phase_current_entity_l3"' in source
    assert '"phase_current_safety_margin_amps"' in source
    assert "validate_home_power_settings(settings)" in source
    assert 'stored_data.get("home_power_settings")' in source


def test_every_shared_rate_write_reaches_the_final_phase_wrapper():
    actions = (
        ROOT / "custom_components/power_sync/automations/actions.py"
    ).read_text()
    planner = (
        ROOT / "custom_components/power_sync/automations/ev_charging_planner.py"
    ).read_text()

    assert "async def _set_vehicle_amps(" in actions
    assert "async def _set_vehicle_amps_unchecked(" in actions
    assert "await _phase_load_managed_target_amps(" in actions
    assert "_phase_load_management_locks.setdefault" in actions
    assert "phase_safe_start_amps" in actions
    assert '"owner_mode": "smart_schedule"' in planner
    assert "from .actions import _set_vehicle_amps" in planner


def test_fail_closed_status_is_exposed_through_loadpoint_site_payload():
    api = (ROOT / "custom_components/power_sync/__init__.py").read_text()
    actions = (
        ROOT / "custom_components/power_sync/automations/actions.py"
    ).read_text()

    assert 'entry_data["phase_load_management_status"] = status' in actions
    assert 'site["phase_load_management"] = phase_status' in api
    assert '"available": allocation.telemetry_valid' in actions
    assert '"telemetry_reason": allocation.telemetry_reason' in actions


def test_manual_or_external_commands_bypass_phase_management():
    actions = (
        ROOT / "custom_components/power_sync/automations/actions.py"
    ).read_text()

    assert 'owner_family(str(owner_mode)) != "manual"' in actions
    assert "if not owner_mode:" in actions
    assert "return await _set_vehicle_amps_unchecked(" in actions
