import asyncio
from pathlib import Path
import shutil
import sys
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
from regression_runner.temporal import replay, TemporalError
from regression_runner.compare import temporal_surface,temporal_changed_methods
import ast
ROOT = Path(__file__).resolve().parents[1]/'custom_components/power_sync'
INITIAL = dict(reserve=20, held_reserve=60, last_action='self_consumption', ev_preserve=False, scheduled_ev_state=False, monitoring=False, configured_reserve=.2)


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
    assert temporal_surface(ROOT/'optimization/coordinator.py')==temporal_surface(tmp_path/'optimization/coordinator.py')
    assert temporal_changed_methods(ROOT/'optimization/coordinator.py',tmp_path/'optimization/coordinator.py')=={'_should_restore_pre_idle_backup_reserve_from_polling'}
    assert not temporal_changed_methods(ROOT/'optimization/coordinator.py',tmp_path/'optimization/coordinator.py')<={'_restore_pre_idle_backup_reserve'}


def test_restore_post_state_includes_action_ev_configuration_and_rejects_unknown_writes(tmp_path):
    (tmp_path/'optimization').mkdir()
    shutil.copytree(ROOT/'licensing',tmp_path/'licensing',ignore=shutil.ignore_patterns('__pycache__'))
    source=ast.parse((ROOT/'optimization/coordinator.py').read_text())
    cls=next(node for node in source.body if isinstance(node,ast.ClassDef) and node.name=='OptimizationCoordinator')
    method=next(node for node in cls.body if isinstance(node,ast.AsyncFunctionDef) and node.name=='_restore_pre_idle_backup_reserve')
    block=next(node for node in method.body if isinstance(node,ast.Try))
    block.body[-1:-1]=ast.parse('self._last_executed_action = None').body
    (tmp_path/'optimization/coordinator.py').write_text(ast.unparse(ast.fix_missing_locations(source)))
    events=[dict(at=0,type='response',value=True),dict(at=0,type='call',method='restore')]
    result=asyncio.run(replay(tmp_path,INITIAL,events))
    assert result['state']['last_action'] is None
    assert asyncio.run(replay(ROOT,INITIAL,events))['state']['last_action']=='self_consumption'
    block.body[-1:-1]=ast.parse('self.unobserved_flag = True').body
    (tmp_path/'optimization/coordinator.py').write_text(ast.unparse(ast.fix_missing_locations(source)))
    with pytest.raises(TemporalError,match='unsupported_temporal_mutation'):
        asyncio.run(replay(tmp_path,INITIAL,events))
