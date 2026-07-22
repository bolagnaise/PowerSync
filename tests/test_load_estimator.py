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
    assert calls["statistics_period"] == "hour"
    assert calls["statistics_types"] == {"mean"}
    assert len(history) == 48
    assert history[0] == (now - timedelta(days=1), 1200.0)
    assert history[-1] == (now - timedelta(minutes=30), 1200.0)


def test_history_merge_uses_statistics_before_raw_cutover(monkeypatch):
    module = _load_estimator_module(monkeypatch)
    start = datetime(2026, 7, 1, tzinfo=timezone.utc)
    statistics = [
        module.LoadHistoryBucket(
            start=start + timedelta(hours=hour),
            energy_wh=500.0,
            coverage_seconds=3600.0,
            source="statistics",
        )
        for hour in range(4)
    ]
    raw = [
        module.LoadHistoryBucket(
            start=start + timedelta(hours=2, minutes=30 * half),
            energy_wh=250.0,
            coverage_seconds=1800.0,
            source="states",
        )
        for half in range(4)
    ]

    cutover = module.LoadEstimator._history_cutover(raw)
    merged = module.LoadEstimator._merge_history_buckets(statistics, raw, cutover)

    assert cutover == start + timedelta(hours=3)
    assert [bucket.start for bucket in merged] == [
        start,
        start + timedelta(hours=1),
        start + timedelta(hours=2),
        start + timedelta(hours=3),
        start + timedelta(hours=3, minutes=30),
    ]
    assert [bucket.source for bucket in merged[:3]] == ["statistics"] * 3
    assert [bucket.source for bucket in merged[3:]] == ["states"] * 2


def test_hourly_statistics_split_preserves_energy(monkeypatch):
    module = _load_estimator_module(monkeypatch)
    start = datetime(2026, 7, 1, tzinfo=timezone.utc)

    buckets = module.LoadEstimator._statistics_to_half_hour_buckets(
        [{"start": start.timestamp(), "mean": 2.0}],
        1000.0,
    )

    assert [bucket.start for bucket in buckets] == [
        start,
        start + timedelta(minutes=30),
    ]
    assert sum(bucket.energy_wh for bucket in buckets) == 2000.0
    assert all(bucket.mean_w == 2000.0 for bucket in buckets)


