"""Source-level integration contracts for phase-aware EV command routing."""

import ast
import asyncio
import importlib.util
import logging
import sys
from pathlib import Path
from types import SimpleNamespace


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


def _load_phase_allocator():
    """Load the real ev_phase_allocator module (pure logic, no HA imports)."""
    spec = importlib.util.spec_from_file_location(
        "power_sync_ev_phase_allocator_for_home_power_api_test",
        ROOT / "custom_components/power_sync/automations/ev_phase_allocator.py",
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    # @dataclass resolves annotations through sys.modules[cls.__module__].
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(spec.name, None)
        raise
    return module


def _home_power_settings_view():
    """Execute the real HomePowerSettingsView against stubbed HA plumbing.

    The GET handler is the capability handshake the mobile Home Power Setup
    screen depends on, so it has to be *executed*, not string-matched.
    """
    source = (ROOT / "custom_components/power_sync/__init__.py").read_text()
    module = ast.parse(source)
    segment = None
    for node in module.body:
        if isinstance(node, ast.ClassDef) and node.name == "HomePowerSettingsView":
            segment = ast.get_source_segment(source, node)
            break
    assert segment is not None, "HomePowerSettingsView not found"

    allocator = _load_phase_allocator()
    responses = []

    def json_response(payload, status=200):
        responses.append((payload, status))
        return SimpleNamespace(payload=payload, status=status)

    namespace = {
        "HomeAssistantView": object,
        "web": SimpleNamespace(json_response=json_response),
        "_LOGGER": logging.getLogger("power_sync_home_power_settings_test"),
        "DOMAIN": "power_sync",
        "normalize_home_power_settings": allocator.normalize_home_power_settings,
        "validate_home_power_settings": allocator.validate_home_power_settings,
        "PHASE_LOAD_MANAGEMENT_SCHEMA_VERSION": (
            allocator.PHASE_LOAD_MANAGEMENT_SCHEMA_VERSION
        ),
    }
    # __init__.py compiles under `from __future__ import annotations`; the
    # extracted segment must too, or its HA type hints resolve eagerly.
    segment = "from __future__ import annotations\n" + segment
    exec(compile(segment, "<HomePowerSettingsView>", "exec"), namespace)
    return namespace["HomePowerSettingsView"], responses


def _get_home_power_settings(stored_settings):
    """Run GET with the automation store present (dict) or absent (None)."""
    view_class, responses = _home_power_settings_view()
    entry = SimpleNamespace(entry_id="entry-372")
    store = None
    if stored_settings is not None:
        store = SimpleNamespace(_data={"home_power_settings": dict(stored_settings)})
    hass = SimpleNamespace(
        data={"power_sync": {"entry-372": {"automation_store": store}}}
    )
    view = view_class(hass, entry)
    asyncio.run(view.get(object()))
    assert responses, "GET produced no response"
    return responses[-1]


def test_home_power_get_advertises_phase_capability_with_stored_settings():
    """Ticket #372: the toggle stays greyed out unless GET returns the flag."""
    payload, status = _get_home_power_settings(
        {"phase_type": "three", "max_grid_import_amps": 32}
    )

    assert status == 200, payload
    assert payload["success"] is True
    assert payload["phase_load_management_supported"] is True
    assert payload["phase_load_management_schema_version"] == 1
    assert payload["settings"]["max_grid_import_amps"] == 32
    assert payload["settings"]["phase_type"] == "three"


def test_home_power_get_advertises_phase_capability_without_a_store():
    """The defaults fallback runs before ``if store:`` and must not raise."""
    payload, status = _get_home_power_settings(None)

    assert status == 200, payload
    assert payload["success"] is True
    assert payload["phase_load_management_supported"] is True
    assert payload["phase_load_management_schema_version"] == 1
    assert payload["settings"]["phase_type"] == "single"


def test_normalize_home_power_settings_accepts_no_argument():
    """The GET defaults path calls it with zero arguments."""
    allocator = _load_phase_allocator()

    assert allocator.normalize_home_power_settings()["phase_type"] == "single"


def _actions_source() -> str:
    return (ROOT / "custom_components/power_sync/automations/actions.py").read_text()


def _actions_function(name: str) -> str:
    source = _actions_source()
    tree = ast.parse(source)
    for node in tree.body:
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == name
        ):
            return ast.get_source_segment(source, node)
    raise AssertionError(f"actions.{name} not found")


def test_manual_starts_take_the_same_dynamic_path_as_every_other_mode():
    start_manual = _actions_function("_start_manual_ev_charging")

    # One path, unconditionally: no feature-flag branch, and no surviving raw
    # _action_start_ev_charging fallback that would energise the charger first
    # and only clamp afterwards.
    assert "_phase_load_management_enabled" not in start_manual
    assert "await _action_start_ev_charging_dynamic(" in start_manual
    assert "await _action_start_ev_charging(" not in start_manual
    assert '"owner_mode": "manual"' in start_manual
    assert '"phase_load_management_required": True' in start_manual
    # The dynamic action takes _start_dynamic_lock itself, so this caller must
    # not hold it -- the lock is not reentrant.
    assert "async with _start_dynamic_lock:" not in start_manual


