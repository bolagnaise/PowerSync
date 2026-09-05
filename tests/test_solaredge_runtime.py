"""Regression coverage for SolarEdge runtime startup routing."""

from __future__ import annotations

import ast
import asyncio
import copy
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

ROOT = Path(__file__).resolve().parent.parent
INIT_PATH = ROOT / "custom_components" / "power_sync" / "__init__.py"


def _async_setup_entry_source() -> str:
    source = INIT_PATH.read_text()
    module = ast.parse(source)
    for node in module.body:
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "async_setup_entry":
            segment = ast.get_source_segment(source, node)
            assert segment is not None
            return segment
    raise AssertionError("async_setup_entry not found")


def test_solaredge_runtime_bypasses_tesla_credentials():
    setup_source = _async_setup_entry_source()
    pre_tesla_setup = setup_source[
        : setup_source.index(
            "tesla_api_token, tesla_api_provider = get_tesla_api_token"
        )
    ]

    assert (
        "active_battery_system = _active_battery_system(entry, hass)" in pre_tesla_setup
    )
    assert (
        "is_solaredge = active_battery_system == BATTERY_SYSTEM_SOLAREDGE"
        in pre_tesla_setup
    )
    assert "elif is_solaredge:" in pre_tesla_setup
    assert (
        "Running in SolarEdge mode - Tesla credentials not required" in pre_tesla_setup
    )


def test_solaredge_runtime_wires_energy_coordinator():
    setup_source = _async_setup_entry_source()

    assert "solaredge_coordinator = None" in setup_source
    assert "SolarEdgeEnergyCoordinator(" in setup_source
    assert '"solaredge_coordinator": solaredge_coordinator' in setup_source
    assert "elif is_solaredge:" in setup_source
    assert 'battery_system = "solaredge"' in setup_source
    assert "energy_coordinator = solaredge_coordinator" in setup_source


def _setup_node(name):
    module = ast.parse(INIT_PATH.read_text())
    setup = next(
        n for n in module.body if getattr(n, "name", None) == "async_setup_entry"
    )
    return next(n for n in setup.body if getattr(n, "name", None) == name)


def _load_node(node, namespace):
    node = copy.deepcopy(node)
    node.returns = None
    for arg in node.args.args:
        arg.annotation = None
    exec(  # noqa: S102 - Execute the repository function under test.
        compile(
            ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[])),
            str(INIT_PATH),
            "exec",
        ),
        namespace,
    )
    return namespace[node.name]


@pytest.mark.parametrize("direction", ["charge", "discharge"])
@pytest.mark.parametrize("release_ok", [True, False])
def test_optimizer_solaredge_failure_propagates(direction, release_ok):
    """Run the actual SolarEdge hardware-refresh branch with a failed write."""
    handler = _setup_node(f"handle_force_{direction}")
    branch = next(
        n
        for n in ast.walk(handler)
        if isinstance(n, ast.If)
        and isinstance(n.test, ast.Name)
        and n.test.id == "solaredge_coord"
    )
    handler.body = branch.body
    coordinator = SimpleNamespace(
        **{f"force_{direction}": AsyncMock(return_value=False)}
    )
    release = AsyncMock(return_value=release_ok)

    async def guarded(write):
        return await write(2000)

    namespace = {
        "solaredge_coord": coordinator,
        "entry_data": {},
        "source": "optimizer",
        "duration": 15,
        "power_w": 2000,
        "_restore_solaredge_curtailment_for_dispatch": release,
        "_guarded_force_discharge_write": guarded,
        "_LOGGER": logging.getLogger(__name__),
        "HomeAssistantError": RuntimeError,
    }
    call = _load_node(handler, namespace)
    with pytest.raises(RuntimeError, match="SolarEdge"):
        asyncio.run(call(None))
    if not release_ok:
        getattr(coordinator, f"force_{direction}").assert_not_awaited()


def test_degraded_solaredge_does_not_release_curtailment():
    direct = SimpleNamespace(restore=AsyncMock(return_value=True))
    coordinator = SimpleNamespace(
        control_health="reconciliation_required", mutation_active=False
    )
    namespace = {
        "_get_solaredge_curtailment_controller": Mock(return_value=direct),
        "_LOGGER": logging.getLogger(__name__),
    }
    call = _load_node(
        _setup_node("_restore_solaredge_curtailment_for_dispatch"), namespace
    )
    result = asyncio.run(
        call(
            {
                "solaredge_coordinator": coordinator,
                "solaredge_curtailment_state": "curtailed",
            },
            "force charge",
        )
    )
    assert result is False
    direct.restore.assert_not_awaited()


