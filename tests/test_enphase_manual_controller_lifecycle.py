"""Regression coverage for manual AC-inverter controller ownership."""

from __future__ import annotations

import ast
import asyncio
import textwrap
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parent.parent
INIT_PATH = ROOT / "custom_components" / "power_sync" / "__init__.py"


def _handler_source(name: str) -> str:
    source = INIT_PATH.read_text()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == name:
            result = ast.get_source_segment(source, node)
            assert result is not None
            return textwrap.dedent(result)
    raise AssertionError(f"{name} was not found")


class _Logger:
    def __getattr__(self, _name):
        return lambda *args, **kwargs: None


class _Controller:
    def __init__(
        self, *, curtail_result: bool = True, restore_result: bool = True
    ) -> None:
        self.curtail_result = curtail_result
        self.restore_result = restore_result
        self.curtail_calls = 0
        self.restore_calls = 0
        self.disconnect_calls = 0

    async def curtail(self, **_kwargs) -> bool:
        self.curtail_calls += 1
        return self.curtail_result

    async def restore(self) -> bool:
        self.restore_calls += 1
        return self.restore_result

    async def disconnect(self) -> None:
        self.disconnect_calls += 1


def _handlers(controller: _Controller):
    entry_id = "entry-id"
    entry_data: dict[str, object] = {}
    hass = SimpleNamespace(data={"power_sync": {entry_id: entry_data}})
    entry = SimpleNamespace(
        entry_id=entry_id,
        options={},
        data={
            "ac_inverter_curtailment_enabled": True,
            "inverter_brand": "enphase",
            "inverter_host": "192.0.2.10",
        },
    )
    created = []
    states = []

    def _get_inverter_controller(**_kwargs):
        created.append(controller)
        return controller

    async def _get_live_status():
        return {"load_power": 800}

    constants = {
        "CONF_AC_INVERTER_CURTAILMENT_ENABLED",
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
    }
    namespace = {
        "asyncio": asyncio,
        "DOMAIN": "power_sync",
        "entry": entry,
        "hass": hass,
        "DEFAULT_INVERTER_PORT": 502,
        "DEFAULT_INVERTER_SLAVE_ID": 1,
        "INVERTER_CONTROL_MODE_LOAD_FOLLOWING": "load_following",
        "INVERTER_CONTROL_MODE_SHUTDOWN": "shutdown",
        "INVERTER_CONTROL_MODE_NORMAL": "normal",
        "_LOGGER": _Logger(),
        "ac_inverter_is_same_hybrid": lambda: False,
        "get_inverter_controller": _get_inverter_controller,
        "get_live_status": _get_live_status,
        "_set_inverter_control_state": lambda *args, **kwargs: states.append(args),
    }
    namespace.update({name: name.removeprefix("CONF_").lower() for name in constants})
    exec(_handler_source("handle_curtail_inverter"), namespace)
    exec(_handler_source("handle_restore_inverter"), namespace)
    return namespace, entry_data, created, states


def test_manual_restore_reuses_and_closes_the_retained_controller():
    controller = _Controller()
    namespace, entry_data, created, states = _handlers(controller)

    asyncio.run(namespace["handle_curtail_inverter"](SimpleNamespace(data={})))
    asyncio.run(namespace["handle_restore_inverter"](SimpleNamespace(data={})))

    assert created == [controller]
    assert controller.curtail_calls == 1
    assert controller.restore_calls == 1
    assert controller.disconnect_calls == 1
    assert "inverter_controller" not in entry_data
    assert states[-1] == ("normal",)


def test_failed_manual_restore_still_closes_and_removes_retained_controller():
    controller = _Controller(restore_result=False)
    namespace, entry_data, _created, states = _handlers(controller)
    entry_data["inverter_controller"] = controller

    asyncio.run(namespace["handle_restore_inverter"](SimpleNamespace(data={})))

    assert controller.restore_calls == 1
    assert controller.disconnect_calls == 1
    assert "inverter_controller" not in entry_data
    assert states == []


def test_failed_manual_curtail_closes_and_does_not_retain_controller():
    controller = _Controller(curtail_result=False)
    namespace, entry_data, _created, states = _handlers(controller)

    asyncio.run(namespace["handle_curtail_inverter"](SimpleNamespace(data={})))

    assert controller.curtail_calls == 1
    assert controller.disconnect_calls == 1
    assert "inverter_controller" not in entry_data
    assert states == []
