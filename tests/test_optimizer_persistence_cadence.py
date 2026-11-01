"""Ticket #145: continuous updates must not starve accounting persistence.

Run the shipped scheduling/constructor statements against verbatim HA 2026.10.4
Store methods, a virtual monotonic clock, and real temporary JSON files. The
fixture preserves HA's timer, cancellation, failure and final-write semantics.
"""

from __future__ import annotations

import ast
import asyncio
from contextlib import suppress
import heapq
import json
import logging
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from conftest import load_source_node


ROOT = Path(__file__).resolve().parents[1]
COORDINATOR = ROOT / "custom_components/power_sync/optimization/coordinator.py"
FIXTURE = ROOT / "tests/fixtures/ha_storage_2026_10_4.py.txt"
FIELDS = (
    "_cost_store", "_battery_provenance_store",
    "_solar_forecast_learning_store", "_battery_efficiency_learning_store",
)
SCHEDULES = (
    "_schedule_cost_save", "_schedule_battery_provenance_save",
    "_schedule_solar_forecast_learning_save",
    "_schedule_battery_efficiency_learning_save",
)


class _Timer:
    def __init__(self, when, callback):
        self.deadline, self.callback, self.cancelled = when, callback, False

    def when(self):
        return self.deadline

    def cancel(self):
        self.cancelled = True


class _Loop:
    def __init__(self):
        self.now, self.sequence, self.timers = 0.0, 0, []
        self.tasks = []

    def time(self):
        return self.now

    def call_at(self, when, callback):
        handle = _Timer(when, callback)
        self.sequence += 1
        heapq.heappush(self.timers, (when, self.sequence, handle))
        return handle

    async def advance(self, target):
        while self.timers and self.timers[0][0] <= target:
            when, _, handle = heapq.heappop(self.timers)
            self.now = when
            if not handle.cancelled:
                handle.callback()
                while self.tasks:
                    await self.tasks.pop(0)
        self.now = target


def _ha_store_class():
    namespace = {
        "callback": lambda f: f, "CoreState": SimpleNamespace(stopping="stopping"),
        "EVENT_HOMEASSISTANT_FINAL_WRITE": "final_write",
        "WriteError": OSError, "json_util": SimpleNamespace(SerializationError=TypeError),
        "_LOGGER": logging.getLogger(__name__),
        "suppress": suppress, "os": os,
    }
    exec(compile(FIXTURE.read_text(), str(FIXTURE), "exec"), namespace)
    cls = namespace["Store"]

    def init(self, hass, version, key):
        self.hass, self.version, self.minor_version, self.key = hass, version, 1, key
        self._data = self._delay_handle = self._unsub_final_write_listener = None
        self._next_write_time, self._read_only = 0.0, False
        self._write_lock = asyncio.Lock()
        self._manager = SimpleNamespace(async_invalidate=lambda key: None)
        self.path = hass.directory / key
        self.writes, self.fail_next = [], False

    async def write(self, data):
        if "data_func" in data:
            data["data"] = data.pop("data_func")()
        if self.fail_next:
            self.fail_next = False
            raise OSError("test disk unavailable")
        self.path.write_text(json.dumps(data))
        self.writes.append((self.hass.loop.time(), data["data"]))

    cls.__init__, cls._async_write_data = init, write
    return cls


def _environment(tmp_path, *, upstream=False):
    loop, listeners = _Loop(), {}

    def listen(event, callback):
        listeners[callback] = event
        return lambda: listeners.pop(callback, None)

    hass = SimpleNamespace(
        loop=loop, state="running", directory=tmp_path,
        bus=SimpleNamespace(async_listen_once=listen),
        async_create_task_internal=lambda coro, **kw: loop.tasks.append(coro),
    )
    store_class = _ha_store_class()
    namespace = {"Store": store_class}
    helper = ROOT / "custom_components/power_sync/optimization/coalescing_store.py"
    if helper.exists() and not upstream:
        # Supply the real HA methods without importing HA or polluting sys.modules.
        node = load_source_node(helper, "CoalescingStore")
        module = ast.Module(body=[ast.ImportFrom(
            module="__future__", names=[ast.alias(name="annotations")], level=0,
        ), node], type_ignores=[])
        exec(compile(ast.fix_missing_locations(module), str(helper), "exec"), namespace)
    else:
        namespace["CoalescingStore"] = store_class

    source = ast.parse(COORDINATOR.read_text())
    for node in source.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and "STORE_" in target.id:
                    try:
                        namespace[target.id] = ast.literal_eval(node.value)
                    except (ValueError, TypeError):
                        pass
    coordinator = SimpleNamespace()
    namespace.update(self=coordinator, hass=hass, entry_id="test")
    init = load_source_node(COORDINATOR, "__init__", parent="OptimizationCoordinator")
    for node in init.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Attribute) and t.attr in FIELDS for t in node.targets
        ):
            exec(compile(ast.Module(body=[node], type_ignores=[]), str(COORDINATOR), "exec"), namespace)
    for name in SCHEDULES:
        node = load_source_node(COORDINATOR, name, parent="OptimizationCoordinator")
        exec(compile(ast.Module(body=[node], type_ignores=[]), str(COORDINATOR), "exec"), namespace)
        setattr(coordinator, name, lambda name=name: namespace[name](coordinator))
    coordinator.value = 0
    data = lambda: {"value": coordinator.value}
    coordinator._cost_data_to_save = coordinator._battery_provenance_data_to_save = data
    coordinator._solar_forecast_learner = SimpleNamespace(to_dict=data)
    coordinator._battery_efficiency_learner = SimpleNamespace(to_dict=data)
    return coordinator, hass, listeners


