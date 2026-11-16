"""Optional support failures must remain diagnosable without logging secrets."""
import ast
import logging
from pathlib import Path
from types import ModuleType
import sys

import pytest


SOURCE = Path(__file__).parents[1] / 'custom_components/power_sync/__init__.py'


def failure_boundary():
    tree = ast.parse(SOURCE.read_text())
    helpers = [node for node in tree.body if isinstance(node, ast.FunctionDef)
               and node.name == '_support_setup_failure_details']
    setup = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef)
                 and node.name == 'async_setup_entry')
    boundary = next(node for node in ast.walk(setup) if isinstance(node, ast.Try)
                    and any(isinstance(child, ast.ImportFrom) and child.module == 'support.ha'
                            for child in node.body))
    wrapper = ast.AsyncFunctionDef(name='run', args=ast.arguments(
        posonlyargs=[], args=[], kwonlyargs=[], kw_defaults=[], defaults=[]),
        body=[boundary, ast.Return(value=ast.Constant(True))], decorator_list=[])
    module = ast.fix_missing_locations(ast.Module(body=[*helpers, wrapper], type_ignores=[]))
    namespace = {'__package__': 'custom_components.power_sync',
                 '_LOGGER': logging.getLogger('support_setup_regression'),
                 'hass': object(), 'entry': object()}
    exec(compile(module, str(SOURCE), 'exec'), namespace)
    return namespace['run']


@pytest.mark.asyncio
@pytest.mark.parametrize('stage', ['capture', 'storage'])
async def test_failed_optional_support_logs_location_without_secrets_and_core_continues(
    stage, monkeypatch, caplog
):
    secret = 'CANARY_CUSTOMER_SECRET'

    def capture():
        private_value = secret
        raise ValueError(private_value)

    def storage():
        private_value = secret
        raise ValueError(private_value)

    async def setup(hass, entry):
        {'capture': capture, 'storage': storage}[stage]()

    adapter = ModuleType('custom_components.power_sync.support.ha')
    adapter.setup = setup
    monkeypatch.setitem(sys.modules, adapter.__name__, adapter)
    caplog.set_level(logging.WARNING, logger='support_setup_regression')
    assert await failure_boundary()() is True
    records = [r for r in caplog.records if r.name == 'support_setup_regression']
    assert len(records) == 1
    message = records[0].getMessage()
    assert 'core integration remains active' in message
    assert 'ValueError' in message
    assert f':{stage}' in message
    assert 'test_support_setup_failure.py:' in message
    assert secret not in caplog.text
    assert records[0].exc_info is None
    assert str(SOURCE.parent) not in message