def test_manual_sessions_always_get_a_periodic_controller():
    record_manual = _actions_function("record_manual_ev_charging_session")

    # Without a timer the session never re-evaluates its rate: it holds
    # whatever current the charger was left on, and under phase management it
    # spends headroom the allocator can never reclaim.  Unconditional -- the
    # controller is how manual sessions work, not a phase-management extra.
    assert "_phase_load_management_enabled" not in record_manual
    assert "async_track_time_interval(" in record_manual
    assert '"cancel_timer"] = (' in record_manual
    assert "await _dynamic_ev_update(" in record_manual
    assert '"fixed_charge_amps": requested_amps' in record_manual
    assert '"phase_load_management_required": True' in record_manual
    # This path adopts a session that is already charging (takeover, or restore
    # after a restart). Adoption stays silent unless there is a budget to
    # enforce -- see test_manual_adoption_is_silent_without_a_budget.
    assert "reconcile_now = _phase_load_management_applies(" in record_manual
    assert '"current_amps": 0 if reconcile_now else requested_amps,' in record_manual
    assert '"target_amps": requested_amps,' in record_manual
    assert "if reconcile_now:" in record_manual
    # The session keeps its own identity; only solar_surplus routes elsewhere
    # in _dynamic_ev_update, so manual needs no mode rewrite.
    assert '"dynamic_mode": "battery_target"' not in record_manual


def test_manual_adoption_is_silent_without_a_budget():
    """Taking over an already-charging session must not command the charger.

    tests/test_ev_ocpp_actions.py pins the observable contract
    (hass.services.calls == []); this pins the reason it holds, so the
    immediate reconcile cannot be made unconditional by accident.
    """
    record_manual = _actions_function("record_manual_ev_charging_session")

    before, sep, after = record_manual.partition("if reconcile_now:")
    assert sep, "the immediate reconcile lost its guard"

    # Exactly one immediate reconcile, and it is behind the guard.
    assert after.count("await _dynamic_ev_update(") == 1
    # The only other call is the periodic timer callback, which is what makes
    # the session controller-managed in both cases.
    assert before.count("await _dynamic_ev_update(") == 1
    assert "async def periodic_update(" in before


def test_manual_rate_adopts_the_live_current_before_falling_back_to_max():
    resolver = _actions_function("_manual_session_target_amps")

    # A restored session and a manual start on an already-charging car must not
    # be silently sped up to the configured maximum.
    assert "_observed_owned_charge_amps(" in resolver
    assert "_resolve_dynamic_max_charge_amps(" in resolver
    assert resolver.index("_observed_owned_charge_amps(") < resolver.index(
        "_resolve_dynamic_max_charge_amps("
    )
    assert "_effective_max_charge_amps(" in resolver

    # Both manual entry points resolve their rate the same way.
    for name in ("record_manual_ev_charging_session", "_start_manual_ev_charging"):
        assert "await _manual_session_target_amps(" in _actions_function(name), name


def test_phase_management_covers_opted_in_manual_and_boost_sessions():
    applies = _actions_function("_phase_load_management_applies")

    # Uncontrolled manual commands must still bypass the clamp: counting them
    # as reservations PowerSync cannot honour would over-allocate other EVs.
    assert 'owner_family(str(owner_mode)) != "manual"' in applies
    assert 'params.get("phase_load_management_required")' in applies


def test_opted_in_flag_survives_into_the_stored_dynamic_params():
    dynamic_start = _actions_function("_action_start_ev_charging_dynamic_locked")

    # full_params is an explicit allow-list, so the flag has to be listed or
    # the periodic controller stops clamping after the first tick.
    assert '"phase_load_management_required": bool(' in dynamic_start
    assert '"phase_requested_amps": params.get("phase_requested_amps")' in dynamic_start


def test_quick_charge_timer_does_not_cancel_the_dynamic_controller():
    init_source = (ROOT / "custom_components/power_sync/__init__.py").read_text()
    tree = ast.parse(init_source)
    arm = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_arm_manual_quick_stop":
            arm = ast.get_source_segment(init_source, node)
            break
    assert arm is not None, "_arm_manual_quick_stop not found"

    # "cancel_timer" is the dynamic controller's periodic update. A quick-charge
    # deadline must use its own slot or it silently kills phase enforcement for
    # the rest of the window.
    assert 'state.get("cancel_timer")' not in arm
    assert 'state["cancel_timer"]' not in arm
    assert 'state["quick_stop_timer"] = async_track_point_in_utc_time(' in arm
    assert '"fixed_charge_amps"' in arm
    assert '"phase_requested_amps"' in arm


def test_allocation_is_mirrored_onto_every_managed_loadpoint():
    managed = _actions_function("_phase_load_managed_target_amps")

    assert 'loadpoint_state["load_management"] = loadpoint_status' in managed
    assert 'entry_data["phase_load_management_status"] = status' in managed
