"""Regression tests for dashboard EV policy command wiring."""

from __future__ import annotations

import ast
import asyncio
import copy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


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

    class _StripNestedImports(ast.NodeTransformer):
        def visit_ImportFrom(self, node):  # noqa: N802
            return None

    method = _StripNestedImports().visit(method)
    compiled = ast.Module(body=[method], type_ignores=[])
    ast.fix_missing_locations(compiled)
    exec(compile(compiled, str(INIT_PATH), "exec"), namespace)
    return namespace[name]


async def _noop_clear_tracked_ev_session(*_args, **_kwargs):
    return None


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


def test_solar_surplus_api_uses_shared_settings_writer():
    import ast

    source = INIT_PATH.read_text()
    view = next(node for node in ast.parse(source).body
                if isinstance(node, ast.ClassDef) and node.name == "SolarSurplusConfigView")
    post = next(node for node in view.body
                if isinstance(node, ast.AsyncFunctionDef) and node.name == "post")
    post_source = ast.get_source_segment(source, post)
    assert "await async_update_solar_surplus_config(" in post_source
    assert "self._hass, entry, store, data" in post_source


def test_policy_quick_stop_does_not_replace_dynamic_controller_timer():
    source = INIT_PATH.read_text()
    method_start = source.index("    def _schedule_policy_quick_stop(")
    method_source = source[method_start:source.index("    async def _start_policy_charging(", method_start)]

    assert "quick_stop_timer" in method_source
    assert "state[\"quick_stop_timer\"]" in method_source
    assert "stop_ev_charging_dynamic" in method_source
    assert "state[\"cancel_timer\"]" not in method_source


def test_manual_quick_stop_adopts_durable_timer_without_cancelling_it():
    """View completion must leave the pre-dispatch durable expiry armed."""
    handle_calls = []

    def durable_timer():
        handle_calls.append("cancelled")

    entry = SimpleNamespace(entry_id="entry-1")
    vehicle = "VIN-A"
    state = {
        "session_id": "session-a",
        "reason": "manual",
        "params": {"charger_type": "tesla"},
        "quick_stop_timer": durable_timer,
    }
    ev_actions = SimpleNamespace(_dynamic_ev_state={"entry-1": {vehicle: state}})
    arm = _command_view_method(
        "_arm_manual_quick_stop",
        {
            "ev_actions": ev_actions,
            "claim_ev_ownership": lambda *_args, **_kwargs: {
                "owner": "manual"
            },
            "get_ev_quick_control_timer": lambda *_args: durable_timer,
            "async_track_point_in_utc_time": lambda *_args: (
                lambda: handle_calls.append("wrong timer cancelled")
            ),
        },
    )

    class View:
        _hass = object()

        @staticmethod
        def _manual_loadpoint_id(_vehicle):
            return vehicle

    stops_at = datetime(2026, 11, 1, 23, 0, tzinfo=timezone.utc)
    assert arm(View(), entry, vehicle, stops_at, 3, "manual") == stops_at.isoformat()
    assert state["quick_stop_timer"] is durable_timer
    assert handle_calls == []


def test_policy_quick_stop_adopts_durable_timer_without_cancelling_it():
    """Policy metadata wiring must preserve the same expiry callback handle."""
    handle_calls = []

    def durable_timer():
        handle_calls.append("cancelled")

    entry = SimpleNamespace(entry_id="entry-1")
    vehicle = "VIN-A"
    state = {
        "params": {"charger_type": "tesla"},
        "quick_stop_timer": durable_timer,
    }
    ev_actions = SimpleNamespace(_dynamic_ev_state={"entry-1": {vehicle: state}})
    schedule = _command_view_method(
        "_schedule_policy_quick_stop",
        {
            "ev_actions": ev_actions,
            "get_ev_ownership": lambda *_args: (
                "lease-a", {"owner": "manual"}
            ),
            "get_ev_quick_control_timer": lambda *_args: durable_timer,
            "async_track_point_in_utc_time": lambda *_args: (
                lambda: handle_calls.append("wrong timer cancelled")
            ),
            "_LOGGER": SimpleNamespace(info=lambda *_args, **_kwargs: None),
        },
    )

    class View:
        _hass = object()

        @staticmethod
        def _get_powersync_entry():
            return entry

        @staticmethod
        def _manual_loadpoint_id(_vehicle):
            return vehicle

    stops_at = datetime(2026, 11, 1, 23, 0, tzinfo=timezone.utc)
    assert schedule(View(), vehicle, 3, "policy", stops_at=stops_at) == stops_at.isoformat()
    assert state["quick_stop_timer"] is durable_timer
    assert handle_calls == []


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
    assert "dt_util.utcnow() >= stops_at" in method_source


