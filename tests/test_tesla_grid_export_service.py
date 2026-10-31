"""Regression tests for Tesla grid-export application responses."""

from __future__ import annotations

import ast
import asyncio
import textwrap
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest


INIT_PATH = (
    Path(__file__).parents[1]
    / "custom_components"
    / "power_sync"
    / "__init__.py"
)


@pytest.mark.parametrize('export_fails', [False, True])
def test_export_selector_finishes_before_next_automation_action(export_fails):
    """A sequential HA automation must not race export with grid charging."""
    tree = ast.parse(INIT_PATH.with_name('select.py').read_text())
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef)
               and n.name == 'TeslaGridExportRuleSelect')
    method = next(n for n in cls.body if isinstance(n, ast.AsyncFunctionDef)
                  and n.name == 'async_select_option')
    namespace = {'DOMAIN': 'power_sync'}
    exec(compile(ast.Module(body=[method], type_ignores=[]), '<export selector>', 'exec'), namespace)

    async def scenario():
        completed, queued = [], []

        async def apply_export():
            await asyncio.sleep(0)
            if export_fails:
                raise RuntimeError('export did not verify')
            completed.append('export')

        async def service(domain, name, data, *, blocking):
            assert (domain, name, data['rule']) == ('power_sync', 'set_grid_export', 'never')
            if blocking:
                await apply_export()
            else:
                queued.append(apply_export())

        selector = SimpleNamespace(hass=SimpleNamespace(services=SimpleNamespace(async_call=service)))
        try:
            if export_fails:
                with pytest.raises(RuntimeError, match='export did not verify'):
                    await namespace['async_select_option'](selector, 'never')
            else:
                await namespace['async_select_option'](selector, 'never')
                assert completed == ['export'], 'The following grid action must wait for export'
                completed.append('grid charging')
                assert completed == ['export', 'grid charging']
        finally:
            for operation in queued:
                operation.close()

    asyncio.run(scenario())


def _load_response_helper():
    source = INIT_PATH.read_text()
    module = ast.parse(source)
    for node in module.body:
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_tesla_grid_export_write_result":
            segment = ast.get_source_segment(source, node)
            assert segment is not None
            namespace: dict[str, Any] = {"Any": Any}
            exec("from __future__ import annotations\n" + segment, namespace)
            return namespace[node.name]
    raise AssertionError("_tesla_grid_export_write_result not found")


def _load_grid_export_handler(namespace: dict[str, Any]):
    source = INIT_PATH.read_text()
    module = ast.parse(source)
    for node in module.body:
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "async_setup_entry":
            for child in node.body:
                if isinstance(child, ast.AsyncFunctionDef) and child.name == "handle_set_grid_export":
                    segment = ast.get_source_segment(source, child)
                    assert segment is not None
                    segment = segment.replace(
                        "            from .const import CONF_POWERWALL_LOCAL_DIN\n",
                        "",
                    ).replace(
                        "            from .powerwall_local.dispatch import dispatch_powerwall_write\n",
                        "",
                    )
                    exec(
                        "from __future__ import annotations\n"
                        + textwrap.dedent(segment),
                        namespace,
                    )
                    return namespace[child.name]
    raise AssertionError("handle_set_grid_export not found")


class _Response:
    def __init__(self, payload: Any):
        self.payload = payload

    async def json(self) -> Any:
        return self.payload


def test_http_200_explicit_rejection_is_not_successful():
    helper = _load_response_helper()

    accepted, reason = asyncio.run(
        helper(_Response({"response": {"result": False, "reason": "not_allowed"}}))
    )

    assert accepted is False
    assert reason == "not_allowed"


def test_http_200_explicit_acceptance_is_successful():
    helper = _load_response_helper()

    accepted, reason = asyncio.run(
        helper(_Response({"response": {"result": True}}))
    )

    assert accepted is True
    assert reason is None


def test_http_200_missing_result_fails_closed():
    helper = _load_response_helper()

    accepted, reason = asyncio.run(helper(_Response({"response": {}})))

    assert accepted is False
    assert reason == "Tesla rejected the grid export rule"


def test_rejected_http_200_does_not_promote_requested_rule_to_snapshot():
    class _Logger:
        def __getattr__(self, _name):
            return lambda *_args, **_kwargs: None

    class _Response:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def json(self):
            return {"response": {"result": False, "reason": "not_allowed"}}

    class _Session:
        def post(self, *_args, **_kwargs):
            return _Response()

    snapshot = SimpleNamespace(grid_export_rule="battery_ok")
    entry_data = {
        "powerwall_local": {"coordinator": SimpleNamespace(data=snapshot)},
    }
    hass = SimpleNamespace(
        data={"power_sync": {"entry1": entry_data}},
        async_create_task=lambda _coro: None,
    )
    entry = SimpleNamespace(
        entry_id="entry1",
        data={
            "powerwall_local_din": "din",
            "tesla_energy_site_id": "site-1",
        },
        options={},
    )
    namespace: dict[str, Any] = {
        "Any": Any,
        "DOMAIN": "power_sync",
        "CONF_BATTERY_CURTAILMENT_ENABLED": "battery_curtailment_enabled",
        "CONF_FLEET_API_BASE_URL": "fleet_api_base_url",
        "CONF_POWERWALL_LOCAL_DIN": "powerwall_local_din",
        "CONF_TESLA_ENERGY_SITE_ID": "tesla_energy_site_id",
        "HomeAssistantError": RuntimeError,
        "_LOGGER": _Logger(),
        "_monitoring_mode_should_block_control": lambda _call: False,
        "_supersede_tesla_charge_kick": lambda *args: None,
        "_prepare_tesla_setting_change": __import__('unittest.mock', fromlist=['AsyncMock']).AsyncMock(return_value=(0, 0, 0)),
        "_command_generation": [0], "_tesla_charge_kick_generation": [0],
        "_tesla_force_apply_export_rule": __import__('unittest.mock', fromlist=['AsyncMock']).AsyncMock(return_value=False),
        "_get_tesla_site_configs": lambda _hass, _entry: [("site-1", "token", "fleet")],
        "async_get_clientsession": lambda _hass: _Session(),
        "get_tesla_api_base_url": lambda *_args: "https://tesla.example",
        "aiohttp": SimpleNamespace(ClientTimeout=lambda total: total),
        "hass": hass,
        "entry": entry,
        "_tesla_grid_export_write_result": _load_response_helper(),
    }

    async def _dispatch(_hass, _entry, *, cloud_call, **_kwargs):
        return await cloud_call()

    async def _update_cached(_rule):
        raise AssertionError("rejected write must not update the cached rule")

    async def _refresh(_label):
        raise AssertionError("rejected write must not schedule a refresh")

    namespace["dispatch_powerwall_write"] = _dispatch
    namespace["update_cached_export_rule"] = _update_cached
    namespace["refresh_powerwall_local_after_settings_write"] = _refresh
    handler = _load_grid_export_handler(namespace)

    with pytest.raises(RuntimeError, match="did not accept"):
        asyncio.run(handler(SimpleNamespace(data={"rule": "never"})))

    assert snapshot.grid_export_rule == "battery_ok"
    assert "cached_export_rule" not in entry_data
