"""Regression coverage for FoxESS entity bridge startup retries."""

from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
INIT_PATH = ROOT / "custom_components" / "power_sync" / "__init__.py"


def _top_level_function(name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    source = INIT_PATH.read_text()
    module = ast.parse(source)
    for node in module.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"{name} not found")


def _async_setup_source() -> str:
    source = INIT_PATH.read_text()
    module = ast.parse(source)
    for node in module.body:
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "async_setup_entry":
            segment = ast.get_source_segment(source, node)
            assert segment is not None
            return segment
    raise AssertionError("async_setup_entry not found")


def test_foxess_entity_bridge_startup_failure_classifier_matches_missing_entities_only():
    function = _top_level_function("_is_foxess_entity_bridge_startup_failure")
    module = ast.Module(body=[function], type_ignores=[])
    ast.fix_missing_locations(module)

    class FoxESSEntityEnergyCoordinator:
        pass

    class OtherCoordinator:
        pass

    namespace = {
        "Any": object,
        "FoxESSEntityEnergyCoordinator": FoxESSEntityEnergyCoordinator,
    }
    exec(compile(module, str(INIT_PATH), "exec"), namespace)

    is_retryable = namespace["_is_foxess_entity_bridge_startup_failure"]

    assert is_retryable(
        FoxESSEntityEnergyCoordinator(),
        Exception("FoxESS entity bridge read failed: foxess_missing_entities:sensor.x"),
    )
    assert not is_retryable(
        FoxESSEntityEnergyCoordinator(),
        Exception("FoxESS Modbus connection refused"),
    )
    assert not is_retryable(
        OtherCoordinator(),
        Exception("FoxESS entity bridge read failed: foxess_missing_entities:sensor.x"),
    )


def test_foxess_entity_bridge_startup_failure_keeps_coordinator_for_retry():
    setup_source = _async_setup_source()
    foxess_refresh_block = setup_source[
        setup_source.index("if foxess_coordinator:") :
        setup_source.index("if goodwe_coordinator:")
    ]

    retry_check_index = foxess_refresh_block.index(
        "_is_foxess_entity_bridge_startup_failure(foxess_coordinator, e)"
    )
    drop_index = foxess_refresh_block.index("foxess_coordinator = None")
    retry_block = foxess_refresh_block[retry_check_index:drop_index]

    assert retry_check_index < drop_index
    assert "keeping coordinator active so it can retry" in retry_block
    assert "foxess_coordinator = None" not in retry_block


def test_goodwe_entity_telemetry_startup_failure_classifier_matches_missing_entities_only():
    function = _top_level_function("_is_goodwe_entity_telemetry_startup_failure")
    module = ast.Module(body=[function], type_ignores=[])
    ast.fix_missing_locations(module)

    class GoodWeEnergyCoordinator:
        _using_entity_telemetry = True

    class DirectGoodWeEnergyCoordinator:
        _using_entity_telemetry = False

    namespace = {
        "Any": object,
        "GoodWeEnergyCoordinator": GoodWeEnergyCoordinator,
    }
    exec(compile(module, str(INIT_PATH), "exec"), namespace)

    is_retryable = namespace["_is_goodwe_entity_telemetry_startup_failure"]

    assert is_retryable(
        GoodWeEnergyCoordinator(),
        Exception("Error fetching GoodWe data: goodwe_entity_missing_entities:sensor.x"),
    )
    assert not is_retryable(
        GoodWeEnergyCoordinator(),
        Exception("GoodWe TCP connection refused"),
    )
    assert not is_retryable(
        DirectGoodWeEnergyCoordinator(),
        Exception("Error fetching GoodWe data: goodwe_entity_missing_entities:sensor.x"),
    )


def test_goodwe_entity_telemetry_startup_failure_keeps_coordinator_for_retry():
    setup_source = _async_setup_source()
    goodwe_refresh_block = setup_source[
        setup_source.index("if goodwe_coordinator:") :
        setup_source.index("if alphaess_coordinator:")
    ]

    retry_check_index = goodwe_refresh_block.index(
        "_is_goodwe_entity_telemetry_startup_failure(goodwe_coordinator, e)"
    )
    drop_index = goodwe_refresh_block.index("goodwe_coordinator = None")
    retry_block = goodwe_refresh_block[retry_check_index:drop_index]

    assert retry_check_index < drop_index
    assert "keeping coordinator active so it can retry" in retry_block
    assert "goodwe_coordinator = None" not in retry_block
