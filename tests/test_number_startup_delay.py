"""Regression test: capability-gated task must not be created for non-Tesla installs.

HA's bootstrap wrap-up waits for all tasks registered via hass.async_create_task()
to drain. Capability-gated Tesla entity tasks must therefore be both Tesla-only
and bounded to a short timeout.

The fix gates hass.async_create_task() behind the same tesla_site_id check
already used for BackupReserveNumber.
"""

from __future__ import annotations

import ast
from pathlib import Path

NUMBER_PATH = (
    Path(__file__).resolve().parent.parent
    / "custom_components"
    / "power_sync"
    / "number.py"
)
ROOT = Path(__file__).resolve().parent.parent
BINARY_SENSOR_PATH = ROOT / "custom_components" / "power_sync" / "binary_sensor.py"
SWITCH_PATH = ROOT / "custom_components" / "power_sync" / "switch.py"
COORDINATOR_PATH = ROOT / "custom_components" / "power_sync" / "coordinator.py"
INIT_PATH = ROOT / "custom_components" / "power_sync" / "__init__.py"
CONST_PATH = ROOT / "custom_components" / "power_sync" / "const.py"


def _find_function(tree: ast.AST, name: str) -> ast.AsyncFunctionDef | ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"function '{name}' not found in {NUMBER_PATH}")


def _all_create_task_calls(tree: ast.AST) -> list[ast.Call]:
    """Return every hass.async_create_task() Call node in the tree."""
    calls = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "async_create_task"
        ):
            calls.append(node)
    return calls


def _is_capability_gated_task_call(call: ast.Call) -> bool:
    """Return True for hass.async_create_task(_add_capability_gated_numbers())."""
    if not call.args:
        return False
    task = call.args[0]
    return (
        isinstance(task, ast.Call)
        and isinstance(task.func, ast.Name)
        and task.func.id == "_add_capability_gated_numbers"
    )


def _is_enclosed_by_tesla_site_id_guard(
    setup_fn: ast.AsyncFunctionDef,
    child: ast.AST,
) -> bool:
    """Return True if child has `if tesla_site_id:` as an AST ancestor."""
    parents: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(setup_fn):
        for node in ast.iter_child_nodes(parent):
            parents[node] = parent

    node = child
    while node in parents:
        node = parents[node]
        if (
            isinstance(node, ast.If)
            and isinstance(node.test, ast.Name)
            and node.test.id == "tesla_site_id"
        ):
            return True
    return False


def test_capability_gated_task_guarded_by_tesla_site_id():
    """hass.async_create_task for _add_capability_gated_numbers must sit inside
    an `if tesla_site_id:` block, not at the top level of async_setup_entry."""
    source = NUMBER_PATH.read_text()
    tree = ast.parse(source)

    setup_fn = _find_function(tree, "async_setup_entry")
    create_task_calls = [
        call
        for call in _all_create_task_calls(setup_fn)
        if _is_capability_gated_task_call(call)
    ]

    assert create_task_calls, (
        "Expected hass.async_create_task(_add_capability_gated_numbers()) call"
    )

    lines = source.splitlines()

    for call in create_task_calls:
        call_line = call.lineno
        assert _is_enclosed_by_tesla_site_id_guard(setup_fn, call), (
            f"hass.async_create_task() at line {call_line} is NOT inside any "
            f"`if tesla_site_id:` block — non-Tesla users will wait 120 s at startup.\n"
            f"  Call site: {lines[call_line - 1].strip()}"
        )


def test_capability_gated_numbers_inner_function_unchanged():
    """_add_capability_gated_numbers itself should still poll for tesla_capabilities."""
    tree = ast.parse(NUMBER_PATH.read_text())
    setup_fn = _find_function(tree, "async_setup_entry")

    # The inner function must still exist inside async_setup_entry
    inner = None
    for node in ast.walk(setup_fn):
        if (
            isinstance(node, ast.AsyncFunctionDef)
            and node.name == "_add_capability_gated_numbers"
        ):
            inner = node
            break

    assert inner is not None, "_add_capability_gated_numbers not found inside async_setup_entry"

    # It must still reference "tesla_capabilities" (unchanged polling logic)
    names = {
        node.value if isinstance(node, ast.Constant) else None
        for node in ast.walk(inner)
    }
    assert "tesla_capabilities" in names, (
        "_add_capability_gated_numbers no longer references 'tesla_capabilities' — "
        "the inner polling logic may have been removed unintentionally"
    )


