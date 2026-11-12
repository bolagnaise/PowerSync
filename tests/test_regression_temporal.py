import asyncio
from pathlib import Path
import shutil
import sys
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
from regression_runner.temporal import replay, TemporalError
ROOT = Path(__file__).resolve().parents[1]/'custom_components/power_sync'
INITIAL = dict(reserve=20, held_reserve=60, last_action='self_consumption', ev_preserve=False, monitoring=False, configured_reserve=.2)


def test_actual_coordinator_retries_failure_then_restores_at_timer_boundary():
    events = [dict(at=0, type='response', value=False), dict(at=0, type='call', method='poll'),
              dict(at=1, type='response', value=True), dict(at=1, type='timer', due=300, method='poll'), dict(at=300, type='advance')]
    result = asyncio.run(replay(ROOT, INITIAL, events))
    assert [row['at'] for row in result['commands']] == [0, 300]
    assert result['state']['reserve'] is None and result['state']['held_reserve'] is None


def test_restart_invalidates_old_timer_and_preserves_effective_state():
    events = [dict(at=0, type='timer', due=300, method='poll'), dict(at=100, type='restart'),
              dict(at=300, type='advance'), dict(at=301, type='call', method='floor')]
    result = asyncio.run(replay(ROOT, INITIAL, events))
    assert result['commands'] == [] and result['restarts'] == 1
    assert result['returns'][-1]['value'] == .2


def test_monitoring_and_ev_preservation_survive_virtual_time_without_commands():
    for state in [{**INITIAL, 'monitoring': True}, {**INITIAL, 'ev_preserve': True}]:
        result = asyncio.run(replay(ROOT, state, [dict(at=0, type='call', method='poll'), dict(at=604800, type='advance')]))
        assert result['commands'] == [] and result['state']['reserve'] == 20


def test_changed_real_method_is_observed_and_unknown_boundaries_fail(tmp_path):
    (tmp_path/'optimization').mkdir()
    shutil.copytree(ROOT/'licensing', tmp_path/'licensing', ignore=shutil.ignore_patterns('__pycache__'))
    source = (ROOT/'optimization/coordinator.py').read_text().replace('self._last_executed_action != "idle"', 'self._last_executed_action == "idle"', 1)
    (tmp_path/'optimization/coordinator.py').write_text(source)
    events = [dict(at=0, type='response', value=True), dict(at=0, type='call', method='poll')]
    baseline = asyncio.run(replay(ROOT, INITIAL, events))
    with pytest.raises(TemporalError, match='unconsumed'):
        asyncio.run(replay(tmp_path, INITIAL, events))
    assert baseline['commands']
    with pytest.raises(TemporalError):
        asyncio.run(replay(ROOT, INITIAL, [dict(at=0, type='call', method='service_call')]))