def test_manual_restore_rechecks_binding_after_recorder_await():
    """A runtime replacement during restore cannot claim a current producer."""
    vehicle = "LRW3F7FS1NC484342"
    deadline = datetime.now(timezone.utc) + timedelta(minutes=3)
    original_runtime = {"automation_store": object()}
    hass = SimpleNamespace(
        data={"power_sync": {"entry-1": original_runtime}},
        states=SimpleNamespace(get=lambda _entity: None),
    )
    entry = SimpleNamespace(entry_id="entry-1")
    record = {
        "obligation_id": "obligation-a",
        "_boost_start_deadline": deadline.timestamp(),
        "params": {"_quick_control_binding": {"producer": "exact-a"}},
    }
    seen = {}

    async def record_manual(_hass, _entry, _vehicle, params, reason):
        seen.update(params)
        # Model the await boundary where HA reloads/rebinds the runtime.
        hass.data["power_sync"]["entry-1"] = {
            "automation_store": object(),
        }

        async def rollback(rollback_reason):
            cleanup_calls.append(((_hass, _entry, _vehicle), {
                "reason": rollback_reason,
            }))

        return {"rollback": rollback}

    cleanup_calls = []

    async def clear_tracked(*args, **kwargs):
        cleanup_calls.append((args, kwargs))

    armed = []
    execute = _command_view_method(
        "restore_manual_quick_sessions",
        {
            "DOMAIN": "power_sync",
            "Mapping": __import__("collections.abc", fromlist=["Mapping"]).Mapping,
            "copy": copy,
            "datetime": datetime,
            "dt_util": SimpleNamespace(UTC=timezone.utc, utcnow=lambda: datetime.now(timezone.utc)),
            "_LOGGER": SimpleNamespace(warning=lambda *args: None, info=lambda *args: None),
            "record_manual_ev_charging_session": record_manual,
            "clear_tracked_ev_charging_session": clear_tracked,
            "get_ev_quick_control_obligation": lambda *_args: record,
            "get_ev_ownership": lambda *_args: (None, None),
            "get_vehicle_route": lambda *_args: None,
            "_tesla_quick_control_binding_current": lambda *_args: True,
        },
    )

    class View:
        _hass = hass

        @staticmethod
        def _manual_action_params(_vehicle):
            return {"charger_type": "tesla", "fixed_charge_amps": 10}

        @staticmethod
        def _arm_manual_quick_stop(*args):
            armed.append(args)

    lease = {
        "quick_control": True,
        "expires_at": deadline.isoformat(),
        "duration_minutes": 3,
        "source_mode": "standard",
        "resume_params": {"vehicle_vin": vehicle, "quick_control": True},
    }
    asyncio.run(execute(View(), entry, {vehicle: lease}, {}))

    assert seen["_boost_start_deadline"] == record["_boost_start_deadline"]
    assert seen["_quick_control_binding"] == record["params"]["_quick_control_binding"]
    assert armed == []
    assert cleanup_calls and cleanup_calls[0][0][2] == vehicle
    assert cleanup_calls[0][1]["reason"] == (
        "Restored finite EV producer authority changed"
    )


