"""Regression coverage for Sungrow export-limit curtailment routing."""

from __future__ import annotations

import asyncio
import ast
import importlib.util
from pathlib import Path
import textwrap
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parent.parent
INIT_PATH = ROOT / "custom_components" / "power_sync" / "__init__.py"
ACTIONS_PATH = ROOT / "custom_components" / "power_sync" / "automations" / "actions.py"
SUNGROW_INVERTER_PATH = ROOT / "custom_components" / "power_sync" / "inverters" / "sungrow.py"
TARIFF_UTILS_PATH = ROOT / "custom_components" / "power_sync" / "tariff_utils.py"


def _load_with_hysteresis():
    """Load the real HD-15/HD-24 hysteresis helper from tariff_utils.py."""
    spec = importlib.util.spec_from_file_location(
        "power_sync_tariff_utils_for_curtailment_test", TARIFF_UTILS_PATH
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module.with_hysteresis


def _function_source(name: str) -> str:
    source = INIT_PATH.read_text()
    module = ast.parse(source)
    for node in module.body:
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "async_setup_entry":
            for child in node.body:
                if isinstance(child, (ast.AsyncFunctionDef, ast.FunctionDef)) and child.name == name:
                    segment = ast.get_source_segment(source, child)
                    assert segment is not None
                    return segment
    raise AssertionError(f"{name} not found")


def _top_level_function_source(name: str) -> str:
    source = INIT_PATH.read_text()
    module = ast.parse(source)
    for node in module.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            segment = ast.get_source_segment(source, node)
            assert segment is not None
            return segment
    raise AssertionError(f"{name} not found")


def _actions_function_source(name: str) -> str:
    source = ACTIONS_PATH.read_text()
    module = ast.parse(source)
    for node in module.body:
        if isinstance(node, ast.AsyncFunctionDef) and node.name == name:
            segment = ast.get_source_segment(source, node)
            assert segment is not None
            return segment
    raise AssertionError(f"{name} not found")


def _class_method_source(path: Path, class_name: str, method_name: str) -> str:
    source = path.read_text()
    module = ast.parse(source)
    for node in module.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for child in node.body:
                if isinstance(child, ast.AsyncFunctionDef) and child.name == method_name:
                    segment = ast.get_source_segment(source, child)
                    assert segment is not None
                    return segment
    raise AssertionError(f"{class_name}.{method_name} not found")


def test_sungrow_has_native_export_limit_curtailment_handler():
    handler = _function_source("handle_sungrow_curtailment")

    assert "sungrow_curtailment_state" in handler
    assert "sungrow_power_limit_w" in handler
    assert "get_current_prices_for_curtailment" in handler
    assert "export_limit_w = 0" in handler
    assert "await sungrow_coord.set_export_limit(export_limit_w)" in handler
    assert "await sungrow_coord.set_export_limit(None)" in handler
    assert "ac_inverter_is_same_hybrid" in handler
    assert "await apply_inverter_curtailment(" in handler


def test_sungrow_native_curtailment_uses_zero_site_export_not_home_load_limit():
    handler = _function_source("handle_sungrow_curtailment")

    load_index = handler.index("home_load_w = int(live_status.get(\"load_power\", 0))")
    target_index = handler.index("export_limit_w = 0")
    command_index = handler.index("await sungrow_coord.set_export_limit(export_limit_w)")

    assert load_index < target_index < command_index
    assert "await sungrow_coord.set_export_limit(home_load_w)" not in handler
    assert "zero-export limit" in handler


def test_sungrow_curtailment_releases_limit_for_active_solar_surplus_ev():
    helper = _function_source("_active_solar_surplus_ev_needs_inverter_headroom")
    handler = _function_source("handle_sungrow_curtailment")

    assert 'solar_config.get("enabled", False)' in helper
    assert "is_ev_plugged_in" in helper
    assert "get_ev_battery_level" in helper
    assert 'state.get("paused")' in helper
    assert "_active_solar_surplus_ev_needs_inverter_headroom" in handler
    assert "should_curtail_for_price = export_uneconomic and not ev_needs_headroom" in handler
    assert "solar surplus EV needs PV headroom" in handler
    assert "curtail=should_curtail_for_price" in handler


def test_ev_live_status_does_not_force_cached_sungrow_curtailment_state():
    source = _actions_function_source("_get_tesla_live_status")

    assert "sungrow_curtailment_state" not in source
    assert 'entry_data.get("inverter_last_state") == "curtailed"' in source
    assert 'live_status["is_curtailed"] = True' in source


def test_inverter_status_api_uses_ac_inverter_state_not_native_sungrow_state():
    source = INIT_PATH.read_text()
    status_section = source[
        source.index("class InverterStatusView"):
        source.index("class SigenergyTariffView")
    ]

    assert "sungrow_curtailment_state" not in status_section
    assert 'inverter_last_state == "curtailed"' in status_section
    assert 'inverter_last_state in ("normal", "running")' in status_section


def test_ac_inverter_restore_keeps_heartbeat_but_skips_sungrow_verify_readback():
    source = _function_source("apply_inverter_curtailment")

    controller_index = source.index("controller = get_inverter_controller(")
    restore_index = source.index("_LOGGER.info(f\"🟢 Restoring inverter")
    signature_index = source.index("restore_sig = inspect.signature(controller.restore)")
    verify_false_index = source.index("await controller.restore(verify=False)")

    assert signature_index < verify_false_index
    assert controller_index < restore_index < verify_false_index


def _stale_ac_curtailment_case(kind: str, replace_during: str | None = None):
    """Execute AC curtailment with a setup generation that is no longer live."""
    entry_id = "entry-id"
    old_generation = object()
    replacement_generation = object()
    if kind == "replacement":
        live_entry_data = {
            "aemo_dispatch_generation": replacement_generation,
        }
    elif kind == "absent":
        live_entry_data = None
    elif kind == "stopping":
        live_entry_data = {
            "aemo_dispatch_generation": old_generation,
            "aemo_dispatch_stopping": True,
        }
    else:  # pragma: no cover - parameterized values are fixed below
        raise AssertionError(kind)

    if replace_during is not None:
        live_entry_data = {"aemo_dispatch_generation": old_generation}

    domain_data = {} if live_entry_data is None else {entry_id: live_entry_data}
    replacement_entry_data = {
        "aemo_dispatch_generation": replacement_generation,
        "replacement_state": "untouched",
    }
    initial_domain_data = dict(domain_data)
    hass = SimpleNamespace(data={"power_sync": domain_data})
    entry = SimpleNamespace(
        entry_id=entry_id,
        options={},
        data={
            "ac_inverter_curtailment_enabled": True,
            "inverter_brand": "sungrow",
            "inverter_host": "192.0.2.10",
        },
    )
    calls = {"construct": 0, "fallback": [], "commands": [], "state": []}

    class _Logger:
        def __getattr__(self, _name):
            return lambda *args, **kwargs: None

    def _get_inverter_controller(**_kwargs):
        calls["construct"] += 1

        async def _curtail(**_kwargs):
            calls["commands"].append("curtail")
            if replace_during == "command_exception":
                hass.data["power_sync"][entry_id] = replacement_entry_data
                raise RuntimeError("stale controller failed after reload")
            return True

        async def _restore(**_kwargs):
            calls["commands"].append("restore")
            return True

        return SimpleNamespace(curtail=_curtail, restore=_restore)

    async def _should_curtail(*_args):
        await asyncio.sleep(0)
        if replace_during == "decision":
            hass.data["power_sync"][entry_id] = replacement_entry_data
        return replace_during in ("live_status", "command_exception")

    async def _get_live_status():
        await asyncio.sleep(0)
        if replace_during == "live_status":
            hass.data["power_sync"][entry_id] = replacement_entry_data
        return {"load_power": 100, "battery_power": 0}

    async def _fallback(curtail, reason):
        calls["fallback"].append((curtail, reason))
        return False

    namespace = {
        "Any": object,
        "DOMAIN": "power_sync",
        "hass": hass,
        "entry": entry,
        "aemo_dispatch_generation": old_generation,
        "_LOGGER": _Logger(),
        "get_inverter_controller": _get_inverter_controller,
        "should_curtail_ac_coupled": _should_curtail,
        "_powerwall_curtailment_fallback": _fallback,
        "_set_inverter_control_state": lambda *args, **kwargs: calls["state"].append(args),
        "get_live_status": _get_live_status,
        "INVERTER_CONTROL_MODE_NORMAL": "normal",
        "INVERTER_CONTROL_MODE_LOAD_FOLLOWING": "load_following",
        "INVERTER_CONTROL_MODE_CURTAILED": "curtailed",
        "INVERTER_CONTROL_MODES": {"normal", "load_following", "curtailed"},
        "DEFAULT_INVERTER_PORT": 502,
        "DEFAULT_INVERTER_SLAVE_ID": 1,
    }
    for constant in (
        "CONF_AC_INVERTER_CURTAILMENT_ENABLED",
        "CONF_POWERWALL_OFFGRID_AS_CURTAILMENT",
        "CONF_INVERTER_BRAND",
        "CONF_INVERTER_HOST",
        "CONF_INVERTER_PORT",
        "CONF_INVERTER_SLAVE_ID",
        "CONF_INVERTER_MODEL",
        "CONF_INVERTER_TOKEN",
        "CONF_FRONIUS_LOAD_FOLLOWING",
        "CONF_INVERTER_RATED_POWER_W",
        "CONF_ENPHASE_USERNAME",
        "CONF_ENPHASE_PASSWORD",
        "CONF_ENPHASE_SERIAL",
        "CONF_ENPHASE_NORMAL_PROFILE",
        "CONF_ENPHASE_ZERO_EXPORT_PROFILE",
        "CONF_ENPHASE_IS_INSTALLER",
        "CONF_SIGENERGY_EXPORT_LIMIT_KW",
        "CONF_INVERTER_ENTITY_PREFIX",
    ):
        namespace[constant] = constant.removeprefix("CONF_").lower()

    exec(_function_source("_aemo_dispatch_entry_data"), namespace)
    exec(_function_source("apply_inverter_curtailment"), namespace)
    result = asyncio.run(namespace["apply_inverter_curtailment"](True, -1.0, 0.0))
    return result, calls, domain_data, initial_domain_data, replacement_entry_data


@pytest.mark.parametrize("kind", ("replacement", "absent", "stopping"))
def test_stale_ac_curtailment_generation_fails_closed(kind):
    result, calls, domain_data, initial_domain_data, _replacement_entry_data = _stale_ac_curtailment_case(kind)

    assert result is False
    assert calls == {"construct": 0, "fallback": [], "commands": [], "state": []}
    assert domain_data == initial_domain_data


@pytest.mark.parametrize("replace_during", ("decision", "live_status"))
def test_inflight_ac_curtailment_generation_replacement_fails_closed(replace_during):
    result, calls, _domain_data, _initial_domain_data, replacement_entry_data = (
        _stale_ac_curtailment_case("replacement", replace_during=replace_during)
    )

    assert result is False
    assert calls["construct"] == 1
    assert calls["commands"] == []
    assert calls["fallback"] == []
    assert calls["state"] == []
    assert replacement_entry_data["replacement_state"] == "untouched"


def test_stale_controller_exception_does_not_fallback_through_replacement():
    result, calls, _domain_data, _initial_domain_data, replacement_entry_data = (
        _stale_ac_curtailment_case(
            "replacement",
            replace_during="command_exception",
        )
    )

    assert result is False
    assert calls["construct"] == 1
    assert calls["commands"] == ["curtail"]
    assert calls["fallback"] == []
    assert calls["state"] == []
    assert replacement_entry_data["replacement_state"] == "untouched"


@pytest.mark.parametrize("replacement", (False, True))
def test_should_curtail_live_status_await_does_not_recreate_runtime(replacement):
    """The smart decision itself must not write after its generation is gone."""
    entry_id = "entry-id"
    old_generation = object()
    replacement_generation = object()
    old_entry_data = {"aemo_dispatch_generation": old_generation}
    replacement_entry_data = {
        "aemo_dispatch_generation": replacement_generation,
        "replacement_state": "untouched",
    }
    domain_data = {entry_id: old_entry_data}
    hass = SimpleNamespace(data={"power_sync": domain_data})
    entry = SimpleNamespace(entry_id=entry_id, options={}, data={})

    async def _get_live_status():
        await asyncio.sleep(0)
        if replacement:
            domain_data[entry_id] = replacement_entry_data
        else:
            domain_data.pop(entry_id)
        return {
            "solar_power": 3958,
            "battery_power": 5,
            "grid_power": -2851.2,
            "load_power": 1112,
            "battery_soc": 98.9,
        }

    namespace = {
        "Any": object,
        "CONF_INVERTER_RESTORE_SOC": "inverter_restore_soc",
        "DEFAULT_INVERTER_RESTORE_SOC": 90,
        "DOMAIN": "power_sync",
        "aemo_dispatch_generation": old_generation,
        "_LOGGER": SimpleNamespace(
            debug=lambda *args, **kwargs: None,
            info=lambda *args, **kwargs: None,
        ),
        "entry": entry,
        "hass": hass,
        "get_live_status": _get_live_status,
        "with_hysteresis": _load_with_hysteresis(),
    }
    exec(_function_source("_aemo_dispatch_entry_data"), namespace)
    exec(textwrap.dedent(_function_source("should_curtail_ac_coupled")), namespace)

    assert asyncio.run(namespace["should_curtail_ac_coupled"](20.09, 0.0)) is False
    assert domain_data.get(entry_id) is (
        replacement_entry_data if replacement else None
    )
    assert replacement_entry_data["replacement_state"] == "untouched"


def _run_fast_load_following_case(
    kind: str,
    *,
    brand: str = "sungrow",
    current_limit: int | None = None,
    load_power: int = 100,
    grid_power: float | None = None,
    applied_device_limit_w: int | None = None,
):
    """Run the extracted timer callback against one generation lifecycle."""
    entry_id = "entry-id"
    old_generation = object()
    replacement_generation = object()
    controller_calls = []
    status_calls = []
    state_calls = []

    async def _curtail(*, home_load_w):
        controller_calls.append(home_load_w)
        return True

    # Fronius only writes a power-limit register in load-following mode; the
    # adapter records what it actually applied so the caller cannot report a
    # device limit that was never sent.
    controller = SimpleNamespace(
        curtail=_curtail,
        last_curtail_limit_w=applied_device_limit_w,
    )
    live_entry_data = {
        "aemo_dispatch_generation": old_generation,
        "inverter_control_mode": "load_following",
        "inverter_last_state": "curtailed",
        "inverter_controller": controller,
        "inverter_power_limit_w": current_limit,
    }
    if kind == "stopping":
        live_entry_data["aemo_dispatch_stopping"] = True
    replacement_entry_data = {
        "aemo_dispatch_generation": replacement_generation,
        "replacement_state": "untouched",
    }
    domain_data = {entry_id: live_entry_data}
    hass = SimpleNamespace(data={"power_sync": domain_data})
    entry = SimpleNamespace(
        entry_id=entry_id,
        options={},
        data={
            "ac_inverter_curtailment_enabled": True,
            "inverter_brand": brand,
            "inverter_host": "192.0.2.10",
        },
    )

    async def _get_live_status():
        status_calls.append(True)
        await asyncio.sleep(0)
        if kind == "replacement":
            domain_data[entry_id] = replacement_entry_data
        return {
            "load_power": load_power,
            "battery_power": 0,
            "grid_power": grid_power,
        }

    def _record_state(*args, **kwargs):
        state_calls.append(args)
        replacement_entry_data["last_state_kwargs"] = kwargs

    namespace = {
        "Any": object,
        "DOMAIN": "power_sync",
        "hass": hass,
        "entry": entry,
        "aemo_dispatch_generation": old_generation,
        "_LOGGER": SimpleNamespace(
            debug=lambda *args, **kwargs: None,
            warning=lambda *args, **kwargs: None,
        ),
        "get_live_status": _get_live_status,
        "_set_inverter_control_state": _record_state,
        "datetime": __import__("datetime").datetime,
        "timedelta": __import__("datetime").timedelta,
        "INVERTER_CONTROL_MODE_NORMAL": "normal",
        "INVERTER_CONTROL_MODE_LOAD_FOLLOWING": "load_following",
        "INVERTER_CONTROL_MODE_SHUTDOWN": "shutdown",
        "INVERTER_CONTROL_MODE_CURTAILED": "curtailed",
        "INVERTER_CONTROL_MODES": {"normal", "load_following", "shutdown", "curtailed"},
    }
    for constant in (
        "CONF_AC_INVERTER_CURTAILMENT_ENABLED",
        "CONF_INVERTER_BRAND",
        "CONF_INVERTER_HOST",
    ):
        namespace[constant] = constant.removeprefix("CONF_").lower()

    exec(_function_source("_aemo_dispatch_entry_data"), namespace)
    exec(_function_source("fast_load_following_update"), namespace)
    result = asyncio.run(namespace["fast_load_following_update"](SimpleNamespace(second=0)))
    return result, status_calls, controller_calls, state_calls, replacement_entry_data


@pytest.mark.parametrize("kind", ("stopping", "replacement", "live"))
def test_fast_load_following_update_generation_fence(kind):
    result, status_calls, controller_calls, state_calls, replacement_entry_data = (
        _run_fast_load_following_case(kind)
    )

    assert result is None
    assert status_calls == ([] if kind == "stopping" else [True])
    assert controller_calls == ([100] if kind == "live" else [])
    assert state_calls == ([('load_following', 100)] if kind == "live" else [])
    assert replacement_entry_data["replacement_state"] == "untouched"


def test_fronius_ac_inverter_uses_load_following_and_fast_refresh():
    curtail_source = _function_source("apply_inverter_curtailment")
    init_source = INIT_PATH.read_text()
    refresh_source = init_source[
        init_source.index("async def fast_load_following_update"):
        init_source.index("# Set up automatic AEMO spike check")
    ]

    assert '"fronius"' in curtail_source[
        curtail_source.index("if inverter_brand in ("):
        curtail_source.index("_LOGGER.info(f\"🔴 Curtailing inverter")
    ]
    assert '"fronius"' in refresh_source[
        refresh_source.index("if inverter_brand not in ("):
        refresh_source.index("if not inverter_host:")
    ]


def test_fronius_matching_cached_target_reapplies_while_export_persists():
    _result, status_calls, controller_calls, state_calls, replacement = (
        _run_fast_load_following_case(
            "live",
            brand="fronius",
            current_limit=1_440,
            load_power=1_440,
            grid_power=-600.0,
            applied_device_limit_w=1_440,
        )
    )

    assert status_calls == [True]
    assert controller_calls == [1_440]
    assert state_calls == [("load_following", 1_440)]
    assert replacement["last_state_kwargs"] == {
        "update_dpel_time": False,
        "device_limit_confirmed": True,
        "physical_converged": False,
        "residual_export_w": 600.0,
    }


def test_fronius_simple_mode_reapply_reports_no_device_limit():
    """Ticket #226: simple mode writes no limit, so none may be reported."""
    _result, status_calls, controller_calls, state_calls, replacement = (
        _run_fast_load_following_case(
            "live",
            brand="fronius",
            current_limit=None,
            load_power=1_066,
            grid_power=-8_477.0,
            applied_device_limit_w=None,
        )
    )

    assert status_calls == [True]
    assert controller_calls == [1_066]
    assert state_calls == [("curtailed", None)]
    assert replacement["last_state_kwargs"] == {
        "device_limit_confirmed": False,
        "physical_converged": False,
        "residual_export_w": 8_477.0,
    }


def test_fronius_initial_ack_does_not_claim_an_unwritten_limit():
    source = _function_source("apply_inverter_curtailment")

    # The apply path must distinguish the two Fronius modes...
    assert "last_curtail_limit_w" in source
    assert "Fronius simple-mode curtailment applied at %s" in source
    assert "no device power limit is set" in source
    assert "Fronius load-following limit of %dW applied at" in source
    # ...and must never re-assert the old unconditional confirmation.
    assert "Fronius limit confirmed at %dW" not in source
    assert "within the 100W convergence threshold" not in source

    # Physical convergence is owned by the 30s cycle, which re-reads live
    # status; the apply path only has telemetry captured before the command.
    success_branch = source[source.index("if success:"):]
    assert "physical_converged=" not in success_branch.split("else:")[0]


def test_sungrow_restore_can_skip_verification_readback():
    source = _class_method_source(SUNGROW_INVERTER_PATH, "SungrowController", "restore")

    assert "verify: bool = True" in source
    verify_index = source.index("if verify:")
    get_status_index = source.index("state = await self.get_status()")

    assert verify_index < get_status_index


def test_hybrid_curtailment_handlers_use_tariff_schedule_fallback():
    for handler_name, state_key in (
        ("handle_foxess_curtailment", "foxess_curtailment_state"),
        ("handle_sigenergy_curtailment", "sigenergy_curtailment_state"),
        ("handle_alphaess_curtailment", "alphaess_curtailment_state"),
        ("handle_goodwe_curtailment", "goodwe_curtailment_state"),
        ("handle_solaredge_curtailment", "solaredge_curtailment_state"),
    ):
        handler = _function_source(handler_name)

        assert state_key in handler
        assert "get_current_prices_for_curtailment" in handler
        assert "using feed-in price from tariff schedule" in handler


def test_goodwe_curtailment_state_is_initialized():
    source = INIT_PATH.read_text()

    assert '"goodwe_curtailment_state": "normal"' in source
    assert source.index('"goodwe_coordinator": goodwe_coordinator') < source.index(
        '"goodwe_curtailment_state": "normal"'
    )


def test_goodwe_curtailment_periodically_reapplies_export_limit():
    handler = _function_source("handle_goodwe_curtailment")

    assert "_last_goodwe_curtailment_reapply" in handler
    assert "GoodWe curtailment RE-APPLY" in handler
    assert 'current_state != "curtailed" or _needs_reapply' in handler
    assert 'entry_data["_last_goodwe_curtailment_reapply"] = _now' in handler
    assert 'entry_data.pop("_last_goodwe_curtailment_reapply", None)' in handler


def test_goodwe_curtailment_releases_limit_before_force_discharge():
    handler = _function_source("handle_force_discharge")
    helper = _function_source("_restore_goodwe_curtailment_for_export")

    assert handler.count("lambda _guarded_w: _restore_goodwe_curtailment_for_export(") >= 2
    release_index = handler.index(
        'lambda _guarded_w: _restore_goodwe_curtailment_for_export(\n                        entry_data,\n                        "optimizer force discharge",'
    )
    optimizer_force_index = handler.index(
        "lambda guarded_w: goodwe_coord.force_discharge("
    )
    manual_release_index = handler.index(
        'lambda _guarded_w: _restore_goodwe_curtailment_for_export(\n                        entry_data,\n                        "force discharge",'
    )
    manual_force_index = handler.rindex(
        "lambda guarded_w: goodwe_coord.force_discharge("
    )

    assert release_index < optimizer_force_index
    assert manual_release_index < manual_force_index
    assert "controller.restore(allow_zero_export_limit=False)" in helper


def test_goodwe_force_discharge_fails_closed_when_curtailment_restore_fails():
    """A failed direct-GoodWe release must never be followed by force export."""
    handler = _function_source("handle_force_discharge")

    optimizer_branch = handler[
        handler.index('"optimizer force discharge"') : handler.index(
            "goodwe_discharge_result =", handler.index('"optimizer force discharge"')
        )
    ]
    manual_branch = handler[
        handler.index('"force discharge"', handler.index('"optimizer force discharge"') + 1) : handler.index(
            "discharge_result =", handler.index('"force discharge"', handler.index('"optimizer force discharge"') + 1)
        )
    ]

    assert "goodwe_curtailment_restore_result" in optimizer_branch
    assert "if not goodwe_curtailment_restore_result:" in optimizer_branch
    assert '"GoodWe curtailment restore before optimizer force discharge "' in optimizer_branch
    assert '"was not confirmed"' in optimizer_branch
    assert "goodwe_curtailment_restore_result" in manual_branch
    assert "if not goodwe_curtailment_restore_result:" in manual_branch
    assert 'force_discharge_state["active"] = False' in manual_branch
    assert "GoodWe force discharge blocked: curtailment restore was not confirmed" in manual_branch


def test_goodwe_curtailment_does_not_reapply_during_force_export():
    handler = _function_source("handle_goodwe_curtailment")
    helper = _function_source("_goodwe_force_export_active")

    assert "get_active_force_state" in helper
    assert 'active_force.get("type") == "discharge"' in helper
    assert "if _goodwe_force_export_active(entry_data):" in handler
    assert '"active force discharge"' in handler
    assert "GoodWe curtailment skipped while force discharge/export is active" in handler


def test_solaredge_curtailment_releases_limit_during_force_dispatch():
    handler = _function_source("handle_solaredge_curtailment")
    active_helper = _function_source("_solaredge_force_dispatch_active")
    restore_helper = _function_source("_restore_solaredge_curtailment_for_dispatch")

    assert "force_charge_state.get(\"active\")" in active_helper
    assert "get_active_force_state" in active_helper
    assert "_optimizer_current_force_action_matches(\"charge\")" in active_helper
    assert "controller.restore()" in restore_helper
    assert "solaredge_curtailment_state" in restore_helper
    assert "if _solaredge_force_dispatch_active(entry_data):" in handler
    assert '"active force dispatch"' in handler
    assert "SolarEdge curtailment skipped while force dispatch is active" in handler


def test_solaredge_force_dispatch_releases_active_power_curtailment_first():
    charge_handler = _function_source("handle_force_charge")
    discharge_handler = _function_source("handle_force_discharge")

    assert charge_handler.count("await _restore_solaredge_curtailment_for_dispatch(") >= 2
    optimizer_charge_release = charge_handler.index(
        'await _restore_solaredge_curtailment_for_dispatch(\n                    entry_data,\n                    "optimizer force charge",'
    )
    optimizer_charge_call = charge_handler.index(
        "await solaredge_coord.force_charge(duration, power_w=power_w)"
    )
    manual_charge_release = charge_handler.rindex(
        'await _restore_solaredge_curtailment_for_dispatch(\n                    entry_data,\n                    "force charge",'
    )
    manual_charge_call = charge_handler.rindex(
        "charge_result = await solaredge_coord.force_charge(duration, power_w=power_w)"
    )

    assert discharge_handler.count("lambda _guarded_w: _restore_solaredge_curtailment_for_dispatch(") >= 2
    optimizer_discharge_release = discharge_handler.index(
        'lambda _guarded_w: _restore_solaredge_curtailment_for_dispatch(\n                        entry_data,\n                        "optimizer force discharge",'
    )
    optimizer_discharge_call = discharge_handler.index(
        "lambda guarded_w: solaredge_coord.force_discharge("
    )
    manual_discharge_release = discharge_handler.rindex(
        'lambda _guarded_w: _restore_solaredge_curtailment_for_dispatch(\n                        entry_data,\n                        "force discharge",'
    )
    manual_discharge_call = discharge_handler.rindex(
        "lambda guarded_w: solaredge_coord.force_discharge("
    )

    assert optimizer_charge_release < optimizer_charge_call
    assert manual_charge_release < manual_charge_call
    assert optimizer_discharge_release < optimizer_discharge_call
    assert manual_discharge_release < manual_discharge_call


def test_periodic_solar_curtailment_routes_to_sungrow_before_tesla_path():
    handler = _function_source("handle_solar_curtailment_check")
    pre_tesla_path = handler[: handler.index("if token_getter is None:")]

    assert "if is_sungrow:" in pre_tesla_path
    assert "await handle_sungrow_curtailment()" in pre_tesla_path


def test_goodwe_runs_a_separate_ac_inverter_controller_after_its_own_handler():
    """#20: GoodWe's native path must not consume a separate Enphase path."""
    calls: list[tuple[str, dict]] = []

    async def handle_goodwe_curtailment(**kwargs):
        calls.append(("goodwe", kwargs))

    async def handle_ac_inverter_curtailment_only(**kwargs):
        calls.append(("ac", kwargs))

    namespace = {
        "entry": SimpleNamespace(
            options={"ac_inverter_curtailment_enabled": True},
            data={},
        ),
        "CONF_AC_INVERTER_CURTAILMENT_ENABLED": "ac_inverter_curtailment_enabled",
        "handle_goodwe_curtailment": handle_goodwe_curtailment,
        "handle_ac_inverter_curtailment_only": handle_ac_inverter_curtailment_only,
        "ac_inverter_is_same_hybrid": lambda: False,
    }
    exec(
        textwrap.dedent(_function_source("handle_goodwe_and_ac_inverter_curtailment")),
        namespace,
    )

    asyncio.run(
        namespace["handle_goodwe_and_ac_inverter_curtailment"](
            feedin_price=6.0,
            import_price=17.0,
            price_source="websocket",
        )
    )

    assert [name for name, _kwargs in calls] == ["goodwe", "ac"]
    assert calls[1][1] == {
        "feedin_price": 6.0,
        "import_price": 17.0,
        "price_source": "websocket",
        "refresh_prices": False,
    }


def test_solar_curtailment_runs_startup_check_before_first_periodic_tick():
    source = INIT_PATH.read_text()
    setup_section = source[
        source.index("# Set up automatic curtailment check every 5 minutes"):
        source.index("# Set up Flow Power v2 tariff rate refresh")
    ]

    assert "async def _startup_curtailment_check" in setup_section
    assert "await asyncio.sleep(5)" in setup_section
    assert "await handle_solar_curtailment_check(None)" in setup_section
    assert "hass.async_create_task(_startup_curtailment_check())" in setup_section
    assert "EVENT_HOMEASSISTANT_STARTED" in setup_section


def test_periodic_solar_curtailment_routes_ac_inverter_without_tesla_token():
    handler = _function_source("handle_solar_curtailment_check")
    token_guard = handler[handler.index("if token_getter is None:"):]

    assert "CONF_AC_INVERTER_CURTAILMENT_ENABLED" in token_guard
    assert "await handle_ac_inverter_curtailment_only(refresh_prices=True)" in token_guard
    assert token_guard.index("await handle_ac_inverter_curtailment_only") < token_guard.index(
        "Solar curtailment skipped - no Tesla API token getter available"
    )


def test_ac_curtailment_live_status_uses_non_tesla_coordinator_before_api():
    helper = _function_source("_get_cached_live_status")
    live_status = _function_source("get_live_status")

    assert "coordinator_data_to_ev_live_status" in helper
    assert '"fronius_reserva_coordinator"' in helper
    assert '"goodwe_coordinator"' in helper
    assert '"solaredge_coordinator"' in helper
    assert 'live_status["is_curtailed"] = True' in helper
    assert "cached_status = _get_cached_live_status()" in live_status
    assert "if not callable(token_getter):" in live_status
    assert live_status.index("cached_status = _get_cached_live_status()") < live_status.index(
        "if not callable(token_getter):"
    )


def test_ac_coupled_curtails_zero_export_when_exporting_and_battery_not_absorbing():
    async def get_live_status():
        return {
            "solar_power": 3958,
            "battery_power": 5,
            "grid_power": -2851.2,
            "load_power": 1112,
            "battery_soc": 98.9,
        }

    entry_data = {}

    namespace = {
        "CONF_INVERTER_RESTORE_SOC": "inverter_restore_soc",
        "DEFAULT_INVERTER_RESTORE_SOC": 90,
        "_LOGGER": SimpleNamespace(
            debug=lambda *args, **kwargs: None,
            info=lambda *args, **kwargs: None,
        ),
        "entry": SimpleNamespace(options={}, data={}, entry_id="test_entry"),
        "get_live_status": get_live_status,
        "hass": SimpleNamespace(data={}),
        "DOMAIN": "power_sync",
        "with_hysteresis": _load_with_hysteresis(),
        "_aemo_dispatch_entry_data": lambda: entry_data,
    }
    exec(textwrap.dedent(_function_source("should_curtail_ac_coupled")), namespace)

    assert asyncio.run(namespace["should_curtail_ac_coupled"](20.09, 0.0)) is True


def test_solar_curtailment_is_not_blocked_by_monitoring_mode():
    periodic_handler = _function_source("handle_solar_curtailment_check")
    websocket_handler = _function_source("handle_solar_curtailment_with_websocket_data")

    assert "_is_monitoring_mode()" not in periodic_handler
    assert "_is_monitoring_mode()" not in websocket_handler
    assert "Would check solar curtailment" not in periodic_handler
    assert "Would check solar curtailment" not in websocket_handler


def test_periodic_tesla_curtailment_uses_any_provider_price_source():
    handler = _function_source("handle_solar_curtailment_check")

    assert "get_current_prices_for_curtailment(" in handler
    assert "price_coordinators = (" in handler
    assert "tariff_schedule" in handler
    assert "No feed-in price available for curtailment check" in handler
    assert "no price coordinator available" not in handler


def test_websocket_solar_curtailment_routes_to_sungrow_with_prices():
    handler = _function_source("handle_solar_curtailment_with_websocket_data")
    pre_tesla_path = handler[: handler.index("if token_getter is None:")]

    assert "if is_sungrow:" in pre_tesla_path
    assert (
        "await handle_sungrow_curtailment("
        "feedin_price=feedin_price, import_price=import_price)"
    ) in pre_tesla_path


def test_websocket_solar_curtailment_routes_goodwe_and_ac_inverter_with_prices():
    handler = _function_source("handle_solar_curtailment_with_websocket_data")
    pre_tesla_path = handler[: handler.index("if token_getter is None:")]

    assert "if is_goodwe:" in pre_tesla_path
    assert "await handle_goodwe_and_ac_inverter_curtailment(" in pre_tesla_path
    assert 'price_source="websocket"' in pre_tesla_path


def test_websocket_solar_curtailment_routes_ac_inverter_without_tesla_token():
    handler = _function_source("handle_solar_curtailment_with_websocket_data")
    token_guard = handler[handler.index("if token_getter is None:"):]

    assert "CONF_AC_INVERTER_CURTAILMENT_ENABLED" in token_guard
    assert "await handle_ac_inverter_curtailment_only(" in token_guard
    assert 'price_source="websocket"' in token_guard
    assert token_guard.index("await handle_ac_inverter_curtailment_only") < token_guard.index(
        "Solar curtailment skipped - no Tesla API token getter available"
    )


def test_websocket_tesla_curtailment_falls_back_to_any_provider_price_source():
    handler = _function_source("handle_solar_curtailment_with_websocket_data")

    assert "get_current_prices_for_curtailment(" in handler
    assert "price_source = \"websocket\"" in handler
    assert "No feed-in price available for websocket curtailment check" in handler


def test_curtailment_price_fallback_uses_tariff_schedule():
    namespace = {
        "Any": object,
        "get_current_price_from_tariff_schedule": lambda tariff: (31.0, 5.0, "PEAK"),
    }
    exec(_top_level_function_source("get_current_prices_for_curtailment"), namespace)

    feedin_price, import_price, source = namespace[
        "get_current_prices_for_curtailment"
    ]({"tariff_schedule": {"tou_periods": {"PEAK": []}}}, ())

    assert feedin_price == -5.0
    assert import_price == 31.0
    assert source == "tariff_schedule"


def test_curtailment_price_fallback_preserves_negative_export_earnings():
    namespace = {
        "Any": object,
        "get_current_price_from_tariff_schedule": lambda tariff: (31.0, -2.5, "PEAK"),
    }
    exec(_top_level_function_source("get_current_prices_for_curtailment"), namespace)

    feedin_price, import_price, source = namespace[
        "get_current_prices_for_curtailment"
    ]({"tariff_schedule": {"tou_periods": {"PEAK": []}}}, ())

    assert feedin_price == 2.5
    assert import_price == 31.0
    assert source == "tariff_schedule"


def test_flow_power_curtailment_prefers_tariff_export_over_raw_aemo():
    namespace = {
        "Any": object,
        "get_current_price_from_tariff_schedule": lambda tariff: (24.5, 0.0, "PERIOD_10_00"),
    }
    exec(_top_level_function_source("get_current_prices_for_curtailment"), namespace)

    aemo_coordinator = SimpleNamespace(
        data={
            "current": [
                {"channelType": "general", "perKwh": 0.61},
                {"channelType": "feedIn", "perKwh": -0.61},
            ],
        }
    )
    entry = SimpleNamespace(options={"electricity_provider": "flow_power"}, data={})

    feedin_price, import_price, source = namespace[
        "get_current_prices_for_curtailment"
    ](
        {"entry": entry, "tariff_schedule": {"buy_prices": {"PERIOD_10_00": 0.245}}},
        (aemo_coordinator,),
    )

    assert feedin_price == -0.0
    assert import_price == 24.5
    assert source == "tariff_schedule"


def test_non_flow_power_curtailment_keeps_live_coordinator_priority():
    namespace = {
        "Any": object,
        "get_current_price_from_tariff_schedule": lambda tariff: (24.5, 0.0, "PERIOD_10_00"),
    }
    exec(_top_level_function_source("get_current_prices_for_curtailment"), namespace)

    aemo_coordinator = SimpleNamespace(
        data={
            "current": [
                {"channelType": "general", "perKwh": 0.61},
                {"channelType": "feedIn", "perKwh": -0.61},
            ],
        }
    )
    entry = SimpleNamespace(options={"electricity_provider": "aemo_vpp"}, data={})

    feedin_price, import_price, source = namespace[
        "get_current_prices_for_curtailment"
    ](
        {"entry": entry, "tariff_schedule": {"buy_prices": {"PERIOD_10_00": 0.245}}},
        (aemo_coordinator,),
    )

    assert feedin_price == -0.61
    assert import_price == 0.61
    assert source == "price_coordinator"


def test_startup_tesla_tariff_fetch_requires_tesla_site():
    namespace = {"Any": object}
    exec(_top_level_function_source("should_fetch_tesla_tariff_on_startup"), namespace)
    should_fetch = namespace["should_fetch_tesla_tariff_on_startup"]

    assert should_fetch("globird", True, object())
    assert not should_fetch("globird", False, object())
    assert not should_fetch("other", True, object())
    assert not should_fetch("nz", True, object())
