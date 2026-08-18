"""Regression tests for OB-23: EV load profile must bucket by LOCAL hour/weekday.

`LoadProfileEstimator.update_from_history` builds a 24-hour weekday/weekend
load profile from Home Assistant history. `State.last_updated` is UTC-aware,
so bucketing directly off it (pre-fix) rotates the learned curve by the
local UTC offset on any non-UTC install. Consumers (`estimate_load_at_hour`
and friends) query the profile using the LOCAL hour, so the buckets must be
built in local time too.
"""

from __future__ import annotations

import asyncio
import ast
import importlib
import math
import sys
import textwrap
import types
from typing import Any
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parent.parent / "custom_components" / "power_sync"

# --- Home Assistant stub environment -----------------------------------
# Mirrors the proven-working stub set from tests/test_ev_price_level_ownership.py
# (confirmed to import power_sync.automations.ev_charging_planner standalone),
# plus a recorder/recorder.history stub (mirroring tests/test_load_estimator.py)
# since update_from_history() does a function-local
# `from homeassistant.components.recorder import get_instance` import.

sys.modules.setdefault("aiohttp", types.ModuleType("aiohttp"))

_ha_root = sys.modules.setdefault("homeassistant", types.ModuleType("homeassistant"))
_ha_config_entries = sys.modules.setdefault(
    "homeassistant.config_entries", types.ModuleType("homeassistant.config_entries")
)
_ha_core = sys.modules.setdefault("homeassistant.core", types.ModuleType("homeassistant.core"))
_ha_exceptions = sys.modules.setdefault(
    "homeassistant.exceptions", types.ModuleType("homeassistant.exceptions")
)
_ha_helpers = sys.modules.setdefault("homeassistant.helpers", types.ModuleType("homeassistant.helpers"))
_ha_storage = sys.modules.setdefault(
    "homeassistant.helpers.storage", types.ModuleType("homeassistant.helpers.storage")
)
_ha_update = sys.modules.setdefault(
    "homeassistant.helpers.update_coordinator",
    types.ModuleType("homeassistant.helpers.update_coordinator"),
)
_ha_er = sys.modules.setdefault(
    "homeassistant.helpers.entity_registry",
    types.ModuleType("homeassistant.helpers.entity_registry"),
)
_ha_dr = sys.modules.setdefault(
    "homeassistant.helpers.device_registry",
    types.ModuleType("homeassistant.helpers.device_registry"),
)
_ha_event = sys.modules.setdefault(
    "homeassistant.helpers.event", types.ModuleType("homeassistant.helpers.event")
)
_ha_aiohttp_client = sys.modules.setdefault(
    "homeassistant.helpers.aiohttp_client",
    types.ModuleType("homeassistant.helpers.aiohttp_client"),
)
_ha_util = sys.modules.setdefault("homeassistant.util", types.ModuleType("homeassistant.util"))
_ha_dt = sys.modules.setdefault("homeassistant.util.dt", types.ModuleType("homeassistant.util.dt"))
_ha_components = sys.modules.setdefault(
    "homeassistant.components", types.ModuleType("homeassistant.components")
)
_ha_recorder = sys.modules.setdefault(
    "homeassistant.components.recorder", types.ModuleType("homeassistant.components.recorder")
)
_ha_recorder_history = sys.modules.setdefault(
    "homeassistant.components.recorder.history",
    types.ModuleType("homeassistant.components.recorder.history"),
)

_ha_core.HomeAssistant = type("HomeAssistant", (), {})
_ha_config_entries.ConfigEntry = type("ConfigEntry", (), {})
_ha_exceptions.ConfigEntryNotReady = type("ConfigEntryNotReady", (Exception,), {})
_ha_er.async_get = lambda hass: getattr(hass, "entity_registry", None)
_ha_dr.async_get = lambda hass: getattr(hass, "device_registry", None)
_ha_storage.Store = type("Store", (), {"__init__": lambda self, *args, **kwargs: None})
_ha_update.DataUpdateCoordinator = type(
    "DataUpdateCoordinator",
    (),
    {
        "__class_getitem__": classmethod(lambda cls, item: cls),
        "__init__": lambda self, *args, **kwargs: None,
    },
)
_ha_event.async_track_time_interval = lambda *args, **kwargs: (lambda: None)
_ha_event.async_track_time_change = lambda *args, **kwargs: (lambda: None)
_ha_event.async_track_point_in_time = lambda *args, **kwargs: (lambda: None)

