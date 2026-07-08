"""Regression tests for load forecasting."""

from __future__ import annotations

import functools
import importlib.util
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parent.parent
COMPONENT_ROOT = ROOT / "custom_components" / "power_sync"
MODULE_PATH = COMPONENT_ROOT / "optimization" / "load_estimator.py"


def _load_estimator_module(monkeypatch):
    ha_root = types.ModuleType("homeassistant")
    ha_core = types.ModuleType("homeassistant.core")
    ha_util = types.ModuleType("homeassistant.util")
    ha_dt = types.ModuleType("homeassistant.util.dt")
    ha_core.HomeAssistant = object
    ha_dt.now = lambda: datetime(2026, 5, 9, tzinfo=timezone.utc)
    ha_dt.utcnow = lambda: datetime(2026, 5, 9, tzinfo=timezone.utc)
    ha_dt.as_local = lambda value: value
    ha_util.dt = ha_dt

    ps_module = types.ModuleType("power_sync")
    ps_module.__path__ = [str(COMPONENT_ROOT)]
    optimization_module = types.ModuleType("power_sync.optimization")
    optimization_module.__path__ = [str(COMPONENT_ROOT / "optimization")]
    const_module = types.ModuleType("power_sync.const")
    const_module.DEFAULT_SOLCAST_ESTIMATE_TYPE = "estimate"
    const_module.DEFAULT_SOLAR_FORECAST_PROVIDER = "solcast"
    const_module.SOLCAST_ESTIMATE = "estimate"
    const_module.SOLCAST_ESTIMATE10 = "estimate10"
    const_module.SOLCAST_ESTIMATE90 = "estimate90"
    const_module.SOLAR_FORECAST_PROVIDER_OPEN_METEO = "open_meteo"
    const_module.SOLAR_FORECAST_PROVIDER_SOLCAST = "solcast"
    const_module.SOLAR_FORECAST_PROVIDERS = {
        "solcast": "Solcast",
        "open_meteo": "Open-Meteo",
    }
    const_module.DOMAIN = "power_sync"

    monkeypatch.setitem(sys.modules, "homeassistant", ha_root)
    monkeypatch.setitem(sys.modules, "homeassistant.core", ha_core)
    monkeypatch.setitem(sys.modules, "homeassistant.util", ha_util)
    monkeypatch.setitem(sys.modules, "homeassistant.util.dt", ha_dt)
    monkeypatch.setitem(sys.modules, "power_sync", ps_module)
    monkeypatch.setitem(sys.modules, "power_sync.optimization", optimization_module)
    monkeypatch.setitem(sys.modules, "power_sync.const", const_module)
    monkeypatch.delitem(sys.modules, "power_sync.optimization.load_estimator", raising=False)

    spec = importlib.util.spec_from_file_location(
        "power_sync.optimization.load_estimator",
        MODULE_PATH,
    )
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


def test_normal_history_fetch_requests_30_days(monkeypatch):
    module = _load_estimator_module(monkeypatch)
    now = datetime(2026, 5, 9, tzinfo=timezone.utc)
    calls = {}

    _install_fake_recorder(
        monkeypatch,
        {
            "sensor.load": [
                SimpleNamespace(
                    state="1200",
                    last_changed=now - timedelta(days=1),
                )
            ],
        },
        calls,
    )
    hass = SimpleNamespace(
        states=SimpleNamespace(
            get=lambda entity_id: SimpleNamespace(
                attributes={"unit_of_measurement": "W"}
            )
        ),
        async_add_executor_job=_fake_executor,
    )
    estimator = module.LoadEstimator(hass, "sensor.load", interval_minutes=5)

    history = _run(estimator._get_load_history())

    assert calls["start_time"] == now - timedelta(days=30)
    assert calls["end_time"] == now
    assert calls["entity_ids"] == ["sensor.load"]
    assert history == [(now - timedelta(days=1), 1200.0)]