def test_manual_restore_authority_callbacks_are_bound_per_vehicle():
    """Two restored VINs keep independent token/deadline callbacks."""
    vehicles = ["LRW3F7FS1NC484342", "5YJTEST0000000001"]
    deadlines = {
        vehicles[0]: datetime.now(timezone.utc) + timedelta(minutes=2),
        vehicles[1]: datetime.now(timezone.utc) + timedelta(minutes=5),
    }
    runtime = {"automation_store": object()}
    hass = SimpleNamespace(
        data={"power_sync": {"entry-1": runtime}},
        states=SimpleNamespace(get=lambda _entity: None),
    )
    entry = SimpleNamespace(entry_id="entry-1")
    records = {
        vehicle: {
            "obligation_id": f"obligation-{index}",
            "_boost_start_deadline": deadlines[vehicle].timestamp(),
            "params": {"_quick_control_binding": {"producer": vehicle}},
        }
        for index, vehicle in enumerate(vehicles)
    }
    callbacks = {}

    async def record_manual(_hass, _entry, vehicle, params, reason):
        callbacks[vehicle] = params["_manual_restore_authority"]

    execute = _command_view_method(
        "restore_manual_quick_sessions",
        {
            "DOMAIN": "power_sync",
            "Mapping": __import__("collections.abc", fromlist=["Mapping"]).Mapping,
            "copy": copy,
            "datetime": datetime,
            "dt_util": SimpleNamespace(UTC=timezone.utc, utcnow=lambda: datetime.now(timezone.utc)),
            "_LOGGER": SimpleNamespace(warning=lambda *args: None, info=lambda *args: None),
            "record_manual_ev_charging_session": record_manual,
            "clear_tracked_ev_charging_session": _noop_clear_tracked_ev_session,
            "get_ev_quick_control_obligation": lambda _hass, _entry, vehicle: records.get(vehicle),
            "get_ev_ownership": lambda *_args: (None, None),
            "get_vehicle_route": lambda *_args: None,
            "_tesla_quick_control_binding_current": lambda *_args: True,
        },
    )

    class View:
        _hass = hass

        @staticmethod
        def _manual_action_params(_vehicle):
            return {"charger_type": "tesla", "fixed_charge_amps": 10}

        @staticmethod
        def _arm_manual_quick_stop(*args):
            pass

    leases = {
        vehicle: {
            "quick_control": True,
            "expires_at": deadlines[vehicle].isoformat(),
            "duration_minutes": 3,
            "source_mode": "standard",
            "resume_params": {"vehicle_vin": vehicle, "quick_control": True},
        }
        for vehicle in vehicles
    }
    asyncio.run(execute(View(), entry, leases, {}))
    assert set(callbacks) == set(vehicles)

    records[vehicles[0]]["obligation_id"] = "replacement-a"
    assert callbacks[vehicles[0]]() is False
    assert callbacks[vehicles[1]]() is True