# The fixture under test converts UTC -> a fixed local offset of +10h
# (e.g. Australia/Sydney, AEST) to make the bug's rotation observable.
LOCAL_OFFSET = timedelta(hours=10)
_ha_dt.now = lambda *args, **kwargs: datetime.now(timezone.utc) + LOCAL_OFFSET
_ha_dt.utcnow = lambda *args, **kwargs: datetime.now(timezone.utc)
_ha_dt.as_local = lambda value: value + LOCAL_OFFSET

_ha_recorder.get_instance = lambda hass: getattr(hass, "_fake_recorder", None)
_ha_recorder_history.get_significant_states = object()  # never actually called by the fake recorder

_ha_helpers.entity_registry = _ha_er
_ha_helpers.device_registry = _ha_dr
_ha_helpers.storage = _ha_storage
_ha_helpers.update_coordinator = _ha_update
_ha_helpers.event = _ha_event
_ha_helpers.aiohttp_client = _ha_aiohttp_client
_ha_root.helpers = _ha_helpers
_ha_util.dt = _ha_dt
_ha_root.util = _ha_util
_ha_root.components = _ha_components
_ha_components.recorder = _ha_recorder
_ha_recorder.history = _ha_recorder_history

_ps = types.ModuleType("power_sync")
_ps.__path__ = [str(ROOT)]
sys.modules["power_sync"] = _ps

_optimization = types.ModuleType("power_sync.optimization")
_optimization.__path__ = [str(ROOT / "optimization")]
sys.modules["power_sync.optimization"] = _optimization

_automations = types.ModuleType("power_sync.automations")
_automations.__path__ = [str(ROOT / "automations")]
sys.modules["power_sync.automations"] = _automations

if not hasattr(sys.modules.get("power_sync.const"), "TESLA_INTEGRATIONS"):
    sys.modules.pop("power_sync.const", None)

ev_planner = importlib.import_module("power_sync.automations.ev_charging_planner")


# --- Fakes ---------------------------------------------------------------


class _FakeState:
    def __init__(
        self,
        entity_id: str,
        state: str,
        last_updated: datetime,
        attributes: dict[str, Any] | None = None,
    ) -> None:
        self.entity_id = entity_id
        self.state = state
        self.last_updated = last_updated
        self.attributes = attributes or {}


class _FakeStates:
    """Minimal hass.states stub supporting async_entity_ids()."""

    def __init__(self, entity_ids: list[str]) -> None:
        self._entity_ids = entity_ids

    def async_entity_ids(self, domain: str | None = None):
        if domain is None:
            return list(self._entity_ids)
        return [e for e in self._entity_ids if e.startswith(f"{domain}.")]


class _FakeRecorder:
    """Returns canned history without touching get_significant_states."""

    def __init__(self, history: dict) -> None:
        self._history = history

    async def async_add_executor_job(self, func, hass, start_time, end_time, entity_ids):
        return self._history


class _FakeHass:
    def __init__(self, load_entity: str, history_states: list[_FakeState]) -> None:
        self.states = _FakeStates([load_entity])
        self._fake_recorder = _FakeRecorder({load_entity: history_states})


LOAD_ENTITY = "sensor.home_load_power"


def _run(coro):
    return asyncio.run(coro)


# --- Tests -----------------------------------------------------------------