def test_history_exact_bucket_uses_multiple_weeks(monkeypatch):
    module = _load_estimator_module(monkeypatch)
    estimator = module.LoadEstimator(SimpleNamespace(), "sensor.load", interval_minutes=5)
    start = datetime(2026, 5, 11, tzinfo=timezone.utc)
    history = [
        (start - timedelta(days=7), 1000.0),
        (start - timedelta(days=14), 2000.0),
        (start - timedelta(days=21), 3000.0),
        (start - timedelta(days=28), 4000.0),
    ]

    forecast = estimator._forecast_from_history(history, start, 1)

    assert 1000.0 < forecast[0] < 4000.0


def test_history_recency_weights_recent_weeks_more(monkeypatch):
    module = _load_estimator_module(monkeypatch)
    estimator = module.LoadEstimator(SimpleNamespace(), "sensor.load", interval_minutes=5)
    start = datetime(2026, 5, 11, tzinfo=timezone.utc)
    history = [
        (start - timedelta(days=7), 1000.0),
        (start - timedelta(days=14), 3000.0),
    ]

    forecast = estimator._forecast_from_history(history, start, 1)

    assert 1000.0 < forecast[0] < 2000.0


def test_recent_load_regime_scales_forecast_up_for_winter_step_change(monkeypatch):
    module = _load_estimator_module(monkeypatch)
    estimator = module.LoadEstimator(SimpleNamespace(), "sensor.load", interval_minutes=5)
    start = datetime(2026, 6, 2, 12, tzinfo=timezone.utc)

    history = []
    for hour_offset in range(30 * 24, 72, -1):
        history.append((start - timedelta(hours=hour_offset), 500.0))
    for hour_offset in range(48, 0, -1):
        history.append((start - timedelta(hours=hour_offset), 1500.0))

    forecast = estimator._forecast_from_history(history, start, 12)

    assert min(forecast) >= 1199.0


def test_recent_load_regime_ignores_short_spike(monkeypatch):
    module = _load_estimator_module(monkeypatch)
    estimator = module.LoadEstimator(SimpleNamespace(), "sensor.load", interval_minutes=5)
    start = datetime(2026, 6, 2, 12, tzinfo=timezone.utc)

    history = []
    for hour_offset in range(30 * 24, 6, -1):
        history.append((start - timedelta(hours=hour_offset), 500.0))
    for hour_offset in range(6, 0, -1):
        history.append((start - timedelta(hours=hour_offset), 1500.0))

    forecast = estimator._forecast_from_history(history, start, 12)

    assert max(forecast) < 900.0


def test_recent_load_regime_ignores_short_ev_charging_spike(monkeypatch):
    module = _load_estimator_module(monkeypatch)
    estimator = module.LoadEstimator(SimpleNamespace(), "sensor.load", interval_minutes=5)
    start = datetime(2026, 6, 2, 12, tzinfo=timezone.utc)

    history = []
    for hour_offset in range(30 * 24, 48, -1):
        history.append((start - timedelta(hours=hour_offset), 500.0))
    for hour_offset in range(48, 0, -1):
        value = 13_000.0 if 24 >= hour_offset > 21 else 500.0
        history.append((start - timedelta(hours=hour_offset), value))

    forecast = estimator._forecast_from_history(history, start, 12)

    assert max(forecast) < 900.0


def test_active_away_mode_scales_forecast_to_current_low_load(monkeypatch):
    module = _load_estimator_module(monkeypatch)
    estimator = module.LoadEstimator(SimpleNamespace(), "sensor.load", interval_minutes=5)
    start = datetime(2026, 6, 2, 12, tzinfo=timezone.utc)
    estimator.away_enabled_at = start - timedelta(days=2)

    history = []
    for hour_offset in range(30 * 24, 48, -1):
        history.append((start - timedelta(hours=hour_offset), 2000.0))
    for hour_offset in range(48, 0, -1):
        history.append((start - timedelta(hours=hour_offset), 300.0))

    forecast = estimator._forecast_from_history(history, start, 12)

    assert max(forecast) < 700.0