def test_manual_restore_carries_persisted_native_route_for_handoff():
    """Reload restore passes the exact journal producer into the recorder."""
    vehicle = "LRW3F7FS1NC484342"
    deadline = datetime.now(timezone.utc) + timedelta(minutes=3)
    identity = {
        "entity_id": "switch.car_charge",
        "platform": "tesla_fleet",
        "config_entry_id": "fleet-entry",
        "device_id": "fleet-device",
        "unique_id": "car-charge",
        "disabled_by": None,
    }
    blocked_route = {
        "kind": "blocked",
        "reason": "route_identity_changed",
        "entity_id": identity["entity_id"],
        "entity_identity": identity,
    }
    hass = SimpleNamespace(
        data={"power_sync": {"entry-1": {"automation_store": object()}}},
        states=SimpleNamespace(get=lambda _entity: None),
    )
    entry = SimpleNamespace(entry_id="entry-1")
    record = {
        "obligation_id": "obligation-native",
        "_boost_start_deadline": deadline.timestamp(),
        "params": {
            "_quick_control_binding": {"producer": "exact-native"},
            "_tesla_command_entity_id": identity["entity_id"],
            "_tesla_native_command_identity": identity,
        },
    }
    seen = {}

    async def record_manual(_hass, _entry, _vehicle, params, reason):
        seen.update(params)
        params["_manual_restore_native_route_bound"] = True

    armed = []
    execute = _command_view_method(
        "restore_manual_quick_sessions",
        {
            "DOMAIN": "power_sync",
            "Mapping": __import__("collections.abc", fromlist=["Mapping"]).Mapping,
            "copy": copy,
            "datetime": datetime,
            "dt_util": SimpleNamespace(
                UTC=timezone.utc,
                utcnow=lambda: datetime.now(timezone.utc),
            ),
            "_LOGGER": SimpleNamespace(warning=lambda *args: None, info=lambda *args: None),
            "record_manual_ev_charging_session": record_manual,
            "clear_tracked_ev_charging_session": _noop_clear_tracked_ev_session,
            "get_ev_quick_control_obligation": lambda *_args: record,
            "get_ev_ownership": lambda *_args: (None, None),
            "get_vehicle_route": lambda *_args: blocked_route,
            "_tesla_quick_control_binding_current": lambda *_args: True,
        },
    )

    class View:
        _hass = hass

        @staticmethod
        def _manual_action_params(_vehicle):
            return {"charger_type": "tesla", "fixed_charge_amps": 10}

        @staticmethod
        def _arm_manual_quick_stop(*args):
            armed.append(args)

    lease = {
        "quick_control": True,
        "expires_at": deadline.isoformat(),
        "duration_minutes": 3,
        "source_mode": "standard",
        "resume_params": {"vehicle_vin": vehicle, "quick_control": True},
    }
    asyncio.run(execute(View(), entry, {vehicle: lease}, {}))

    assert seen["_tesla_restored_native_route"] == {
        "kind": "ha",
        "entity_id": identity["entity_id"],
        "entity_identity": identity,
    }
    assert seen["_tesla_command_entity_id"] == identity["entity_id"]
    assert seen["_tesla_native_command_identity"] == identity
    assert len(armed) == 1