def test_load_profile_bucketed_by_local_hour_not_utc():
    """08:00 UTC == 18:00 local (+10h). The reading must land in local bucket 18."""
    utc_instant = datetime(2026, 7, 8, 8, 0, tzinfo=timezone.utc)  # Wed both UTC and local
    state = _FakeState(LOAD_ENTITY, "3500", utc_instant)  # 3500 W -> 3.5 kW
    hass = _FakeHass(LOAD_ENTITY, [state])

    estimator = ev_planner.LoadProfileEstimator(hass)
    _run(estimator.update_from_history(days=14))

    weekday_profile = estimator._load_history["weekday"]

    # Fixed (local-hour) behaviour: the learned reading lands at local hour 18.
    assert weekday_profile[18] == pytest.approx(3.5)
    # Bucket 8 (the UTC hour) must NOT have been touched by this reading -
    # it should still hold the untouched default for that hour.
    assert weekday_profile[8] == pytest.approx(
        ev_planner.LoadProfileEstimator.DEFAULT_WEEKDAY_PROFILE[8]
    )
    assert weekday_profile[8] != pytest.approx(3.5)


def test_load_profile_weekend_flag_uses_local_weekday():
    """Sun 23:00 UTC == Mon 09:00 local (+10h): must bucket as WEEKDAY hour 9."""
    utc_instant = datetime(2026, 7, 12, 23, 0, tzinfo=timezone.utc)  # Sunday in UTC
    state = _FakeState(LOAD_ENTITY, "2500", utc_instant)  # 2500 W -> 2.5 kW
    hass = _FakeHass(LOAD_ENTITY, [state])

    estimator = ev_planner.LoadProfileEstimator(hass)
    _run(estimator.update_from_history(days=14))

    weekday_profile = estimator._load_history["weekday"]
    weekend_profile = estimator._load_history["weekend"]

    # Fixed behaviour: local time is Monday 09:00 -> weekday bucket 9.
    assert weekday_profile[9] == pytest.approx(2.5)
    # The weekend bucket for hour 23 must remain the untouched default -
    # the UTC-Sunday reading must not have landed there.
    assert weekend_profile[23] == pytest.approx(
        ev_planner.LoadProfileEstimator.DEFAULT_WEEKEND_PROFILE[23]
    )
    assert weekend_profile[23] != pytest.approx(2.5)


def test_load_profile_preserves_kw_sensor_units():
    """Recorder states already expressed in kW must not be divided twice."""
    utc_instant = datetime(2026, 8, 17, 22, 0, tzinfo=timezone.utc)
    state = _FakeState(
        LOAD_ENTITY,
        "1.514",
        utc_instant,
        {"unit_of_measurement": "kW"},
    )
    hass = _FakeHass(LOAD_ENTITY, [state])

    estimator = ev_planner.LoadProfileEstimator(hass)
    _run(estimator.update_from_history(days=14))

    # 22:00 UTC is 08:00 local on the following weekday. Before the fix this
    # became 0.001514 kW, fabricating early-morning solar surplus for EV plans.
    assert estimator._load_history["weekday"][8] == pytest.approx(1.514)


# --- HD-16: dual EV-load overlays must not stack ---------------------------
#
# `OptimizationCoordinator._run_optimization` overlays EV charging demand
# onto the LP load forecast from two independent sources: the external
# `planned_ev_load_entity` sensor (`_get_planned_ev_load_forecast`) and the
# internal AutoScheduleExecutor plan (`_get_ev_planned_load`). Pre-fix, both
# overlays are added unconditionally with no mutual exclusion, so a user who
# configures both for the same vehicle double-counts EV demand in the LP
# load forecast. The overlay block is extracted straight from
# coordinator.py's source (AST/text-slice, per the AGENTS.md source-extraction
# pattern) and exec'd against a fake coordinator, so the test runs the real
# production logic rather than a re-implementation.

COORDINATOR_PATH = (
    Path(__file__).resolve().parent.parent
    / "custom_components"
    / "power_sync"
    / "optimization"
    / "coordinator.py"
)


def _ev_overlay_block_source() -> str:
    source = COORDINATOR_PATH.read_text()
    marker = "# Overlay EV charging plan onto load forecast"
    marker_idx = source.index(marker)
    line_start = source.rindex("\n", 0, marker_idx) + 1
    end_marker = "import_prices = prices[0] if prices else []"
    end_idx = source.index(end_marker, marker_idx)
    return textwrap.dedent(source[line_start:end_idx])


