"""
Load estimation for battery optimization.

Supports multiple forecast sources:
1. Local pattern-based estimation from Home Assistant history
2. Simple pattern-based fallback

The historical model uses recorder data grouped by local day and time, with
recency weighting and outlier handling to avoid overreacting to one unusual day.
"""
from __future__ import annotations

import bisect
import functools
import inspect
import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from ..const import (
    DEFAULT_SOLAR_FORECAST_PROVIDER,
    DEFAULT_SOLCAST_ESTIMATE_TYPE,
    SOLAR_FORECAST_PROVIDER_OPEN_METEO,
    SOLAR_FORECAST_PROVIDER_SOLCAST,
    SOLAR_FORECAST_PROVIDERS,
    SOLCAST_ESTIMATE,
    SOLCAST_ESTIMATE10,
    SOLCAST_ESTIMATE90,
)

_LOGGER = logging.getLogger(__name__)

HISTORY_LOOKBACK_DAYS = 30
AWAY_RECOVERY_DAYS = 7
RECENCY_HALF_LIFE_DAYS = 14
MIN_EXACT_BUCKET_SAMPLES = 2
SINGLE_EXACT_BUCKET_WEIGHT = 0.6
OUTLIER_MIN_SAMPLES = 4
OUTLIER_MIN_THRESHOLD_W = 500.0
OUTLIER_MEDIAN_FRACTION = 0.5
MAD_NORMAL_SCALE = 1.4826
RECENT_LOAD_WINDOW_HOURS = 48
RECENT_LOAD_BASELINE_EXCLUDE_HOURS = 48
RECENT_LOAD_MIN_COVERAGE_HOURS = 12
RECENT_LOAD_DEADBAND = 0.15
RECENT_LOAD_BLEND = 0.7
RECENT_LOAD_MIN_SCALE = 0.8
RECENT_LOAD_MAX_SCALE = 2.5
HISTORY_BUCKET_SECONDS = 30 * 60
HISTORY_BUCKET_MIN_COVERAGE_SECONDS = 25 * 60
RECENT_LOAD_FULL_CONFIDENCE_SAMPLES = 2
RECENT_LOAD_FULL_CONFIDENCE_DATES = 3
RECENT_LOAD_MIN_EXPECTED_WH = 100.0
ACTIVE_AWAY_LOAD_BLEND = 1.0
ACTIVE_AWAY_LOAD_MIN_SCALE = 0.2


@dataclass(frozen=True, slots=True)
class LoadHistoryBucket:
    """One normalized Recorder load interval."""

    start: datetime
    energy_wh: float
    coverage_seconds: float
    source: Literal["states", "statistics"]

    @property
    def mean_w(self) -> float:
        """Return mean power over the valid portion of the interval."""
        if self.coverage_seconds <= 0:
            return 0.0
        return self.energy_wh * 3600.0 / self.coverage_seconds

_SOLCAST_ESTIMATE_FIELDS = {
    SOLCAST_ESTIMATE: ("pv_estimate", "pv_estimate50"),
    SOLCAST_ESTIMATE10: ("pv_estimate10", "pv_estimate", "pv_estimate50"),
    SOLCAST_ESTIMATE90: ("pv_estimate90", "pv_estimate", "pv_estimate50"),
}

OPEN_METEO_SOLAR_FORECAST_DOMAIN = "open_meteo_solar_forecast"
OPEN_METEO_WATTS_ATTR = "watts"
OPEN_METEO_DAILY_SENSOR_SUFFIXES = (
    "_energy_production_today",
    "_energy_production_tomorrow",
)