def test_history_outlier_does_not_dominate_bucket(monkeypatch):
    module = _load_estimator_module(monkeypatch)
    estimator = module.LoadEstimator(SimpleNamespace(), "sensor.load", interval_minutes=5)
    start = datetime(2026, 5, 11, tzinfo=timezone.utc)
    history = [
        (start - timedelta(days=7), 1000.0),
        (start - timedelta(days=14), 1000.0),
        (start - timedelta(days=21), 1000.0),
        (start - timedelta(days=28), 10000.0),
    ]

    forecast = estimator._forecast_from_history(history, start, 1)

    assert forecast[0] == 1000.0


def test_lone_exact_bucket_blends_with_same_day_type(monkeypatch):
    module = _load_estimator_module(monkeypatch)
    estimator = module.LoadEstimator(SimpleNamespace(), "sensor.load", interval_minutes=5)
    start = datetime(2026, 5, 11, tzinfo=timezone.utc)
    monday = start - timedelta(days=7)
    history = [(monday, 1000.0)]
    for day_offset in range(1, 5):
        history.append((monday + timedelta(days=day_offset), 2000.0))

    forecast = estimator._forecast_from_history(history, start, 1)

    assert forecast[0] == 1400.0


def test_history_fallback_prefers_same_day_type_for_missing_dow(monkeypatch):
    module = _load_estimator_module(monkeypatch)
    estimator = module.LoadEstimator(SimpleNamespace(), "sensor.load", interval_minutes=5)

    history = []
    monday = datetime(2026, 5, 4, tzinfo=timezone.utc)
    saturday = datetime(2026, 5, 9, tzinfo=timezone.utc)

    # No Sunday or future Monday buckets are present. Weekday history is low,
    # weekend history is high, so missing days should not collapse to one
    # identical "same time any day" profile.
    for day_offset in range(1, 5):
        day = monday + timedelta(days=day_offset)
        for half_hour in range(48):
            history.append((day + timedelta(minutes=30 * half_hour), 500.0))

    for half_hour in range(48):
        history.append((saturday + timedelta(minutes=30 * half_hour), 2000.0))

    forecast = estimator._forecast_from_history(
        history,
        datetime(2026, 5, 10, tzinfo=timezone.utc),
        576,
    )

    sunday_kwh = sum(forecast[:288]) / 1000 / 12
    monday_kwh = sum(forecast[288:576]) / 1000 / 12

    assert sunday_kwh > monday_kwh * 2
    assert abs(sunday_kwh - 48) < 0.1
    assert abs(monday_kwh - 12) < 0.1


def test_away_window_is_excluded_from_30_day_history(monkeypatch):
    module = _load_estimator_module(monkeypatch)
    now = datetime(2026, 5, 9, tzinfo=timezone.utc)
    calls = {}
    before_away = SimpleNamespace(
        state="1000",
        last_changed=datetime(2026, 5, 4, tzinfo=timezone.utc),
    )
    during_away = SimpleNamespace(
        state="9000",
        last_changed=datetime(2026, 5, 6, tzinfo=timezone.utc),
    )
    after_away = SimpleNamespace(
        state="1200",
        last_changed=datetime(2026, 5, 8, tzinfo=timezone.utc),
    )

    _install_fake_recorder(
        monkeypatch,
        {"sensor.load": [before_away, during_away, after_away]},
        calls,
    )
    hass = SimpleNamespace(
        states=SimpleNamespace(
            get=lambda entity_id: SimpleNamespace(
                attributes={"unit_of_measurement": "W"}
            )
        ),
        async_add_executor_job=_fake_executor,
    )
    estimator = module.LoadEstimator(hass, "sensor.load", interval_minutes=5)
    estimator.away_enabled_at = datetime(2026, 5, 5, tzinfo=timezone.utc)
    estimator.away_disabled_at = datetime(2026, 5, 7, tzinfo=timezone.utc)

    history = _run(estimator._get_load_history())

    assert calls["start_time"] == now - timedelta(days=32)
    assert history == [
        (before_away.last_changed, 1000.0),
        (after_away.last_changed, 1200.0),
    ]


