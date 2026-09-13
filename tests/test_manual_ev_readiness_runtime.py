"""Runtime regression coverage for manual Tesla readiness resolution."""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace


INIT_PATH = (
    Path(__file__).resolve().parent.parent
    / "custom_components"
    / "power_sync"
    / "__init__.py"
)
VIN = "5YJTEST0000000001"


def _manual_plugged_in_method():
    tree = ast.parse(INIT_PATH.read_text())
    command_view = next(
        node for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "EVVehicleCommandView"
    )
    method = next(
        node for node in command_view.body
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name == "_is_vehicle_plugged_in"
    )
    namespace = {
        "DOMAIN": "power_sync",
        "dt_util": SimpleNamespace(utcnow=lambda: None),
        "get_tesla_ble_plug_state": lambda *_args: None,
        "_ble_prefix_for_vehicle": lambda *_args: None,
        "_vehicle_matches_identifier": (
            lambda vehicle, identifier: vehicle.get("vehicle_id") == identifier
        ),
        "_LOGGER": SimpleNamespace(debug=lambda *_args: None, warning=lambda *_args: None),
    }
    exec(
        compile(ast.Module(body=[method], type_ignores=[]), str(INIT_PATH), "exec"),
        namespace,
    )
    return namespace["_is_vehicle_plugged_in"], namespace


def _view(status):
    method, namespace = _manual_plugged_in_method()
    namespace["_get_ev_vehicles_status"] = lambda _hass, _entry: status
    hass = SimpleNamespace(data={"power_sync": {"_ev_cache": {}}})

    async def no_entity(*_args):
        return None

    view = SimpleNamespace(
        _hass=hass,
        _get_tesla_ev_entity=no_entity,
        _get_powersync_config=lambda: {},
        _get_powersync_entry=lambda: object(),
    )
    return method, view


def test_manual_start_accepts_the_exact_connected_status_loadpoint():
    """Ticket-57: card-ready exact VIN is manual-start-ready too."""
    method, view = _view([{"vehicle_id": VIN, "is_connected": True}])

    assert asyncio.run(method(view, VIN)) is True


def test_manual_start_does_not_accept_another_or_disconnected_loadpoint():
    method, view = _view([
        {"vehicle_id": "5YJTEST0000000002", "is_connected": True},
        {"vehicle_id": VIN, "is_connected": False},
    ])

    assert asyncio.run(method(view, VIN)) is False