@pytest.mark.parametrize(
    ("native_route", "replace_during_handoff", "stop_releases_lease"),
    [
        (True, False, False),
        (True, False, True),
        (True, True, False),
        (False, False, False),
    ],
)
def test_expired_manual_native_restore_rebinds_cleanup_stop_before_dispatch(
    native_route,
    replace_during_handoff,
    stop_releases_lease,
):
    """Expired cleanup clears only after its exact obligation is still current."""
    vehicle = "LRW3F7FS1NC484342"
    deadline = datetime.now(timezone.utc) - timedelta(minutes=1)
    identity = {
        "entity_id": "switch.car_charge",
        "platform": "tesla_fleet",
        "config_entry_id": "fleet-entry",
        "device_id": "fleet-device",
        "unique_id": "car-charge",
        "disabled_by": None,
    }
    blocked_route = {
        "kind": "blocked",
        "reason": "route_identity_changed",
        "entity_id": identity["entity_id"],
        "entity_identity": identity,
    }
    record_params = {
        "_quick_control_binding": {"producer": "exact-native"},
    }
    if native_route:
        record_params.update({
            "_tesla_command_entity_id": identity["entity_id"],
            "_tesla_native_command_identity": identity,
        })
    record = {
        "obligation_id": "obligation-expired-native",
        "_boost_start_deadline": deadline.timestamp(),
        "params": record_params,
    }
    cleanup_lease = {"owner_mode": "manual", "ownership_generation": "cleanup"}
    hass = SimpleNamespace(
        data={"power_sync": {"entry-1": {
            "automation_store": object(),
            "ev_ownership": {vehicle: cleanup_lease} if native_route else {},
        }}},
        states=SimpleNamespace(get=lambda _entity: None),
    )
    entry = SimpleNamespace(entry_id="entry-1")
    calls = []

    async def rebind(*args, **kwargs):
        calls.append(("rebind", kwargs))
        if replace_during_handoff:
            hass.data["power_sync"]["entry-1"]["ev_ownership"][vehicle] = {
                "owner_mode": "boost",
                "ownership_generation": "replacement",
            }
        return True

    async def execute_stop(*args, **kwargs):
        calls.append(("stop", kwargs))
        if stop_releases_lease:
            hass.data["power_sync"]["entry-1"]["ev_ownership"].pop(
                vehicle, None
            )
        return True, ""

    def claim(*args, **kwargs):
        calls.append(("claim", kwargs))
        hass.data["power_sync"]["entry-1"]["ev_ownership"][vehicle] = cleanup_lease
        return cleanup_lease

    def release(*args, **kwargs):
        calls.append(("release", kwargs))
        hass.data["power_sync"]["entry-1"]["ev_ownership"].pop(vehicle, None)

    execute = _command_view_method(
        "restore_manual_quick_sessions",
        {
            "DOMAIN": "power_sync",
            "Mapping": __import__("collections.abc", fromlist=["Mapping"]).Mapping,
            "copy": copy,
            "datetime": datetime,
            "dt_util": SimpleNamespace(UTC=timezone.utc, utcnow=lambda: datetime.now(timezone.utc)),
            "asyncio": asyncio,
            "_LOGGER": SimpleNamespace(warning=lambda *args: None, info=lambda *args: None),
            "record_manual_ev_charging_session": _noop_clear_tracked_ev_session,
            "clear_tracked_ev_charging_session": _noop_clear_tracked_ev_session,
            "get_ev_quick_control_obligation": lambda *_args: record,
            "get_ev_ownership": lambda _hass, _entry, _vehicle: (
                vehicle,
                hass.data["power_sync"]["entry-1"]["ev_ownership"].get(vehicle),
            ),
            "get_vehicle_route": lambda *_args: blocked_route if native_route else None,
            "async_rebind_native_session_route": rebind,
            "async_reconcile_quick_control_stopped": AsyncMock(return_value=False),
            "_tesla_quick_control_binding_current": lambda *_args: True,
            "claim_ev_ownership": claim,
            "release_ev_ownership": release,
            "clear_ev_quick_control_obligation": lambda *_args, **_kwargs: calls.append(("clear", {})),
        },
    )

    class View:
        _hass = hass

        @staticmethod
        def _manual_action_params(_vehicle):
            return {"charger_type": "tesla", "fixed_charge_amps": 10}

        _execute_manual_ev_action_for_entry = execute_stop

        @staticmethod
        def _arm_manual_quick_stop(*args):
            raise AssertionError("expired restore must not arm a timer")

    lease = {
        "quick_control": True,
        "expires_at": deadline.isoformat(),
        "duration_minutes": 3,
        "source_mode": "standard",
        "session_id": "old-session",
        "resume_params": {"vehicle_vin": vehicle, "quick_control": True},
    }
    asyncio.run(execute(View(), entry, {}, {vehicle: lease}))

    if not native_route:
        expected = ["stop", "clear"]
    elif replace_during_handoff:
        expected = ["claim", "rebind", "stop"]
    elif stop_releases_lease:
        expected = ["claim", "rebind", "stop", "clear"]
    else:
        expected = ["claim", "rebind", "stop", "release", "clear"]
    assert [kind for kind, _ in calls] == expected
    if native_route:
        assert calls[1][1]["mode"] == "cleanup_stop"
        assert calls[1][1]["entity_id"] == identity["entity_id"]
    if replace_during_handoff:
        assert hass.data["power_sync"]["entry-1"]["ev_ownership"][vehicle][
            "owner_mode"
        ] == "boost"


def test_unload_does_not_drop_nonmanual_dynamic_ev_sessions():
    source = INIT_PATH.read_text()
    unload_start = source.index("async def async_unload_entry(")
    unload_source = source[unload_start:]

    assert "_dynamic_ev_state.pop(entry.entry_id" not in unload_source


def test_manual_tesla_key_rejection_reaches_view_without_raw_provider_text():
    async def failed_action(_hass, _entry, _action, params):
        params["_manual_command_failure"].update({
            "provider": "tesla", "code": "key_not_paired",
            "message": "private provider diagnostic that must not reach the UI",
        })
        return False

    execute = _command_view_method(
        "_execute_manual_ev_action_for_entry", {"_execute_single_action": failed_action},
    )
    class View:
        _hass = object()
        @staticmethod
        def _manual_action_params(_vehicle):
            return {"charger_type": "tesla"}

    success, message = asyncio.run(execute(View(), object(), "start_ev_charging", "car_b", {}, "test"))
    assert success is False
    assert message == (
        "Tesla rejected this vehicle's Fleet command key. Pair the key "
        "used by its Home Assistant vehicle integration, then retry."
    )
    assert "private" not in message