class _FakeOverlayCoordinator:
    """Stands in for `self` inside the extracted overlay block."""

    def __init__(
        self,
        *,
        external_forecast: list[float] | None,
        internal_forecast: list[float] | None,
        ev_integration_enabled: bool,
    ) -> None:
        self._external_forecast = external_forecast
        self._internal_forecast = internal_forecast
        self._ev_integration_enabled = ev_integration_enabled
        self._config = SimpleNamespace(interval_minutes=5)
        self.hass = SimpleNamespace(config=SimpleNamespace(time_zone="Australia/Brisbane"))
        self._last_planned_ev_load_forecast_w = None
        self._last_effective_ev_load_forecast_w = None
        self._last_smart_schedule_ev_load_w = None
        self._last_price_level_expected_ev_load_w = None
        self._last_price_level_projection = None
        self._planned_ev_load_entity_id = "sensor.external" if external_forecast else None
        self._warned_dual_ev_overlay = False

    def _get_planned_ev_load_forecast(self, n_intervals: int) -> list[float] | None:
        return self._external_forecast

    async def _build_price_level_projection(self, n_intervals: int):
        return SimpleNamespace(
            expected_w=tuple(0.0 for _ in range(n_intervals)),
            expected_by_loadpoint={},
            conditional_cap_w=tuple(0.0 for _ in range(n_intervals)),
            windows=(),
            warnings=(),
            with_suppressed_expected=lambda **_kwargs: self._empty_projection(n_intervals),
        )

    def _empty_projection(self, n_intervals: int):
        return SimpleNamespace(
            expected_w=tuple(0.0 for _ in range(n_intervals)),
            expected_by_loadpoint={},
            conditional_cap_w=tuple(0.0 for _ in range(n_intervals)),
            windows=(),
            warnings=(),
        )

    def _get_ev_planned_load_components(self, n_intervals: int):
        return {"ev": self._internal_forecast} if self._internal_forecast else {}

    def _merge_internal_ev_load_components(
        self, *, n_intervals, smart_components, price_components
    ):
        smart = list(next(iter(smart_components.values()), [0.0] * n_intervals))
        price = list(next(iter(price_components.values()), [0.0] * n_intervals))
        effective = [max(smart[i], price[i]) for i in range(n_intervals)]
        marginal = [max(0.0, effective[i] - smart[i]) for i in range(n_intervals)]
        return effective, smart, marginal

    def _price_level_projection_payload(self, **_kwargs):
        return {"schema_version": 1, "components": {}, "windows": []}


def _run_overlay(coordinator: _FakeOverlayCoordinator, load: list[float]) -> list[float]:
    source = _ev_overlay_block_source()
    wrapped = "async def _run(load):\n" + textwrap.indent(source, "    ") + "\n    return load\n"
    namespace = {
        "self": coordinator,
        "_LOGGER": SimpleNamespace(warning=lambda *a, **k: None),
    }
    exec(compile(wrapped, "<ev_overlay_block>", "exec"), namespace)
    return asyncio.run(namespace["_run"](list(load)))


def test_ev_overlay_external_entity_only_applies_external_forecast():
    coordinator = _FakeOverlayCoordinator(
        external_forecast=[1000.0, 1000.0, 1000.0, 1000.0],
        internal_forecast=None,
        ev_integration_enabled=False,
    )
    result = _run_overlay(coordinator, [2000.0, 2000.0, 2000.0, 2000.0])
    assert result == [3000.0, 3000.0, 3000.0, 3000.0]


def test_ev_overlay_internal_plan_only_applies_internal_forecast():
    coordinator = _FakeOverlayCoordinator(
        external_forecast=None,
        internal_forecast=[500.0, 500.0, 500.0, 500.0],
        ev_integration_enabled=True,
    )
    result = _run_overlay(coordinator, [2000.0, 2000.0, 2000.0, 2000.0])
    assert result == [2500.0, 2500.0, 2500.0, 2500.0]


