"""Regression tests for resource-leak fixes in async_unload_entry.

OB-28: async_unload_entry only called async_shutdown() on the Sungrow brand
coordinator. Sigenergy / AlphaESS / FoxESS / GoodWe / Solax / SolarEdge /
SajH2 / FroniusReserva / Neovolt / AnkerSolix / ESYSunhome each expose an
async_shutdown() that disconnects a Modbus (or equivalent) client, but were
never invoked on unload — orphaning one client per brand per reload until the
inverter's connection pool exhausted.

OB-30: async_unload_entry tore down the (inert) Tesla signaling client but
never stopped the powerwall_local coordinator. Its keep-alive listener
(self._keepalive_unsub, anchored in PowerwallLocalCoordinator.__init__) keeps
the coordinator's periodic schedule armed even with zero entity listeners, so
simply popping entry_data leaves the 2s TEDAPI poll timer running — leaking
one poller per reload.

Uses the AST source-extraction pattern established in
tests/test_force_mode_controls.py: parse __init__.py with ast, isolate the
async_unload_entry function's source, and assert on the teardown calls it
must contain. This repo's convention for the 34k-line __init__.py god-file is
source assertions rather than exercising the function directly (it depends on
a live HomeAssistant/ConfigEntry object graph that is impractical to fake).
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INIT_PATH = ROOT / "custom_components" / "power_sync" / "__init__.py"
COORDINATOR_PATH = ROOT / "custom_components" / "power_sync" / "coordinator.py"
POWERWALL_LOCAL_COORDINATOR_PATH = (
    ROOT / "custom_components" / "power_sync" / "powerwall_local" / "coordinator.py"
)

# Brand coordinators (excluding Sungrow, which was already handled) that each
# expose async_shutdown() on their DataUpdateCoordinator subclass and must be
# shut down on unload alongside Sungrow.
NON_SUNGROW_BRAND_COORDINATOR_KEYS = (
    "sigenergy_coordinator",
    "foxess_coordinator",
    "goodwe_coordinator",
    "alphaess_coordinator",
    "esy_sunhome_coordinator",
    "solax_coordinator",
    "saj_h2_coordinator",
    "fronius_reserva_coordinator",
    "neovolt_coordinator",
    "solaredge_coordinator",
    "anker_solix_coordinator",
)


def _find_function(tree: ast.AST, function_name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == function_name:
            return node
    raise AssertionError(f"{function_name} not found")


def _find_class_method(
    tree: ast.AST,
    class_name: str,
    method_name: str,
) -> ast.FunctionDef | ast.AsyncFunctionDef:
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef) or node.name != class_name:
            continue
        for child in node.body:
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and child.name == method_name:
                return child
    raise AssertionError(f"{class_name}.{method_name} not found")


def _unload_entry_source() -> str:
    source = INIT_PATH.read_text()
    tree = ast.parse(source)
    function = _find_function(tree, "async_unload_entry")
    return ast.get_source_segment(source, function)


def test_unload_entry_shuts_down_sungrow_coordinator():
    """Sanity check: the pre-existing Sungrow shutdown must still be present."""
    unload_source = _unload_entry_source()
    assert '"sungrow_coordinator"' in unload_source
    assert "async_shutdown" in unload_source


def test_unload_entry_shuts_down_every_non_sungrow_brand_coordinator():
    """OB-28: every brand coordinator with async_shutdown() must be stopped
    on unload, not just Sungrow.

    Each brand's coordinator (see coordinator.py) holds an
    AsyncModbusTcpClient (or equivalent) opened in async_setup_entry. Failing
    to call async_shutdown() on unload orphans that connection until the
    inverter's connection pool exhausts. AlphaESS additionally releases
    forced dispatch (0722H) in its async_shutdown() before disconnecting,
    since it has no auto-revert — skipping it can strand forced dispatch.
    """
    unload_source = _unload_entry_source()

    missing = [
        key for key in NON_SUNGROW_BRAND_COORDINATOR_KEYS if key not in unload_source
    ]
    assert not missing, (
        f"async_unload_entry never references these brand coordinator entry_data "
        f"keys, so their async_shutdown() (and Modbus disconnect) is never "
        f"invoked on unload: {missing}"
    )

    # The keys alone aren't proof of a shutdown call (they're already
    # referenced by the pre-existing energy-accumulator flush loop) — the
    # fix must actually call async_shutdown for the non-Sungrow brands.
    # Assert on a loop/host of shutdown calls rather than requiring the
    # literal keys and "async_shutdown" appear in the same statement, since a
    # single loop with a shared coord_key tuple satisfies the intent.
    shutdown_call_count = unload_source.count("async_shutdown()")
    assert shutdown_call_count >= 2, (
        "Expected async_unload_entry to call async_shutdown() for Sungrow "
        "AND at least one other brand coordinator (a bare single call means "
        "only Sungrow is still being shut down); found "
        f"{shutdown_call_count} call(s)"
    )


def test_alphaess_async_shutdown_releases_dispatch_before_disconnect():
    """AlphaESSEnergyCoordinator.async_shutdown must release forced dispatch
    (0722H) before disconnecting — AlphaESS has no auto-revert, so skipping
    this on an orphaned coordinator (OB-28) can strand forced dispatch.
    """
    source = COORDINATOR_PATH.read_text()
    tree = ast.parse(source)
    method = _find_class_method(tree, "AlphaESSEnergyCoordinator", "async_shutdown")
    method_source = ast.get_source_segment(source, method)

    release_idx = method_source.find("await self._controller.release_dispatch()")
    disconnect_idx = method_source.find("await self._controller.disconnect()")
    assert release_idx != -1, "AlphaESS async_shutdown no longer calls release_dispatch"
    assert disconnect_idx != -1, "AlphaESS async_shutdown no longer calls disconnect"
    assert release_idx < disconnect_idx, (
        "AlphaESS async_shutdown must release dispatch before disconnecting "
        "(no auto-revert means 0722H=1 stays locked otherwise)"
    )


def test_unload_entry_stops_powerwall_local_coordinator():
    """OB-30: the powerwall_local coordinator's 2s TEDAPI poller must be
    stopped on unload.

    PowerwallLocalCoordinator anchors a keep-alive no-op listener
    (self._keepalive_unsub) at construction so its periodic schedule stays
    armed even with zero entity listeners (see
    powerwall_local/coordinator.py). Popping entry_data alone does not stop
    the timer, so unload must null update_interval and unsubscribe the
    keepalive listener — mirroring the teardown pattern already used in
    powerwall_local/views.py's ensure_coordinator().
    """
    unload_source = _unload_entry_source()

    assert 'pw_local.get("coordinator")' in unload_source or (
        '"powerwall_local"' in unload_source and "coordinator" in unload_source
    ), "async_unload_entry never looks up the powerwall_local coordinator"

    assert "update_interval = None" in unload_source, (
        "async_unload_entry never nulls the powerwall_local coordinator's "
        "update_interval, so its periodic poll can still reschedule"
    )
    assert "_keepalive_unsub" in unload_source, (
        "async_unload_entry never unsubscribes the powerwall_local "
        "keep-alive listener that keeps the coordinator's schedule armed"
    )


def test_powerwall_local_coordinator_has_keepalive_unsub_attribute():
    """Confirm the attribute name assumed by the OB-30 fix actually exists
    on PowerwallLocalCoordinator, so the unload-time getattr lookup isn't
    silently a no-op against a renamed attribute.
    """
    source = POWERWALL_LOCAL_COORDINATOR_PATH.read_text()
    tree = ast.parse(source)
    init_method = _find_class_method(tree, "PowerwallLocalCoordinator", "__init__")
    init_source = ast.get_source_segment(source, init_method)

    assert "self._keepalive_unsub = self.async_add_listener" in init_source, (
        "PowerwallLocalCoordinator no longer stores its keep-alive listener "
        "unsubscribe callback as self._keepalive_unsub"
    )
