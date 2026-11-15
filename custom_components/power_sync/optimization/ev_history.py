"""Weekday EV arrival predictions from observed trips, never charger authority.

SOC loss is learned between the comparable time of day and the observed return,
so the outward commute is not counted again. Actual arrival always wins. All
energy conversion and charging policy remain with the existing EV planners.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import math
from statistics import median
from typing import Iterable

RETENTION_DAYS = 56
MIN_MATCHING_DAYS = 2
MAX_SOC_AGE = timedelta(hours=12)


@dataclass(frozen=True)
class EVHistoryPoint:
    timestamp: datetime
    soc: float | None
    home: bool | None


@dataclass(frozen=True)
class EVArrivalForecast:
    arrival: datetime
    soc: float
    remaining_drive_soc: float
    sample_days: int

    def to_dict(self) -> dict:
        return {
            "source": "weekday_trip_history",
            "expected_arrival": self.arrival.isoformat(),
            "expected_arrival_soc": round(self.soc, 2),
            "remaining_drive_soc": round(self.remaining_drive_soc, 2),
            "comparable_days": self.sample_days,
            "forecast_only": True,
        }


def valid_soc(value) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and 0 <= number <= 100 else None


def forecast_arrival(
    points: Iterable[EVHistoryPoint], *, now: datetime,
    current_soc: float | None, home: bool | None,
) -> EVArrivalForecast | None:
    """Predict the next return using independent matching weekdays.

    Unknown presence is not evidence of being away. No synthetic observations,
    future rows, missed returns, or off-site charging trips train a prediction.
    UTC identifies observations; site-local wall time identifies the routine.
    """
    current_soc = valid_soc(current_soc)
    if home is not False or current_soc is None or now.tzinfo is None:
        return None
    now_utc = now.astimezone(timezone.utc)
    cutoff = now_utc - timedelta(days=RETENTION_DAYS)
    history = sorted(
        (p for p in points if p.timestamp.tzinfo is not None
         and cutoff <= p.timestamp.astimezone(timezone.utc) < now_utc),
        key=lambda p: p.timestamp.timestamp(),
    )
    # One return per historical local date. Repeated DST hours or duplicate
    # providers must not turn one day into multiple supporting observations.
    samples: dict[int, dict[str, tuple[float, float]]] = {0: {}, 1: {}}
    for index, point in enumerate(history):
        if index == 0 or point.home is not True:
            continue
        previous = history[index - 1]
        if previous.home is not False:
            continue
        local = point.timestamp.astimezone(now.tzinfo)
        offset = (local.weekday() - now.weekday()) % 7
        if offset not in samples:
            continue
        comparison = (local - timedelta(days=offset)).replace(
            hour=now.hour, minute=now.minute, second=now.second, microsecond=0)
        if comparison.timestamp() >= point.timestamp.timestamp():
            continue
        before = [p for p in history[:index]
                  if p.timestamp.timestamp() <= comparison.timestamp()]
        if not before:
            continue
        baseline = before[-1]
        baseline_soc = valid_soc(baseline.soc)
        if (baseline.home is not False or baseline_soc is None
                or comparison.timestamp() - baseline.timestamp.timestamp()
                > MAX_SOC_AGE.total_seconds()):
            continue
        arrival_soc = valid_soc(point.soc)
        if arrival_soc is None:
            continue
        trip = [p for p in history if comparison.timestamp()
                <= p.timestamp.timestamp() <= point.timestamp.timestamp()]
        if any(valid_soc(p.soc) is None or p.home is None for p in trip):
            continue
        if any(p.home is True for p in trip[:-1]):
            continue  # Separate visits are separate obligations.
        socs = [baseline_soc, *(valid_soc(p.soc) for p in trip)]
        # Charging away changes the trip's consumption relationship. Do not
        # interpret its net change as the remaining commute's driving loss.
        if any(b > a + 1 for a, b in zip(socs, socs[1:])):
            continue
        minute = local.hour * 60 + local.minute + local.second / 60
        samples[offset][local.date().isoformat()] = (
            minute, max(0.0, baseline_soc - arrival_soc),
        )
    candidates = []
    for offset, days in samples.items():
        if len(days) < MIN_MATCHING_DAYS:
            continue
        minutes = [sample[0] for sample in days.values()]
        return_minute = median(minutes)
        if median(abs(value - return_minute) for value in minutes) > 90:
            continue
        arrival = (now + timedelta(days=offset)).replace(
            hour=int(return_minute // 60), minute=int(return_minute % 60),
            second=0, microsecond=0)
        # Expire a missed return rather than repeatedly moving it forward.
        if not 0 < arrival.timestamp() - now.timestamp() <= 24 * 3600:
            continue
        if arrival.astimezone(timezone.utc).astimezone(now.tzinfo) != arrival:
            continue
        loss = median(sample[1] for sample in days.values())
        candidates.append(EVArrivalForecast(arrival, max(0.0, current_soc - loss),
                                            loss, len(days)))
    return min(candidates, key=lambda candidate: candidate.arrival.timestamp()) if candidates else None
