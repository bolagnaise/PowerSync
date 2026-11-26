"""Amber export forecast accuracy: earlier forecasts compared with settled prices.

Amber's forecast can show export price spikes that never settle. The optional
Amber Forecast Confidence setting relies less on those spikes, but users need a
way to see whether forecast spikes usually happen for them before choosing it.

This tracker keeps, for each 30-minute period, the export price Amber forecast
about 1 hour and about 4 hours beforehand, and the settled export price once
Amber reports it. It is display-only: nothing here feeds the optimizer.

All prices are export earnings in $/kWh (Amber feedIn ``perKwh`` is negated, so
a positive value means you are paid to export).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import math
from typing import Any, Callable, Iterable

# Same ordinary-price constants as optimization/price_forecast_confidence.py,
# so a "spike" here is close to what Amber Forecast Confidence discounts. Kept
# local so this display module does not import the optimizer package.
ORDINARY_SAMPLE_CEILING = 1.0  # $/kWh; exclude extreme prices from the baseline.
ORDINARY_PRICE_BUFFER = 0.10  # $/kWh above the ordinary median.

BUCKET_MINUTES = 30
# Forecast leads to record. Each lead keeps the latest forecast made between
# LEAD and LEAD + LEAD_WINDOW minutes before the period started, so a forecast
# from a long outage is never presented as a "1 hour ahead" forecast.
LEADS_MINUTES = (60, 240)
LEAD_WINDOW_MINUTES = 60
RETENTION_DAYS = 7
# Points returned to the dashboard chart.
CHART_HOURS = 48
# A settled period needs most of its 5-minute prices.
MIN_SETTLED_COVERAGE_MINUTES = 25
# A forecast spike "happened" when at least this share of its forecast premium
# above the ordinary price was settled.
HAPPENED_SHARE = 0.5
# Advice needs enough history and enough forecast spikes to mean anything.
MIN_HOURS_FOR_ADVICE = 48
MIN_EVENTS_FOR_ADVICE = 3
MISSED_MAJORITY = 0.5
MIN_BASELINE_SAMPLES = 4

STORE_VERSION = 1


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _floor(moment: datetime, minutes: int) -> datetime:
    moment = moment.astimezone(timezone.utc).replace(second=0, microsecond=0)
    return moment - timedelta(minutes=moment.minute % minutes)


def _finite(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def _entry_window(entry: dict) -> tuple[datetime, datetime] | None:
    """Return an Amber interval's [start, end) in UTC.

    Amber's ``startTime`` is one second after the boundary, so the start is
    taken from the interval end and duration where possible.
    """
    duration = _finite(entry.get("duration"))
    if duration is None or duration <= 0:
        duration = float(BUCKET_MINUTES)
    end = _parse_time(entry.get("endTime")) or _parse_time(entry.get("nemTime"))
    if end is not None:
        end = _floor(end + timedelta(seconds=30), 5)
        return end - timedelta(minutes=duration), end
    start = _parse_time(entry.get("startTime"))
    if start is None:
        return None
    start = _floor(start, 5)
    return start, start + timedelta(minutes=duration)


def _export_price(entry: dict, forecast_type: str) -> float | None:
    """Export earnings in $/kWh for an Amber feedIn interval."""
    cents = None
    if entry.get("type") == "ForecastInterval":
        advanced = entry.get("advancedPrice")
        if isinstance(advanced, dict):
            cents = _finite(advanced.get(forecast_type))
            if cents is None:
                cents = _finite(advanced.get("predicted"))
    if cents is None:
        cents = _finite(entry.get("perKwh"))
    if cents is None:
        return None
    return -cents / 100


def _feed_in(entries: Iterable[Any]) -> list[dict]:
    return [
        e for e in (entries or [])
        if isinstance(e, dict) and e.get("channelType") == "feedIn"
    ]


def _key(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


class AmberForecastAccuracyTracker:
    """Rolling per-period record of forecast and settled Amber export prices."""

    def __init__(
        self,
        *,
        forecast_type: Callable[[], str] | None = None,
        on_change: Callable[[], None] | None = None,
    ) -> None:
        self._forecast_type = forecast_type or (lambda: "predicted")
        self._on_change = on_change
        # bucket start key -> {"f60": price, "f240": price, "a": {start key: [price, minutes]}}
        self._buckets: dict[str, dict[str, Any]] = {}
        self._first_seen: str | None = None

    # ── persistence ──────────────────────────────────────────────
    def as_dict(self) -> dict[str, Any]:
        return {"buckets": self._buckets, "first_seen": self._first_seen}

    def load(self, data: Any) -> None:
        if not isinstance(data, dict):
            return
        buckets = data.get("buckets")
        if isinstance(buckets, dict):
            self._buckets = {
                k: v for k, v in buckets.items()
                if isinstance(k, str) and isinstance(v, dict) and _parse_time(k)
            }
        first_seen = data.get("first_seen")
        self._first_seen = first_seen if _parse_time(first_seen) else None

    # ── recording ────────────────────────────────────────────────
    def observe(
        self,
        forecast: Iterable[Any] | None,
        settled: Iterable[Any] | None,
        now: datetime,
    ) -> bool:
        """Record one Amber refresh. Returns True when anything changed."""
        if now.tzinfo is None:
            return False
        now = now.astimezone(timezone.utc)
        changed = False
        if self._first_seen is None:
            self._first_seen = _key(now)
            changed = True
        try:
            forecast_type = str(self._forecast_type() or "predicted")
        except Exception:  # noqa: BLE001 - option lookup must not stop recording
            forecast_type = "predicted"

        # Forecasts: average the forecast entries that fall in each period.
        sums: dict[datetime, list[float]] = {}
        for entry in _feed_in(forecast):
            if entry.get("type") != "ForecastInterval":
                continue
            window = _entry_window(entry)
            price = _export_price(entry, forecast_type)
            if window is None or price is None:
                continue
            start, end = window
            bucket = _floor(start, BUCKET_MINUTES)
            while bucket < end:
                overlap = (min(end, bucket + timedelta(minutes=BUCKET_MINUTES)) - max(start, bucket)).total_seconds() / 60
                if overlap > 0:
                    acc = sums.setdefault(bucket, [0.0, 0.0])
                    acc[0] += price * overlap
                    acc[1] += overlap
                bucket += timedelta(minutes=BUCKET_MINUTES)
        for bucket, (total, minutes) in sums.items():
            if minutes <= 0:
                continue
            lead = (bucket - now).total_seconds() / 60
            for target in LEADS_MINUTES:
                if target <= lead < target + LEAD_WINDOW_MINUTES:
                    value = round(total / minutes, 5)
                    record = self._buckets.setdefault(_key(bucket), {})
                    if record.get(f"f{target}") != value:
                        record[f"f{target}"] = value
                        changed = True

        # Settled prices: Amber ActualInterval entries only.
        for entry in _feed_in(settled):
            if entry.get("type") != "ActualInterval":
                continue
            window = _entry_window(entry)
            price = _finite(entry.get("perKwh"))
            if window is None or price is None:
                continue
            start, end = window
            if end > now + timedelta(minutes=1):
                continue
            minutes = (end - start).total_seconds() / 60
            if minutes <= 0 or minutes > BUCKET_MINUTES:
                continue
            record = self._buckets.setdefault(_key(_floor(start, BUCKET_MINUTES)), {})
            actuals = record.setdefault("a", {})
            value = [round(-price / 100, 5), minutes]
            if actuals.get(_key(start)) != value:
                actuals[_key(start)] = value
                changed = True

        changed = self._prune(now) or changed
        if changed and self._on_change is not None:
            self._on_change()
        return changed

    def _prune(self, now: datetime) -> bool:
        oldest = now - timedelta(days=RETENTION_DAYS)
        newest = now + timedelta(days=3)
        stale = [k for k in self._buckets if not (oldest <= _parse_time(k) <= newest)]
        for k in stale:
            del self._buckets[k]
        return bool(stale)

    # ── reading ──────────────────────────────────────────────────
    @staticmethod
    def _settled(record: dict) -> float | None:
        actuals = record.get("a")
        if not isinstance(actuals, dict):
            return None
        total = minutes = 0.0
        for item in actuals.values():
            if not isinstance(item, list) or len(item) != 2:
                continue
            price, dur = _finite(item[0]), _finite(item[1])
            if price is None or dur is None or dur <= 0:
                continue
            total += price * dur
            minutes += dur
        if minutes < MIN_SETTLED_COVERAGE_MINUTES:
            return None
        return total / minutes

    def _periods(self, now: datetime) -> list[tuple[datetime, float | None, dict]]:
        out = []
        for key, record in self._buckets.items():
            start = _parse_time(key)
            if start is None:
                continue
            out.append((start, self._settled(record) if start + timedelta(minutes=BUCKET_MINUTES) <= now else None, record))
        out.sort(key=lambda item: item[0])
        return out

    @staticmethod
    def _baseline(settled: list[float]) -> float | None:
        ordinary = [p for p in settled if 0 <= p <= ORDINARY_SAMPLE_CEILING]
        if len(ordinary) < MIN_BASELINE_SAMPLES:
            return None
        center = _median(ordinary)
        deviation = _median([abs(p - center) for p in ordinary])
        return min(ORDINARY_SAMPLE_CEILING, center + max(ORDINARY_PRICE_BUFFER, 3 * deviation))

    def summary(self, now: datetime, *, confidence_enabled: bool | None = None) -> dict[str, Any]:
        """Return accuracy figures per lead, recent chart points and events."""
        now = now.astimezone(timezone.utc)
        periods = [p for p in self._periods(now) if p[0] >= now - timedelta(days=RETENTION_DAYS)]
        settled = [(start, actual, record) for start, actual, record in periods if actual is not None]
        baseline = self._baseline([actual for _, actual, _ in settled])
        first_seen = _parse_time(self._first_seen)
        hours_tracked = 0.0
        if first_seen is not None:
            hours_tracked = round(max(0.0, min((now - first_seen).total_seconds() / 3600, RETENTION_DAYS * 24)), 1)

        chart_from = now - timedelta(hours=CHART_HOURS)
        leads: dict[str, dict[str, Any]] = {}
        all_events: list[dict[str, Any]] = []
        for target in LEADS_MINUTES:
            field = f"f{target}"
            events: list[dict[str, Any]] = []
            unforecast = 0
            current: dict[str, Any] | None = None
            previous_start: datetime | None = None
            for start, actual, record in settled:
                forecast = _finite(record.get(field))
                spike = baseline is not None and forecast is not None and forecast > baseline
                contiguous = previous_start is not None and start - previous_start == timedelta(minutes=BUCKET_MINUTES)
                if spike:
                    if current is None or not contiguous:
                        current = {"start": start, "end": start, "forecast_peak": forecast, "actual_peak": actual}
                        events.append(current)
                    current["end"] = start
                    current["forecast_peak"] = max(current["forecast_peak"], forecast)
                    current["actual_peak"] = max(current["actual_peak"], actual)
                else:
                    current = None
                    if baseline is not None and forecast is not None and actual > baseline:
                        unforecast += 1
                previous_start = start
            for event in events:
                premium = event["forecast_peak"] - baseline
                event["happened"] = event["actual_peak"] >= baseline + HAPPENED_SHARE * premium
            happened = sum(1 for e in events if e["happened"])
            missed = len(events) - happened
            hit_rate = round(100 * happened / len(events)) if events else None
            leads[str(target)] = {
                "lead_minutes": target,
                "periods_compared": sum(1 for _, _, r in settled if _finite(r.get(field)) is not None),
                "predicted_spikes": len(events),
                "happened": happened,
                "missed": missed,
                "hit_rate": hit_rate,
                "unforecast_spike_periods": unforecast,
                "advice": self._advice(hours_tracked, len(events), hit_rate, confidence_enabled),
            }
            for event in events:
                if event["end"] + timedelta(minutes=BUCKET_MINUTES) <= chart_from:
                    continue
                all_events.append({
                    "lead_minutes": target,
                    "start": _key(event["start"]),
                    "end": _key(event["end"] + timedelta(minutes=BUCKET_MINUTES)),
                    "forecast_peak_per_kwh": round(event["forecast_peak"], 4),
                    "actual_peak_per_kwh": round(event["actual_peak"], 4),
                    "happened": event["happened"],
                })

        points = []
        for start, actual, record in periods:
            if start < chart_from or start > now:
                continue
            point: dict[str, Any] = {"t": _key(start)}
            if actual is not None:
                point["actual"] = round(actual, 4)
            for target in LEADS_MINUTES:
                value = _finite(record.get(f"f{target}"))
                if value is not None:
                    point[f"f{target}"] = round(value, 4)
            if len(point) > 1:
                points.append(point)

        return {
            "hours_tracked": hours_tracked,
            "baseline_per_kwh": round(baseline, 4) if baseline is not None else None,
            "happened_share": HAPPENED_SHARE,
            "confidence_enabled": confidence_enabled,
            "leads": leads,
            "points": points,
            "events": all_events,
        }

    @staticmethod
    def _advice(hours: float, events: int, hit_rate: int | None, enabled: bool | None) -> str:
        if hours < MIN_HOURS_FOR_ADVICE or events < MIN_EVENTS_FOR_ADVICE or hit_rate is None:
            return "collecting"
        if hit_rate < MISSED_MAJORITY * 100:
            return "confidence_suits" if enabled else "consider_confidence"
        return "consider_off" if enabled else "keep_off"