def test_manual_stop_and_current_commands_keep_safe_failure_detail():
    message = "Tesla rejected this vehicle's Fleet command key."
    class View:
        @staticmethod
        def _manual_action_params(_vehicle):
            return {"charger_type": "tesla"}

        @staticmethod
        def _record_manual_stop_hold(*_args):
            pass

        async def _execute_manual_ev_action(self, *_args):
            return False, message

        async def _is_vehicle_plugged_in(self, _vehicle):
            return True

    stop = _command_view_method("_stop_charging", {})
    amps = _command_view_method("_set_charging_amps", {})
    assert asyncio.run(stop(View(), "car_b")) == (False, message)
    assert asyncio.run(amps(View(), 10, "car_b")) == (False, message)


def test_manual_start_deadline_is_captured_before_slow_readiness_and_reused_for_stop():
    now = datetime(2026, 11, 1, 22, 0, tzinfo=timezone.utc)
    clock = [now]

    class _Dt:
        UTC = timezone.utc

        @staticmethod
        def utcnow():
            return clock[0]

    start = _command_view_method(
        "_start_charging",
        {"dt_util": _Dt, "timedelta": timedelta},
    )
    captured = {}

    class View:
        @staticmethod
        def _manual_action_params(_vehicle):
            return {"charger_type": "tesla"}

        @staticmethod
        def _active_non_manual_owner_message(_vehicle):
            return None

        async def _loadpoint_ready_for_manual_start(self, _vehicle, _params):
            clock[0] = now + timedelta(minutes=2)
            return True, ""

        async def _execute_manual_ev_action(self, _action, _vehicle, params, _reason):
            captured["params"] = params
            clock[0] = now + timedelta(minutes=3)
            return True, ""

        def _schedule_manual_quick_stop(self, *_args, **kwargs):
            captured["stops_at"] = kwargs["stops_at"]
            return kwargs["stops_at"].isoformat()

    success, message = asyncio.run(start(View(), "VIN", 1, "standard"))

    expected = now + timedelta(minutes=1)
    assert success is False
    assert message == "EV charge request expired before completion"
    assert captured["params"]["expires_at"] == expected.isoformat()
    assert captured["params"]["_boost_start_deadline"] == expected.timestamp()
    assert captured["stops_at"] == expected


def test_manual_start_cancellation_does_not_arm_a_new_quick_stop():
    now = datetime(2026, 11, 1, 22, 0, tzinfo=timezone.utc)

    class _Dt:
        UTC = timezone.utc

        @staticmethod
        def utcnow():
            return now

    start = _command_view_method(
        "_start_charging",
        {"dt_util": _Dt, "timedelta": timedelta},
    )
    scheduled = []

    class View:
        @staticmethod
        def _manual_action_params(_vehicle):
            return {"charger_type": "tesla"}

        @staticmethod
        def _active_non_manual_owner_message(_vehicle):
            return None

        async def _loadpoint_ready_for_manual_start(self, _vehicle, _params):
            return True, ""

        async def _execute_manual_ev_action(self, *_args):
            raise asyncio.CancelledError

        def _schedule_manual_quick_stop(self, *_args, **_kwargs):
            scheduled.append(True)
            return "unexpected"

    try:
        asyncio.run(start(View(), "VIN", 1, "standard"))
    except asyncio.CancelledError:
        pass
    else:
        raise AssertionError("cancellation must propagate")
    assert scheduled == []