@pytest.mark.parametrize("direction", ["charge", "discharge"])
@pytest.mark.parametrize("release_ok", [True, False])
def test_manual_solaredge_failure_does_not_arm_timer(direction, release_ok):
    handler = _setup_node(f"handle_force_{direction}")
    branch = next(
        n
        for n in handler.body
        if isinstance(n, ast.If)
        and isinstance(n.test, ast.Name)
        and n.test.id == "is_solaredge_local"
    )
    handler.body = branch.body
    coord = SimpleNamespace(**{f"force_{direction}": AsyncMock(return_value=False)})
    data = {"solaredge_coordinator": coord}
    notify = AsyncMock()
    hass = SimpleNamespace(
        data={"power_sync": {"entry": data}},
        async_create_task=lambda coroutine: coroutine.close(),
    )
    timer = Mock()
    dispatch = Mock()

    async def guarded(write):
        return await write(2000)

    namespace = {
        "hass": hass,
        "entry": SimpleNamespace(entry_id="entry"),
        "DOMAIN": "power_sync",
        "source": "user",
        "duration": 15,
        "command_power_w": 2000,
        "force_charge_state": {"active": False},
        "force_discharge_state": {"active": False},
        "_restore_solaredge_curtailment_for_dispatch": AsyncMock(
            return_value=release_ok
        ),
        "_guarded_force_discharge_write": guarded,
        "_LOGGER": logging.getLogger(__name__),
        "HomeAssistantError": RuntimeError,
        "_notify_api_error": notify,
        "async_track_point_in_utc_time": timer,
        "async_dispatcher_send": dispatch,
    }
    call = _load_node(handler, namespace)
    with pytest.raises(RuntimeError, match="SolarEdge"):
        asyncio.run(call(None))
    timer.assert_not_called()
    dispatch.assert_not_called()
    if not release_ok:
        getattr(coord, f"force_{direction}").assert_not_awaited()


def test_solaredge_startup_does_not_replay_persisted_force():
    service = AsyncMock()
    namespace = {
        "active_battery_system": "solaredge",
        "BATTERY_SYSTEM_SOLAREDGE": "solaredge",
        "_LOGGER": logging.getLogger(__name__),
        "hass": SimpleNamespace(services=SimpleNamespace(async_call=service)),
    }
    call = _load_node(_setup_node("restore_force_mode_from_persistence"), namespace)
    asyncio.run(call())
    service.assert_not_awaited()


def test_degraded_monitoring_handoff_performs_no_cleanup_writes():
    path = ROOT / "custom_components" / "power_sync" / "monitoring.py"
    node = next(
        n
        for n in ast.parse(path.read_text()).body
        if getattr(n, "name", None) == "async_prepare_monitoring_handoff"
    )
    service = AsyncMock()
    data = {
        "solaredge_coordinator": SimpleNamespace(
            control_health="reconciliation_required"
        ),
        "force_charge_state": {"active": True},
    }
    hass = SimpleNamespace(
        data={"power_sync": {"entry": data}},
        services=SimpleNamespace(async_call=service),
    )
    namespace = {
        "DOMAIN": "power_sync",
        "SERVICE_RESTORE_NORMAL": "restore_normal",
        "_HANDOFF_ACTIVE": "_monitoring_handoff_active",
    }
    call = _load_node(node, namespace)
    asyncio.run(call(hass, SimpleNamespace(entry_id="entry")))
    service.assert_not_awaited()
    assert data["force_charge_state"]["active"] is True


def test_reconciliation_service_is_explicit_and_scoped():
    coordinator = SimpleNamespace(
        reconcile=AsyncMock(return_value=True), control_health="ready"
    )
    namespace = {
        "hass": SimpleNamespace(
            data={"power_sync": {"entry": {"solaredge_coordinator": coordinator}}}
        ),
        "DOMAIN": "power_sync",
        "HomeAssistantError": RuntimeError,
    }
    call = _load_node(_setup_node("handle_reconcile_solaredge_control"), namespace)
    with pytest.raises(RuntimeError, match="acknowledge"):
        asyncio.run(call(SimpleNamespace(data={"entry_id": "entry"})))
    coordinator.reconcile.assert_not_awaited()
    result = asyncio.run(
        call(SimpleNamespace(data={"entry_id": "entry", "acknowledge": True}))
    )
    assert result == {"success": True, "control_health": "ready"}
    coordinator.reconcile.assert_awaited_once_with()