def test_ev_charger_power_subtracted_from_load_history(monkeypatch):
    """Configured EV charger power is removed from the load history so recurring
    EV charging embedded in the whole-home sensor is not double-counted against
    the planned-EV overlay."""
    module = _load_estimator_module(monkeypatch)
    calls = {}
    load_states = [
        SimpleNamespace(state="500", last_changed=datetime(2026, 5, 5, tzinfo=timezone.utc)),
        SimpleNamespace(state="7500", last_changed=datetime(2026, 5, 6, tzinfo=timezone.utc)),
        SimpleNamespace(state="7500", last_changed=datetime(2026, 5, 7, tzinfo=timezone.utc)),
        SimpleNamespace(state="500", last_changed=datetime(2026, 5, 8, tzinfo=timezone.utc)),
    ]
    ev_states = [
        SimpleNamespace(state="7000", last_changed=datetime(2026, 5, 6, tzinfo=timezone.utc)),
        SimpleNamespace(state="0", last_changed=datetime(2026, 5, 8, tzinfo=timezone.utc)),
    ]
    _install_fake_recorder(
        monkeypatch,
        {"sensor.load": load_states, "sensor.ev_power": ev_states},
        calls,
    )
    hass = SimpleNamespace(
        states=SimpleNamespace(
            get=lambda entity_id: SimpleNamespace(
                attributes={"unit_of_measurement": "W"}
            )
        ),
        async_add_executor_job=_fake_executor,
    )
    estimator = module.LoadEstimator(hass, "sensor.load", interval_minutes=5)
    estimator.ev_power_entity_ids = ["sensor.ev_power"]

    history = _run(estimator._get_load_history())

    assert history == [
        (load_states[0].last_changed, 500.0),  # before EV history -> unchanged
        (load_states[1].last_changed, 500.0),  # 7500 - 7000
        (load_states[2].last_changed, 500.0),  # EV step held from 05-06
        (load_states[3].last_changed, 500.0),  # 500 - 0
    ]


def test_ev_subtraction_noop_without_configured_entity(monkeypatch):
    """No EV entity configured -> load history is unchanged (zero regression)."""
    module = _load_estimator_module(monkeypatch)
    calls = {}
    load_states = [
        SimpleNamespace(state="7500", last_changed=datetime(2026, 5, 6, tzinfo=timezone.utc)),
        SimpleNamespace(state="500", last_changed=datetime(2026, 5, 8, tzinfo=timezone.utc)),
    ]
    _install_fake_recorder(monkeypatch, {"sensor.load": load_states}, calls)
    hass = SimpleNamespace(
        states=SimpleNamespace(
            get=lambda entity_id: SimpleNamespace(
                attributes={"unit_of_measurement": "W"}
            )
        ),
        async_add_executor_job=_fake_executor,
    )
    estimator = module.LoadEstimator(hass, "sensor.load", interval_minutes=5)

    history = _run(estimator._get_load_history())

    assert history == [
        (load_states[0].last_changed, 7500.0),
        (load_states[1].last_changed, 500.0),
    ]


def test_active_away_mode_records_departure_without_excluding_history(monkeypatch):
    module = _load_estimator_module(monkeypatch)
    now = datetime(2026, 5, 9, tzinfo=timezone.utc)
    calls = {}
    during_away = SimpleNamespace(
        state="9000",
        last_changed=datetime(2026, 5, 8, tzinfo=timezone.utc),
    )

    _install_fake_recorder(
        monkeypatch,
        {"sensor.load": [during_away]},
        calls,
    )
    hass = SimpleNamespace(
        states=SimpleNamespace(
            get=lambda entity_id: SimpleNamespace(
                attributes={"unit_of_measurement": "W"}
            )
        ),
        async_add_executor_job=_fake_executor,
    )
    estimator = module.LoadEstimator(hass, "sensor.load", interval_minutes=5)
    estimator.away_enabled_at = datetime(2026, 5, 7, tzinfo=timezone.utc)
    estimator.away_disabled_at = None

    history = _run(estimator._get_load_history())

    assert calls["start_time"] == now - timedelta(days=30)
    assert history == [(during_away.last_changed, 9000.0)]