def test_policy_start_deadline_is_passed_to_dynamic_action_and_not_extended():
    now = datetime(2026, 11, 1, 22, 0, tzinfo=timezone.utc)
    clock = [now]

    class _Dt:
        UTC = timezone.utc

        @staticmethod
        def utcnow():
            return clock[0]

    action = SimpleNamespace(
        action_type="start_ev_charging_dynamic",
        params={
            "duration_minutes": 1,
            "quick_control": True,
            "source_mode": "solar_only",
        },
        label="Solar-only EV charging started",
    )
    start = _command_view_method(
        "_start_policy_charging",
        {
            "dt_util": _Dt,
            "timedelta": timedelta,
            "build_ev_policy_action": lambda *_args: action,
        },
    )
    captured = {}

    class View:
        @staticmethod
        def _active_non_manual_owner_message(_vehicle):
            return None

        @staticmethod
        def _manual_action_params(_vehicle):
            return {"charger_type": "tesla"}

        async def _loadpoint_ready_for_manual_start(self, _vehicle, _params):
            return True, ""

        async def _execute_manual_ev_action(self, _action, _vehicle, params, _reason):
            captured["params"] = params
            clock[0] = now + timedelta(minutes=2)
            return True, ""

        def _schedule_policy_quick_stop(self, *_args, **kwargs):
            captured["stops_at"] = kwargs["stops_at"]
            return kwargs["stops_at"].isoformat()

    success, message = asyncio.run(start(View(), "solar_only", "VIN", 1))

    expected = now + timedelta(minutes=1)
    assert success is False
    assert message == "EV charge request expired before completion"
    assert captured["params"]["expires_at"] == expected.isoformat()
    assert captured["params"]["_boost_start_deadline"] == expected.timestamp()
    assert captured["stops_at"] == expected


def test_full_policy_start_passes_its_deadline_to_manual_start():
    now = datetime(2026, 11, 1, 22, 0, tzinfo=timezone.utc)

    class _Dt:
        UTC = timezone.utc

        @staticmethod
        def utcnow():
            return now

    action = SimpleNamespace(
        action_type="start_ev_charging",
        params={"duration_minutes": 1, "source_mode": "grid_allowed"},
    )
    start = _command_view_method(
        "_start_policy_charging",
        {
            "dt_util": _Dt,
            "timedelta": timedelta,
            "build_ev_policy_action": lambda *_args: action,
        },
    )
    captured = {}

    class View:
        async def _start_charging(self, vehicle, duration, source_mode, *, stops_at):
            captured.update(
                vehicle=vehicle,
                duration=duration,
                source_mode=source_mode,
                stops_at=stops_at,
            )
            return True, "started"

    success, message = asyncio.run(start(View(), "full_grid_solar", "VIN", 1))

    assert (success, message) == (True, "started")
    assert captured == {
        "vehicle": "VIN",
        "duration": 1,
        "source_mode": "grid_allowed",
        "stops_at": now + timedelta(minutes=1),
    }


def test_manual_stop_hold_is_recorded_before_serialized_action_wait():
    stop = _command_view_method("_stop_charging", {})
    events = []

    class View:
        @staticmethod
        def _manual_action_params(_vehicle):
            return {"charger_type": "tesla"}

        def _record_manual_stop_hold(self, _vehicle, reason):
            events.append(("hold", reason))

        async def _execute_manual_ev_action(self, *_args):
            events.append(("action",))
            await asyncio.sleep(0)
            return False, "stop blocked"

    result = asyncio.run(stop(View(), "VIN"))

    assert result == (False, "stop blocked")
    assert events == [("hold", "Manual stop from mobile"), ("action",)]


def test_anonymous_manual_stop_hold_uses_one_active_canonical_loadpoint():
    recorded = []
    entry = SimpleNamespace(entry_id="entry-1")
    ev_actions = SimpleNamespace(
        _dynamic_ev_state={
            "entry-1": {
                "VIN-A": {"active": True},
                "_default": {"active": False},
            }
        }
    )
    record = _command_view_method(
        "_record_manual_stop_hold",
        {
            "ev_actions": ev_actions,
            "record_manual_stop_hold": lambda _hass, _entry, vehicle, **kwargs: recorded.append(
                (vehicle, kwargs["reason"])
            ),
        },
    )

    class View:
        _hass = object()

        @staticmethod
        def _get_powersync_entry():
            return entry

    record(View(), None, "early stop")

    assert recorded == [("VIN-A", "early stop")]


