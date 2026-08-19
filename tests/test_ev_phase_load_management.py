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