def test_open_meteo_hass_data_watts_are_expanded_to_optimizer_slots(monkeypatch):
    module = _load_estimator_module(monkeypatch)
    start = datetime(2026, 5, 9, 10, 0, tzinfo=timezone.utc)
    watts = {
        start.isoformat(): 1000,
        (start + timedelta(minutes=15)).isoformat(): 2000,
        (start + timedelta(minutes=30)).isoformat(): 0,
    }
    hass = SimpleNamespace(
        data={
            "open_meteo_solar_forecast": {
                "entry-1": SimpleNamespace(data=SimpleNamespace(watts=watts)),
            }
        },
        states=_FakeStates(),
    )
    forecaster = module.SolcastForecaster(hass, interval_minutes=5)

    forecast = _run(forecaster.get_forecast(horizon_hours=1, start_time=start))

    assert forecast == [
        1000.0,
        1000.0,
        1000.0,
        2000.0,
        2000.0,
        2000.0,
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
    ]


def test_open_meteo_multiple_entries_are_summed(monkeypatch):
    module = _load_estimator_module(monkeypatch)
    start = datetime(2026, 5, 9, 10, 0, tzinfo=timezone.utc)
    hass = SimpleNamespace(
        data={
            "open_meteo_solar_forecast": {
                "north": SimpleNamespace(data=SimpleNamespace(watts={start: 1000})),
                "west": SimpleNamespace(data=SimpleNamespace(watts={start: 500})),
            }
        },
        states=_FakeStates(),
    )
    forecaster = module.SolcastForecaster(hass, interval_minutes=5)

    forecast = _run(forecaster.get_forecast(horizon_hours=1, start_time=start))

    assert forecast == [1500.0] * 12


def test_solar_forecast_default_prefers_solcast_when_both_providers_have_data(monkeypatch):
    module = _load_estimator_module(monkeypatch)
    start = datetime(2026, 5, 9, 10, 0, tzinfo=timezone.utc)
    solcast_state = SimpleNamespace(
        entity_id="sensor.solcast_pv_forecast_forecast_today",
        state="12",
        attributes={
            "detailedForecast": [
                {"period_start": start.isoformat(), "pv_estimate": 2.0},
            ],
        },
    )
    hass = SimpleNamespace(
        data={
            "open_meteo_solar_forecast": {
                "entry-1": SimpleNamespace(data=SimpleNamespace(watts={start: 1000})),
            }
        },
        states=_FakeStates([solcast_state], {
            "sensor.solcast_pv_forecast_forecast_today": solcast_state,
        }),
    )
    forecaster = module.SolcastForecaster(hass, interval_minutes=30)

    forecast = _run(forecaster.get_forecast(horizon_hours=1, start_time=start))

    assert forecast == [2000.0, 0.0]
    assert forecaster.last_forecast_source == "solcast"


def test_solar_forecast_open_meteo_preference_wins_when_both_providers_have_data(monkeypatch):
    module = _load_estimator_module(monkeypatch)
    start = datetime(2026, 5, 9, 10, 0, tzinfo=timezone.utc)
    solcast_state = SimpleNamespace(
        entity_id="sensor.solcast_pv_forecast_forecast_today",
        state="12",
        attributes={
            "detailedForecast": [
                {"period_start": start.isoformat(), "pv_estimate": 2.0},
            ],
        },
    )
    hass = SimpleNamespace(
        data={
            "open_meteo_solar_forecast": {
                "entry-1": SimpleNamespace(data=SimpleNamespace(watts={start: 1000})),
            }
        },
        states=_FakeStates([solcast_state], {
            "sensor.solcast_pv_forecast_forecast_today": solcast_state,
        }),
    )
    forecaster = module.SolcastForecaster(
        hass,
        interval_minutes=30,
        provider_preference="open_meteo",
    )

    forecast = _run(forecaster.get_forecast(horizon_hours=1, start_time=start))

    assert forecast == [1000.0, 1000.0]
    assert forecaster.last_forecast_source == "open_meteo"