def test_anonymous_manual_stop_hold_skips_ambiguous_active_loadpoints():
    recorded = []
    entry = SimpleNamespace(entry_id="entry-1")
    ev_actions = SimpleNamespace(
        _dynamic_ev_state={
            "entry-1": {
                "VIN-A": {"active": True},
                "VIN-B": {"active": True},
            }
        }
    )
    record = _command_view_method(
        "_record_manual_stop_hold",
        {
            "ev_actions": ev_actions,
            "record_manual_stop_hold": lambda *_args, **_kwargs: recorded.append(True),
        },
    )

    class View:
        _hass = object()

        @staticmethod
        def _get_powersync_entry():
            return entry

    record(View(), None, "ambiguous stop")

    assert recorded == []


def test_anonymous_manual_stop_hold_skips_when_no_loadpoint_is_active():
    recorded = []
    entry = SimpleNamespace(entry_id="entry-1")
    ev_actions = SimpleNamespace(_dynamic_ev_state={"entry-1": {}})
    record = _command_view_method(
        "_record_manual_stop_hold",
        {
            "ev_actions": ev_actions,
            "record_manual_stop_hold": lambda *_args, **_kwargs: recorded.append(True),
        },
    )

    class View:
        _hass = object()

        @staticmethod
        def _get_powersync_entry():
            return entry

    record(View(), None, "unknown stop")

    assert recorded == []


def test_manual_ble_wake_uses_shared_exact_car_wait_before_native_status():
    """Unknown Fleet status must not turn a BLE Wake request into a no-op."""
    for provider in ("tesla_ble", "both"):
        wake = AsyncMock(return_value=True)
        entry = object()
        hass = object()
        resolved = []

        def resolve(actual_hass, actual_entry, vehicle):
            resolved.append((actual_hass, actual_entry, vehicle))
            return "second_bridge"

        view = SimpleNamespace(
            _hass=hass,
            _get_powersync_config=lambda: {"ev_provider": provider},
            _get_powersync_entry=lambda: entry,
            _is_vehicle_asleep=AsyncMock(side_effect=AssertionError("native cache is not BLE wake proof")),
        )
        method = _command_view_method("_wake_vehicle", {
            "CONF_EV_PROVIDER": "ev_provider",
            "EV_PROVIDER_FLEET_API": "fleet_api",
            "EV_PROVIDER_TESLA_BLE": "tesla_ble",
            "EV_PROVIDER_BOTH": "both",
            "EV_PROVIDER_TESLEMETRY_BT": "teslemetry_bt",
            "_resolve_ble_prefix_for_vehicle": resolve,
            "_is_ble_available": lambda *_args: True,
            "_wake_tesla_ble": wake,
        })

        assert asyncio.run(method(view, "second-car-vin")) is True
        assert resolved == [(hass, entry, "second-car-vin")]
        wake.assert_awaited_once_with(
            hass, "second_bridge", wait_timeout=120, allow_backoff=False,
        )


def test_manual_ble_wake_keeps_failed_or_quarantined_bridge_unconfirmed():
    """A failed wake stays failed and quarantine cannot be bypassed by HTTP."""
    for available in (True, False):
        wake = AsyncMock(return_value=False)
        view = SimpleNamespace(
            _hass=object(),
            _get_powersync_config=lambda: {"ev_provider": "tesla_ble"},
            _get_powersync_entry=lambda: object(),
        )
        method = _command_view_method("_wake_vehicle", {
            "CONF_EV_PROVIDER": "ev_provider",
            "EV_PROVIDER_FLEET_API": "fleet_api",
            "EV_PROVIDER_TESLA_BLE": "tesla_ble",
            "EV_PROVIDER_BOTH": "both",
            "EV_PROVIDER_TESLEMETRY_BT": "teslemetry_bt",
            "_resolve_ble_prefix_for_vehicle": lambda *_args: "second_bridge",
            "_is_ble_available": lambda *_args: available,
            "_wake_tesla_ble": wake,
        })

        assert asyncio.run(method(view, "second-car-vin")) is False
        assert wake.await_count == int(available)