def test_capability_gated_waits_use_shared_bounded_timeout():
    """Tesla capability-gated platforms must not reintroduce 120 s startup waits."""
    assert "TESLA_CAPABILITY_WAIT_SECONDS = 30.0" in CONST_PATH.read_text()

    for path in (NUMBER_PATH, BINARY_SENSOR_PATH, SWITCH_PATH):
        source = path.read_text()
        assert "TESLA_CAPABILITY_WAIT_SECONDS" in source
        assert "waited < 120.0" not in source
        assert "within 120s" not in source


def test_tesla_capability_probe_writes_to_persistent_entry_data():
    """Capability probe results must not be written to a throwaway default dict."""
    source = COORDINATOR_PATH.read_text()
    tree = ast.parse(source)
    probe = _find_function(tree, "_async_probe_tesla_capabilities")
    probe_source = ast.get_source_segment(source, probe)

    assert probe_source is not None
    assert ".setdefault(DOMAIN, {}).setdefault(self._entry_id, {})" in probe_source
    assert '.get(self._entry_id, {})' not in probe_source


def test_setup_preserves_early_tesla_capability_results():
    """async_setup_entry replaces hass.data after first refresh; keep early probe results."""
    source = INIT_PATH.read_text()

    assert "existing_entry_data = hass.data.setdefault(DOMAIN, {}).get(entry.entry_id, {})" in source
    assert 'existing_entry_data.get("tesla_capabilities")' in source
    assert '"tesla_capabilities": tesla_capabilities or {}' in source
    assert 'existing_entry_data.get("tesla_site_country")' in source
    assert '"tesla_site_country": tesla_site_country' in source


def test_setup_initializes_grid_charging_preferences_in_canonical_entry_data():
    """Persistent preferences must not write through hass.data before setup owns it."""
    source = INIT_PATH.read_text()
    tree = ast.parse(source)
    setup_source = ast.get_source_segment(
        source,
        _find_function(tree, "async_setup_entry"),
    )

    assert setup_source is not None
    canonical_marker = (
        "existing_entry_data = "
        "hass.data.setdefault(DOMAIN, {}).get(entry.entry_id, {})"
    )
    before_canonical, after_canonical = setup_source.split(canonical_marker, 1)
    assert (
        'hass.data[DOMAIN][entry.entry_id]["tesla_grid_charging_preferences"]'
        not in before_canonical
    )
    assert (
        '"tesla_grid_charging_preferences": tesla_grid_charging_preferences'
        in after_canonical
    )


def test_amber_websocket_start_is_timeout_bounded():
    """Amber WebSocket startup must fall back instead of blocking setup indefinitely."""
    source = INIT_PATH.read_text()

    assert "AMBER_WEBSOCKET_START_TIMEOUT_SECONDS = 15.0" in CONST_PATH.read_text()
    assert "asyncio.wait_for(" in source
    assert "ws_client.start()" in source
    assert "AMBER_WEBSOCKET_START_TIMEOUT_SECONDS" in source
    assert "except asyncio.TimeoutError:" in source


def test_force_power_slider_covers_power_capable_batteries():
    """The force-power control should appear for every power_w force path."""
    source = NUMBER_PATH.read_text()

    for constant_name in (
        "CONF_FOXESS_HOST",
        "CONF_FOXESS_SERIAL_PORT",
        "CONF_GOODWE_HOST",
        "CONF_SIGENERGY_STATION_ID",
        "CONF_SUNGROW_HOST",
        "CONF_ALPHAESS_MODBUS_HOST",
        "CONF_ESY_CONFIG_ENTRY_ID",
        "CONF_SOLAX_CONFIG_ENTRY_ID",
        "CONF_SOLAX_ENTITY_PREFIX",
        "CONF_SAJ_CONFIG_ENTRY_ID",
        "CONF_NEOVOLT_CONFIG_ENTRY_ID",
        "CONF_NEOVOLT_CONFIG_ENTRY_IDS",
    ):
        assert constant_name in source


def test_force_power_slider_max_is_site_relative():
    """The force-power control should not expose a fixed 50 kW slider first."""
    source = NUMBER_PATH.read_text()

    assert "FORCE_POWER_FALLBACK_MAX_KW = 50.0" in source
    assert "FORCE_POWER_STEP_KW = 0.05" in source
    assert "_attr_native_step = FORCE_POWER_STEP_KW" in source
    assert "def native_max_value(self) -> float:" in source
    assert "math.ceil(max_kw / FORCE_POWER_STEP_KW)" in source
    assert "CONF_OPTIMIZATION_MAX_CHARGE_W" in source
    assert "CONF_OPTIMIZATION_MAX_DISCHARGE_W" in source
    assert "CONF_SAJ_INVERTER_RATED_KW" in source
    assert "battery_max_charge_power_w" in source
    assert "battery_max_discharge_power_w" in source
    assert "_attr_native_max_value = 50" not in source