class LoadEstimator:
    """
    Estimate household load forecast from multiple sources.

    Priority order:
    1. Historical pattern matching from Home Assistant recorder
    2. Simple pattern-based fallback
    """

    def __init__(
        self,
        hass: HomeAssistant,
        load_entity_id: str | None = None,
        interval_minutes: int = 5,
        weather_entity_id: str | None = None,
    ):
        """
        Initialize the load estimator.

        Args:
            hass: Home Assistant instance
            load_entity_id: Entity ID for load sensor (e.g., sensor.power_sync_home_load)
            interval_minutes: Forecast interval in minutes
            weather_entity_id: Optional HA weather entity for temperature-aware forecasting
        """
        self.hass = hass
        self.load_entity_id = load_entity_id
        self.interval_minutes = interval_minutes
        self.weather_entity_id = weather_entity_id
        # Optional EV charger power sensor entity ids. When set (by the
        # coordinator, only for battery brands whose home-load sensor does NOT
        # already exclude EV charging), their recorded power is subtracted from
        # the load history so recurring EV charging is not double-counted once
        # the planned-EV overlay is added back on top of the forecast.
        self.ev_power_entity_ids: list[str] = []
        self.away_enabled_at: datetime | None = None   # when switch turned ON (departure)
        self.away_disabled_at: datetime | None = None  # when switch turned OFF (return)
        self._history_cache: dict[str, list[tuple[datetime, float]]] = {}
        self._cache_time: datetime | None = None
        self._cache_duration = timedelta(hours=1)
        self._history_diagnostics: dict[str, Any] = {}
        self._recent_load_diagnostics: dict[str, Any] = {}

        # Temperature sensitivity cache
        self._temp_alpha: float | None = None
        self._temp_bucket_averages: dict[tuple[int, int, int], float] | None = None
        self._temp_history: list[tuple[datetime, float]] = []
        self._temp_alpha_fitted: bool = False  # True once fitting has run (even if α=None)
        self._temp_cache_time: datetime | None = None
        self._get_forecasts_unsupported: bool = False  # Latched when service is missing

    @property
    def away_mode(self) -> bool:
        """True when the user is currently away (switch ON, not yet returned)."""
        return bool(self.away_enabled_at and not self.away_disabled_at)

    @property
    def _in_recovery(self) -> bool:
        """True during the 7-day window after returning from a trip."""
        if not self.away_disabled_at or not self.away_enabled_at:
            return False
        return (dt_util.utcnow() - self.away_disabled_at) < timedelta(days=AWAY_RECOVERY_DAYS)

    @staticmethod
    def _states_to_half_hour_buckets(
        raw_states,
        start_time: datetime,
        end_time: datetime,
        multiplier: float,
        source: Literal["states", "statistics"] = "states",
        *,
        allow_zero: bool = False,
    ) -> list[LoadHistoryBucket]:
        """Integrate Recorder state durations into half-hour energy buckets."""
        states = sorted(
            (
                state
                for state in raw_states
                if getattr(state, "last_changed", None) is not None
            ),
            key=lambda state: state.last_changed,
        )
        if not states or end_time <= start_time:
            return []

        accumulated: dict[datetime, list[float]] = defaultdict(lambda: [0.0, 0.0])
        for index, state in enumerate(states):
            interval_start = max(start_time, state.last_changed)
            interval_end = min(
                end_time,
                states[index + 1].last_changed if index + 1 < len(states) else end_time,
            )
            if interval_end <= interval_start:
                continue
            try:
                watts = float(state.state) * multiplier
            except (ValueError, TypeError):
                continue
            if allow_zero:
                valid = 0 <= watts < 100_000
            else:
                valid = 0 < watts < 100_000
            if not valid:
                continue

            cursor = interval_start
            while cursor < interval_end:
                epoch = cursor.timestamp()
                bucket_epoch = int(epoch // HISTORY_BUCKET_SECONDS) * HISTORY_BUCKET_SECONDS
                bucket_start = datetime.fromtimestamp(bucket_epoch, tz=cursor.tzinfo)
                bucket_end = datetime.fromtimestamp(
                    bucket_epoch + HISTORY_BUCKET_SECONDS,
                    tz=cursor.tzinfo,
                )
                segment_end = min(interval_end, bucket_end)
                seconds = (segment_end - cursor).total_seconds()
                if seconds <= 0:
                    break
                accumulated[bucket_start][0] += watts * seconds / 3600.0
                accumulated[bucket_start][1] += seconds
                cursor = segment_end

        buckets = [
            LoadHistoryBucket(
                start=bucket_start,
                energy_wh=values[0],
                coverage_seconds=values[1],
                source=source,
            )
            for bucket_start, values in accumulated.items()
            if values[1] >= HISTORY_BUCKET_MIN_COVERAGE_SECONDS
        ]
        return sorted(buckets, key=lambda bucket: bucket.start)

    @staticmethod
    def _statistics_to_half_hour_buckets(
        statistics: list[dict[str, Any]] | None,
        multiplier: float,
    ) -> list[LoadHistoryBucket]:
        """Split hourly mean-power statistics into energy-preserving half-hours."""
        buckets: list[LoadHistoryBucket] = []
        for entry in statistics or []:
            start = entry.get("start")
            mean = entry.get("mean")
            if isinstance(start, (int, float)):
                start = datetime.fromtimestamp(start, tz=timezone.utc)
            if not isinstance(start, datetime) or mean is None:
                continue
            try:
                mean_w = float(mean) * multiplier
            except (ValueError, TypeError):
                continue
            if not (0 < mean_w < 100_000):
                continue
            for offset in (0, 30):
                buckets.append(
                    LoadHistoryBucket(
                        start=start + timedelta(minutes=offset),
                        energy_wh=mean_w * 0.5,
                        coverage_seconds=HISTORY_BUCKET_SECONDS,
                        source="statistics",
                    )
                )
        return sorted(buckets, key=lambda bucket: bucket.start)

    @staticmethod
    def _history_cutover(
        raw_buckets: list[LoadHistoryBucket],
    ) -> datetime | None:
        """Return the first complete hourly boundary covered by raw history."""
        if not raw_buckets:
            return None
        first = min(bucket.start for bucket in raw_buckets)
        return first.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)

    @staticmethod
    def _merge_history_buckets(
        statistics_buckets: list[LoadHistoryBucket],
        raw_buckets: list[LoadHistoryBucket],
        cutover: datetime | None,
    ) -> list[LoadHistoryBucket]:
        """Merge normalized sources without overlapping bucket starts."""
        if not raw_buckets:
            selected = statistics_buckets
        elif not statistics_buckets or cutover is None:
            selected = raw_buckets
        else:
            selected = [
                bucket for bucket in statistics_buckets if bucket.start < cutover
            ] + [
                bucket for bucket in raw_buckets if bucket.start >= cutover
            ]

        deduplicated: dict[datetime, LoadHistoryBucket] = {}
        for bucket in sorted(selected, key=lambda item: item.start):
            deduplicated[bucket.start] = bucket
        return list(deduplicated.values())

    @staticmethod
    def _subtract_ev_buckets(
        load_buckets: list[LoadHistoryBucket],
        ev_bucket_groups: list[list[LoadHistoryBucket]],
    ) -> list[LoadHistoryBucket]:
        """Subtract EV mean power only where matching normalized coverage exists."""
        ev_by_start: dict[datetime, float] = defaultdict(float)
        for buckets in ev_bucket_groups:
            for bucket in buckets:
                ev_by_start[bucket.start] += bucket.mean_w
        if not ev_by_start:
            return load_buckets
        return [
            LoadHistoryBucket(
                start=bucket.start,
                energy_wh=max(0.0, bucket.mean_w - ev_by_start.get(bucket.start, 0.0))
                * bucket.coverage_seconds
                / 3600.0,
                coverage_seconds=bucket.coverage_seconds,
                source=bucket.source,
            )
            for bucket in load_buckets
        ]

    @classmethod
    def _build_normalized_history(
        cls,
        raw_history: dict | None,
        statistics: dict | None,
        load_entity_id: str,
        ev_entity_ids: list[str],
        multipliers: dict[str, float],
        start_time: datetime,
        end_time: datetime,
        away_start: datetime | None,
        away_end: datetime | None,
    ) -> tuple[list[tuple[datetime, float]], dict[str, Any]]:
        """Normalize, EV-adjust, merge and away-filter Recorder load history."""
        raw_load = cls._states_to_half_hour_buckets(
            (raw_history or {}).get(load_entity_id, []),
            start_time,
            end_time,
            multipliers.get(load_entity_id, 1.0),
        )
        raw_ev = [
            cls._states_to_half_hour_buckets(
                (raw_history or {}).get(entity_id, []),
                start_time,
                end_time,
                multipliers.get(entity_id, 1.0),
                allow_zero=True,
            )
            for entity_id in ev_entity_ids
        ]
        raw_load = cls._subtract_ev_buckets(raw_load, raw_ev)

        statistic_load = cls._statistics_to_half_hour_buckets(
            (statistics or {}).get(load_entity_id),
            multipliers.get(load_entity_id, 1.0),
        )
        statistic_ev = [
            cls._statistics_to_half_hour_buckets(
                (statistics or {}).get(entity_id),
                multipliers.get(entity_id, 1.0),
            )
            for entity_id in ev_entity_ids
        ]
        statistic_load = cls._subtract_ev_buckets(statistic_load, statistic_ev)

        cutover = cls._history_cutover(raw_load)
        merged = cls._merge_history_buckets(statistic_load, raw_load, cutover)

        excluded = 0
        if away_start is not None and away_end is not None:
            before = len(merged)
            merged = [
                bucket
                for bucket in merged
                if not (
                    bucket.start < away_end
                    and bucket.start + timedelta(seconds=HISTORY_BUCKET_SECONDS) > away_start
                )
            ]
            excluded = before - len(merged)
            if merged:
                away_span = max(timedelta(0), away_end - away_start)
                cutoff = (
                    merged[-1].start
                    - timedelta(days=HISTORY_LOOKBACK_DAYS)
                    - away_span
                )
                merged = [bucket for bucket in merged if bucket.start >= cutoff]

        sources = {bucket.source for bucket in merged}
        diagnostics = {
            "history_source": (
                "merged" if len(sources) > 1 else next(iter(sources), "none")
            ),
            "raw_history_hours": round(len(raw_load) * 0.5, 1),
            "statistics_history_hours": round(len(statistic_load) * 0.5, 1),
            "history_cutover": cutover.isoformat() if cutover else None,
            "history_span_days": round(
                (merged[-1].start - merged[0].start).total_seconds() / 86400.0,
                1,
            ) if len(merged) > 1 else 0.0,
            "history_distinct_days": len(
                {
                    (dt_util.as_local(bucket.start) if bucket.start.tzinfo else bucket.start).date()
                    for bucket in merged
                }
            ),
            "away_excluded_buckets": excluded,
            "ev_history_entities": len(ev_entity_ids),
        }
        return [(bucket.start, bucket.mean_w) for bucket in merged], diagnostics

    @staticmethod
    def _distinct_date_samples(
        samples: list[tuple[datetime, float]],
    ) -> list[tuple[datetime, float]]:
        """Collapse duplicate updates so each local date contributes once per slot."""
        by_date: dict[Any, list[tuple[datetime, float]]] = defaultdict(list)
        for timestamp, value in samples:
            local_timestamp = dt_util.as_local(timestamp) if timestamp.tzinfo else timestamp
            by_date[local_timestamp.date()].append((timestamp, value))
        collapsed: list[tuple[datetime, float]] = []
        for date_samples in by_date.values():
            timestamp = max(sample[0] for sample in date_samples)
            value = sum(sample[1] for sample in date_samples) / len(date_samples)
            collapsed.append((timestamp, value))
        return sorted(collapsed, key=lambda sample: sample[0])

    async def get_forecast(
        self,
        horizon_hours: int = 48,
        start_time: datetime | None = None,
    ) -> list[float]:
        """
        Generate load forecast in Watts for each interval.

        Uses historical patterns, then falls back to a simple residential profile.

        Args:
            horizon_hours: Forecast horizon in hours
            start_time: Start time for forecast (default: now)

        Returns:
            List of load values in Watts for each interval
        """
        if start_time is None:
            start_time = dt_util.now()

        n_intervals = horizon_hours * 60 // self.interval_minutes

        # Fallback to historical pattern (with optional temperature adjustment)
        try:
            history = await self._get_load_history()
            if history:
                # Fetch temperature data and fit sensitivity if weather entity configured
                forecast_temps: list[tuple[datetime, float]] | None = None
                bucket_temp_avgs: dict | None = None
                alpha: float | None = None
                historical_temps: list[tuple[datetime, float]] | None = None
                if self.weather_entity_id:
                    (
                        forecast_temps,
                        bucket_temp_avgs,
                        alpha,
                        historical_temps,
                    ) = await self._get_temperature_adjustment(history, horizon_hours)
                # Building the forecast scans the complete normalized history
                # and applies the recent-regime adjustment. Keep that pure-CPU
                # work off the event loop.
                forecast = await self.hass.async_add_executor_job(
                    functools.partial(
                        self._forecast_from_history,
                        history, start_time, n_intervals,
                        forecast_temps=forecast_temps,
                        bucket_temp_averages=bucket_temp_avgs,
                        alpha=alpha,
                        historical_temps=historical_temps,
                    )
                )
                avg_w = sum(forecast) / len(forecast) if forecast else 0
                _LOGGER.info(
                    "Using historical load forecast (%d history points, avg %.0fW%s%s)",
                    len(history), avg_w,
                    ", temperature-adjusted" if alpha is not None else "",
                    ", recovery mode (vacation period excluded)" if self._in_recovery else "",
                )
                return forecast
        except Exception as e:
            _LOGGER.warning("Failed to get load history: %s", e)

        # Final fallback: use current load or default
        self._recent_load_diagnostics = {
            "mode": "fallback",
            "recent_matched_hours": 0.0,
            "local_scale_min": 1.0,
            "local_scale_max": 1.0,
            "adjusted_slot_count": 0,
        }
        current_load = self._get_current_load()
        _LOGGER.warning(
            "Using simple forecast fallback (%.0fW base) — "
            "no load history available for %s",
            current_load, self.load_entity_id,
        )
        return self._simple_forecast(current_load, start_time, n_intervals)

    async def _get_load_history(self) -> list[tuple[datetime, float]]:
        """Get historical load data from Home Assistant recorder.

        After Away Mode ends, the away window is excluded until it ages out of
        the 30-day lookback so the LP uses normal household patterns.
        """
        if not self.load_entity_id:
            self._history_diagnostics = {"history_source": "not_configured"}
            _LOGGER.debug("No load entity ID configured, skipping history")
            return []

        now = dt_util.utcnow()

        # Keep away timestamps until the away period is outside the history
        # window. The user-facing recovery concept remains 7 days via
        # _in_recovery, but local history still excludes recent away data.
        if (
            self.away_disabled_at
            and (now - self.away_disabled_at) >= timedelta(days=HISTORY_LOOKBACK_DAYS)
        ):
            _LOGGER.info("Away mode history window expired — clearing timestamps")
            self.away_enabled_at = None
            self.away_disabled_at = None

        away_end = self.away_disabled_at
        has_away_window = bool(
            self.away_enabled_at
            and away_end
            and away_end >= now - timedelta(days=HISTORY_LOOKBACK_DAYS)
        )
        if has_away_window:
            away_days = max(1, (away_end - self.away_enabled_at).days)
            days = min(90, away_days + HISTORY_LOOKBACK_DAYS)
        else:
            days = HISTORY_LOOKBACK_DAYS

        cache_key = (
            f"{self.load_entity_id}:en={self.away_enabled_at}"
            f":dis={self.away_disabled_at}:ev={','.join(self.ev_power_entity_ids)}"
        )

        # Check cache
        if (
            self._cache_time
            and now - self._cache_time < self._cache_duration
            and cache_key in self._history_cache
        ):
            return self._history_cache[cache_key]

        # Determine unit multiplier from current state. Match _get_current_load:
        # a missing/absent unit defaults to kW (PowerSync's own home-load sensor
        # reports kW), so kW history is not silently parsed 1000x low as Watts.
        # Only an explicit non-kW unit (e.g. "W") is treated as Watts.
        multiplier = 1000.0
        current_state = self.hass.states.get(self.load_entity_id)
        if current_state:
            unit = (current_state.attributes.get("unit_of_measurement") or "").strip().lower()
            if unit and unit != "kw":
                multiplier = 1.0
            _LOGGER.debug(
                "Load sensor %s: unit=%s, multiplier=%.0f",
                self.load_entity_id, unit, multiplier,
            )

        try:
            from homeassistant.components.recorder import get_instance
            from homeassistant.components.recorder.history import get_significant_states
            from homeassistant.components.recorder.statistics import (
                statistics_during_period,
            )

            instance = get_instance(self.hass)

            start_time = now - timedelta(days=days)
            end_time = now

            history_entity_ids = [self.load_entity_id, *self.ev_power_entity_ids]
            multipliers = {self.load_entity_id: multiplier}
            multipliers.update(self._resolve_power_multipliers(self.ev_power_entity_ids))

            raw_history: dict = {}
            statistics: dict = {}
            try:
                raw_history = await instance.async_add_executor_job(
                    get_significant_states,
                    self.hass,
                    start_time,
                    end_time,
                    history_entity_ids,
                )
            except Exception as exc:
                _LOGGER.warning(
                    "Raw Recorder load history unavailable for %s: %s",
                    self.load_entity_id,
                    exc,
                )

            try:
                statistics = await instance.async_add_executor_job(
                    statistics_during_period,
                    self.hass,
                    start_time,
                    end_time,
                    set(history_entity_ids),
                    "hour",
                    None,
                    {"mean"},
                )
            except Exception as exc:
                _LOGGER.debug(
                    "Hourly Recorder statistics unavailable for %s; using raw history: %s",
                    self.load_entity_id,
                    exc,
                )

            result, diagnostics = await self.hass.async_add_executor_job(
                self._build_normalized_history,
                raw_history,
                statistics,
                self.load_entity_id,
                self.ev_power_entity_ids,
                multipliers,
                start_time,
                end_time,
                self.away_enabled_at if has_away_window else None,
                away_end if has_away_window else None,
            )
            self._history_diagnostics = diagnostics

            if not result:
                _LOGGER.warning("No usable Recorder history found for %s", self.load_entity_id)
                return []

            # Cache the result
            self._history_cache[cache_key] = result
            self._cache_time = now

            if result:
                avg_w = sum(v for _, v in result) / len(result)
                _LOGGER.info(
                    "Loaded %d normalized history buckets for %s "
                    "(avg %.0fW, %.1f days, source=%s, raw=%.1fh, statistics=%.1fh%s)",
                    len(result), self.load_entity_id, avg_w, days,
                    diagnostics.get("history_source"),
                    diagnostics.get("raw_history_hours", 0.0),
                    diagnostics.get("statistics_history_hours", 0.0),
                    (
                        ", away window excluded "
                        f"({diagnostics.get('away_excluded_buckets', 0)} buckets)"
                        if has_away_window else ""
                    ),
                )
            return result

        except ImportError:
            self._history_diagnostics = {"history_source": "recorder_unavailable"}
            _LOGGER.warning("Recorder not available for load history")
            return []
        except Exception as e:
            self._history_diagnostics = {"history_source": "error"}
            _LOGGER.error("Error fetching load history for %s: %s", self.load_entity_id, e)
            return []

    def _forecast_from_history(
        self,
        history: list[tuple[datetime, float]],
        start_time: datetime,
        n_intervals: int,
        forecast_temps: list[tuple[datetime, float]] | None = None,
        bucket_temp_averages: dict | None = None,
        alpha: float | None = None,
        historical_temps: list[tuple[datetime, float]] | None = None,
    ) -> list[float]:
        """Generate forecast using historical pattern matching with optional temperature scaling.

        forecast_temps: hourly (datetime, temp_c) pairs for the forecast horizon
        bucket_temp_averages: (dow, hour, half_hour) -> historical avg temp_c
        alpha: sensitivity coefficient — load changes alpha*100% per °C deviation
        """
        # Group by (day_of_week, hour, half_hour)
        pattern: dict[tuple[int, int, int], list[tuple[datetime, float]]] = defaultdict(list)
        all_samples: list[tuple[datetime, float]] = []

        for timestamp, value in history:
            # Convert UTC timestamps to local time for correct time-of-day matching
            local_ts = dt_util.as_local(timestamp) if timestamp.tzinfo else timestamp
            dow = local_ts.weekday()
            hour = local_ts.hour
            half_hour = 0 if local_ts.minute < 30 else 1
            key = (dow, hour, half_hour)
            sample = (timestamp, value)
            pattern[key].append(sample)
            all_samples.append(sample)

        # Build hourly forecast-temp lookup (slot_local_hour -> temp_c) for O(1) per slot
        temp_map: dict[datetime, float] = {}
        if forecast_temps and alpha is not None and bucket_temp_averages is not None:
            for ft_ts, ft_temp in forecast_temps:
                local_ft = dt_util.as_local(ft_ts) if ft_ts.tzinfo else ft_ts
                slot_hour = local_ft.replace(minute=0, second=0, microsecond=0)
                temp_map[slot_hour] = ft_temp

        # Generate forecast
        forecast = []
        current_time = start_time

        for _ in range(n_intervals):
            local_cur = dt_util.as_local(current_time) if current_time.tzinfo else current_time
            dow = local_cur.weekday()
            hour = local_cur.hour
            half_hour = 0 if local_cur.minute < 30 else 1
            key = (dow, hour, half_hour)

            base = self._history_bucket_forecast(
                pattern,
                all_samples,
                dow,
                hour,
                half_hour,
                current_time,
            )

            # Temperature scaling
            if temp_map and bucket_temp_averages is not None and alpha is not None:
                slot_hour = local_cur.replace(minute=0, second=0, microsecond=0)
                t_cast = temp_map.get(slot_hour)
                mu_temp = bucket_temp_averages.get(key)
                if t_cast is not None and mu_temp is not None:
                    delta_t = t_cast - mu_temp
                    scale = max(0.5, min(2.5, 1.0 + alpha * delta_t))
                    base = base * scale

            forecast.append(base)
            current_time += timedelta(minutes=self.interval_minutes)

        # Apply smoothing
        forecast = self._smooth_forecast(forecast)

        if self.away_mode:
            recent_scale = self._recent_load_scale(history, start_time)
            if recent_scale is not None:
                forecast = [value * recent_scale for value in forecast]
            self._recent_load_diagnostics = {
                "mode": "away_global",
                "global_scale": round(recent_scale or 1.0, 3),
            }
        else:
            horizon_starts = [
                start_time + timedelta(minutes=self.interval_minutes * index)
                for index in range(n_intervals)
            ]
            recent_scales = self._recent_load_scales(
                history,
                start_time,
                horizon_starts,
                historical_temps=historical_temps,
                bucket_temp_averages=bucket_temp_averages,
                alpha=alpha,
            )
            if len(recent_scales) != len(forecast):
                _LOGGER.warning(
                    "Recent load scale length mismatch (%d != %d); using neutral scales",
                    len(recent_scales),
                    len(forecast),
                )
                recent_scales = [1.0] * len(forecast)
            forecast = [
                value * recent_scales[index]
                for index, value in enumerate(forecast)
            ]

        return forecast

    def _recent_load_scales(
        self,
        history: list[tuple[datetime, float]],
        start_time: datetime,
        horizon_starts: list[datetime],
        *,
        historical_temps: list[tuple[datetime, float]] | None = None,
        bucket_temp_averages: dict[tuple[int, int, int], float] | None = None,
        alpha: float | None = None,
    ) -> list[float]:
        """Return confidence-qualified clock-time-local recent load scales."""
        neutral = [1.0] * len(horizon_starts)
        if not history or not horizon_starts:
            self._recent_load_diagnostics = {
                "mode": "clock_time_local",
                "recent_matched_hours": 0.0,
                "local_scale_min": 1.0,
                "local_scale_max": 1.0,
                "adjusted_slot_count": 0,
            }
            return neutral

        ref_time = dt_util.as_local(start_time) if start_time.tzinfo else start_time
        recent_start = ref_time - timedelta(hours=RECENT_LOAD_WINDOW_HOURS)
        baseline_end = recent_start - timedelta(hours=RECENT_LOAD_BASELINE_EXCLUDE_HOURS)
        older_pattern: dict[
            tuple[int, int, int], list[tuple[datetime, float]]
        ] = defaultdict(list)
        recent_samples: list[tuple[datetime, float]] = []

        for timestamp, value in history:
            sample_time = dt_util.as_local(timestamp) if timestamp.tzinfo else timestamp
            if sample_time < baseline_end:
                key = (
                    sample_time.weekday(),
                    sample_time.hour,
                    0 if sample_time.minute < 30 else 1,
                )
                older_pattern[key].append((timestamp, value))
            elif recent_start <= sample_time <= ref_time:
                recent_samples.append((sample_time, value))

        sorted_temps = sorted(historical_temps or [], key=lambda item: item[0])
        temp_timestamps = [timestamp for timestamp, _ in sorted_temps]
        observed_wh: dict[int, float] = defaultdict(float)
        expected_wh: dict[int, float] = defaultdict(float)
        sample_ratios: dict[int, list[float]] = defaultdict(list)
        baseline_date_counts: dict[int, list[int]] = defaultdict(list)

        for sample_time, actual_w in recent_samples:
            key = (
                sample_time.weekday(),
                sample_time.hour,
                0 if sample_time.minute < 30 else 1,
            )
            exact_samples = self._clip_outliers(
                self._distinct_date_samples(older_pattern.get(key, []))
            )
            if len(exact_samples) < MIN_EXACT_BUCKET_SAMPLES:
                continue
            expected_w = self._weighted_average(exact_samples, sample_time)
            if expected_w is None or expected_w <= 0:
                continue

            if (
                sorted_temps
                and bucket_temp_averages is not None
                and alpha is not None
                and key in bucket_temp_averages
            ):
                temperature = self._nearest_temperature(
                    sample_time,
                    sorted_temps,
                    temp_timestamps,
                )
                if temperature is not None:
                    temperature_scale = max(
                        0.5,
                        min(
                            2.5,
                            1.0
                            + alpha
                            * (temperature - bucket_temp_averages[key]),
                        ),
                    )
                    expected_w *= temperature_scale

            slot = sample_time.hour * 2 + (1 if sample_time.minute >= 30 else 0)
            sample_energy_wh = actual_w * 0.5
            expected_energy_wh = expected_w * 0.5
            if expected_energy_wh < RECENT_LOAD_MIN_EXPECTED_WH:
                continue
            observed_wh[slot] += sample_energy_wh
            expected_wh[slot] += expected_energy_wh
            sample_ratios[slot].append(sample_energy_wh / expected_energy_wh)
            baseline_date_counts[slot].append(len(exact_samples))

        scale_by_slot: dict[int, float] = {}
        matched_samples = sum(len(ratios) for ratios in sample_ratios.values())
        for slot, actual_energy in observed_wh.items():
            expected_energy = expected_wh.get(slot, 0.0)
            ratios = sample_ratios.get(slot, [])
            if expected_energy <= 0 or not ratios:
                continue
            ratio = actual_energy / expected_energy
            if abs(ratio - 1.0) < RECENT_LOAD_DEADBAND:
                continue

            coverage_confidence = min(
                1.0,
                len(ratios) / RECENT_LOAD_FULL_CONFIDENCE_SAMPLES,
            )
            date_count = int(self._median(baseline_date_counts[slot]))
            date_confidence = min(
                1.0,
                date_count / RECENT_LOAD_FULL_CONFIDENCE_DATES,
            )
            stability_confidence = self._recent_ratio_stability(ratios)
            confidence = coverage_confidence * date_confidence * stability_confidence
            candidate = max(
                RECENT_LOAD_MIN_SCALE,
                min(RECENT_LOAD_MAX_SCALE, ratio),
            )
            scale = 1.0 + confidence * (candidate - 1.0)
            scale_by_slot[slot] = max(
                RECENT_LOAD_MIN_SCALE,
                min(RECENT_LOAD_MAX_SCALE, scale),
            )

        scales = []
        for timestamp in horizon_starts:
            local_timestamp = dt_util.as_local(timestamp) if timestamp.tzinfo else timestamp
            slot = local_timestamp.hour * 2 + (1 if local_timestamp.minute >= 30 else 0)
            scales.append(scale_by_slot.get(slot, 1.0))

        adjusted = [scale for scale in scales if abs(scale - 1.0) >= 0.001]
        self._recent_load_diagnostics = {
            "mode": "clock_time_local",
            "recent_matched_hours": round(matched_samples * 0.5, 1),
            "local_scale_min": round(min(scales) if scales else 1.0, 3),
            "local_scale_max": round(max(scales) if scales else 1.0, 3),
            "adjusted_slot_count": len(adjusted),
            "baseline_date_count_min": min(
                (min(values) for values in baseline_date_counts.values()),
                default=0,
            ),
            "baseline_date_count_max": max(
                (max(values) for values in baseline_date_counts.values()),
                default=0,
            ),
        }
        if adjusted:
            _LOGGER.info(
                "Recent clock-time load adjustment: matched=%.1fh, "
                "adjusted_slots=%d, scale=%.2f-%.2fx",
                self._recent_load_diagnostics["recent_matched_hours"],
                self._recent_load_diagnostics["adjusted_slot_count"],
                self._recent_load_diagnostics["local_scale_min"],
                self._recent_load_diagnostics["local_scale_max"],
            )
        return scales

    @staticmethod
    def _recent_ratio_stability(ratios: list[float]) -> float:
        """Return robust confidence in repeated recent time-local ratios."""
        if len(ratios) < 2:
            return 0.5
        median = LoadEstimator._median(ratios)
        if median <= 0:
            return 0.0
        mad = LoadEstimator._median([abs(ratio - median) for ratio in ratios])
        relative_mad = mad / median
        return max(0.0, min(1.0, 1.0 - relative_mad / 0.5))

    @staticmethod
    def _nearest_temperature(
        timestamp: datetime,
        sorted_temps: list[tuple[datetime, float]],
        sorted_timestamps: list[datetime],
    ) -> float | None:
        """Return the nearest temperature within two hours."""
        index = bisect.bisect_left(sorted_timestamps, timestamp)
        best_temperature = None
        best_gap = 7200.0
        for candidate in (index - 1, index):
            if 0 <= candidate < len(sorted_temps):
                temp_time, temperature = sorted_temps[candidate]
                gap = abs((timestamp - temp_time).total_seconds())
                if gap < best_gap:
                    best_gap = gap
                    best_temperature = temperature
        return best_temperature

    def _recent_load_scale(
        self,
        history: list[tuple[datetime, float]],
        start_time: datetime,
    ) -> float | None:
        """Return a recent-regime multiplier when load has clearly shifted.

        The day/time pattern and weather model are deliberately still the base
        forecast. This multiplier catches step changes such as the first cold
        snap of winter where the 30-day history is too slow to move.
        """
        if not history:
            return None

        ref_time = dt_util.as_local(start_time) if start_time.tzinfo else start_time
        recent_end = ref_time
        active_away = self.away_mode and self.away_enabled_at is not None
        if not active_away:
            return None
        away_start = (
            dt_util.as_local(self.away_enabled_at)
            if self.away_enabled_at.tzinfo
            else self.away_enabled_at
        )
        if ref_time.tzinfo and not away_start.tzinfo:
            away_start = away_start.replace(tzinfo=ref_time.tzinfo)
        elif away_start.tzinfo and not ref_time.tzinfo:
            away_start = away_start.replace(tzinfo=None)
        recent_start = max(
            away_start,
            recent_end - timedelta(hours=RECENT_LOAD_WINDOW_HOURS),
        )
        baseline_end = away_start

        older_pattern: dict[tuple[int, int, int], list[tuple[datetime, float]]] = defaultdict(list)
        recent_samples: list[tuple[datetime, float]] = []
        actual_values: list[float] = []
        expected_values: list[float] = []
        matched_timestamps: list[datetime] = []

        # Single pass over the full history: bucket the older baseline samples
        # and collect the (much smaller) recent window. Converting each timestamp
        # to local time is the per-point cost, so doing it once here rather than
        # in two separate full scans halves the work on a large history.
        for ts, value in history:
            sample_time = dt_util.as_local(ts) if ts.tzinfo else ts
            if sample_time < baseline_end:
                key = (
                    sample_time.weekday(),
                    sample_time.hour,
                    0 if sample_time.minute < 30 else 1,
                )
                older_pattern[key].append((ts, value))
            elif recent_start <= sample_time <= recent_end:
                recent_samples.append((sample_time, value))

        for sample_time, value in recent_samples:
            key = (
                sample_time.weekday(),
                sample_time.hour,
                0 if sample_time.minute < 30 else 1,
            )
            exact_samples = self._clip_outliers(older_pattern.get(key, []))
            if len(exact_samples) < MIN_EXACT_BUCKET_SAMPLES:
                continue

            expected = self._weighted_average(exact_samples, sample_time)
            if expected is None or expected <= 0:
                continue

            actual_values.append(value)
            expected_values.append(expected)
            matched_timestamps.append(sample_time)

        matched_coverage = 0.0
        if len(matched_timestamps) > 1:
            matched_coverage = (
                max(matched_timestamps) - min(matched_timestamps)
            ).total_seconds() / 3600.0

        if not actual_values or matched_coverage < RECENT_LOAD_MIN_COVERAGE_HOURS:
            return None

        ratios = [
            actual / expected
            for actual, expected in zip(actual_values, expected_values)
            if expected > 0
        ]
        if not ratios:
            return None

        recent_avg = sum(actual_values) / len(actual_values)
        baseline_avg = sum(expected_values) / len(expected_values)
        ratio = self._median(ratios)
        if abs(ratio - 1.0) < RECENT_LOAD_DEADBAND:
            return None

        blend = ACTIVE_AWAY_LOAD_BLEND if active_away else RECENT_LOAD_BLEND
        min_scale = ACTIVE_AWAY_LOAD_MIN_SCALE if active_away else RECENT_LOAD_MIN_SCALE
        scale = 1.0 + (ratio - 1.0) * blend
        scale = max(min_scale, min(RECENT_LOAD_MAX_SCALE, scale))
        _LOGGER.info(
            "%sload regime adjustment: recent=%.0fW over %.1fh, "
            "matched_history=%.0fW, median_ratio=%.2fx, scale=%.2fx",
            "Away mode " if active_away else "Recent ",
            recent_avg,
            matched_coverage,
            baseline_avg,
            ratio,
            scale,
        )
        return scale

    def _history_bucket_forecast(
        self,
        pattern: dict[tuple[int, int, int], list[tuple[datetime, float]]],
        all_samples: list[tuple[datetime, float]],
        dow: int,
        hour: int,
        half_hour: int,
        reference_time: datetime,
    ) -> float:
        """Return robust weighted load estimate for a day/time bucket."""
        key = (dow, hour, half_hour)
        exact_samples = self._clip_outliers(
            self._distinct_date_samples(pattern.get(key, []))
        )
        if len(exact_samples) >= MIN_EXACT_BUCKET_SAMPLES:
            exact = self._weighted_average(exact_samples, reference_time)
            if exact is not None:
                return exact

        same_type_days = [5, 6] if dow >= 5 else [0, 1, 2, 3, 4]
        if len(exact_samples) == 1:
            exact = self._weighted_average(exact_samples, reference_time)
            fallback = self._weighted_average_for_days(
                pattern,
                [d for d in same_type_days if d != dow],
                hour,
                half_hour,
                reference_time,
            )
            if exact is not None and fallback is not None:
                return (
                    exact * SINGLE_EXACT_BUCKET_WEIGHT
                    + fallback * (1.0 - SINGLE_EXACT_BUCKET_WEIGHT)
                )

        fallback = self._weighted_average_for_days(
            pattern,
            same_type_days,
            hour,
            half_hour,
            reference_time,
        )
        if fallback is not None:
            return fallback

        fallback = self._weighted_average_for_days(
            pattern,
            range(7),
            hour,
            half_hour,
            reference_time,
        )
        if fallback is not None:
            return fallback

        global_average = self._weighted_average(all_samples, reference_time)
        return global_average if global_average is not None else 500.0

    def _weighted_average_for_days(
        self,
        pattern: dict[tuple[int, int, int], list[tuple[datetime, float]]],
        days: list[int] | range,
        hour: int,
        half_hour: int,
        reference_time: datetime,
    ) -> float | None:
        """Return robust weighted average for matching days at a time bucket."""
        samples: list[tuple[datetime, float]] = []
        for day in days:
            samples.extend(pattern.get((day, hour, half_hour), []))
        return self._weighted_average(
            self._distinct_date_samples(samples),
            reference_time,
        )

    def _weighted_average(
        self,
        samples: list[tuple[datetime, float]],
        reference_time: datetime,
    ) -> float | None:
        """Calculate a robust recency-weighted average for load samples."""
        clipped = self._clip_outliers(samples)
        if not clipped:
            return None

        ref_time = dt_util.as_local(reference_time) if reference_time.tzinfo else reference_time
        weighted_total = 0.0
        weight_total = 0.0
        for ts, value in clipped:
            sample_time = dt_util.as_local(ts) if ts.tzinfo else ts
            if ref_time.tzinfo and not sample_time.tzinfo:
                sample_time = sample_time.replace(tzinfo=ref_time.tzinfo)
            elif sample_time.tzinfo and not ref_time.tzinfo:
                sample_time = sample_time.replace(tzinfo=None)
            age_days = max(0.0, (ref_time - sample_time).total_seconds() / 86400.0)
            weight = 0.5 ** (age_days / RECENCY_HALF_LIFE_DAYS)
            weighted_total += value * weight
            weight_total += weight

        if weight_total <= 0:
            return None
        return weighted_total / weight_total

    def _clip_outliers(
        self,
        samples: list[tuple[datetime, float]],
    ) -> list[tuple[datetime, float]]:
        """Remove extreme bucket samples using a MAD-style threshold."""
        if len(samples) < OUTLIER_MIN_SAMPLES:
            return samples

        values = [value for _, value in samples]
        median = self._median(values)
        deviations = [abs(value - median) for value in values]
        mad = self._median(deviations)
        threshold = max(
            OUTLIER_MIN_THRESHOLD_W,
            abs(median) * OUTLIER_MEDIAN_FRACTION,
            3.0 * MAD_NORMAL_SCALE * mad,
        )
        clipped = [
            sample
            for sample in samples
            if abs(sample[1] - median) <= threshold
        ]
        return clipped or samples

    @staticmethod
    def _median(values: list[float]) -> float:
        """Return median for a non-empty value list."""
        ordered = sorted(values)
        mid = len(ordered) // 2
        if len(ordered) % 2:
            return ordered[mid]
        return (ordered[mid - 1] + ordered[mid]) / 2.0

    async def _get_temperature_adjustment(
        self,
        history: list[tuple[datetime, float]],
        horizon_hours: int,
    ) -> tuple[
        list[tuple[datetime, float]] | None,
        dict | None,
        float | None,
        list[tuple[datetime, float]] | None,
    ]:
        """Return forecast/history temperatures and the fitted adjustment model.

        Uses a 1-hour cache for the fitted alpha. Returns neutral values if
        temperature data is unavailable or the fit is too weak to be useful.
        """
        now = dt_util.utcnow()

        # Use cached alpha if still warm (re-fetch forecast temps each time — cheap)
        if (
            self._temp_alpha_fitted
            and self._temp_cache_time
            and now - self._temp_cache_time < self._cache_duration
        ):
            if self._temp_alpha is None:
                return None, None, None, self._temp_history or None
            forecast_temps = await self._fetch_forecast_temperatures(horizon_hours)
            return (
                forecast_temps or None,
                self._temp_bucket_averages,
                self._temp_alpha,
                self._temp_history or None,
            )

        # Fetch historical temperatures matching the filtered load history
        # window used by the forecast model.
        hist_start = min(ts for ts, _ in history)
        hist_end = max(ts for ts, _ in history)

        temp_history = await self._fetch_historical_temperatures(hist_start, hist_end)
        if not temp_history:
            self._temp_alpha = None
            self._temp_bucket_averages = None
            self._temp_history = []
            self._temp_alpha_fitted = True
            self._temp_cache_time = now
            return None, None, None, None

        # Temperature fitting scans the complete normalized load history and
        # performs a bisect per bucket. Keep it off the event loop.
        bucket_temp_avgs, alpha = await self.hass.async_add_executor_job(
            self._compute_temperature_fit, history, temp_history
        )

        self._temp_alpha = alpha
        self._temp_bucket_averages = bucket_temp_avgs
        self._temp_history = temp_history
        self._temp_alpha_fitted = True
        self._temp_cache_time = now

        if alpha is None:
            return None, None, None, temp_history

        # Fetch forecast temperatures
        forecast_temps = await self._fetch_forecast_temperatures(horizon_hours)
        return forecast_temps or None, bucket_temp_avgs, alpha, temp_history

    async def _fetch_historical_temperatures(
        self,
        start: datetime,
        end: datetime,
    ) -> list[tuple[datetime, float]]:
        """Query recorder for outdoor temperature from the configured weather entity."""
        if not self.weather_entity_id:
            return []
        try:
            from homeassistant.components.recorder import get_instance
            from homeassistant.components.recorder.history import get_significant_states

            instance = get_instance(self.hass)
            history = await instance.async_add_executor_job(
                get_significant_states,
                self.hass,
                start,
                end,
                [self.weather_entity_id],
            )
            if not history or self.weather_entity_id not in history:
                return []

            result = []
            for state in history[self.weather_entity_id]:
                temp = state.attributes.get("temperature")
                if temp is not None:
                    try:
                        result.append((state.last_changed, float(temp)))
                    except (ValueError, TypeError):
                        continue
            return sorted(result, key=lambda x: x[0])
        except Exception as e:
            _LOGGER.warning("Failed to fetch temperature history from %s: %s", self.weather_entity_id, e)
            return []

    async def _fetch_forecast_temperatures(
        self,
        horizon_hours: int = 48,
    ) -> list[tuple[datetime, float]]:
        """Fetch hourly forecast temperature via weather.get_forecasts service."""
        if not self.weather_entity_id or self._get_forecasts_unsupported:
            return []

        # Determine which forecast types the entity supports before calling the service.
        # WeatherEntityFeature: FORECAST_DAILY=1, FORECAST_HOURLY=2
        state = self.hass.states.get(self.weather_entity_id)
        supported = int((state.attributes.get("supported_features") or 0) if state else 0)
        FORECAST_HOURLY = 2
        FORECAST_DAILY = 1
        if supported and not (supported & (FORECAST_HOURLY | FORECAST_DAILY)):
            _LOGGER.debug(
                "%s reports no forecast support (supported_features=%d) — temperature forecast disabled",
                self.weather_entity_id, supported,
            )
            self._get_forecasts_unsupported = True
            return []

        forecast_type = "hourly" if (not supported or supported & FORECAST_HOURLY) else "daily"

        try:
            resp = await self.hass.services.async_call(
                "weather",
                "get_forecasts",
                {"entity_id": self.weather_entity_id, "type": forecast_type},
                blocking=True,
                return_response=True,
            )
            if not resp or self.weather_entity_id not in resp:
                # Try daily as fallback if hourly was attempted and returned nothing.
                if forecast_type == "hourly" and supported & FORECAST_DAILY:
                    resp = await self.hass.services.async_call(
                        "weather",
                        "get_forecasts",
                        {"entity_id": self.weather_entity_id, "type": "daily"},
                        blocking=True,
                        return_response=True,
                    )
                if not resp or self.weather_entity_id not in resp:
                    return []
            forecasts = resp[self.weather_entity_id].get("forecast", [])
            cutoff = dt_util.utcnow() + timedelta(hours=horizon_hours)
            result = []
            for entry in forecasts:
                dt_str = entry.get("datetime")
                temp = entry.get("temperature")
                if dt_str is None or temp is None:
                    continue
                try:
                    from datetime import datetime as _dt
                    ft = _dt.fromisoformat(dt_str)
                    if ft.tzinfo is None:
                        ft = dt_util.as_utc(ft)
                    if ft > cutoff:
                        break
                    result.append((ft, float(temp)))
                except (ValueError, TypeError):
                    continue
            return result
        except Exception as e:
            _LOGGER.debug(
                "weather.get_forecasts unsupported for %s — temperature forecast disabled: %s",
                self.weather_entity_id, e,
            )
            self._get_forecasts_unsupported = True
            return []

    def _compute_bucket_temp_averages(
        self,
        temp_history: list[tuple[datetime, float]],
    ) -> dict[tuple[int, int, int], float]:
        """Group temperature history into (dow, hour, half_hour) buckets and average."""
        bucket: dict[tuple[int, int, int], list[float]] = defaultdict(list)
        for ts, temp_c in temp_history:
            local_ts = dt_util.as_local(ts) if ts.tzinfo else ts
            key = (local_ts.weekday(), local_ts.hour, 0 if local_ts.minute < 30 else 1)
            bucket[key].append(temp_c)
        return {k: sum(v) / len(v) for k, v in bucket.items()}

    def _resolve_power_multipliers(
        self, entity_ids: list[str]
    ) -> dict[str, float]:
        """Return a W multiplier per entity from its current unit (kW -> 1000).

        Runs on the event loop (reads hass.states). Charger power sensors
        usually report Watts; only an explicit "kW" unit is scaled. Defaulting
        an unknown unit to Watts is deliberately conservative — it can only
        under-subtract EV power (leaving some double-count), never over-subtract
        (which would under-forecast household load).
        """
        multipliers: dict[str, float] = {}
        for eid in entity_ids:
            state = self.hass.states.get(eid)
            unit = ""
            if state is not None:
                unit = (
                    state.attributes.get("unit_of_measurement") or ""
                ).strip().lower()
            multipliers[eid] = 1000.0 if unit == "kw" else 1.0
        return multipliers

    def _compute_temperature_fit(
        self,
        history: list[tuple[datetime, float]],
        temp_history: list[tuple[datetime, float]],
    ) -> tuple[dict[tuple[int, int, int], float] | None, float | None]:
        """Bucket the load history and fit temperature sensitivity (executor-only).

        Iterates the full normalized history and runs a bisect per bucket, so it
        must not run on the event loop. It operates only on passed-in data and is
        safe in a worker thread.
        """
        load_pattern: dict[tuple[int, int, int], list[float]] = defaultdict(list)
        for ts, val in history:
            local_ts = dt_util.as_local(ts) if ts.tzinfo else ts
            key = (local_ts.weekday(), local_ts.hour, 0 if local_ts.minute < 30 else 1)
            load_pattern[key].append(val)
        bucket_averages = {k: sum(v) / len(v) for k, v in load_pattern.items()}

        bucket_temp_avgs = self._compute_bucket_temp_averages(temp_history)
        alpha = self._fit_temperature_sensitivity(
            history, temp_history, bucket_averages, bucket_temp_avgs
        )
        return bucket_temp_avgs, alpha

    def _fit_temperature_sensitivity(
        self,
        history: list[tuple[datetime, float]],
        temp_history: list[tuple[datetime, float]],
        bucket_averages: dict[tuple[int, int, int], float],
        bucket_temp_averages: dict[tuple[int, int, int], float],
    ) -> float | None:
        """Fit a global linear sensitivity coefficient α.

        α is the fraction of bucket-average load that changes per °C of temperature
        deviation from the bucket-average temperature:
            load_adj = bucket_avg × (1 + α × ΔT)

        Uses closed-form regression through the origin on (ΔT, fractional_load_deviation).
        Returns None if data is insufficient or the fit is too weak.
        """
        if not temp_history:
            return None

        # Build sorted temp list for nearest-neighbour lookup
        sorted_temps = sorted(temp_history, key=lambda x: x[0])
        sorted_timestamps = [t for t, _ in sorted_temps]

        sum_xy = 0.0
        sum_xx = 0.0
        n_pairs = 0

        for ts, load_w in history:
            local_ts = dt_util.as_local(ts) if ts.tzinfo else ts
            key = (local_ts.weekday(), local_ts.hour, 0 if local_ts.minute < 30 else 1)
            mu_load = bucket_averages.get(key)
            mu_temp = bucket_temp_averages.get(key)
            if mu_load is None or mu_temp is None or mu_load <= 0:
                continue

            # Find nearest temperature reading within a 2-hour window
            idx = bisect.bisect_left(sorted_timestamps, ts)
            temp_c = None
            best_gap = 7200  # 2-hour tolerance in seconds
            for i in [idx - 1, idx]:
                if 0 <= i < len(sorted_temps):
                    t, tc = sorted_temps[i]
                    gap = abs((ts - t).total_seconds())
                    if gap < best_gap:
                        best_gap = gap
                        temp_c = tc

            if temp_c is None:
                continue

            y = (load_w - mu_load) / mu_load  # Fractional load deviation
            x = temp_c - mu_temp              # °C deviation from slot avg

            sum_xy += x * y
            sum_xx += x * x
            n_pairs += 1

        if n_pairs < 50 or sum_xx < 0.1:
            _LOGGER.debug(
                "Temperature sensitivity: insufficient data (%d pairs, sum_xx=%.3f), skipping",
                n_pairs, sum_xx,
            )
            return None

        alpha = sum_xy / sum_xx
        # Clamp: load rarely drops below 50% in cold; AC can scale 2.5× in heat
        alpha = max(-0.02, min(0.15, alpha))

        if abs(alpha) < 0.005:
            _LOGGER.debug("Temperature sensitivity too weak (α=%.4f), skipping", alpha)
            return None

        _LOGGER.info(
            "Temperature sensitivity fitted: α=%.4f/°C from %d data pairs",
            alpha, n_pairs,
        )
        return alpha

    def invalidate_cache(self) -> None:
        """Invalidate history and temperature caches (e.g. when away_mode changes)."""
        self._history_cache.clear()
        self._cache_time = None
        self._temp_bucket_averages = None
        self._temp_history = []
        self._temp_alpha_fitted = False
        self._temp_cache_time = None

    def _get_current_load(self) -> float:
        """Get current load from Home Assistant state."""
        if not self.load_entity_id:
            return 500.0  # Default 500W

        try:
            state = self.hass.states.get(self.load_entity_id)
            if state and state.state not in ("unknown", "unavailable"):
                # Load is typically in kW, convert to W
                value = float(state.state)
                # Check unit - if already in W, use as-is; if in kW, convert
                unit = state.attributes.get("unit_of_measurement", "kW")
                if unit.lower() == "kw":
                    value *= 1000
                # Sanity check: residential load should be < 100kW
                if value > 100_000:
                    _LOGGER.warning(
                        "Load sensor %s returned implausible value %.0fW, using default",
                        self.load_entity_id, value,
                    )
                    return 500.0
                return max(0, value)
        except (ValueError, TypeError, AttributeError):
            pass

        return 500.0  # Default

    def _simple_forecast(
        self,
        base_load: float,
        start_time: datetime,
        n_intervals: int,
    ) -> list[float]:
        """
        Generate simple forecast based on typical daily pattern.

        Uses a generic residential load profile when no history is available.
        """
        forecast = []
        current_time = start_time

        # Generic residential load pattern (multipliers by hour)
        # Lower at night, peaks in morning and evening
        hourly_pattern = {
            0: 0.4, 1: 0.3, 2: 0.3, 3: 0.3, 4: 0.3, 5: 0.4,
            6: 0.6, 7: 0.8, 8: 0.9, 9: 0.8, 10: 0.7, 11: 0.7,
            12: 0.8, 13: 0.7, 14: 0.6, 15: 0.6, 16: 0.7, 17: 0.9,
            18: 1.2, 19: 1.3, 20: 1.2, 21: 1.0, 22: 0.7, 23: 0.5,
        }

        for _ in range(n_intervals):
            hour = current_time.hour
            multiplier = hourly_pattern.get(hour, 0.7)
            forecast.append(base_load * multiplier)
            current_time += timedelta(minutes=self.interval_minutes)

        return self._smooth_forecast(forecast)

    def _smooth_forecast(self, values: list[float], window: int = 3) -> list[float]:
        """Apply simple moving average smoothing to forecast."""
        if len(values) <= window:
            return values

        smoothed = []
        for i in range(len(values)):
            start = max(0, i - window // 2)
            end = min(len(values), i + window // 2 + 1)
            smoothed.append(sum(values[start:end]) / (end - start))

        return smoothed

    async def get_average_daily_load(self) -> float:
        """Get average daily load in kWh."""
        history = await self._get_load_history()
        if not history:
            return 15.0  # Default 15 kWh/day

        # Calculate average power in W
        avg_power = sum(v for _, v in history) / len(history)

        # Convert to daily kWh
        return avg_power * 24 / 1000


class SolcastForecaster:
    """
    Wrapper for solar production forecasts.

    Retrieves solar production forecasts from supported Home Assistant
    integrations, using the configured provider preference and falling back to
    the other supported provider when available.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        solcast_entity: str | None = None,
        interval_minutes: int = 5,
        estimate_type: str = DEFAULT_SOLCAST_ESTIMATE_TYPE,
        provider_preference: str = DEFAULT_SOLAR_FORECAST_PROVIDER,
    ):
        """
        Initialize Solcast forecaster.

        Args:
            hass: Home Assistant instance
            solcast_entity: Solcast sensor entity ID
            interval_minutes: Forecast interval in minutes
            estimate_type: Solcast estimate to use: estimate, estimate10, or estimate90
            provider_preference: Preferred forecast source: solcast or open_meteo
        """
        self.hass = hass
        self.solcast_entity = solcast_entity
        self.interval_minutes = interval_minutes
        self.provider_preference = (
            provider_preference
            if provider_preference in SOLAR_FORECAST_PROVIDERS
            else DEFAULT_SOLAR_FORECAST_PROVIDER
        )
        self.last_forecast_source: str | None = None
        self.estimate_type = (
            estimate_type
            if estimate_type in _SOLCAST_ESTIMATE_FIELDS
            else DEFAULT_SOLCAST_ESTIMATE_TYPE
        )

    def _provider_order(self) -> tuple[str, str]:
        """Return preferred provider first, then fallback provider."""
        if self.provider_preference == SOLAR_FORECAST_PROVIDER_OPEN_METEO:
            return (SOLAR_FORECAST_PROVIDER_OPEN_METEO, SOLAR_FORECAST_PROVIDER_SOLCAST)
        return (SOLAR_FORECAST_PROVIDER_SOLCAST, SOLAR_FORECAST_PROVIDER_OPEN_METEO)

    def _get_pv_estimate(self, period: dict[str, Any]) -> float:
        """Return the configured Solcast estimate value for a forecast period."""
        for field in _SOLCAST_ESTIMATE_FIELDS[self.estimate_type]:
            value = period.get(field)
            if value is not None:
                try:
                    return float(value)
                except (TypeError, ValueError):
                    continue
        return 0.0

    def _find_solcast_state(self, patterns: list[str]):
        """Find the first usable Solcast forecast sensor from common entity ids."""
        for entity_id in patterns:
            state = self.hass.states.get(entity_id)
            if state and state.state not in ("unavailable", "unknown", None, ""):
                return state
        return None

    def _iter_solcast_detailed_states(self) -> list[Any]:
        """Return Solcast sensor states that expose detailed forecast periods."""
        async_all = getattr(self.hass.states, "async_all", None)
        if not callable(async_all):
            return []

        try:
            states = async_all("sensor")
        except TypeError:
            states = async_all()

        detailed_states: list[Any] = []
        for state in states or []:
            entity_id = getattr(state, "entity_id", "")
            if "solcast" not in entity_id:
                continue
            attributes = getattr(state, "attributes", {}) or {}
            detailed = attributes.get("detailedForecast")
            if isinstance(detailed, list) and detailed:
                detailed_states.append(state)

        return detailed_states

    async def get_forecast(
        self,
        horizon_hours: int = 48,
        start_time: datetime | None = None,
    ) -> list[float]:
        """
        Get solar forecast in Watts for each interval.

        Returns:
            List of solar generation values in Watts
        """
        if start_time is None:
            start_time = dt_util.now()

        n_intervals = horizon_hours * 60 // self.interval_minutes

        for provider in self._provider_order():
            if provider == SOLAR_FORECAST_PROVIDER_SOLCAST:
                forecast = await self._get_solcast_forecast(start_time, n_intervals)
            else:
                forecast = self._get_open_meteo_forecast(start_time, n_intervals)
            if forecast is not None:
                self.last_forecast_source = provider
                return forecast

        # No solar forecast available — use zero solar so LP makes
        # purely price-based decisions rather than guessing production
        self.last_forecast_source = None
        _LOGGER.warning(
            "Solar forecast not available — using zero solar forecast. "
            "Install Solcast Solar or Open-Meteo Solar Forecast for optimal battery scheduling."
        )
        return [0.0] * n_intervals

    async def get_daily_summary(
        self,
        horizon_hours: int = 48,
        start_time: datetime | None = None,
    ) -> dict[str, Any]:
        """Return today/tomorrow kWh summary using the configured provider order."""
        if start_time is None:
            start_time = dt_util.now()

        forecast = await self.get_forecast(horizon_hours=horizon_hours, start_time=start_time)
        interval_hours = self.interval_minutes / 60
        today = start_time.date()
        tomorrow = today + timedelta(days=1)
        today_kwh = 0.0
        tomorrow_kwh = 0.0

        for idx, watts in enumerate(forecast):
            slot_time = start_time + timedelta(minutes=idx * self.interval_minutes)
            kwh = max(0.0, float(watts or 0.0)) * interval_hours / 1000
            if slot_time.date() == today:
                today_kwh += kwh
            elif slot_time.date() == tomorrow:
                tomorrow_kwh += kwh

        return {
            "today_kwh": today_kwh,
            "tomorrow_kwh": tomorrow_kwh,
            "today_forecast_kwh": today_kwh,
            "source": self.last_forecast_source,
        }

    def _get_open_meteo_forecast(
        self,
        start_time: datetime,
        n_intervals: int,
    ) -> list[float] | None:
        """Get forecast from the Open-Meteo Solar Forecast integration."""
        forecasts: list[list[float]] = []

        try:
            open_meteo_data = self.hass.data.get(OPEN_METEO_SOLAR_FORECAST_DOMAIN)
            if open_meteo_data:
                forecast = self._extract_from_open_meteo_integration(
                    open_meteo_data,
                    start_time,
                    n_intervals,
                )
                if forecast is not None:
                    forecasts.append(forecast)

            if not forecasts:
                forecast = self._read_from_open_meteo_sensors(start_time, n_intervals)
                if forecast is not None:
                    forecasts.append(forecast)

            if not forecasts:
                return None

            combined = self._sum_forecasts(forecasts, n_intervals)
            total_kwh = sum(combined) * (self.interval_minutes / 60) / 1000
            _LOGGER.info(
                "Open-Meteo solar forecast: %d intervals, peak=%.1fW, total=%.1fkWh",
                len(combined),
                max(combined) if combined else 0,
                total_kwh,
            )
            return combined
        except Exception as e:
            _LOGGER.warning("Could not get Open-Meteo solar forecast: %s", e)
            return None

    def _extract_from_open_meteo_integration(
        self,
        open_meteo_data: Any,
        start_time: datetime,
        n_intervals: int,
    ) -> list[float] | None:
        """Extract forecasts from hass.data for Open-Meteo Solar Forecast."""
        forecasts: list[list[float]] = []

        forecast = self._try_extract_open_meteo_estimate(
            open_meteo_data,
            start_time,
            n_intervals,
        )
        if forecast is not None:
            forecasts.append(forecast)

        if isinstance(open_meteo_data, dict):
            for value in open_meteo_data.values():
                forecast = self._try_extract_open_meteo_estimate(
                    value,
                    start_time,
                    n_intervals,
                )
                if forecast is not None:
                    forecasts.append(forecast)

        if not forecasts:
            return None
        return self._sum_forecasts(forecasts, n_intervals)

    def _try_extract_open_meteo_estimate(
        self,
        data: Any,
        start_time: datetime,
        n_intervals: int,
    ) -> list[float] | None:
        """Parse an Open-Meteo Estimate object, coordinator, or dict."""
        estimate = data.data if hasattr(data, "data") else data
        watts = None
        if hasattr(estimate, OPEN_METEO_WATTS_ATTR):
            watts = getattr(estimate, OPEN_METEO_WATTS_ATTR)
        elif isinstance(estimate, dict):
            watts = estimate.get(OPEN_METEO_WATTS_ATTR)

        if not watts:
            return None
        return self._parse_open_meteo_watts(watts, start_time, n_intervals)

    def _read_from_open_meteo_sensors(
        self,
        start_time: datetime,
        n_intervals: int,
    ) -> list[float] | None:
        """Read Open-Meteo forecast data from sensor attributes."""
        forecasts: list[list[float]] = []
        states_obj = getattr(self.hass, "states", None)
        async_all = getattr(states_obj, "async_all", None)
        if not callable(async_all):
            return None

        try:
            all_states = async_all("sensor")
        except TypeError:
            all_states = async_all()

        for state in all_states or []:
            entity_id = getattr(state, "entity_id", "")
            attributes = getattr(state, "attributes", {})
            watts = attributes.get(OPEN_METEO_WATTS_ATTR)
            if (
                not self._is_open_meteo_daily_sensor(entity_id)
                and not self._looks_like_open_meteo_watts(watts)
            ):
                continue
            forecast = self._parse_open_meteo_watts(watts, start_time, n_intervals)
            if forecast is not None:
                forecasts.append(forecast)

        if not forecasts:
            return None
        return self._sum_forecasts(forecasts, n_intervals)

    def _is_open_meteo_daily_sensor(self, entity_id: str) -> bool:
        """Return true for Open-Meteo daily energy sensors carrying watts attrs."""
        if not entity_id.startswith("sensor."):
            return False
        object_id = entity_id.split(".", 1)[1]
        return (
            object_id.endswith(OPEN_METEO_DAILY_SENSOR_SUFFIXES)
            or "_energy_production_d" in object_id
        )

    def _looks_like_open_meteo_watts(self, watts: Any) -> bool:
        """Return true when a sensor exposes Open-Meteo timestamp-to-Watts data."""
        if not isinstance(watts, dict) or not watts:
            return False

        for raw_time, raw_power in watts.items():
            try:
                if not isinstance(raw_time, datetime):
                    datetime.fromisoformat(str(raw_time).replace("Z", "+00:00"))
                float(raw_power)
                return True
            except (TypeError, ValueError):
                continue
        return False

    def _parse_open_meteo_watts(
        self,
        watts: Any,
        start_time: datetime,
        n_intervals: int,
    ) -> list[float] | None:
        """Parse Open-Meteo timestamp-to-Watts data into optimizer intervals."""
        if not isinstance(watts, dict):
            return None

        points: list[tuple[datetime, float]] = []
        for raw_time, raw_power in watts.items():
            try:
                point_time = self._parse_forecast_time(raw_time, start_time)
                point_power = max(0.0, float(raw_power))
            except (TypeError, ValueError):
                continue
            points.append((point_time, point_power))

        if not points:
            return None

        sorted_points = sorted(points, key=lambda item: item[0])
        last_point_time = sorted_points[-1][0]
        result: list[float] = []
        point_index = 0
        current_power = 0.0
        current_time = start_time

        for _ in range(n_intervals):
            while (
                point_index < len(sorted_points)
                and sorted_points[point_index][0] <= current_time
            ):
                current_power = sorted_points[point_index][1]
                point_index += 1
            if point_index >= len(sorted_points) and current_time > last_point_time:
                # Past the last forecast point: zero-fill instead of carrying
                # the final value forward, matching Solcast's period-window
                # behavior (a period-less point has no "current" reading).
                current_power = 0.0
            result.append(current_power)
            current_time += timedelta(minutes=self.interval_minutes)

        return result

    def _parse_forecast_time(self, value: Any, start_time: datetime) -> datetime:
        """Parse a forecast timestamp and align it to the optimizer timezone."""
        forecast_time = (
            value
            if isinstance(value, datetime)
            else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        )
        if start_time.tzinfo is not None:
            forecast_time = (
                forecast_time.replace(tzinfo=start_time.tzinfo)
                if forecast_time.tzinfo is None
                else forecast_time.astimezone(start_time.tzinfo)
            )
        return forecast_time

    def _sum_forecasts(
        self,
        forecasts: list[list[float]],
        n_intervals: int,
    ) -> list[float]:
        """Sum multiple same-horizon forecast arrays."""
        combined = [0.0] * n_intervals
        for forecast in forecasts:
            for idx, value in enumerate(forecast[:n_intervals]):
                combined[idx] += value
        return combined

    async def _get_solcast_forecast(
        self,
        start_time: datetime,
        n_intervals: int,
    ) -> list[float] | None:
        """Get forecast from Solcast integration if available."""
        try:
            # Prefer the integration's full cached forecast. Daily sensor
            # attributes normally expose only today and tomorrow, which leaves
            # the tail of a rolling 48-hour horizon empty late in the day and
            # can create a false forecast jump when those sensors roll over.
            solcast_solar_data = self.hass.data.get("solcast_solar")
            if solcast_solar_data:
                forecast = await self._extract_from_solcast_solar_integration(
                    solcast_solar_data, start_time, n_intervals
                )
                if forecast:
                    _LOGGER.debug(
                        "Using full solar forecast from Solcast Solar integration hass.data"
                    )
                    return forecast

            # Fallback: Read detailedForecast from Solcast sensor attributes.
            # This preserves compatibility with older integrations and users
            # that expose forecast sensors without hass.data internals.
            forecast = self._read_from_solcast_sensors(start_time, n_intervals)
            if forecast:
                _LOGGER.debug(
                    "Using solar forecast from Solcast sensor attributes "
                    "(%d intervals, peak=%.1fW)",
                    len(forecast), max(forecast) if forecast else 0,
                )
                return forecast

            # Fallback: Check for Solcast data in PowerSync's own coordinator
            from ..const import DOMAIN

            domain_data = self.hass.data.get(DOMAIN, {})

            for entry_data in domain_data.values():
                if not isinstance(entry_data, dict):
                    continue

                # Try solcast_coordinator first (primary source)
                solcast_coordinator = entry_data.get("solcast_coordinator")
                if solcast_coordinator and solcast_coordinator.data:
                    coordinator_data = solcast_coordinator.data

                    # Check for raw forecast periods (preferred - full 48h data)
                    forecasts = coordinator_data.get("forecasts")
                    if forecasts and isinstance(forecasts, list) and len(forecasts) > 0:
                        parsed = self._parse_solcast_data(
                            forecasts,
                            start_time,
                            n_intervals,
                        )
                        if parsed:
                            return parsed

                    # Fallback to hourly_forecast (processed format from Solcast HA integration)
                    hourly = coordinator_data.get("hourly_forecast")
                    if hourly and isinstance(hourly, list) and len(hourly) > 0:
                        return self._parse_hourly_forecast(
                            hourly,
                            start_time,
                            n_intervals,
                        )

                # Fallback to solcast_forecast key
                solcast_data = entry_data.get("solcast_forecast")
                if solcast_data:
                    forecasts = solcast_data.get("forecasts")
                    if forecasts and isinstance(forecasts, list) and len(forecasts) > 0:
                        parsed = self._parse_solcast_data(
                            forecasts,
                            start_time,
                            n_intervals,
                        )
                        if parsed:
                            return parsed

            return None

        except Exception as e:
            _LOGGER.warning(f"Could not get Solcast forecast: {e}")
            return None

    def _read_from_solcast_sensors(
        self,
        start_time: datetime,
        n_intervals: int,
    ) -> list[float] | None:
        """Read forecast from Solcast PV Forecast sensor attributes.

        The BJReplay/ha-solcast-solar integration (v4+) exposes detailedForecast
        as attributes on sensor.solcast_pv_forecast_forecast_today and
        sensor.solcast_pv_forecast_forecast_tomorrow. Each entry has:
            period_start: ISO timestamp
            pv_estimate: power in kW
            pv_estimate10: P10 estimate
            pv_estimate90: P90 estimate
        """
        # Try common entity ID patterns used by Solcast integrations.
        today_state = self._find_solcast_state([
            "sensor.solcast_pv_forecast_forecast_today",
            "sensor.solcast_forecast_today",
            "sensor.solcast_pv_forecast_today",
        ])
        fallback_states = self._iter_solcast_detailed_states()
        if not today_state and not fallback_states:
            return None

        today_detailed = (
            today_state.attributes.get("detailedForecast") if today_state else None
        )
        if not today_detailed or not isinstance(today_detailed, list):
            if fallback_states:
                today_state = fallback_states[0]
                today_detailed = today_state.attributes["detailedForecast"]
            else:
                return None

        # Combine today + tomorrow for 48h coverage
        combined_forecast = list(today_detailed)

        tomorrow_state = self._find_solcast_state([
            "sensor.solcast_pv_forecast_forecast_tomorrow",
            "sensor.solcast_forecast_tomorrow",
            "sensor.solcast_pv_forecast_tomorrow",
        ])
        if tomorrow_state:
            tomorrow_detailed = (
                tomorrow_state.attributes.get("detailedForecast")
                or tomorrow_state.attributes.get("forecast_tomorrow")
                or tomorrow_state.attributes.get("detailedHourly")
                or tomorrow_state.attributes.get("forecasts")
            )
            if tomorrow_detailed and isinstance(tomorrow_detailed, list):
                combined_forecast.extend(tomorrow_detailed)

        seen_entity_ids = {
            getattr(state, "entity_id", None)
            for state in (today_state, tomorrow_state)
            if state is not None
        }
        for state in fallback_states:
            entity_id = getattr(state, "entity_id", None)
            if entity_id in seen_entity_ids:
                continue
            combined_forecast.extend(state.attributes["detailedForecast"])
            seen_entity_ids.add(entity_id)

        if not combined_forecast:
            return None

        # Build period-indexed lookup. Newer sensors expose period_start;
        # Solcast API-style payloads expose period_end. Treat the estimate as
        # applying to the whole 30-minute period instead of nearest-point
        # matching, otherwise the LP can shift solar into the wrong slots.
        forecast_periods: list[tuple[datetime, datetime, float]] = []
        for item in combined_forecast:
            if not isinstance(item, dict):
                continue
            period_start_str = item.get("period_start")
            period_end_str = item.get("period_end") or item.get("period")
            if not period_start_str and not period_end_str:
                continue
            try:
                if period_start_str:
                    period_start = (
                        period_start_str
                        if isinstance(period_start_str, datetime)
                        else datetime.fromisoformat(period_start_str.replace("Z", "+00:00"))
                    )
                    period_end = period_start + timedelta(minutes=30)
                else:
                    period_end = (
                        period_end_str
                        if isinstance(period_end_str, datetime)
                        else datetime.fromisoformat(period_end_str.replace("Z", "+00:00"))
                    )
                    period_start = period_end - timedelta(minutes=30)
                if start_time.tzinfo is not None:
                    period_start = (
                        period_start.replace(tzinfo=start_time.tzinfo)
                        if period_start.tzinfo is None
                        else period_start.astimezone(start_time.tzinfo)
                    )
                    period_end = (
                        period_end.replace(tzinfo=start_time.tzinfo)
                        if period_end.tzinfo is None
                        else period_end.astimezone(start_time.tzinfo)
                    )
                pv_kw = self._get_pv_estimate(item)
                forecast_periods.append((period_start, period_end, pv_kw * 1000))
            except (ValueError, TypeError):
                continue

        if not forecast_periods:
            return None

        # Generate interval forecast aligned to start_time
        result: list[float] = []
        current_time = start_time
        sorted_periods = sorted(forecast_periods, key=lambda p: p[0])
        horizon_end = start_time + timedelta(
            minutes=n_intervals * self.interval_minutes
        )
        if not any(
            period_start < horizon_end and period_end > start_time
            for period_start, period_end, _ in sorted_periods
        ):
            _LOGGER.debug(
                "Ignoring Solcast sensor forecast with no periods in the "
                "requested horizon"
            )
            return None

        for _ in range(n_intervals):
            power_w = 0.0
            for period_start, period_end, period_power_w in sorted_periods:
                if period_start <= current_time < period_end:
                    power_w = period_power_w
                    break
                if period_start > current_time:
                    break

            result.append(power_w)
            current_time += timedelta(minutes=self.interval_minutes)

        # Validate: should have some non-zero values during daytime
        if not any(v > 0 for v in result):
            _LOGGER.debug("Solcast sensor forecast is all zeros — may be nighttime or stale data")

        total_kwh = sum(result) * (self.interval_minutes / 60) / 1000
        _LOGGER.info(
            "Solcast sensor forecast: %d periods from %d entries, "
            "peak=%.1fW, total=%.1fkWh (48h), estimate_type=%s",
            len(result), len(forecast_periods),
            max(result) if result else 0,
            total_kwh,
            self.estimate_type,
        )

        return result

    async def _extract_from_solcast_solar_integration(
        self,
        solcast_data: Any,
        start_time: datetime,
        n_intervals: int,
    ) -> list[float] | None:
        """Extract forecast data from the Solcast Solar integration (solcast_solar domain).

        The Solcast Solar integration stores data in various formats depending on version.
        hass.data["solcast_solar"] is typically a dict of {entry_id: coordinator}.
        """
        try:
            def parse_cached_forecasts(source: Any) -> list[float] | None:
                """Parse the full cached forecast exposed by current Solcast releases."""
                cached = getattr(source, "data_forecasts", None)
                if not isinstance(cached, (list, tuple)) or not cached:
                    return None
                parsed = self._parse_detailed_forecast(
                    list(cached), start_time, n_intervals
                )
                return parsed if parsed else None

            # The integration may store a coordinator or direct data
            # Try common data structures used by solcast_solar integration

            result = parse_cached_forecasts(solcast_data)
            if result:
                return result

            # Check if it's a coordinator with data attribute
            if hasattr(solcast_data, 'data') and solcast_data.data:
                result = self._try_extract_forecast(solcast_data.data, start_time, n_intervals)
                if result:
                    return result

            # If it's a dict, it could be either:
            # 1. A forecast data dict (has keys like 'detailedForecast', 'forecasts', etc.)
            # 2. A dict of {entry_id: coordinator} (Solcast Solar v4+ pattern)
            if isinstance(solcast_data, dict):
                # First try as direct forecast data
                result = self._try_extract_forecast(solcast_data, start_time, n_intervals)
                if result:
                    return result

                # Not forecast data — iterate values looking for coordinators or nested dicts
                for value in solcast_data.values():
                    result = parse_cached_forecasts(value)
                    if result:
                        return result
                    # Also check the coordinator's solcast attribute (Solcast Solar v4+)
                    if hasattr(value, 'solcast'):
                        solcast_api = value.solcast
                        result = parse_cached_forecasts(solcast_api)
                        if result:
                            return result
                        # Try get_forecast_list() method if available
                        if hasattr(solcast_api, 'get_forecast_list'):
                            try:
                                forecast_list = solcast_api.get_forecast_list()
                                if inspect.isawaitable(forecast_list):
                                    forecast_list = await forecast_list
                                if forecast_list:
                                    parsed = self._parse_detailed_forecast(
                                        forecast_list, start_time, n_intervals
                                    )
                                    if parsed:
                                        return parsed
                            except Exception:
                                pass
                        # Try detailedForecast attribute
                        if hasattr(solcast_api, 'detailedForecast'):
                            detailed = solcast_api.detailedForecast
                            if detailed and isinstance(detailed, list):
                                parsed = self._parse_detailed_forecast(
                                    detailed, start_time, n_intervals
                                )
                                if parsed:
                                    return parsed
                    if hasattr(value, 'data') and value.data:
                        result = self._try_extract_forecast(value.data, start_time, n_intervals)
                        if result:
                            return result
                    if isinstance(value, dict):
                        result = self._try_extract_forecast(value, start_time, n_intervals)
                        if result:
                            return result

            # Non-dict, non-coordinator: try to iterate as generic iterable
            if hasattr(solcast_data, 'items'):
                for key, value in solcast_data.items():
                    if hasattr(value, 'data') and value.data:
                        result = self._try_extract_forecast(value.data, start_time, n_intervals)
                        if result:
                            return result

            return None

        except Exception as e:
            _LOGGER.debug(f"Could not extract from Solcast Solar integration: {e}")
            return None

    def _try_extract_forecast(
        self,
        data: Any,
        start_time: datetime,
        n_intervals: int,
    ) -> list[float] | None:
        """Try to extract forecast from a data dict using known formats."""
        if not isinstance(data, dict):
            return None

        # Format 1: detailedForecast (list of period dicts with pv_estimate)
        detailed = data.get('detailedForecast')
        if detailed and isinstance(detailed, list) and len(detailed) > 0:
            parsed = self._parse_detailed_forecast(detailed, start_time, n_intervals)
            if parsed:
                return parsed

        # Format 2: forecasts (raw API response format)
        forecasts = data.get('forecasts')
        if forecasts and isinstance(forecasts, list) and len(forecasts) > 0:
            parsed = self._parse_solcast_data(forecasts, start_time, n_intervals)
            if parsed:
                return parsed

        # Format 3: forecast_today / forecast_tomorrow
        forecast_today = data.get('forecast_today', [])
        forecast_tomorrow = data.get('forecast_tomorrow', [])
        combined = (forecast_today or []) + (forecast_tomorrow or [])
        if combined:
            parsed = self._parse_solcast_data(combined, start_time, n_intervals)
            if parsed:
                return parsed

        # Format 4: hourly_forecast (processed Solcast HA format)
        hourly = data.get('hourly_forecast')
        if hourly and isinstance(hourly, list) and len(hourly) > 0:
            parsed = self._parse_hourly_forecast(hourly, start_time, n_intervals)
            if parsed:
                return parsed

        return None

    def _parse_detailed_forecast(
        self,
        detailed: list[dict[str, Any]],
        start_time: datetime,
        n_intervals: int,
    ) -> list[float]:
        """Parse detailedForecast format from Solcast Solar integration."""
        return self._parse_solcast_data(detailed, start_time, n_intervals)

    def _parse_hourly_forecast(
        self,
        hourly: list[dict[str, Any]],
        start_time: datetime,
        n_intervals: int,
    ) -> list[float]:
        """Parse hourly forecast format (from Solcast HA integration) into interval values.

        This format has: time (HH:MM), hour (int), pv_estimate_kw (float)
        """
        # Build lookup by hour
        forecast_by_hour: dict[int, float] = {}
        for item in hourly:
            try:
                hour = item.get("hour", 0)
                pv_kw = item.get("pv_estimate_kw", 0) or 0
                forecast_by_hour[hour] = pv_kw * 1000  # Convert kW to W
            except (KeyError, ValueError, TypeError):
                continue

        # Generate interval forecast
        result = []
        current_time = start_time

        for _ in range(n_intervals):
            hour = current_time.hour
            # Use the hour's value, or 0 if not available (likely nighttime or future day)
            result.append(forecast_by_hour.get(hour, 0.0))
            current_time += timedelta(minutes=self.interval_minutes)

        return result

    def _parse_solcast_data(
        self,
        forecasts: list[dict[str, Any]],
        start_time: datetime,
        n_intervals: int,
    ) -> list[float]:
        """Parse Solcast forecast data into interval values."""
        forecast_periods: list[tuple[datetime, datetime, float]] = []

        for item in forecasts:
            try:
                period_start_str = item.get("period_start")
                period_end_str = item.get("period_end") or item.get("period")
                if not period_start_str and not period_end_str:
                    continue
                if period_start_str:
                    start = (
                        period_start_str
                        if isinstance(period_start_str, datetime)
                        else datetime.fromisoformat(period_start_str.replace("Z", "+00:00"))
                    )
                    end = start + timedelta(minutes=30)
                else:
                    end = (
                        period_end_str
                        if isinstance(period_end_str, datetime)
                        else datetime.fromisoformat(period_end_str.replace("Z", "+00:00"))
                    )
                    start = end - timedelta(minutes=30)
                if start_time.tzinfo is not None:
                    start = (
                        start.replace(tzinfo=start_time.tzinfo)
                        if start.tzinfo is None
                        else start.astimezone(start_time.tzinfo)
                    )
                    end = (
                        end.replace(tzinfo=start_time.tzinfo)
                        if end.tzinfo is None
                        else end.astimezone(start_time.tzinfo)
                    )
                pv_kw = self._get_pv_estimate(item)
                forecast_periods.append((start, end, pv_kw * 1000))
            except (KeyError, ValueError, TypeError) as e:
                _LOGGER.debug(f"Error parsing Solcast forecast item: {e}")
                continue

        if not forecast_periods:
            return []

        # Generate interval forecast
        result = []
        current_time = start_time
        sorted_periods = sorted(forecast_periods, key=lambda p: p[0])
        horizon_end = start_time + timedelta(
            minutes=n_intervals * self.interval_minutes
        )
        if not any(
            period_start < horizon_end and period_end > start_time
            for period_start, period_end, _ in sorted_periods
        ):
            return []

        for _ in range(n_intervals):
            power_w = 0.0
            for period_start, period_end, period_power_w in sorted_periods:
                if period_start <= current_time < period_end:
                    power_w = period_power_w
                    break
                if period_start > current_time:
                    break
            result.append(power_w)
            current_time += timedelta(minutes=self.interval_minutes)

        return result

    def _generate_default_solar_curve(
        self,
        start_time: datetime,
        n_intervals: int,
    ) -> list[float]:
        """
        Generate a default solar production curve.

        Uses a simple bell curve centered at noon with seasonal adjustment.
        """
        forecast = []
        current_time = start_time

        # Assume 5kW peak system as default
        peak_power = 5000

        for _ in range(n_intervals):
            hour = current_time.hour + current_time.minute / 60.0

            # Simple bell curve: sunrise ~6am, sunset ~6pm, peak at noon
            if 6 <= hour <= 18:
                # Normalized position in day (0 at 6am, 1 at 6pm)
                t = (hour - 6) / 12

                # Bell curve: peak at t=0.5 (noon)
                # Using cosine for smooth curve
                import math
                solar_factor = math.sin(t * math.pi)
                solar_factor = max(0, solar_factor)

                # Seasonal adjustment (simple - assume ~80% of peak)
                forecast.append(peak_power * solar_factor * 0.8)
            else:
                forecast.append(0.0)

            current_time += timedelta(minutes=self.interval_minutes)

        return forecast
