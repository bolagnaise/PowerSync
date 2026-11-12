"""Closed temporal driver for actual coordinator reserve-recovery methods.

Run candidate sources only inside the private OS worker. This profile exercises named source methods, not a second implementation of
their decisions. Unknown events, state, boundaries or changed uncovered methods
are unsupported. Provider fetching, full optimization and hardware physics are
separate coverage profiles.
"""
import ast
import asyncio
import copy
import hashlib
import heapq
import inspect
import logging
import math
from pathlib import Path
from types import SimpleNamespace

METHODS = frozenset({'_reserve_ratio', '_force_discharge_reserve_floor', '_scheduled_ev_preserve_active',
                     '_should_restore_pre_idle_backup_reserve_from_polling', '_restore_pre_idle_backup_reserve'})
STATE = frozenset({'reserve', 'held_reserve', 'last_action', 'ev_preserve', 'monitoring', 'configured_reserve'})


class TemporalError(ValueError):
    pass


def coordinator(root):
    source = (Path(root)/'optimization/coordinator.py').read_text()
    module = ast.parse(source)
    cls = next(node for node in module.body if isinstance(node, ast.ClassDef) and any(
        isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)) and method.name == '_run_optimization' for method in node.body))
    methods = [copy.deepcopy(node) for node in cls.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in METHODS]
    if {node.name for node in methods} != METHODS:
        raise TemporalError('unsupported_coordinator_surface')
    for node in methods:
        node.decorator_list = [decorator for decorator in node.decorator_list if not isinstance(decorator,ast.Name) or decorator.id != 'observe_reserve_restore']
        if node.decorator_list and not all(isinstance(item, ast.Name) and item.id in ('staticmethod', 'restoration_work') for item in node.decorator_list):
            raise TemporalError('unsupported_decorator')
    extracted = ast.Module(body=[ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0),
        ast.ClassDef(name='ObservedCoordinator', bases=[], keywords=[], body=methods, decorator_list=[])], type_ignores=[])
    # Execute the real restoration decorator/fence, with the runtime data key
    # taken from source. No licence manager or hardware client is started.
    guard = ast.parse((Path(root)/'licensing/guard.py').read_text())
    runtime = ast.parse((Path(root)/'licensing/runtime.py').read_text())
    data_key = next(node for node in runtime.body if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == 'DATA_KEY' for target in node.targets))
    body = []
    for node in guard.body:
        if isinstance(node, ast.ImportFrom) and node.level:
            if node.module != 'runtime' or [name.name for name in node.names] != ['DATA_KEY']:
                raise TemporalError('unsupported_guard_import')
            body.append(copy.deepcopy(data_key))
        else:
            body.append(node)
    namespace = {'_LOGGER': logging.getLogger('private_temporal_replay'), '_temporal_monitoring':lambda *_:None, '_temporal_boundary':lambda *_:None, '_temporal_exception':lambda *_:None}
    exec(compile(ast.fix_missing_locations(ast.Module(body=body, type_ignores=[])), '<reviewed-restoration-guard>', 'exec'), namespace)
    exec(compile(ast.fix_missing_locations(extracted), '<reviewed-coordinator-methods>', 'exec'), namespace)
    return namespace['ObservedCoordinator'], hashlib.sha256((ast.dump(extracted, include_attributes=False)+ast.dump(guard, include_attributes=False)+ast.dump(data_key, include_attributes=False)).encode()).hexdigest()


