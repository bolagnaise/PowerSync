"""Regression coverage for Sungrow export-limit curtailment routing."""

from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
INIT_PATH = ROOT / "custom_components" / "power_sync" / "__init__.py"
ACTIONS_PATH = ROOT / "custom_components" / "power_sync" / "automations" / "actions.py"
SUNGROW_INVERTER_PATH = ROOT / "custom_components" / "power_sync" / "inverters" / "sungrow.py"


def _function_source(name: str) -> str:
    source = INIT_PATH.read_text()
    module = ast.parse(source)
    for node in module.body:
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "async_setup_entry":
            for child in node.body:
                if isinstance(child, ast.AsyncFunctionDef) and child.name == name:
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
    assert "await sungrow_coord.set_export_limit(home_load_w)" in handler
    assert "await sungrow_coord.set_export_limit(None)" in handler
    assert "ac_inverter_is_same_hybrid" in handler
    assert "await apply_inverter_curtailment(" in handler


def test_sungrow_curtailment_releases_limit_for_active_solar_surplus_ev():
    helper = _function_source("_active_solar_surplus_ev_needs_inverter_headroom")
    handler = _function_source("handle_sungrow_curtailment")

    assert 'solar_config.get("enabled", False)' in helper
    assert "is_ev_plugged_in" in helper
    assert "get_ev_battery_level" in helper
    assert 'state.get("paused")' in helper
    assert "_active_solar_surplus_ev_needs_inverter_headroom" in handler
    assert "should_curtail_for_price = export_earnings < 1 and not ev_needs_headroom" in handler
    assert "solar surplus EV needs PV headroom" in handler
    assert "curtail=should_curtail_for_price" in handler


def test_ev_live_status_marks_native_sungrow_curtailment_as_curtailed():
    source = _actions_function_source("_get_tesla_live_status")

    assert 'entry_data.get("sungrow_curtailment_state") == "curtailed"' in source
    assert 'live_status["is_curtailed"] = True' in source


def test_inverter_status_api_marks_native_sungrow_curtailment_as_curtailed():
    source = INIT_PATH.read_text()

    assert 'sungrow_curtailment_state = entry_data.get("sungrow_curtailment_state")' in source
    assert 'or sungrow_curtailment_state == "curtailed"' in source
    assert 'or sungrow_curtailment_state == "normal"' in source


def test_ac_inverter_restore_keeps_heartbeat_but_skips_sungrow_verify_readback():
    source = _function_source("apply_inverter_curtailment")

    controller_index = source.index("controller = get_inverter_controller(")
    restore_index = source.index("_LOGGER.info(f\"🟢 Restoring inverter")
    signature_index = source.index("restore_sig = inspect.signature(controller.restore)")
    verify_false_index = source.index("await controller.restore(verify=False)")

    assert signature_index < verify_false_index
    assert controller_index < restore_index < verify_false_index


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


def test_periodic_solar_curtailment_routes_to_sungrow_before_tesla_path():
    handler = _function_source("handle_solar_curtailment_check")
    pre_tesla_path = handler[: handler.index("if token_getter is None:")]

    assert "if is_sungrow:" in pre_tesla_path
    assert "await handle_sungrow_curtailment()" in pre_tesla_path


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


def test_startup_tesla_tariff_fetch_requires_tesla_site():
    namespace = {"Any": object}
    exec(_top_level_function_source("should_fetch_tesla_tariff_on_startup"), namespace)
    should_fetch = namespace["should_fetch_tesla_tariff_on_startup"]

    assert should_fetch("globird", True, object())
    assert not should_fetch("globird", False, object())
    assert not should_fetch("other", True, object())
    assert not should_fetch("nz", True, object())
