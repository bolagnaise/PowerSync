"""Regression coverage for GoodWe DC curtailment on entity-only profiles.

Discord #386: a GoodWe ESA driven through the community GoodWe HA integration
exported 5.92 kW at 100% SOC while the dashboard read "CURTAILED - Export
confirmed stopped".  ``GoodWeEnergyCoordinator`` builds no direct control
surface on any entity-telemetry profile, so ``handle_goodwe_curtailment``
returned at DEBUG without issuing a command and without recording anything -
leaving the status marker free to invent a curtailment from the price alone.
"""

from __future__ import annotations

import ast
import asyncio
import importlib.util
import math
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any


ROOT = Path(__file__).resolve().parent.parent
INIT_PATH = ROOT / "custom_components" / "power_sync" / "__init__.py"
COORDINATOR_PATH = ROOT / "custom_components" / "power_sync" / "coordinator.py"
TARIFF_UTILS_PATH = ROOT / "custom_components" / "power_sync" / "tariff_utils.py"


def _load_with_hysteresis():
    spec = importlib.util.spec_from_file_location(
        "power_sync_tariff_utils_for_goodwe_curtailment_test", TARIFF_UTILS_PATH
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module.with_hysteresis


def _nested_function_source(name: str) -> str:
    source = INIT_PATH.read_text()
    module = ast.parse(source)
    for node in module.body:
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "async_setup_entry":
            for child in node.body:
                if (
                    isinstance(child, (ast.AsyncFunctionDef, ast.FunctionDef))
                    and child.name == name
                ):
                    segment = ast.get_source_segment(source, child)
                    assert segment is not None
                    return segment
    raise AssertionError(f"{name} not found")


class _Logger:
    def __init__(self) -> None:
        self.records: list[tuple[str, str]] = []

    def _record(self, level: str):
        def _log(message, *args, **kwargs):
            self.records.append((level, str(message) % args if args else str(message)))

        return _log

    def __getattr__(self, name: str):
        return self._record(name)

    def levels(self, needle: str) -> list[str]:
        return [level for level, message in self.records if needle in message]


class _Controller:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def curtail(self) -> bool:
        self.calls.append("curtail")
        return True

    async def restore(self, **_kwargs) -> bool:
        self.calls.append("restore")
        return True


class _UnverifiedController(_Controller):
    def get_grid_export_restore_state(self):
        return {"grid_export": 0, "grid_export_limit": 5000}

    async def curtail(self) -> bool:
        self.calls.append("curtail")
        return False


class _RestoreFailureController(_Controller):
    async def restore(self, **_kwargs) -> bool:
        self.calls.append("restore")
        return False


class _Store:
    def __init__(self) -> None:
        self.data: dict[str, Any] = {}

    async def async_load(self):
        return dict(self.data)

    async def async_save(self, data) -> None:
        self.data = dict(data)


def _build_goodwe_curtailment_handler(
    controller: Any,
    *,
    feedin_price: float,
    store=None,
    initial_state: str = "normal",
    coordinator_data: dict[str, Any] | None = None,
    last_update_success: bool = False,
    last_reapply: float | None = None,
    last_effect_retry: float | None = None,
    monotonic_values: tuple[float, ...] | None = None,
):
    """Build the real handler against a fake coordinator."""
    entry_data: dict[str, Any] = {"goodwe_curtailment_state": initial_state}
    if store is not None:
        entry_data["store"] = store
    if last_reapply is not None:
        entry_data["_last_goodwe_curtailment_reapply"] = last_reapply
    if last_effect_retry is not None:
        entry_data["_last_goodwe_curtailment_effect_retry"] = last_effect_retry
    logger = _Logger()
    entry_data["goodwe_coordinator"] = SimpleNamespace(
        _controller=controller,
        data=coordinator_data,
        last_update_success=last_update_success,
        update_interval=timedelta(seconds=30),
    )
    hass = SimpleNamespace(data={"power_sync": {"entry": entry_data}})
    dispatches: list[tuple[Any, ...]] = []
    namespace: dict[str, Any] = {
        "DOMAIN": "power_sync",
        "hass": hass,
        "entry": SimpleNamespace(entry_id="entry"),
        "_LOGGER": logger,
        "with_hysteresis": _load_with_hysteresis(),
        "export_earnings_are_uneconomic": lambda value, active, _entry: (
            _load_with_hysteresis()(
                value,
                active,
                enter_threshold=1.0,
                exit_threshold=1.2,
            )
        ),
        "_goodwe_force_export_active": lambda _entry_data: False,
        "async_dispatcher_send": lambda *args, **_kwargs: dispatches.append(args),
        "get_current_prices_for_curtailment": lambda *_a, **_k: (None, None, None),
        "amber_coordinator": None,
        "localvolts_coordinator": None,
        "aemo_sensor_coordinator": None,
        "flow_power_kwatch_coordinator": None,
        "octopus_coordinator": None,
        "datetime": datetime,
        "timezone": timezone,
        "timedelta": timedelta,
        "math": math,
        "asyncio": asyncio,
    }
    handler_source = _nested_function_source("_handle_goodwe_curtailment_locked")
    if monotonic_values is not None:
        ticks = iter(monotonic_values)
        namespace["_test_time"] = SimpleNamespace(monotonic=lambda: next(ticks))
        handler_source = handler_source.replace(
            "import time as _time_mod", "_time_mod = _test_time"
        )
    exec(handler_source, namespace)
    exec(_nested_function_source("handle_goodwe_curtailment"), namespace)
    return namespace["handle_goodwe_curtailment"], entry_data, logger, dispatches


def _run_goodwe_curtailment(controller: Any, *, feedin_price: float, repeat=1, **kwargs):
    handler, entry_data, logger, dispatches = _build_goodwe_curtailment_handler(
        controller, feedin_price=feedin_price, **kwargs
    )
    for _ in range(repeat):
        asyncio.run(handler(feedin_price=feedin_price, import_price=17.22))
    return entry_data, logger, dispatches


class _BlockingController(_Controller):
    """Keep the first write in flight while a second callback arrives."""

    def __init__(self):
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.inflight = 0
        self.max_inflight = 0

    async def _write(self, action):
        self.calls.append(action)
        self.inflight += 1
        self.max_inflight = max(self.max_inflight, self.inflight)
        try:
            if len(self.calls) == 1:
                self.started.set()
                await self.release.wait()
            return True
        finally:
            self.inflight -= 1

    async def curtail(self):
        return await self._write("curtail")

    async def restore(self, **_kwargs):
        return await self._write("restore")


def _run_overlapping_callbacks(initial_state, first_price, second_price):
    async def run():
        controller = _BlockingController()
        handler, state, _logger, _dispatches = _build_goodwe_curtailment_handler(
            controller, feedin_price=first_price, initial_state=initial_state
        )
        first = asyncio.create_task(handler(first_price, 17.22))
        await controller.started.wait()
        second = asyncio.create_task(handler(second_price, 17.22))
        await asyncio.sleep(0)
        controller.release.set()
        await asyncio.gather(first, second)
        return controller, state

    return asyncio.run(run())


def test_overlapping_initial_callbacks_send_one_curtail_command():
    controller, state = _run_overlapping_callbacks("normal", 6.0, 6.0)
    assert controller.calls == ["curtail"]
    assert controller.max_inflight == 1
    assert state["goodwe_curtailment_state"] == "curtailed"


def test_overlapping_restore_callbacks_send_one_restore_command():
    controller, state = _run_overlapping_callbacks("curtailed", -6.0, -6.0)
    assert controller.calls == ["restore"]
    assert controller.max_inflight == 1
    assert state["goodwe_curtailment_state"] == "normal"


def test_new_uneconomic_price_waits_for_restore_then_curtails():
    controller, state = _run_overlapping_callbacks("curtailed", -6.0, 6.0)
    assert controller.calls == ["restore", "curtail"]
    assert controller.max_inflight == 1
    assert state["goodwe_curtailment_state"] == "curtailed"


def test_new_economic_price_restores_after_inflight_curtail():
    controller, state = _run_overlapping_callbacks("normal", 6.0, -6.0)
    assert controller.calls == ["curtail", "restore"]
    assert controller.max_inflight == 1
    assert state["goodwe_curtailment_state"] == "normal"


def test_cancelled_write_remains_pending_and_keeps_retry_backoff():
    async def run(initial_state, price, timestamp_key, action):
        controller = _BlockingController()
        handler, state, _logger, dispatches = _build_goodwe_curtailment_handler(
            controller, feedin_price=price, initial_state=initial_state
        )
        task = asyncio.create_task(handler(price, 17.22))
        await controller.started.wait()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        assert state["goodwe_curtailment_state"] == "pending"
        assert state[timestamp_key] > 0
        assert not state["_goodwe_curtailment_lock"].locked()
        assert [call[1] for call in dispatches] == [
            "power_sync_curtailment_updated_entry"
        ]
        await handler(price, 17.22)
        assert controller.calls == [action]

    asyncio.run(run("normal", 6.0, "_last_goodwe_curtailment_reapply", "curtail"))
    asyncio.run(
        run("curtailed", -6.0, "_last_goodwe_curtailment_restore_attempt", "restore")
    )


def test_entity_only_profile_records_unsupported_and_warns():
    """No control surface must be visible, not a silent DEBUG return."""
    # feedin_price 6.0 => export_earnings -6.0 c/kWh: the user pays to export.
    entry_data, logger, dispatches = _run_goodwe_curtailment(None, feedin_price=6.0)

    assert entry_data["goodwe_curtailment_state"] == "unsupported"
    assert logger.levels("no export-limit surface") == ["warning"]
    assert [call[1] for call in dispatches] == ["power_sync_curtailment_updated_entry"]


def test_repeated_polls_do_not_repeat_the_unsupported_warning():
    entry_data, logger, _dispatches = _run_goodwe_curtailment(None, feedin_price=6.0)
    assert logger.levels("no export-limit surface") == ["warning"]

    # Second poll on an entry already marked unsupported stays quiet.
    entry_data["goodwe_coordinator"] = SimpleNamespace(_controller=None)
    second = _Logger()
    namespace: dict[str, Any] = {
        "DOMAIN": "power_sync",
        "hass": SimpleNamespace(data={"power_sync": {"entry": entry_data}}),
        "entry": SimpleNamespace(entry_id="entry"),
        "_LOGGER": second,
        "with_hysteresis": _load_with_hysteresis(),
        "export_earnings_are_uneconomic": lambda value, active, _entry: (
            _load_with_hysteresis()(
                value,
                active,
                enter_threshold=1.0,
                exit_threshold=1.2,
            )
        ),
        "_goodwe_force_export_active": lambda _entry_data: False,
        "async_dispatcher_send": lambda *_args, **_kwargs: None,
        "asyncio": asyncio,
    }
    exec(_nested_function_source("_handle_goodwe_curtailment_locked"), namespace)
    exec(_nested_function_source("handle_goodwe_curtailment"), namespace)
    asyncio.run(
        namespace["handle_goodwe_curtailment"](feedin_price=6.0, import_price=17.22)
    )

    assert second.levels("no export-limit surface") == []
    assert entry_data["goodwe_curtailment_state"] == "unsupported"


def test_direct_control_profile_still_curtails():
    """The working direct-UDP path must be untouched."""
    controller = _Controller()
    entry_data, _logger, dispatches = _run_goodwe_curtailment(controller, feedin_price=6.0)

    assert controller.calls == ["curtail"]
    assert entry_data["goodwe_curtailment_state"] == "curtailed"
    assert [call[1] for call in dispatches] == ["power_sync_curtailment_updated_entry"]


def test_direct_profile_retries_fresh_material_export_before_periodic_interval():
    """#29: an acknowledged GoodWe register write is not a physical effect."""
    controller = _Controller()
    entry_data, logger, _dispatches = _run_goodwe_curtailment(
        controller,
        feedin_price=6.0,
        initial_state="curtailed",
        coordinator_data={
            "grid_power": -2.552,
            "last_update": datetime.now(timezone.utc),
        },
        last_update_success=True,
        repeat=2,
    )

    # The first pass retries because 2.552 kW export is fresh and material.
    # The second is coalesced by the 60-second effect-retry timer, not the
    # 15-minute periodic refresh timer.
    assert controller.calls == ["curtail"]
    assert entry_data["goodwe_curtailment_state"] == "curtailed"
    assert entry_data["_last_goodwe_curtailment_reapply"] > 0
    assert entry_data["_last_goodwe_curtailment_effect_retry"] > 0
    assert logger.levels("fresh direct telemetry still reports material export") == [
        "info"
    ]


def test_direct_profile_uses_only_one_early_effect_retry_per_episode():
    """#29: persistent physical export must not produce a five-minute command loop."""
    controller = _Controller()
    entry_data, logger, _dispatches = _run_goodwe_curtailment(
        controller,
        feedin_price=6.0,
        initial_state="curtailed",
        coordinator_data={
            "grid_power": -4.95,
            "last_update": datetime.now(timezone.utc),
        },
        last_update_success=True,
        last_reapply=1000.0,
        last_effect_retry=0.0,
        repeat=2,
        monotonic_values=(1000.0, 1061.0),
    )

    assert controller.calls == ["curtail"]
    assert entry_data["_goodwe_curtailment_effect_retry_used"] is True
    assert logger.levels("fresh direct telemetry still reports material export") == [
        "info"
    ]


def test_direct_profile_waits_for_periodic_retry_without_fresh_material_export():
    controller = _Controller()
    last_apply = time.monotonic()
    _entry_data, _logger, _dispatches = _run_goodwe_curtailment(
        controller,
        feedin_price=6.0,
        initial_state="curtailed",
        coordinator_data={
            "grid_power": -0.25,
            "last_update": datetime.now(timezone.utc),
        },
        last_update_success=True,
        last_reapply=last_apply,
        last_effect_retry=last_apply,
    )

    assert controller.calls == []


def test_direct_profile_waits_for_periodic_retry_with_stale_export_telemetry():
    controller = _Controller()
    last_apply = time.monotonic()
    _entry_data, _logger, _dispatches = _run_goodwe_curtailment(
        controller,
        feedin_price=6.0,
        initial_state="curtailed",
        coordinator_data={
            "grid_power": -2.552,
            "last_update": datetime.now(timezone.utc) - timedelta(minutes=3),
        },
        last_update_success=True,
        last_reapply=last_apply,
        last_effect_retry=last_apply,
    )

    assert controller.calls == []


def test_unverified_direct_command_stays_pending_and_persists_restore_baseline():
    controller = _UnverifiedController()
    store = _Store()

    entry_data, _logger, dispatches = _run_goodwe_curtailment(
        controller, feedin_price=6.0, store=store
    )

    assert controller.calls == ["curtail"]
    assert entry_data["goodwe_curtailment_state"] == "pending"
    assert store.data["goodwe_curtailment_restore_state"] == {
        "grid_export": 0,
        "grid_export_limit": 5000,
    }
    assert [call[1] for call in dispatches] == ["power_sync_curtailment_updated_entry"]


def test_failed_curtailment_is_throttled_while_pending():
    """#29: a failed direct write must not become a callback command storm."""
    controller = _UnverifiedController()

    entry_data, _logger, _dispatches = _run_goodwe_curtailment(
        controller,
        feedin_price=6.0,
        repeat=3,
        monotonic_values=(1000.0, 1001.0, 1002.0),
    )

    assert controller.calls == ["curtail"]
    assert entry_data["goodwe_curtailment_state"] == "pending"
    assert entry_data["_last_goodwe_curtailment_reapply"] == 1000.0


def test_failed_restore_stays_pending_and_refreshes_the_card():
    controller = _RestoreFailureController()

    entry_data, _logger, dispatches = _run_goodwe_curtailment(
        controller,
        # -2c/kWh feed-in means export earnings are +2c/kWh: restore.
        feedin_price=-2.0,
        initial_state="curtailed",
    )

    assert controller.calls == ["restore"]
    assert entry_data["goodwe_curtailment_state"] == "pending"
    assert [call[1] for call in dispatches] == ["power_sync_curtailment_updated_entry"]


def test_failed_restore_is_throttled_while_pending():
    """A failed release must likewise wait for the normal retry cadence."""
    controller = _RestoreFailureController()

    entry_data, _logger, _dispatches = _run_goodwe_curtailment(
        controller,
        feedin_price=-2.0,
        initial_state="curtailed",
        repeat=3,
    )

    assert controller.calls == ["restore"]
    assert entry_data["goodwe_curtailment_state"] == "pending"
    assert "_last_goodwe_curtailment_restore_attempt" in entry_data


def test_entity_telemetry_profiles_build_no_control_surface():
    """Pin the root cause: the coordinator leaves _controller unset."""
    source = COORDINATOR_PATH.read_text()
    tree = ast.parse(source)
    coordinator = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "GoodWeEnergyCoordinator"
    )
    init = next(
        child
        for child in coordinator.body
        if isinstance(child, ast.FunctionDef) and child.name == "__init__"
    )
    segment = ast.get_source_segment(source, init)
    assert segment is not None
    assert "self._controller = None" in segment
    assert "GoodWeEntityTelemetryController" in segment
    # No curtail()/restore() is grafted onto the telemetry controller, so the
    # handler genuinely has nothing to command on this profile.
    telemetry = next(
        node
        for node in ast.parse(
            (
                ROOT
                / "custom_components"
                / "power_sync"
                / "inverters"
                / "goodwe_entity.py"
            ).read_text()
        ).body
        if isinstance(node, ast.ClassDef)
        and node.name == "GoodWeEntityTelemetryController"
    )
    methods = {
        child.name
        for child in telemetry.body
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert "curtail" not in methods