def test_ev_overlay_both_configured_does_not_double_count_ev_demand():
    """HD-16 regression: external planned_ev_load_entity AND an internal
    AutoScheduleExecutor plan configured for the same vehicle must not both
    land in the load forecast. External wins (it is the user's explicit,
    most current configuration) - the internal overlay must be skipped."""
    coordinator = _FakeOverlayCoordinator(
        external_forecast=[1000.0, 1000.0, 1000.0, 1000.0],
        internal_forecast=[500.0, 500.0, 500.0, 500.0],
        ev_integration_enabled=True,
    )
    result = _run_overlay(coordinator, [2000.0, 2000.0, 2000.0, 2000.0])

    # Only the external overlay should be applied (2000 + 1000 = 3000).
    # Pre-fix this stacks both overlays: 2000 + 1000 + 500 = 3500.
    assert result == [3000.0, 3000.0, 3000.0, 3000.0]


def _extract_coordinator_method(name: str):
    tree = ast.parse(COORDINATOR_PATH.read_text())
    method = next(
        node
        for class_node in tree.body
        if isinstance(class_node, ast.ClassDef)
        and class_node.name == "OptimizationCoordinator"
        for node in class_node.body
        if isinstance(node, ast.FunctionDef) and node.name == name
    )
    namespace = {
        "__name__": "power_sync.optimization._ev_plan_test",
        "__package__": "power_sync.optimization",
        "datetime": datetime,
        "timedelta": timedelta,
        "dt_util": _ha_dt,
        "math": math,
        "Any": Any,
        "_LOGGER": SimpleNamespace(debug=lambda *args, **kwargs: None),
    }
    ast.fix_missing_locations(method)
    exec(compile(ast.Module(body=[method], type_ignores=[]), str(COORDINATOR_PATH), "exec"), namespace)
    return namespace[name]


def test_internal_ev_forecast_prorates_trimmed_time_critical_window():
    """Optimizer demand must start at the trimmed boundary and conserve energy."""
    brisbane_tz = timezone(timedelta(hours=10))
    now = datetime(2026, 7, 24, 2, 0, tzinfo=brisbane_tz)
    old_now = _ha_dt.now
    old_executor = ev_planner.get_auto_schedule_executor()
    _ha_dt.now = lambda: now

    plan = ev_planner.ChargingPlan(
        vehicle_id="ev",
        current_soc=57,
        target_soc=80,
        target_time="2026-07-24T05:00:00",
        energy_needed_kwh=19.1666666667,
        windows=[
            ev_planner.PlannedChargingWindow(
                start_time="2026-07-24T02:23:45",
                end_time="2026-07-24T03:00:00",
                source="grid_offpeak",
                estimated_power_kw=7.36,
                estimated_energy_kwh=4.4466666667,
                price_cents_kwh=50.0,
                reason="target_deadline",
            ),
            ev_planner.PlannedChargingWindow(
                start_time="2026-07-24T03:00:00",
                end_time="2026-07-24T04:00:00",
                source="grid_offpeak",
                estimated_power_kw=7.36,
                estimated_energy_kwh=7.36,
                price_cents_kwh=50.0,
                reason="target_deadline",
            ),
            ev_planner.PlannedChargingWindow(
                start_time="2026-07-24T04:00:00",
                end_time="2026-07-24T05:00:00",
                source="grid_offpeak",
                estimated_power_kw=7.36,
                estimated_energy_kwh=7.36,
                price_cents_kwh=50.0,
                reason="target_deadline",
            ),
        ],
    )
    settings = SimpleNamespace(
        enabled=True,
        max_charge_amps=32,
        voltage=230,
        phases=1,
    )
    executor = SimpleNamespace(
        _state={"ev": SimpleNamespace(current_plan=plan)},
        _settings={"ev": settings},
        _sync_charger_params_from_vehicle_configs=lambda *_args: None,
    )
    ev_planner.set_auto_schedule_executor(executor)

    try:
        coordinator = SimpleNamespace(
            _config=SimpleNamespace(interval_minutes=5),
            _price_timestamps=lambda count: [
                now + timedelta(minutes=5 * index) for index in range(count)
            ],
        )
        components = _extract_coordinator_method(
            "_get_ev_planned_load_components"
        )(
            coordinator,
            36,
        )
        ev_load = [
            sum(values[index] for values in components.values())
            for index in range(36)
        ] if components else None
    finally:
        _ha_dt.now = old_now
        ev_planner.set_auto_schedule_executor(old_executor)

    assert ev_load is not None
    assert ev_load[:4] == [0.0, 0.0, 0.0, 0.0]
    assert ev_load[4] == pytest.approx(1840.0)
    assert max(ev_load) == pytest.approx(7360.0)
    assert sum(ev_load) / 1000 * (5 / 60) == pytest.approx(
        plan.energy_needed_kwh
    )


