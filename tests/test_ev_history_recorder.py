"""Recorded trip backfill, local retention and actual-arrival reconciliation."""
from datetime import datetime, timedelta, timezone
import ast
import asyncio
import importlib.util
from pathlib import Path
import sys
import types
from unittest.mock import AsyncMock

import pytest

ROOT = Path(__file__).resolve().parents[1] / 'custom_components/power_sync/optimization'
NOW = datetime(2026, 11, 16, 12, tzinfo=timezone.utc)


@pytest.fixture
def recorder_module(monkeypatch):
    name = 'history_recorder_probe'
    package = types.ModuleType(name)
    package.__path__ = [str(ROOT)]
    monkeypatch.setitem(sys.modules, name, package)
    for leaf in ('ev_history', 'ev_history_recorder'):
        spec = importlib.util.spec_from_file_location(f'{name}.{leaf}', ROOT / f'{leaf}.py')
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, spec.name, module)
        spec.loader.exec_module(module)
    return module


def state(value, stamp=NOW):
    return types.SimpleNamespace(state=str(value), last_changed=stamp, last_updated=stamp)


def test_backfill_survives_purge_then_actual_arrival_removes_prediction(recorder_module, monkeypatch):
    module = recorder_module
    raw = {'sensor.car_soc': [], 'device_tracker.car_location': []}
    for week in (1, 2, 3):
        day = NOW - timedelta(weeks=week)
        for hour, soc, place in [(8, 75, 'work'), (16, 65, 'home')]:
            raw['sensor.car_soc'].append(state(soc, day.replace(hour=hour)))
            raw['device_tracker.car_location'].append(state(place, day.replace(hour=hour)))
    async def run(callable, *args):
        return callable(*args)
    fake_recorder = types.ModuleType('homeassistant.components.recorder')
    fake_recorder.get_instance = lambda hass: types.SimpleNamespace(async_add_executor_job=run)
    fake_history = types.ModuleType('homeassistant.components.recorder.history')
    fake_history.get_significant_states = lambda *args: raw
    monkeypatch.setitem(sys.modules, fake_recorder.__name__, fake_recorder)
    monkeypatch.setitem(sys.modules, fake_history.__name__, fake_history)
    states = {'sensor.car_soc': state(75), 'device_tracker.car_location': state('work')}
    saved = []
    def create(existing=None):
        recorder = object.__new__(module.EVHistoryRecorder)
        recorder.hass = types.SimpleNamespace(states=types.SimpleNamespace(get=states.get), async_add_executor_job=run)
        recorder.store = types.SimpleNamespace(async_load=AsyncMock(return_value=existing),
            async_delay_save=lambda callback, delay: saved.append(callback()))
        recorder.loaded, recorder.points, recorder.refreshed = False, {}, {}
        return recorder
    sources = {'car': module.EVHistorySources('sensor.car_soc', 'device_tracker.car_location')}
    recorder = create()
    estimates = asyncio.run(recorder.forecasts(sources, NOW))
    assert estimates['car'].soc == 65
    assert estimates['car'].arrival.hour == 16
    assert saved[-1]['sources']
    # Raw history has been purged, but the opt-in local summary still trains
    # the same weekday expectation after restart.
    raw.clear()
    restarted = create(saved[-1])
    estimates = asyncio.run(restarted.forecasts(sources, NOW))
    assert estimates['car'].soc == 65
    states['device_tracker.car_location'] = state('home')
    states['sensor.car_soc'] = state(82)
    assert asyncio.run(restarted.forecasts(sources, NOW)) == {}
    assert restarted.status['car']['source'] == 'live'


def test_unknown_and_stale_actual_soc_cannot_create_a_historical_session(recorder_module):
    assert recorder_module.presence('unavailable') is None
    assert recorder_module.presence('unknown', True) is None
    assert recorder_module.presence('off', True) is False
    assert recorder_module.presence('home') is True


def test_disabled_feature_never_reads_history_and_clears_old_results():
    tree = ast.parse((ROOT / 'coordinator.py').read_text())
    node = next(node for node in ast.walk(tree) if isinstance(node, ast.AsyncFunctionDef)
                and node.name == '_refresh_historical_ev_forecasts')
    namespace = {}
    exec(compile(ast.Module(body=[node], type_ignores=[]), '<history refresh>', 'exec'), namespace)
    coordinator = types.SimpleNamespace(_use_historical_ev_data=False,
        _historical_ev_forecasts={'stale': object()}, _historical_ev_plans={'stale': object()})
    asyncio.run(namespace[node.name](coordinator))
    assert coordinator._historical_ev_forecasts == {}
    assert coordinator._historical_ev_plans == {}
    assert coordinator._historical_ev_status == {'source': 'disabled'}


@pytest.mark.parametrize('changed', [False, True])
def test_return_or_setting_change_during_solve_cannot_publish_old_visit(changed):
    tree = ast.parse((ROOT / 'coordinator.py').read_text())
    run = next(node for node in ast.walk(tree) if isinstance(node, ast.AsyncFunctionDef)
               and node.name == '_run_optimization')
    guard = next(node for node in ast.walk(run) if isinstance(node, ast.If)
                 and isinstance(node.test, ast.Compare)
                 and isinstance(node.test.left, ast.Name)
                 and node.test.left.id == 'historical_ev_generation')
    commit = next(node for node in ast.walk(run) if isinstance(node, ast.Assign)
                  and any(isinstance(target, ast.Attribute) and target.attr == '_current_schedule'
                          for target in node.targets))
    probe = ast.parse('async def probe(self, historical_ev_generation, result):\n    pass\n').body[0]
    probe.body = [guard, commit, ast.Return(value=ast.Constant(True))]
    namespace = {}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[probe], type_ignores=[])), '<solve publication>', 'exec'), namespace)
    queued = []
    coordinator = types.SimpleNamespace(_historical_ev_generation=int(changed),
        _current_schedule='previous', _schedule_settings_reoptimization=lambda: queued.append(True))
    result = asyncio.run(namespace['probe'](coordinator, 0, types.SimpleNamespace(schedule='candidate')))
    assert result is (not changed)
    assert coordinator._current_schedule == ('previous' if changed else 'candidate')
    assert queued == ([True] if changed else [])


@pytest.mark.parametrize('value', ['75', '74'])
def test_trip_listener_ignores_attribute_refreshes_but_invalidates_new_soc(value):
    tree = ast.parse((ROOT / 'coordinator.py').read_text())
    callback = next(node for node in ast.walk(tree) if isinstance(node, ast.AsyncFunctionDef)
                    and node.name == '_history_input_changed')
    queued = []
    coordinator = types.SimpleNamespace(_use_historical_ev_data=True, _enabled=True,
        _historical_ev_generation=0, _historical_ev_forecasts={'car': 1}, _historical_ev_plans={'car': 1},
        _schedule_settings_reoptimization=lambda: queued.append(True))
    namespace = {'self': coordinator}
    exec(compile(ast.Module(body=[callback], type_ignores=[]), '<trip listener>', 'exec'), namespace)
    event = types.SimpleNamespace(data={'old_state': state(75), 'new_state': state(value)})
    asyncio.run(namespace[callback.name](event))
    changed = value != '75'
    assert coordinator._historical_ev_generation == int(changed)
    assert bool(coordinator._historical_ev_plans) is (not changed)
    assert queued == ([True] if changed else [])