@pytest.mark.parametrize("field,schedule", zip(FIELDS, SCHEDULES))
def test_continuous_updates_write_latest_data_every_five_minutes(tmp_path, field, schedule):
    async def run():
        coordinator, hass, _ = _environment(tmp_path)
        store = getattr(coordinator, field)
        for second in range(0, 901, 30):
            await hass.loop.advance(second)
            coordinator.value = second
            getattr(coordinator, schedule)()
        assert store.writes == [(300, {"value": 270}), (600, {"value": 570}),
                                (900, {"value": 870})]
        assert json.loads(store.path.read_text())["data"] == {"value": 870}
    asyncio.run(run())


def test_upstream_debounce_starves_until_updates_stop(tmp_path):
    async def run():
        coordinator, hass, _ = _environment(tmp_path, upstream=True)
        for second in range(0, 901, 30):
            await hass.loop.advance(second)
            coordinator.value = second
            coordinator._schedule_cost_save()
        assert coordinator._cost_store.writes == []
        await hass.loop.advance(1200)
        assert coordinator._cost_store.writes == [(1200, {"value": 900})]
    asyncio.run(run())


def test_pending_save_uses_replacement_callback_and_recovers_from_callback_error(tmp_path):
    async def run():
        coordinator, hass, _ = _environment(tmp_path)
        store = coordinator._cost_store
        store.async_delay_save(lambda: {"value": "old"}, 300)
        await hass.loop.advance(100)
        store.async_delay_save(lambda: {"value": "latest"}, 300)
        await hass.loop.advance(300)
        assert store.writes == [(300, {"value": "latest"})]

        def broken_callback():
            raise ValueError("invalid snapshot")

        store.async_delay_save(broken_callback, 300)
        with pytest.raises(ValueError, match="invalid snapshot"):
            await hass.loop.advance(600)
        store.async_delay_save(lambda: {"value": "recovered"}, 300)
        await hass.loop.advance(900)
        assert store.writes[-1] == (900, {"value": "recovered"})
    asyncio.run(run())


def test_updates_during_disk_io_are_saved_by_the_next_deadline(tmp_path):
    async def run():
        coordinator, hass, _ = _environment(tmp_path)
        store = coordinator._cost_store
        write = store._async_write_data

        async def update_while_writing(data):
            await write(data)  # consume the snapshot as HA does before awaiting I/O
            coordinator.value = 2
            coordinator._schedule_cost_save()
            store._async_write_data = write

        store._async_write_data = update_while_writing
        coordinator.value = 1
        coordinator._schedule_cost_save()
        await hass.loop.advance(600)
        assert store.writes == [(300, {"value": 1}), (600, {"value": 2})]
    asyncio.run(run())


@pytest.mark.parametrize("field,schedule", zip(FIELDS, SCHEDULES))
def test_failed_write_retries_on_next_update(tmp_path, field, schedule):
    async def run():
        coordinator, hass, _ = _environment(tmp_path)
        store = getattr(coordinator, field)
        store.fail_next = True
        getattr(coordinator, schedule)()
        await hass.loop.advance(300)
        assert not store.path.exists()
        coordinator.value = 2
        getattr(coordinator, schedule)()
        await hass.loop.advance(600)
        assert store.writes == [(600, {"value": 2})]
    asyncio.run(run())


@pytest.mark.parametrize("operation", ["flush", "remove", "final_write"])
def test_cancel_flush_and_shutdown_allow_subsequent_saves(tmp_path, operation):
    async def run():
        coordinator, hass, listeners = _environment(tmp_path)
        store = coordinator._cost_store
        coordinator._schedule_cost_save()
        await hass.loop.advance(100)
        coordinator.value = 1
        if operation == "flush":
            await store.async_save(coordinator._cost_data_to_save())
        elif operation == "remove":
            # Remove's executor is the only additional boundary under test.
            async def executor(func, *args):
                return func(*args)
            hass.async_add_executor_job = executor
            await store.async_remove()
        else:
            hass.state = "stopping"
            await hass.loop.advance(300)
            assert not store.writes
            for callback in list(listeners):
                listeners.pop(callback)  # HA bus removes listen_once before dispatch
                await callback(None)
            hass.state = "running"
        assert not listeners
        before = len(store.writes)
        await hass.loop.advance(400)
        assert len(store.writes) == before  # cancelled timers cannot write stale data
        coordinator.value = 2
        coordinator._schedule_cost_save()
        await hass.loop.advance(700)
        assert store.writes[-1] == (700, {"value": 2})
    asyncio.run(run())