def test_recorder_statistics_extend_history_before_raw_retention(monkeypatch):
    module = _load_estimator_module(monkeypatch)
    now = datetime(2026, 5, 9, tzinfo=timezone.utc)
    calls = {}
    raw_start = now - timedelta(days=1)
    statistic_start = now - timedelta(days=20)
    _install_fake_recorder(
        monkeypatch,
        {
            "sensor.load": [
                SimpleNamespace(state="1200", last_changed=raw_start),
            ],
        },
        calls,
        statistics={
            "sensor.load": [
                {"start": statistic_start.timestamp(), "mean": 800.0},
            ],
        },
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

    assert history[0] == (statistic_start, 800.0)
    assert history[1] == (statistic_start + timedelta(minutes=30), 800.0)
    assert any(timestamp >= raw_start + timedelta(hours=1) for timestamp, _ in history)
    assert estimator._history_diagnostics["history_source"] == "merged"
    assert estimator._history_diagnostics["history_span_days"] >= 19.0


def test_statistics_only_history_remains_usable_when_raw_is_empty(monkeypatch):
    module = _load_estimator_module(monkeypatch)
    now = datetime(2026, 5, 9, tzinfo=timezone.utc)
    statistic_start = now - timedelta(days=20)
    calls = {}
    _install_fake_recorder(
        monkeypatch,
        {"sensor.load": []},
        calls,
        statistics={
            "sensor.load": [
                {"start": statistic_start.timestamp(), "mean": 750.0},
            ],
        },
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

    assert history == [
        (statistic_start, 750.0),
        (statistic_start + timedelta(minutes=30), 750.0),
    ]
    assert estimator._history_diagnostics["history_source"] == "statistics"


def test_state_normalization_is_independent_of_update_density(monkeypatch):
    module = _load_estimator_module(monkeypatch)
    start = datetime(2026, 7, 1, tzinfo=timezone.utc)
    sparse = [
        SimpleNamespace(state="1000", last_changed=start),
        SimpleNamespace(state="1000", last_changed=start + timedelta(hours=1)),
    ]
    dense = [
        SimpleNamespace(
            state="1000",
            last_changed=start + timedelta(minutes=minute),
        )
        for minute in range(0, 61, 5)
    ]

    sparse_buckets = module.LoadEstimator._states_to_half_hour_buckets(
        sparse, start, start + timedelta(hours=1), 1.0, "states"
    )
    dense_buckets = module.LoadEstimator._states_to_half_hour_buckets(
        dense, start, start + timedelta(hours=1), 1.0, "states"
    )

    assert [(b.start, round(b.mean_w, 6)) for b in sparse_buckets] == [
        (b.start, round(b.mean_w, 6)) for b in dense_buckets
    ]
    assert [b.energy_wh for b in sparse_buckets] == [500.0, 500.0]


def test_state_normalization_does_not_bridge_unavailable_gap(monkeypatch):
    module = _load_estimator_module(monkeypatch)
    start = datetime(2026, 7, 1, tzinfo=timezone.utc)
    states = [
        SimpleNamespace(state="1000", last_changed=start),
        SimpleNamespace(
            state="unavailable",
            last_changed=start + timedelta(minutes=30),
        ),
        SimpleNamespace(state="1000", last_changed=start + timedelta(hours=1)),
        SimpleNamespace(
            state="1000",
            last_changed=start + timedelta(hours=1, minutes=30),
        ),
    ]

    buckets = module.LoadEstimator._states_to_half_hour_buckets(
        states,
        start,
        start + timedelta(hours=1, minutes=30),
        1.0,
        "states",
    )

    assert [bucket.start for bucket in buckets] == [
        start,
        start + timedelta(hours=1),
    ]
    assert [bucket.energy_wh for bucket in buckets] == [500.0, 500.0]


def test_baseline_confidence_counts_distinct_dates_not_updates(monkeypatch):
    module = _load_estimator_module(monkeypatch)
    estimator = module.LoadEstimator(SimpleNamespace(), "sensor.load", interval_minutes=5)
    start = datetime(2026, 7, 18, 19, tzinfo=timezone.utc)
    same_saturday_updates = [
        (start - timedelta(days=7) + timedelta(minutes=minute), 1200.0)
        for minute in range(0, 30, 2)
    ]

    samples = estimator._distinct_date_samples(same_saturday_updates)

    assert len(samples) == 1


def test_recent_daytime_anomaly_does_not_scale_evening_slots(monkeypatch):
    module = _load_estimator_module(monkeypatch)
    estimator = module.LoadEstimator(SimpleNamespace(), "sensor.load", interval_minutes=5)
    start = datetime(2026, 7, 18, 12, tzinfo=timezone.utc)
    history = []

    # Prior Thursdays and Fridays establish the exact recent comparison slots.
    for days_ago in (8, 9, 15, 16, 22, 23, 29):
        day = start - timedelta(days=days_ago)
        for half_hour in range(48):
            history.append(
                (
                    day.replace(hour=0, minute=0) + timedelta(minutes=30 * half_hour),
                    500.0,
                )
            )

    # Recent evidence is extreme only around midday; there is no recent
    # evening evidence, so evening forecast slots must remain neutral.
    for days_ago in (1, 2):
        day = start - timedelta(days=days_ago)
        for half_hour in range(20, 29):
            history.append(
                (
                    day.replace(hour=0, minute=0) + timedelta(minutes=30 * half_hour),
                    5000.0,
                )
            )

    horizon_starts = [start + timedelta(minutes=5 * i) for i in range(24 * 12)]
    scales = estimator._recent_load_scales(history, start, horizon_starts)
    scale_19 = scales[int((19 - start.hour) * 12)]
    scale_20 = scales[int((20 - start.hour) * 12)]

    assert scale_19 == 1.0
    assert scale_20 == 1.0
    assert max(scales[:36]) > 1.0
    assert max(scales) <= 2.5


def test_sparse_recent_evidence_cannot_reach_the_scale_cap(monkeypatch):
    module = _load_estimator_module(monkeypatch)
    estimator = module.LoadEstimator(SimpleNamespace(), "sensor.load", interval_minutes=5)
    start = datetime(2026, 7, 18, 12, tzinfo=timezone.utc)
    recent = start - timedelta(days=1)
    history = [
        (recent - timedelta(days=7 * weeks), 1000.0)
        for weeks in (1, 2, 3)
    ]
    history.append((recent, 10_000.0))

    scales = estimator._recent_load_scales(history, start, [start])

    assert scales == [1.375]
    assert scales[0] < module.RECENT_LOAD_MAX_SCALE


def test_recent_scale_temperature_adjustment_is_applied_once(monkeypatch):
    module = _load_estimator_module(monkeypatch)
    estimator = module.LoadEstimator(SimpleNamespace(), "sensor.load", interval_minutes=5)
    start = datetime(2026, 7, 18, 12, tzinfo=timezone.utc)
    history = []
    recent_samples = []

    for days_ago in (1, 2):
        recent = start - timedelta(days=days_ago)
        recent_samples.append(recent)
        history.extend(
            (recent - timedelta(days=7 * weeks), 1000.0)
            for weeks in (1, 2, 3)
        )
        # The 1.5x observation is fully explained by a 5 C temperature
        # deviation at alpha=0.1, so it must not create another regime scale.
        history.append((recent, 1500.0))

    bucket_temps = {
        (sample.weekday(), sample.hour, 0): 20.0
        for sample in recent_samples
    }
    scales = estimator._recent_load_scales(
        history,
        start,
        [start],
        historical_temps=[(sample, 25.0) for sample in recent_samples],
        bucket_temp_averages=bucket_temps,
        alpha=0.1,
    )

    assert scales == [1.0]


def test_four_saturday_replay_does_not_recreate_extreme_evening_forecast(monkeypatch):
    """Reduced replay of the live 5.86 kW evening forecast regression."""
    module = _load_estimator_module(monkeypatch)
    estimator = module.LoadEstimator(SimpleNamespace(), "sensor.load", interval_minutes=5)
    start = datetime(2026, 7, 18, 17, tzinfo=timezone.utc)
    saturday_hourly_kw = {
        7: [1.812, 1.401, 2.638, 1.544, 0.857, 0.692],
        14: [1.241, 0.913, 0.286, 0.352, 0.373, 0.357],
        21: [1.215, 2.031, 2.149, 0.612, 0.286, 0.134],
        28: [0.244, 1.399, 0.528, 1.068, 0.638, 0.064],
    }
    history = []
    for days_ago, values in saturday_hourly_kw.items():
        saturday = start - timedelta(days=days_ago)
        for hour_offset, value_kw in enumerate(values):
            for minute in (0, 30):
                history.append(
                    (
                        saturday.replace(
                            hour=17 + hour_offset,
                            minute=minute,
                        ),
                        value_kw * 1000.0,
                    )
                )

    forecast = estimator._forecast_from_history(history, start, 6 * 12)

    assert forecast[2 * 12] < 3000.0  # 19:00 was 5.863 kW
    assert forecast[3 * 12] < 2500.0  # 20:00 was 4.442 kW
    assert forecast[5 * 12] < 1500.0  # 22:00 was 1.514 kW


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
    for half_hour_offset in range(30 * 48, 144, -1):
        history.append((start - timedelta(minutes=30 * half_hour_offset), 500.0))
    for half_hour_offset in range(96, 0, -1):
        history.append((start - timedelta(minutes=30 * half_hour_offset), 1500.0))

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

    assert round(forecast[0], 6) == 1000.0


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
    assert history
    assert all(
        not (estimator.away_enabled_at <= timestamp < estimator.away_disabled_at)
        for timestamp, _ in history
    )
    assert dict(history)[before_away.last_changed] == 1000.0
    assert dict(history)[estimator.away_disabled_at] == 9000.0
    assert dict(history)[after_away.last_changed] == 1200.0


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

    assert history
    assert all(round(value, 6) == 500.0 for _, value in history)


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

    assert history
    history_map = dict(history)
    assert history_map[load_states[0].last_changed] == 7500.0
    assert history_map[load_states[1].last_changed] == 500.0


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
    assert len(history) == 48
    assert history[0] == (during_away.last_changed, 9000.0)
    assert all(value == 9000.0 for _, value in history)


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


def test_open_meteo_zero_fills_after_last_forecast_point(monkeypatch):
    module = _load_estimator_module(monkeypatch)
    start = datetime(2026, 5, 9, 10, 0, tzinfo=timezone.utc)
    watts = {
        start.isoformat(): 1000,
        (start + timedelta(minutes=5)).isoformat(): 2000,
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

    # Past the last forecast point (start+5min), Open-Meteo should zero-fill
    # like Solcast does rather than carrying the last point's value forward
    # for the rest of the horizon.
    assert forecast[0] == 1000.0
    assert forecast[1] == 2000.0
    assert forecast[2] == 0.0
    assert forecast[-1] == 0.0


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

    # Entries are summed at the shared timestamp (1000 + 500); past that
    # single forecast point Open-Meteo zero-fills rather than carrying the
    # summed value forward for the rest of the horizon.
    assert forecast == [1500.0] + [0.0] * 11


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

    # Past the single forecast point, Open-Meteo zero-fills rather than
    # carrying the value forward for the rest of the horizon.
    assert forecast == [1000.0, 0.0]
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


def test_solcast_in_window_zero_sensor_remains_a_valid_provider(monkeypatch):
    module = _load_estimator_module(monkeypatch)
    start = datetime(2026, 5, 9, 10, 0, tzinfo=timezone.utc)
    solcast_state = SimpleNamespace(
        entity_id="sensor.solcast_pv_forecast_forecast_today",
        state="0",
        attributes={
            "detailedForecast": [
                {"period_start": start.isoformat(), "pv_estimate": 0.0},
            ],
        },
    )
    hass = SimpleNamespace(
        data={},
        states=_FakeStates(
            [solcast_state],
            {solcast_state.entity_id: solcast_state},
        ),
    )
    forecaster = module.SolcastForecaster(hass, interval_minutes=30)

    forecast = _run(forecaster.get_forecast(horizon_hours=1, start_time=start))

    assert forecast == [0.0, 0.0]
    assert forecaster.last_forecast_source == "solcast"


def test_solcast_stale_sensor_periods_are_not_reported_as_available(monkeypatch):
    module = _load_estimator_module(monkeypatch)
    start = datetime(2026, 5, 9, 10, 0, tzinfo=timezone.utc)
    stale_start = start - timedelta(days=1)
    solcast_state = SimpleNamespace(
        entity_id="sensor.solcast_pv_forecast_forecast_today",
        state="0",
        attributes={
            "detailedForecast": [
                {"period_start": stale_start.isoformat(), "pv_estimate": 2.0},
            ],
        },
    )
    hass = SimpleNamespace(
        data={},
        states=_FakeStates(
            [solcast_state],
            {solcast_state.entity_id: solcast_state},
        ),
    )
    forecaster = module.SolcastForecaster(hass, interval_minutes=30)

    forecast = _run(forecaster.get_forecast(horizon_hours=1, start_time=start))

    assert forecast == [0.0, 0.0]
    assert forecaster.last_forecast_source is None


def test_solcast_external_zero_forecast_remains_a_valid_provider(monkeypatch):
    module = _load_estimator_module(monkeypatch)
    start = datetime(2026, 5, 9, 10, 0, tzinfo=timezone.utc)
    hass = SimpleNamespace(
        data={
            "solcast_solar": {
                "entry-1": SimpleNamespace(
                    data_forecasts=[
                        {"period_start": start.isoformat(), "pv_estimate": 0.0},
                    ],
                ),
            },
        },
        states=_FakeStates(),
    )
    forecaster = module.SolcastForecaster(hass, interval_minutes=30)

    forecast = _run(forecaster.get_forecast(horizon_hours=1, start_time=start))

    assert forecast == [0.0, 0.0]
    assert forecaster.last_forecast_source == "solcast"


def test_solcast_builtin_stale_periods_are_not_reported_as_available(monkeypatch):
    module = _load_estimator_module(monkeypatch)
    start = datetime(2026, 5, 9, 10, 0, tzinfo=timezone.utc)
    stale_start = start - timedelta(days=1)
    hass = SimpleNamespace(
        data={
            "power_sync": {
                "entry-1": {
                    "solcast_coordinator": SimpleNamespace(
                        data={
                            "forecasts": [
                                {
                                    "period_start": stale_start.isoformat(),
                                    "pv_estimate": 2.0,
                                },
                            ],
                        },
                    ),
                },
            },
        },
        states=_FakeStates(),
    )
    forecaster = module.SolcastForecaster(hass, interval_minutes=30)

    forecast = _run(forecaster.get_forecast(horizon_hours=1, start_time=start))

    assert forecast == [0.0, 0.0]
    assert forecaster.last_forecast_source is None


def test_solcast_horizon_coverage_uses_half_open_boundaries(monkeypatch):
    module = _load_estimator_module(monkeypatch)
    start = datetime(2026, 5, 9, 10, 0, tzinfo=timezone.utc)
    forecaster = module.SolcastForecaster(
        SimpleNamespace(data={}, states=_FakeStates()),
        interval_minutes=30,
    )

    ending_at_start = forecaster._parse_solcast_data(
        [{"period_end": start.isoformat(), "pv_estimate": 1.0}],
        start,
        2,
    )
    starting_at_end = forecaster._parse_solcast_data(
        [
            {
                "period_start": (start + timedelta(hours=1)).isoformat(),
                "pv_estimate": 1.0,
            },
        ],
        start,
        2,
    )

    assert ending_at_start == []
    assert starting_at_end == []


def test_solcast_horizon_coverage_normalizes_non_utc_offsets(monkeypatch):
    module = _load_estimator_module(monkeypatch)
    local_tz = timezone(timedelta(hours=10))
    start = datetime(2026, 5, 9, 10, 0, tzinfo=local_tz)
    forecaster = module.SolcastForecaster(
        SimpleNamespace(data={}, states=_FakeStates()),
        interval_minutes=30,
    )

    forecast = forecaster._parse_solcast_data(
        [
            {
                "period_start": datetime(
                    2026, 5, 9, 0, 0, tzinfo=timezone.utc
                ).isoformat(),
                "pv_estimate": 1.0,
            },
        ],
        start,
        2,
    )

    assert forecast == [1000.0, 0.0]


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


def test_solcast_prefers_full_integration_forecast_over_two_daily_sensors(monkeypatch):
    module = _load_estimator_module(monkeypatch)
    start = datetime(2026, 7, 17, 23, 55, tzinfo=timezone.utc)
    today_noon = datetime(2026, 7, 17, 12, 0, tzinfo=timezone.utc)
    tomorrow_noon = datetime(2026, 7, 18, 12, 0, tzinfo=timezone.utc)
    day_three_noon = datetime(2026, 7, 19, 12, 0, tzinfo=timezone.utc)

    today_state = SimpleNamespace(
        entity_id="sensor.solcast_pv_forecast_forecast_today",
        state="0",
        attributes={
            "detailedForecast": [
                {"period_start": today_noon.isoformat(), "pv_estimate": 1.0},
            ],
        },
    )
    tomorrow_state = SimpleNamespace(
        entity_id="sensor.solcast_pv_forecast_forecast_tomorrow",
        state="10",
        attributes={
            "detailedForecast": [
                {"period_start": tomorrow_noon.isoformat(), "pv_estimate": 1.0},
            ],
        },
    )
    solcast_api = SimpleNamespace(
        data_forecasts=[
            {"period_start": tomorrow_noon, "pv_estimate": 1.0},
            {"period_start": day_three_noon, "pv_estimate": 2.0},
        ],
    )
    hass = SimpleNamespace(
        data={"solcast_solar": {"solcast": solcast_api}},
        states=_FakeStates(
            [today_state, tomorrow_state],
            {
                today_state.entity_id: today_state,
                tomorrow_state.entity_id: tomorrow_state,
            },
        ),
    )
    forecaster = module.SolcastForecaster(hass, interval_minutes=30)

    forecast = _run(forecaster.get_forecast(horizon_hours=48, start_time=start))

    assert forecast[25] == 1000.0
    assert forecast[73] == 2000.0
    assert forecaster.last_forecast_source == "solcast"


def test_solcast_prefers_nested_full_cache_over_partial_coordinator_data(monkeypatch):
    module = _load_estimator_module(monkeypatch)
    start = datetime(2026, 7, 17, 23, 55, tzinfo=timezone.utc)
    tomorrow_noon = datetime(2026, 7, 18, 12, 0, tzinfo=timezone.utc)
    day_three_noon = datetime(2026, 7, 19, 12, 0, tzinfo=timezone.utc)
    tomorrow_period = {
        "period_start": tomorrow_noon,
        "pv_estimate": 1.0,
    }
    coordinator = SimpleNamespace(
        data={"detailedForecast": [tomorrow_period]},
        solcast=SimpleNamespace(
            data_forecasts=[
                tomorrow_period,
                {"period_start": day_three_noon, "pv_estimate": 2.0},
            ],
        ),
    )
    hass = SimpleNamespace(
        data={"solcast_solar": {"entry-1": coordinator}},
        states=_FakeStates(),
    )
    forecaster = module.SolcastForecaster(hass, interval_minutes=30)

    forecast = _run(forecaster.get_forecast(horizon_hours=48, start_time=start))

    assert forecast[25] == 1000.0
    assert forecast[73] == 2000.0
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

    assert forecast[:6] == [800.0, 800.0, 800.0, 1200.0, 0.0, 0.0]


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

    assert forecast[:6] == [700.0, 700.0, 700.0, 900.0, 0.0, 0.0]


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


def _install_fake_recorder(monkeypatch, history, calls, statistics=None):
    components_module = types.ModuleType("homeassistant.components")
    recorder_module = types.ModuleType("homeassistant.components.recorder")
    recorder_history_module = types.ModuleType("homeassistant.components.recorder.history")
    recorder_statistics_module = types.ModuleType(
        "homeassistant.components.recorder.statistics"
    )
    history_marker = object()
    statistics_marker = object()

    class FakeRecorder:
        async def async_add_executor_job(self, func, *args):
            if func is history_marker:
                hass, start_time, end_time, entity_ids = args
                calls["start_time"] = start_time
                calls["end_time"] = end_time
                calls["entity_ids"] = entity_ids
                return history
            if func is statistics_marker:
                (
                    hass,
                    start_time,
                    end_time,
                    statistic_ids,
                    period,
                    units,
                    types_requested,
                ) = args
                calls["statistics_start_time"] = start_time
                calls["statistics_end_time"] = end_time
                calls["statistic_ids"] = statistic_ids
                calls["statistics_period"] = period
                calls["statistics_types"] = types_requested
                return statistics or {}
            raise AssertionError(f"unexpected recorder function: {func!r}")

    recorder_module.get_instance = lambda hass: FakeRecorder()
    recorder_history_module.get_significant_states = history_marker
    recorder_statistics_module.statistics_during_period = statistics_marker

    monkeypatch.setitem(sys.modules, "homeassistant.components", components_module)
    monkeypatch.setitem(sys.modules, "homeassistant.components.recorder", recorder_module)
    monkeypatch.setitem(
        sys.modules,
        "homeassistant.components.recorder.history",
        recorder_history_module,
    )
    monkeypatch.setitem(
        sys.modules,
        "homeassistant.components.recorder.statistics",
        recorder_statistics_module,
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