def test_solar_forecast_preferred_provider_falls_back_when_unavailable(monkeypatch):
    module = _load_estimator_module(monkeypatch)
    start = datetime(2026, 5, 9, 10, 0, tzinfo=timezone.utc)
    solcast_state = SimpleNamespace(
        entity_id="sensor.solcast_pv_forecast_forecast_today",
        state="12",
        attributes={
            "detailedForecast": [
                {"period_start": start.isoformat(), "pv_estimate": 2.0},
            ],
        },
    )
    hass = SimpleNamespace(
        data={},
        states=_FakeStates([solcast_state], {
            "sensor.solcast_pv_forecast_forecast_today": solcast_state,
        }),
    )
    forecaster = module.SolcastForecaster(
        hass,
        interval_minutes=30,
        provider_preference="open_meteo",
    )

    forecast = _run(forecaster.get_forecast(horizon_hours=1, start_time=start))

    assert forecast == [2000.0, 0.0]
    assert forecaster.last_forecast_source == "solcast"


def test_solcast_sensor_scan_uses_renamed_detailed_forecast_entities(monkeypatch):
    module = _load_estimator_module(monkeypatch)
    start = datetime(2026, 5, 9, 10, 0, tzinfo=timezone.utc)
    solcast_state = SimpleNamespace(
        entity_id="sensor.solcast_home_forecast_today",
        state="18.8",
        attributes={
            "detailedForecast": [
                {"period_start": start.isoformat(), "pv_estimate": 1.5},
            ],
        },
    )
    hass = SimpleNamespace(
        data={},
        states=_FakeStates([solcast_state]),
    )
    forecaster = module.SolcastForecaster(hass, interval_minutes=30)

    forecast = _run(forecaster.get_forecast(horizon_hours=1, start_time=start))

    assert forecast == [1500.0, 0.0]
    assert forecaster.last_forecast_source == "solcast"


def test_solcast_external_async_forecast_list_is_awaited(monkeypatch):
    module = _load_estimator_module(monkeypatch)
    start = datetime(2026, 5, 9, 10, 0, tzinfo=timezone.utc)

    class AsyncSolcastApi:
        async def get_forecast_list(self):
            return [
                {
                    "period_start": start.isoformat(),
                    "pv_estimate": 2.5,
                },
            ]

    hass = SimpleNamespace(
        data={
            "solcast_solar": {
                "entry-1": SimpleNamespace(solcast=AsyncSolcastApi()),
            },
        },
        states=_FakeStates(),
    )
    forecaster = module.SolcastForecaster(hass, interval_minutes=30)

    forecast = _run(forecaster.get_forecast(horizon_hours=1, start_time=start))

    assert forecast == [2500.0, 0.0]
    assert forecaster.last_forecast_source == "solcast"


def test_solar_forecast_invalid_provider_normalizes_to_solcast(monkeypatch):
    module = _load_estimator_module(monkeypatch)
    forecaster = module.SolcastForecaster(
        SimpleNamespace(data={}, states=_FakeStates()),
        provider_preference="invalid",
    )

    assert forecaster.provider_preference == "solcast"


def test_open_meteo_sensor_watts_attributes_are_used_without_hass_data(monkeypatch):
    module = _load_estimator_module(monkeypatch)
    start = datetime(2026, 5, 9, 10, 0, tzinfo=timezone.utc)
    state = SimpleNamespace(
        entity_id="sensor.roof_energy_production_today",
        state="12000",
        attributes={
            "watts": {
                start.isoformat(): 800,
                (start + timedelta(minutes=15)).isoformat(): 1200,
            }
        },
    )
    hass = SimpleNamespace(
        data={},
        states=_FakeStates([state]),
    )
    forecaster = module.SolcastForecaster(hass, interval_minutes=5)

    forecast = _run(forecaster.get_forecast(horizon_hours=1, start_time=start))

    assert forecast[:6] == [800.0, 800.0, 800.0, 1200.0, 1200.0, 1200.0]