def test_solaredge_restore_forwards_timer_generation_and_failure():
    node = _setup_node("handle_restore_normal")
    branch = next(
        n
        for n in node.body
        if isinstance(n, ast.If)
        and isinstance(n.test, ast.Name)
        and n.test.id == "is_solaredge_local"
    )
    node.body = branch.body
    coord = SimpleNamespace(restore_normal=AsyncMock(return_value=False))
    namespace = {
        "hass": SimpleNamespace(
            data={"power_sync": {"entry": {"solaredge_coordinator": coord}}}
        ),
        "entry": SimpleNamespace(entry_id="entry"),
        "DOMAIN": "power_sync",
        "source": "force_timer",
        "HomeAssistantError": RuntimeError,
    }
    call = _load_node(node, namespace)
    with pytest.raises(RuntimeError, match="SolarEdge"):
        asyncio.run(call(SimpleNamespace(data={"_solaredge_generation": 7})))
    coord.restore_normal.assert_awaited_once_with(
        automatic=False, expected_generation=7
    )


def test_solaredge_coordinator_publishes_failure_health():
    path = ROOT / "custom_components" / "power_sync" / "coordinator.py"
    klass = next(
        n
        for n in ast.parse(path.read_text()).body
        if getattr(n, "name", None) == "SolarEdgeEnergyCoordinator"
    )
    namespace = {}
    operation = AsyncMock(return_value=False)
    controller = SimpleNamespace(
        get_status=lambda: {
            "control_health": "reconciliation_required",
            "last_mutation": {"outcome": "unknown"},
            "mutation_active": False,
        }
    )
    update = Mock()
    coord = SimpleNamespace(
        data={"battery_level": 50},
        _controller=controller,
        async_set_updated_data=update,
    )
    call = _load_node(
        next(n for n in klass.body if getattr(n, "name", None) == "_control_result"),
        namespace,
    )
    assert asyncio.run(call(coord, operation())) is False
    assert update.call_args.args[0] == {
        "battery_level": 50,
        "control_health": "reconciliation_required",
        "last_mutation": {"outcome": "unknown"},
        "mutation_active": False,
    }


@pytest.mark.parametrize(
    "service, method, marker",
    [
        ("handle_set_self_consumption", "restore_normal", "is_solaredge_sc"),
        ("handle_set_backup_reserve", "set_backup_reserve", None),
    ],
)
def test_solaredge_settings_failure_reaches_service_caller(service, method, marker):
    handler = _setup_node(service)
    if marker:
        branch = next(
            n
            for n in ast.walk(handler)
            if isinstance(n, ast.If)
            and isinstance(n.test, ast.Name)
            and n.test.id == marker
        )
        handler.body = branch.body
    else:
        # Select the actual SolarEdge reserve try block, including its handlers.
        handler.body = [
            next(
                n
                for n in ast.walk(handler)
                if isinstance(n, ast.Try)
                and any(
                    isinstance(c, ast.Call)
                    and isinstance(c.func, ast.Attribute)
                    and c.func.attr == "set_backup_reserve"
                    and isinstance(c.func.value, ast.Name)
                    and c.func.value.id == "solaredge_coord"
                    for c in ast.walk(n)
                )
            )
        ]
    coord = SimpleNamespace(**{method: AsyncMock(return_value=False)})

    async def guarded(write):
        return await write()

    namespace = {
        "hass": SimpleNamespace(
            data={"power_sync": {"entry": {"solaredge_coordinator": coord}}}
        ),
        "entry": SimpleNamespace(entry_id="entry"),
        "DOMAIN": "power_sync",
        "source": "optimizer",
        "percent": 25,
        "_LOGGER": logging.getLogger(__name__),
        "HomeAssistantError": RuntimeError,
        "_control_call_source": lambda call: "optimizer",
        "_guarded_self_consumption_write": guarded,
    }
    call = _load_node(handler, namespace)
    with pytest.raises(RuntimeError, match="SolarEdge"):
        asyncio.run(call(None))
    assert getattr(coord, method).call_args.kwargs["automatic"] is True


@pytest.mark.parametrize("result", [True, False])
def test_curtailment_client_closes_before_mutation_lock_is_released(result):
    events = []

    async def operate():
        events.append("write")
        return result

    async def disconnect():
        events.append("disconnect")

    async def protected(callback, *, automatic):
        assert automatic is True
        events.append("lock")
        success = await callback()
        events.append("unlock")
        return success

    call = _load_node(_setup_node("_solaredge_curtailment_write"), {})
    assert (
        asyncio.run(
            call(
                SimpleNamespace(run_external_mutation=protected),
                SimpleNamespace(disconnect=disconnect),
                operate,
            )
        )
        is result
    )
    assert events == ["lock", "write", "disconnect", "unlock"]


