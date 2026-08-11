"""Fail-closed solar forecast provenance for optimizer visualizations."""
from __future__ import annotations

from dataclasses import dataclass
import math
from collections.abc import Sequence


@dataclass(frozen=True)
class SolarForecastProvenance:
    """Aligned display series derived only from explicit optimizer evidence."""

    raw_forecast_values_kw: list[float]
    planned_forecast_values_kw: list[float]
    curtailment_values_kw: list[float]


def derive_solar_forecast_provenance(
    raw_weather_kw: Sequence[float],
    adjusted_available_kw: Sequence[float],
    solver_available_kw: Sequence[float],
    lp_curtailment_w: Sequence[float] | None,
    *,
    tolerance_kw: float = 1e-6,
) -> SolarForecastProvenance | None:
    """Return exact aligned display series, or ``None`` when proof is incomplete.

    Non-curtailment forecast adjustments are intentionally excluded from the
    curtailment band. The band spans planned generation through the adjusted
    available forecast, never the raw weather forecast.
    """
    if lp_curtailment_w is None:
        return None
    lengths = {
        len(raw_weather_kw),
        len(adjusted_available_kw),
        len(solver_available_kw),
        len(lp_curtailment_w),
    }
    if len(lengths) != 1:
        return None

    raw_values: list[float] = []
    planned_values: list[float] = []
    curtailment_values: list[float] = []
    tolerance = max(0.0, float(tolerance_kw))

    for raw, adjusted, solver, lp_w in zip(
        raw_weather_kw,
        adjusted_available_kw,
        solver_available_kw,
        lp_curtailment_w,
        strict=True,
    ):
        values = (float(raw), float(adjusted), float(solver), float(lp_w))
        if not all(math.isfinite(value) for value in values):
            return None
        raw_value, adjusted_value, solver_value, lp_value_w = values
        if min(raw_value, adjusted_value, solver_value) < -tolerance:
            return None
        if solver_value > adjusted_value + tolerance:
            return None

        raw_value = max(0.0, raw_value)
        adjusted_value = max(0.0, adjusted_value)
        solver_value = max(0.0, solver_value)
        lp_value_kw = lp_value_w / 1000.0
        if lp_value_kw < -tolerance or lp_value_kw > solver_value + tolerance:
            return None
        lp_value_kw = min(solver_value, max(0.0, lp_value_kw))
        if lp_value_kw <= tolerance:
            lp_value_kw = 0.0

        pre_lp_curtailment_kw = adjusted_value - solver_value
        if pre_lp_curtailment_kw <= tolerance:
            pre_lp_curtailment_kw = 0.0
        planned_kw = max(0.0, solver_value - lp_value_kw)
        explicit_curtailment_kw = pre_lp_curtailment_kw + lp_value_kw
        if abs((planned_kw + explicit_curtailment_kw) - adjusted_value) > tolerance:
            return None

        raw_values.append(raw_value)
        planned_values.append(planned_kw)
        curtailment_values.append(explicit_curtailment_kw)

    return SolarForecastProvenance(
        raw_forecast_values_kw=raw_values,
        planned_forecast_values_kw=planned_values,
        curtailment_values_kw=curtailment_values,
    )