def test_open_meteo_renamed_sensor_watts_attributes_are_used(monkeypatch):
    module = _load_estimator_module(monkeypatch)
    start = datetime(2026, 5, 9, 10, 0, tzinfo=timezone.utc)
    state = SimpleNamespace(
        entity_id="sensor.my_rooftop_forecast",
        state="12000",
        attributes={
            "watts": {
                start.isoformat(): 700,
                (start + timedelta(minutes=15)).isoformat(): 900,
            }
        },
    )
    hass = SimpleNamespace(
        data={},
        states=_FakeStates([state]),
    )
    forecaster = module.SolcastForecaster(hass, interval_minutes=5)

    forecast = _run(forecaster.get_forecast(horizon_hours=1, start_time=start))

    assert forecast[:6] == [700.0, 700.0, 700.0, 900.0, 900.0, 900.0]


class _FakeStates:
    def __init__(self, states=None, state_map=None):
        self._states = states or []
        self._state_map = state_map or {}

    def get(self, entity_id):
        return self._state_map.get(entity_id)

    def async_all(self, domain=None):
        if domain is None:
            return self._states
        return [
            state
            for state in self._states
            if getattr(state, "entity_id", "").startswith(f"{domain}.")
        ]


def _install_fake_recorder(monkeypatch, history, calls):
    components_module = types.ModuleType("homeassistant.components")
    recorder_module = types.ModuleType("homeassistant.components.recorder")
    recorder_history_module = types.ModuleType("homeassistant.components.recorder.history")

    class FakeRecorder:
        async def async_add_executor_job(
            self,
            func,
            hass,
            start_time,
            end_time,
            entity_ids,
        ):
            calls["start_time"] = start_time
            calls["end_time"] = end_time
            calls["entity_ids"] = entity_ids
            return history

    recorder_module.get_instance = lambda hass: FakeRecorder()
    recorder_history_module.get_significant_states = object()

    monkeypatch.setitem(sys.modules, "homeassistant.components", components_module)
    monkeypatch.setitem(sys.modules, "homeassistant.components.recorder", recorder_module)
    monkeypatch.setitem(
        sys.modules,
        "homeassistant.components.recorder.history",
        recorder_history_module,
    )


def _run(coro):
    import asyncio

    return asyncio.run(coro)


async def _fake_executor(func, *args):
    """Run an executor-offloaded function inline (for mock hass in tests)."""
    return func(*args)


def test_get_forecast_offloads_history_build_to_executor(monkeypatch):
    """The full-history forecast build must run off the event loop.

    _forecast_from_history iterates the entire load history (and re-scans it for
    the recent-regime adjustment), so running it inline on the event loop froze
    HA every optimisation cycle. Regression guard: get_forecast must hand it to
    async_add_executor_job, never call it on the loop.
    """
    module = _load_estimator_module(monkeypatch)
    now = datetime(2026, 5, 9, tzinfo=timezone.utc)
    calls = {}

    history = [
        (now - timedelta(days=offset), 1000.0)
        for offset in range(1, 21)
    ]
    _install_fake_recorder(monkeypatch, {"sensor.load": [
        SimpleNamespace(state="1000", last_changed=ts) for ts, _ in history
    ]}, calls)

    offloaded: list[str] = []

    async def _spy_executor(func, *args):
        target = func.func if isinstance(func, functools.partial) else func
        offloaded.append(getattr(target, "__name__", repr(target)))
        return func(*args)

    hass = SimpleNamespace(
        states=SimpleNamespace(
            get=lambda entity_id: SimpleNamespace(
                attributes={"unit_of_measurement": "W"}
            )
        ),
        async_add_executor_job=_spy_executor,
    )
    estimator = module.LoadEstimator(hass, "sensor.load", interval_minutes=5)

    forecast = _run(estimator.get_forecast(horizon_hours=12))

    assert forecast, "expected a non-empty forecast"
    assert "_forecast_from_history" in offloaded, (
        f"forecast build was not offloaded to the executor; saw {offloaded}"
    )