@pytest.mark.parametrize(
    "difference", [None, "inverter_host", "inverter_port", "inverter_slave_id"]
)
def test_same_solaredge_ac_inverter_is_blocked_but_separate_device_remains(difference):
    node = _setup_node("ac_inverter_is_same_hybrid")
    config = {
        "inverter_brand": "solaredge",
        "inverter_host": "192.0.2.10",
        "inverter_port": 1502,
        "inverter_slave_id": 1,
        "solaredge_host": "192.0.2.10",
        "solaredge_port": 1502,
        "solaredge_slave_id": 1,
        "battery_system": "solaredge",
    }
    if difference:
        config[difference] = "192.0.2.11" if difference.endswith("host") else 2
    namespace = {
        "is_sungrow": False,
        "is_solaredge": True,
        "entry": SimpleNamespace(data=config, options={}),
    }
    for key in config:
        namespace[f"CONF_{key.upper()}"] = key
    namespace.update(
        DEFAULT_INVERTER_PORT=502,
        DEFAULT_INVERTER_SLAVE_ID=1,
        DEFAULT_SOLAREDGE_PORT=1502,
        DEFAULT_SOLAREDGE_SLAVE_ID=1,
    )
    helper = next(
        n
        for n in ast.parse(INIT_PATH.read_text()).body
        if getattr(n, "name", None) == "_solaredge_ac_inverter_matches_battery"
    )
    namespace["_active_battery_system"] = lambda entry: entry.options.get(
        "battery_system", entry.data.get("battery_system")
    )
    namespace["BATTERY_SYSTEM_SOLAREDGE"] = "solaredge"
    _load_node(helper, namespace)
    call = _load_node(node, namespace)
    assert call() is (difference is None)


@pytest.mark.parametrize(
    "name, result",
    [
        ("apply_inverter_curtailment", False),
        ("fast_load_following_update", None),
        ("handle_curtail_inverter", None),
        ("handle_restore_inverter", None),
    ],
)
def test_duplicate_solaredge_generic_control_returns_before_controller_creation(
    name, result
):
    node = _setup_node(name)
    namespace = {
        "ac_inverter_is_same_hybrid": lambda: True,
        "_aemo_dispatch_entry_data": dict,
        "_LOGGER": logging.getLogger(__name__),
        "entry": SimpleNamespace(data={}, options={"enabled": True}),
        "CONF_AC_INVERTER_CURTAILMENT_ENABLED": "enabled",
        "INVERTER_CONTROL_MODE_LOAD_FOLLOWING": "load_following",
        "INVERTER_CONTROL_MODE_SHUTDOWN": "shutdown",
    }
    call = _load_node(node, namespace)
    argument = SimpleNamespace(data={}) if name.startswith("handle_") else True
    assert asyncio.run(call(argument)) is result


@pytest.mark.parametrize(
    "operation", ["force_charge", "force_discharge", "restore_normal"]
)
@pytest.mark.parametrize("source", ["user", "optimizer"])
def test_solaredge_confirmed_manual_service_returns_response_dict(operation, source):
    """Response-requesting HA calls need a dict after confirmed hardware success."""
    from datetime import datetime, timedelta, timezone

    handler = _setup_node(f"handle_{operation}")
    branch = next(
        n
        for n in handler.body
        if isinstance(n, ast.If)
        and isinstance(n.test, ast.Name)
        and n.test.id == "is_solaredge_local"
    )
    if source == "optimizer" and operation.startswith("force_"):
        branch = next(
            n
            for n in ast.walk(handler)
            if isinstance(n, ast.If)
            and isinstance(n.test, ast.Name)
            and n.test.id == "solaredge_coord"
        )
    handler.body = branch.body
    coordinator = SimpleNamespace(
        generation=3, **{operation: AsyncMock(return_value=True)}
    )

    async def guarded(write):
        return await write(500)

    namespace = {
        "hass": SimpleNamespace(
            data={"power_sync": {"entry": {"solaredge_coordinator": coordinator}}}
        ),
        "entry": SimpleNamespace(entry_id="entry"),
        "DOMAIN": "power_sync",
        "source": source,
        "solaredge_coord": coordinator,
        "entry_data": {"solaredge_coordinator": coordinator},
        "power_w": 500,
        "command_power_w": 500,
        "duration": 15,
        "force_charge_state": {"active": False},
        "force_discharge_state": {"active": False},
        "hold_soc_state": {"active": False},
        "self_consumption_state": {"active": False},
        "_restore_solaredge_curtailment_for_dispatch": AsyncMock(return_value=True),
        "_guarded_force_discharge_write": guarded,
        "_restore_superseded": lambda _reason: False,
        "dt_util": SimpleNamespace(utcnow=lambda: datetime.now(timezone.utc)),
        "timedelta": timedelta,
        "_LOGGER": logging.getLogger(__name__),
        "HomeAssistantError": RuntimeError,
        "async_dispatcher_send": Mock(),
        "async_track_point_in_utc_time": Mock(),
        "persist_force_mode_state": AsyncMock(),
        "suppress_notification": True,
    }
    call = _load_node(handler, namespace)
    response = asyncio.run(call(SimpleNamespace(data={})))
    assert isinstance(response, dict)
    assert response["success"] is True