async def replay(root, initial, events):
    if type(initial) is not dict or set(initial) != STATE or not 1 <= len(events) <= 10000:
        raise TemporalError('temporal_capture_incomplete')
    def validate_state(value):
        for key in ('reserve', 'held_reserve', 'configured_reserve'):
            number = value[key]
            if number is not None and (type(number) not in (int, float) or not math.isfinite(number) or not 0 <= number <= 100):
                raise TemporalError('invalid_temporal_state')
        if any(type(value[key]) is not bool for key in ('ev_preserve', 'monitoring')) or value['last_action'] not in ('idle', 'self_consumption', 'charge', 'export', None):
            raise TemporalError('invalid_temporal_state')
    validate_state(initial)
    missing = [False]
    cls, source = coordinator(root)
    observed = cls()
    commands, returns = [], []
    response = []
    generation = 0
    clock = 0
    queue = []
    state = copy.deepcopy(initial)
    def install():
        observed._pre_idle_backup_reserve = state['reserve']
        observed._idle_hold_reserve = state['held_reserve']
        observed._last_executed_action = state['last_action']
        observed._scheduled_ev_no_discharge_active = state['ev_preserve']
        observed._config = SimpleNamespace(backup_reserve=state['configured_reserve'])
        observed.entry_id = 'logical-installation'
        observed.hass = SimpleNamespace(data={'power_sync': {'logical-installation': {'scheduled_ev_preserve_state': {'active': state['ev_preserve']}}}})
        observed._monitoring_mode_active = lambda: state['monitoring']
    install()
    class Battery:
        async def set_backup_reserve(self, reserve):
            commands.append({'at': clock, 'generation': generation, 'operation': 'set_backup_reserve', 'reserve': reserve})
            if not response:
                missing[0] = True
                raise TemporalError('boundary_response_missing')
            result = response.pop(0)
            if result == 'exception':
                raise RuntimeError('recorded_boundary_exception')
            return result
    async def call(method):
        if method == 'poll':
            value = observed._should_restore_pre_idle_backup_reserve_from_polling()
            if value:
                value = await observed._restore_pre_idle_backup_reserve(Battery())
        elif method == 'restore':
            value = await observed._restore_pre_idle_backup_reserve(Battery())
        elif method == 'floor':
            value = observed._force_discharge_reserve_floor()
        elif method == 'ev_preserve':
            value = observed._scheduled_ev_preserve_active()
        else:
            raise TemporalError('unsupported_temporal_call')
        if missing[0]:
            raise TemporalError('boundary_response_missing')
        returns.append({'at': clock, 'generation': generation, 'value': value})
        state['reserve'], state['held_reserve'] = observed._pre_idle_backup_reserve, observed._idle_hold_reserve
    for index, event in enumerate(events):
        if type(event) is not dict or type(event.get('at')) is not int or not clock <= event['at'] <= 7*86400:
            raise TemporalError('invalid_virtual_clock')
        while queue and queue[0][0] <= event['at']:
            when, _, timer_generation, method = heapq.heappop(queue)
            clock = when
            if timer_generation == generation:
                await call(method)
        clock = event['at']
        kind = event.get('type')
        if kind == 'state' and set(event) == {'at', 'type', 'changes'} and type(event['changes']) is dict and not event['changes'].keys()-STATE:
            state.update(event['changes']); validate_state(state); install()
        elif kind == 'response' and set(event) == {'at', 'type', 'value'} and (event['value'] is None or type(event['value']) is bool or event['value'] == 'exception'):
            response.append(event['value'])
        elif kind == 'call' and set(event) == {'at', 'type', 'method'}:
            await call(event['method'])
        elif kind == 'timer' and set(event) == {'at', 'type', 'due', 'method'} and type(event['due']) is int and clock <= event['due'] <= 7*86400:
            heapq.heappush(queue, (event['due'], index, generation, event['method']))
        elif kind == 'restart' and set(event) == {'at', 'type'}:
            generation += 1; observed = cls(); install()
        elif kind == 'advance' and set(event) == {'at', 'type'}:
            pass
        else:
            raise TemporalError('unsupported_temporal_event')
    if queue or response:
        raise TemporalError('unconsumed_temporal_boundaries')
    return {'profile': 'reserve-recovery-temporal-v1', 'source': source, 'methods': sorted(METHODS),
            'commands': commands, 'returns': returns, 'state': state, 'elapsed_virtual_seconds': clock, 'restarts': generation}
