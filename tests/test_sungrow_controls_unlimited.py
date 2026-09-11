"""Regression coverage for the Sungrow Controls Unlimited sentinel."""

from __future__ import annotations

import ast
import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace


INIT_PATH = Path(__file__).resolve().parent.parent / "custom_components" / "power_sync" / "__init__.py"


class _Response:
    def __init__(self, body, status=200):
        self.body = body
        self.status = status


class _Request:
    async def json(self):
        return {"export_limit_w": 0}


def _load_diagnostics_view():
    tree = ast.parse(INIT_PATH.read_text())
    class_node = next(
        node for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "SungrowDiagnosticsView"
    )
    module = ast.fix_missing_locations(ast.Module(body=[class_node], type_ignores=[]))
    web = SimpleNamespace(
        Request=object,
        Response=object,
        json_response=lambda body, status=200: _Response(body, status),
    )
    namespace = {
        "web": web,
        "HomeAssistant": object,
        "HomeAssistantView": object,
        "DOMAIN": "power_sync",
        "CONF_SUNGROW_HOST": "sungrow_host",
        "_LOGGER": logging.getLogger(__name__),
        "_parse_json_request": lambda request: request.json(),
        "_network_envelope_blocks_unguarded_export_write": lambda *_: None,
    }
    exec(compile(module, str(INIT_PATH), "exec"), namespace)
    return namespace["SungrowDiagnosticsView"]


def test_controls_zero_unlimited_disables_limit_instead_of_enabling_winet_floor():
    class Coordinator:
        data = {"battery_level": 50}

        def __init__(self):
            self.export_limits = []
            self.refreshes = 0

        async def set_export_limit(self, watts):
            self.export_limits.append(watts)
            return True

        async def async_request_refresh(self):
            self.refreshes += 1

    coordinator = Coordinator()
    entry = SimpleNamespace(
        entry_id="entry-1", data={"sungrow_host": "192.0.2.1"}, options={}
    )
    hass = SimpleNamespace(
        data={"power_sync": {"entry-1": {"sungrow_coordinator": coordinator}}},
        config_entries=SimpleNamespace(async_entries=lambda _domain: [entry]),
    )
    view = _load_diagnostics_view()(hass)

    response = asyncio.run(view.post(_Request()))

    assert response.status == 200
    assert response.body == {"success": True, "results": {"export_limit_w": True}}
    assert coordinator.export_limits == [None]
    assert coordinator.refreshes == 1
