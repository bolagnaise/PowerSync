"""Authenticated calibration view validates and uses the actual service registry."""
import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest


def fixture(body, *, user=True, result=None):
    class View:
        def json(self, payload, status_code=200): return status_code, payload
    def validate(value):
        if type(value) is not int or not 1 <= value <= 2**32-1: raise ValueError()
    namespace = dict(HomeAssistantView=View, HomeAssistant=object, web=SimpleNamespace(Request=object, Response=object),
        HomeAssistantError=RuntimeError, DOMAIN="power_sync", validate_delay_hours=validate,
        Context=lambda **kw: SimpleNamespace(**kw))
    path = Path(__file__).resolve().parents[1] / 'custom_components/power_sync/powerwall_local/views.py'
    node = next(n for n in ast.parse(path.read_text()).body if isinstance(n, ast.ClassDef) and n.name == 'PowerwallCalibrationDelayView')
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(path), 'exec'), namespace)
    service = AsyncMock(return_value=result or {'command_acknowledged': True})
    view = namespace[node.name](SimpleNamespace(services=SimpleNamespace(async_call=service)))
    req = SimpleNamespace(json=AsyncMock(return_value=body), get=lambda key: SimpleNamespace(id='owner') if user else None, query={})
    return view, req, service


@pytest.mark.parametrize('body', [None, [], {}, {'delay_hours': True, 'request_id': 'id'}, {'delay_hours': 0, 'request_id': 'id'},
    {'delay_hours': '1', 'request_id': 'id'}, {'delay_hours': 1, 'request_id': ''}, {'delay_hours': 1, 'request_id': '☀'}, {'delay_hours': 1, 'request_id': 'id', 'entry_id': []}])
def test_invalid_request_never_calls_service(body):
    view, req, service = fixture(body)
    assert asyncio.run(view.post(req))[0] == 400
    service.assert_not_awaited()


def test_view_uses_real_authenticated_context_and_only_allowed_fields():
    view, req, service = fixture({'delay_hours': 2, 'request_id': 'id', 'entry_id': 'target', 'user_id': 'forged'})
    assert asyncio.run(view.post(req))[0] == 200
    args = service.await_args
    assert args.args == ('power_sync', 'powerwall_delay_battery_calibration', {'delay_hours': 2, 'request_id': 'id', 'entry_id': 'target'})
    assert args.kwargs['context'].user_id == 'owner'
    assert args.kwargs['return_response'] is True and args.kwargs['blocking'] is True


def test_missing_auth_and_unconfirmed_ack():
    view, req, service = fixture({'delay_hours': 1, 'request_id': 'id'}, user=False)
    assert asyncio.run(view.post(req))[0] == 401
    service.assert_not_awaited()
    view, req, service = fixture({'delay_hours': 1, 'request_id': 'id'}, result={'command_acknowledged': False})
    assert asyncio.run(view.post(req))[0] == 503
