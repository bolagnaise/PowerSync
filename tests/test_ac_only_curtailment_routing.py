"""Regression coverage for AC-only automatic solar-curtailment routing.

An explicitly enabled separate AC inverter must make the automatic trigger
eligible without enabling the battery/native-DC routes.  The REST and
WebSocket handlers must then use the existing AC helper for both economic
entry and restore decisions.
"""

from __future__ import annotations

import ast
import asyncio
import textwrap
from pathlib import Path
from types import SimpleNamespace


INIT_PATH = (
    Path(__file__).resolve().parent.parent
    / "custom_components"
    / "power_sync"
    / "__init__.py"
)


def _nested_function_source(name: str) -> str:
    source = INIT_PATH.read_text()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            segment = ast.get_source_segment(source, node)
            assert segment is not None
            return textwrap.dedent(segment)
    raise AssertionError(f"{name} not found")


def _build_route_helpers(entry):
    def effective_configuration(_entry):
        options = _entry.options
        data = _entry.data
        battery_enabled = bool(
            options.get(
                "battery_curtailment_enabled",
                data.get("battery_curtailment_enabled", False),
            )
        )
        battery_system = options.get(
            "battery_system", data.get("battery_system")
        )
        dc_key = {
            "solaredge": "solaredge_dc_curtailment_enabled",
        }.get(battery_system)
        direct_dc_enabled = bool(
            options.get(dc_key, data.get(dc_key, False))
        ) if dc_key else False
        return battery_enabled, direct_dc_enabled

    namespace = {
        "entry": entry,
        "CONF_AC_INVERTER_CURTAILMENT_ENABLED": "ac_inverter_curtailment_enabled",
        "get_effective_solar_curtailment_configuration": effective_configuration,
    }
    exec(_nested_function_source("_effective_solar_curtailment_enabled"), namespace)
    exec(_nested_function_source("_ac_only_solar_curtailment_enabled"), namespace)
    return (
        namespace["_effective_solar_curtailment_enabled"],
        namespace["_ac_only_solar_curtailment_enabled"],
    )


def test_ac_opt_in_is_eligible_but_does_not_authorize_battery_or_dc_routes():
    ac_only = SimpleNamespace(
        options={"ac_inverter_curtailment_enabled": True},
        data={},
    )
    effective, ac_only_route = _build_route_helpers(ac_only)
    assert effective() is True
    assert ac_only_route() is True

    for option in ("battery_curtailment_enabled", "solaredge_dc_curtailment_enabled"):
        mixed_options = {
            "ac_inverter_curtailment_enabled": True,
            option: True,
        }
        if option == "solaredge_dc_curtailment_enabled":
            mixed_options["battery_system"] = "solaredge"
        mixed = SimpleNamespace(options=mixed_options, data={})
        effective, ac_only_route = _build_route_helpers(mixed)
        assert effective() is True
        assert ac_only_route() is False

    disabled = SimpleNamespace(options={}, data={})
    effective, ac_only_route = _build_route_helpers(disabled)
    assert effective() is False
    assert ac_only_route() is False


def _build_ac_handler(entry, hass, calls, prices):
    async def apply_inverter_curtailment(
        *, curtail, import_price=None, export_earnings=None
    ):
        calls.append((curtail, import_price, export_earnings))

    namespace = {
        "hass": hass,
        "DOMAIN": "power_sync",
        "entry": entry,
        "amber_coordinator": None,
        "localvolts_coordinator": None,
        "aemo_sensor_coordinator": None,
        "flow_power_kwatch_coordinator": None,
        "octopus_coordinator": None,
        "get_current_prices_for_curtailment": lambda *args: prices["current"],
        "export_earnings_are_uneconomic": lambda earnings, was_active, _entry: (
            earnings < 0
        ),
        "apply_inverter_curtailment": apply_inverter_curtailment,
        "_LOGGER": SimpleNamespace(
            debug=lambda *args, **kwargs: None,
            info=lambda *args, **kwargs: None,
            warning=lambda *args, **kwargs: None,
        ),
    }
    exec(_nested_function_source("handle_ac_inverter_curtailment_only"), namespace)
    return namespace["handle_ac_inverter_curtailment_only"]


def _build_public_handler(name, entry, hass, ac_handler):
    namespace = {
        "ServiceCall": object,
        "entry": entry,
        "hass": hass,
        "DOMAIN": "power_sync",
        "_effective_solar_curtailment_enabled": lambda: True,
        "_monitoring_mode_allows_curtailment": lambda enabled: True,
        "_ac_only_solar_curtailment_enabled": lambda: True,
        "handle_ac_inverter_curtailment_only": ac_handler,
        "_LOGGER": SimpleNamespace(
            debug=lambda *args, **kwargs: None,
            info=lambda *args, **kwargs: None,
        ),
    }
    exec(_nested_function_source(name), namespace)
    return namespace[name]


def test_rest_and_websocket_handlers_reach_ac_curtail_and_restore():
    entry = SimpleNamespace(
        entry_id="entry1",
        options={"ac_inverter_curtailment_enabled": True},
        data={},
    )
    hass = SimpleNamespace(data={"power_sync": {"entry1": {}}})
    calls = []
    prices = {"current": (5.0, 25.0, "synthetic")}
    ac_handler = _build_ac_handler(entry, hass, calls, prices)
    rest = _build_public_handler("handle_solar_curtailment_check", entry, hass, ac_handler)
    websocket = _build_public_handler(
        "handle_solar_curtailment_with_websocket_data", entry, hass, ac_handler
    )

    async def run():
        await rest()
        prices["current"] = (-5.0, 25.0, "synthetic")
        await rest()
        await websocket(
            {"feedIn": {"perKwh": 5.0}, "general": {"perKwh": 25.0}}
        )
        await websocket(
            {"feedIn": {"perKwh": -5.0}, "general": {"perKwh": 25.0}}
        )

    asyncio.run(run())

    assert calls == [
        (True, 25.0, -5.0),
        (False, 25.0, 5.0),
        (True, 25.0, -5.0),
        (False, 25.0, 5.0),
    ]