def test_internal_ev_forecast_ignores_disabled_vehicle_beside_enabled_vehicle():
    """Only the enabled vehicle may contribute to a multi-EV optimiser overlay."""
    brisbane_tz = timezone(timedelta(hours=10))
    now = datetime(2026, 8, 11, 12, 0, tzinfo=brisbane_tz)
    old_now = _ha_dt.now
    old_executor = ev_planner.get_auto_schedule_executor()
    _ha_dt.now = lambda: now

    disabled_plan = ev_planner.ChargingPlan(
        vehicle_id="tessy",
        current_soc=50,
        target_soc=80,
        target_time="2026-08-11T14:00:00",
        energy_needed_kwh=4.0,
        windows=[
            ev_planner.PlannedChargingWindow(
                start_time="2026-08-11T12:00:00+10:00",
                end_time="2026-08-11T14:00:00+10:00",
                source="grid_peak",
                estimated_power_kw=2.0,
                estimated_energy_kwh=4.0,
                price_cents_kwh=31.0,
                reason="cached_disabled_plan",
            ),
        ],
    )
    enabled_plan = ev_planner.ChargingPlan(
        vehicle_id="w3rt1e",
        current_soc=64,
        target_soc=80,
        target_time="2026-08-11T14:00:00",
        energy_needed_kwh=4.0,
        windows=[
            ev_planner.PlannedChargingWindow(
                start_time="2026-08-11T12:00:00+10:00",
                end_time="2026-08-11T14:00:00+10:00",
                source="grid_peak",
                estimated_power_kw=2.0,
                estimated_energy_kwh=4.0,
                price_cents_kwh=31.0,
                reason="enabled_plan",
            ),
        ],
    )
    executor = SimpleNamespace(
        _state={
            "tessy": SimpleNamespace(current_plan=disabled_plan),
            "w3rt1e": SimpleNamespace(current_plan=enabled_plan),
        },
        _settings={
            "tessy": SimpleNamespace(
                enabled=False,
                max_charge_amps=10,
                voltage=230,
                phases=1,
            ),
            "w3rt1e": SimpleNamespace(
                enabled=True,
                max_charge_amps=10,
                voltage=230,
                phases=1,
            ),
        },
        _sync_charger_params_from_vehicle_configs=lambda *_args: None,
    )
    ev_planner.set_auto_schedule_executor(executor)

    try:
        coordinator = SimpleNamespace(
            _config=SimpleNamespace(interval_minutes=5),
            _price_timestamps=lambda count: [
                now + timedelta(minutes=5 * index) for index in range(count)
            ],
        )
        components = _extract_coordinator_method(
            "_get_ev_planned_load_components"
        )(
            coordinator,
            24,
        )
        ev_load = [
            sum(values[index] for values in components.values())
            for index in range(24)
        ] if components else None
    finally:
        _ha_dt.now = old_now
        ev_planner.set_auto_schedule_executor(old_executor)

    assert ev_load == pytest.approx([2000.0] * 24)


