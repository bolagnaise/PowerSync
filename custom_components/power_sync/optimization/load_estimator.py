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
from datetime import datetime, timedelta
from typing import Any

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
ACTIVE_AWAY_LOAD_BLEND = 1.0
ACTIVE_AWAY_LOAD_MIN_SCALE = 0.2

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

        # Temperature sensitivity cache
        self._temp_alpha: float | None = None
        self._temp_bucket_averages: dict[tuple[int, int, int], float] | None = None
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
                if self.weather_entity_id:
                    forecast_temps, bucket_temp_avgs, alpha = await self._get_temperature_adjustment(
                        history, horizon_hours
                    )
                # Building the forecast iterates the full load history (tens to
                # hundreds of thousands of recorder points) and re-scans it for
                # the recent-regime adjustment — heavy, pure-CPU work. Run it off
                # the event loop so it can't freeze HA on every optimisation
                # cycle. _forecast_from_history operates only on its arguments
                # and constants, so it is safe in a worker thread.
                forecast = await self.hass.async_add_executor_job(
                    functools.partial(
                        self._forecast_from_history,
                        history, start_time, n_intervals,
                        forecast_temps=forecast_temps,
                        bucket_temp_averages=bucket_temp_avgs,
                        alpha=alpha,
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

            instance = get_instance(self.hass)

            start_time = now - timedelta(days=days)
            end_time = now

            # Get history from recorder
            history = await instance.async_add_executor_job(
                get_significant_states,
                self.hass,
                start_time,
                end_time,
                [self.load_entity_id],
            )

            if not history or self.load_entity_id not in history:
                _LOGGER.warning("No history found for %s", self.load_entity_id)
                return []

            # Parsing/filtering the raw states (tens of thousands of points) is
            # pure-CPU work — run it off the event loop so it can't block HA.
            result, excluded = await self.hass.async_add_executor_job(
                self._parse_load_history,
                history[self.load_entity_id],
                multiplier,
                has_away_window,
                self.away_enabled_at,
                away_end,
            )

            # Subtract configured EV charger power from the load history so a
            # recurring EV charging pattern embedded in the whole-home load
            # sensor is not double-counted against the planned-EV overlay the
            # coordinator adds on top of the forecast.
            if result and self.ev_power_entity_ids:
                ev_multipliers = self._resolve_power_multipliers(
                    self.ev_power_entity_ids
                )
                ev_history = await instance.async_add_executor_job(
                    get_significant_states,
                    self.hass,
                    start_time,
                    end_time,
                    self.ev_power_entity_ids,
                )
                result = await self.hass.async_add_executor_job(
                    self._subtract_ev_power,
                    result,
                    ev_history,
                    self.ev_power_entity_ids,
                    ev_multipliers,
                )

            # Cache the result
            self._history_cache[cache_key] = result
            self._cache_time = now

            if result:
                avg_w = sum(v for _, v in result) / len(result)
                _LOGGER.info(
                    "Loaded %d history points for %s (avg %.0fW, %.1f days%s)",
                    len(result), self.load_entity_id, avg_w, days,
                    f", away window excluded ({excluded} points)" if has_away_window else "",
                )
            else:
                _LOGGER.warning(
                    "History returned for %s but no valid numeric values",
                    self.load_entity_id,
                )
            return result

        except ImportError:
            _LOGGER.warning("Recorder not available for load history")
            return []
        except Exception as e:
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

        recent_scale = self._recent_load_scale(history, start_time)
        if recent_scale is not None:
            forecast = [value * recent_scale for value in forecast]

        return forecast

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
        if active_away:
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
        else:
            recent_start = recent_end - timedelta(hours=RECENT_LOAD_WINDOW_HOURS)
            baseline_end = recent_start - timedelta(hours=RECENT_LOAD_BASELINE_EXCLUDE_HOURS)

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
        exact_samples = self._clip_outliers(pattern.get(key, []))
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
        return self._weighted_average(samples, reference_time)

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
    ) -> tuple[list[tuple[datetime, float]] | None, dict | None, float | None]:
        """Fetch temperature data and return (forecast_temps, bucket_temp_avgs, alpha).

        Uses a 1-hour cache for the fitted alpha.  Returns (None, None, None) if
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
                return None, None, None
            forecast_temps = await self._fetch_forecast_temperatures(horizon_hours)
            return forecast_temps or None, self._temp_bucket_averages, self._temp_alpha

        # Fetch historical temperatures matching the filtered load history
        # window used by the forecast model.
        hist_start = min(ts for ts, _ in history)
        hist_end = max(ts for ts, _ in history)

        temp_history = await self._fetch_historical_temperatures(hist_start, hist_end)
        if not temp_history:
            self._temp_alpha = None
            self._temp_bucket_averages = None
            self._temp_alpha_fitted = True
            self._temp_cache_time = now
            return None, None, None

        # Bucketing the full load history (tens of thousands of points) and
        # fitting the sensitivity coefficient (a bisect per point) is heavy,
        # pure-CPU work. Run it off the event loop so it can't freeze HA during
        # the optimiser's first forecast at setup.
        bucket_temp_avgs, alpha = await self.hass.async_add_executor_job(
            self._compute_temperature_fit, history, temp_history
        )

        self._temp_alpha = alpha
        self._temp_bucket_averages = bucket_temp_avgs
        self._temp_alpha_fitted = True
        self._temp_cache_time = now

        if alpha is None:
            return None, None, None

        # Fetch forecast temperatures
        forecast_temps = await self._fetch_forecast_temperatures(horizon_hours)
        return forecast_temps or None, bucket_temp_avgs, alpha

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

    @staticmethod
    def _parse_load_history(
        raw_states,
        multiplier: float,
        has_away_window: bool,
        away_start: datetime | None,
        away_end: datetime | None,
    ) -> tuple[list[tuple[datetime, float]], int]:
        """Parse + away-filter raw recorder states (executor-only, CPU-bound).

        Iterates every recorder state for the load sensor (tens of thousands of
        points), so it must not run on the event loop. Pure function over its
        arguments — safe in a worker thread.
        """
        result: list[tuple[datetime, float]] = []
        for state in raw_states:
            try:
                value_watts = float(state.state) * multiplier
                # Filter invalid values: must be positive and < 100kW residential max
                if 0 < value_watts < 100_000:
                    result.append((state.last_changed, value_watts))
            except (ValueError, TypeError):
                continue

        # Away Mode recovery: exclude the completed away window
        # [enabled_at, disabled_at], then keep the most recent 30 days of
        # the remaining data.
        excluded = 0
        if has_away_window:
            before = len(result)
            result = [
                (ts, w) for (ts, w) in result
                if not (away_start <= ts <= away_end)
            ]
            excluded = before - len(result)
            if result:
                result.sort(key=lambda h: h[0])
                # Extend the retention window back by the away duration so
                # ~HISTORY_LOOKBACK_DAYS of *actual* (non-away) history survive.
                # A flat calendar cutoff from the newest sample would let the
                # excluded away gap eat into the window, discarding the extra
                # pre-away history that _get_load_history deliberately fetched
                # and collapsing per-bucket sample counts after long trips.
                away_span = timedelta(0)
                if away_start is not None and away_end is not None:
                    away_span = max(timedelta(0), away_end - away_start)
                cutoff = (
                    result[-1][0]
                    - timedelta(days=HISTORY_LOOKBACK_DAYS)
                    - away_span
                )
                result = [h for h in result if h[0] >= cutoff]
        return result, excluded

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

    @staticmethod
    def _subtract_ev_power(
        load_samples: list[tuple[datetime, float]],
        ev_history: dict | None,
        ev_entity_ids: list[str],
        ev_multipliers: dict[str, float],
    ) -> list[tuple[datetime, float]]:
        """Subtract concurrent EV charger power from each load sample (Watts).

        Pure/CPU-bound (executor-safe). For each load sample, the EV power at
        that instant is the most recent recorded value of each EV entity at or
        before the sample time (states are step functions). The load is clamped
        at zero so a noisy over-subtraction can never produce negative load.
        """
        import bisect

        timelines: list[tuple[list[datetime], list[float]]] = []
        for eid in ev_entity_ids:
            states = ev_history.get(eid) if ev_history else None
            if not states:
                continue
            mult = ev_multipliers.get(eid, 1.0)
            ts_list: list[datetime] = []
            w_list: list[float] = []
            for state in states:
                try:
                    watts = float(state.state) * mult
                except (ValueError, TypeError):
                    continue
                # Ignore implausible/negative charger readings.
                if not (0 <= watts < 100_000):
                    continue
                ts_list.append(state.last_changed)
                w_list.append(watts)
            if ts_list:
                timelines.append((ts_list, w_list))

        if not timelines:
            return load_samples

        adjusted: list[tuple[datetime, float]] = []
        for ts, load_w in load_samples:
            ev_w = 0.0
            for ts_list, w_list in timelines:
                idx = bisect.bisect_right(ts_list, ts) - 1
                if idx >= 0:
                    ev_w += w_list[idx]
            adjusted.append((ts, max(0.0, load_w - ev_w)))
        return adjusted

    def _compute_temperature_fit(
        self,
        history: list[tuple[datetime, float]],
        temp_history: list[tuple[datetime, float]],
    ) -> tuple[dict[tuple[int, int, int], float] | None, float | None]:
        """Bucket the load history and fit temperature sensitivity (executor-only).

        Iterates the full load history (tens of thousands of points) and runs a
        bisect per point in the fit, so this must NOT run on the event loop — it
        would block HA during the optimiser's first forecast. Operates purely on
        the passed-in data, so it is safe to run in a worker thread.
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
            # Primary: Read detailedForecast from Solcast sensor attributes
            # The BJReplay/ha-solcast-solar integration (v4+) exposes
            # detailedForecast as attributes on forecast_today/tomorrow sensors
            forecast = self._read_from_solcast_sensors(start_time, n_intervals)
            if forecast:
                _LOGGER.debug(
                    "Using solar forecast from Solcast sensor attributes "
                    "(%d intervals, peak=%.1fW)",
                    len(forecast), max(forecast) if forecast else 0,
                )
                return forecast

            # Fallback: try the Solcast Solar integration hass.data
            solcast_solar_data = self.hass.data.get("solcast_solar")
            if solcast_solar_data:
                forecast = await self._extract_from_solcast_solar_integration(
                    solcast_solar_data, start_time, n_intervals
                )
                if forecast:
                    _LOGGER.debug("Using solar forecast from Solcast Solar integration hass.data")
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
                        return self._parse_solcast_data(
                            forecasts,
                            start_time,
                            n_intervals,
                        )

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
                        return self._parse_solcast_data(
                            forecasts,
                            start_time,
                            n_intervals,
                        )

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
            # The integration may store a coordinator or direct data
            # Try common data structures used by solcast_solar integration

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
                    if hasattr(value, 'data') and value.data:
                        result = self._try_extract_forecast(value.data, start_time, n_intervals)
                        if result:
                            return result
                    # Also check the coordinator's solcast attribute (Solcast Solar v4+)
                    if hasattr(value, 'solcast'):
                        solcast_api = value.solcast
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
                                    if parsed and any(v > 0 for v in parsed):
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
                                if parsed and any(v > 0 for v in parsed):
                                    return parsed
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
            if parsed and any(v > 0 for v in parsed):
                return parsed

        # Format 2: forecasts (raw API response format)
        forecasts = data.get('forecasts')
        if forecasts and isinstance(forecasts, list) and len(forecasts) > 0:
            parsed = self._parse_solcast_data(forecasts, start_time, n_intervals)
            if parsed and any(v > 0 for v in parsed):
                return parsed

        # Format 3: forecast_today / forecast_tomorrow
        forecast_today = data.get('forecast_today', [])
        forecast_tomorrow = data.get('forecast_tomorrow', [])
        combined = (forecast_today or []) + (forecast_tomorrow or [])
        if combined:
            parsed = self._parse_solcast_data(combined, start_time, n_intervals)
            if parsed and any(v > 0 for v in parsed):
                return parsed

        # Format 4: hourly_forecast (processed Solcast HA format)
        hourly = data.get('hourly_forecast')
        if hourly and isinstance(hourly, list) and len(hourly) > 0:
            parsed = self._parse_hourly_forecast(hourly, start_time, n_intervals)
            if parsed and any(v > 0 for v in parsed):
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