def test_internal_ev_forecast_suppresses_price_blocked_plan_but_keeps_deadline():
    """Cached non-executable windows must not become optimiser household load."""
    brisbane_tz = timezone(timedelta(hours=10))
    now = datetime(2026, 8, 18, 16, 0, tzinfo=brisbane_tz)
    old_now = _ha_dt.now
    old_executor = ev_planner.get_auto_schedule_executor()
    _ha_dt.now = lambda: now

    def plan(vehicle_id, priority):
        return ev_planner.ChargingPlan(
            vehicle_id=vehicle_id,
            current_soc=60,
            target_soc=80,
            target_time=None,
            energy_needed_kwh=2.3,
            windows=[
                ev_planner.PlannedChargingWindow(
                    start_time=now.isoformat(),
                    end_time=(now + timedelta(hours=1)).isoformat(),
                    source="grid_peak",
                    estimated_power_kw=2.3,
                    estimated_energy_kwh=2.3,
                    price_cents_kwh=31.0,
                    reason="cached_plan",
                )
            ],
            priority=priority,
            max_grid_price_cents=25.0,
        )

    settings = SimpleNamespace(
        enabled=True,
        max_charge_amps=10,
        voltage=230,
        phases=1,
    )
    executor = SimpleNamespace(
        _state={
            "w3rt1e": SimpleNamespace(
                current_plan=plan("w3rt1e", "cost_optimized")
            ),
            "tessy": SimpleNamespace(
                current_plan=plan("tessy", "time_critical")
            ),
        },
        _settings={"w3rt1e": settings, "tessy": settings},
        _sync_charger_params_from_vehicle_configs=lambda *_args: None,
    )
    ev_planner.set_auto_schedule_executor(executor)

    try:
        coordinator = SimpleNamespace(
            _config=SimpleNamespace(interval_minutes=5),
            _price_timestamps=lambda count: [
                now + timedelta(minutes=5 * index) for index in range(count)
            ],
        )
        components = _extract_coordinator_method(
            "_get_ev_planned_load_components"
        )(coordinator, 12)
    finally:
        _ha_dt.now = old_now
        ev_planner.set_auto_schedule_executor(old_executor)

    assert set(components) == {"tessy"}
    assert components["tessy"] == pytest.approx([2300.0] * 12)


def test_internal_ev_arbitration_uses_max_for_one_loadpoint_and_sum_for_distinct():
    """Smart and Price-Level candidates share a charger without stacking."""
    merge = _extract_coordinator_method("_merge_internal_ev_load_components")
    coordinator = SimpleNamespace(
        _canonical_ev_loadpoint_ids=lambda identifiers: {
            "fleet_vin": "physical_a",
            "ble_alias": "physical_a",
            "other": "physical_b",
        },
    )

    effective, smart, price = merge(
        coordinator,
        n_intervals=2,
        smart_components={"fleet_vin": [7000, 0]},
        price_components={
            "ble_alias": (3600, 7200),
            "other": (2000, 2000),
        },
    )

    assert effective == pytest.approx([9000, 9200])
    assert smart == pytest.approx([7000, 0])
    assert price == pytest.approx([2000, 9200])


def test_price_level_projection_payload_is_versioned_and_array_aligned():
    payload_method = _extract_coordinator_method("_price_level_projection_payload")
    now = datetime(2026, 8, 17, 8, 0, tzinfo=timezone.utc)
    old_now = _ha_dt.now
    _ha_dt.now = lambda: now
    try:
        projection = SimpleNamespace(
            conditional_cap_w=(0.0, 7400.0),
            windows=(SimpleNamespace(to_dict=lambda: {"id": "window"}),),
            warnings=("diagnostic",),
        )
        coordinator = SimpleNamespace(
            hass=SimpleNamespace(config=SimpleNamespace(time_zone="Australia/Brisbane")),
            _config=SimpleNamespace(interval_minutes=5),
        )
        payload = payload_method(
            coordinator,
            projection=projection,
            effective_source="internal",
            external_w=[0.0, 0.0],
            smart_w=[7000.0, 0.0],
            price_expected_w=[0.0, 7200.0],
        )
    finally:
        _ha_dt.now = old_now

    assert payload["schema_version"] == 1
    assert payload["timezone"] == "Australia/Brisbane"
    assert payload["interval_minutes"] == 5
    assert payload["effective_source"] == "internal"
    assert payload["components"] == {
        "external_w": [0.0, 0.0],
        "smart_schedule_w": [7000.0, 0.0],
        "price_level_expected_w": [0.0, 7200.0],
        "price_level_conditional_cap_w": [0.0, 7400.0],
    }
    assert payload["windows"] == [{"id": "window"}]
    assert payload["warnings"] == ["diagnostic"]
