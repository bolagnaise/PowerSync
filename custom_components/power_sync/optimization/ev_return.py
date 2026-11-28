"""Decide whether an away car should count as home charging demand.

An away car used to keep its whole Smart Schedule plan in the battery plan, as
if it were plugged in at home from now on. A car several hours away that will
not be back tonight therefore made the optimizer hold battery and import for
charging that could never happen.

The car now contributes home demand only from an expected return time. Sources,
strongest first:

1. The owner's per-car answer ("not home until ..." or "home at ...").
2. Navigation to home, using the car's own ETA.
3. A range shortfall: the car cannot reach home on its current range, so it must
   charge on the way and contributes no home demand.
4. The learned weekday arrival forecast (when historical car data is enabled).

Without any of these the away car contributes nothing. Live data takes over the
moment it is home again. These decisions shape forecasts only; they never grant
or issue a charger command.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import math
from typing import Any, Mapping

# Roads are longer than the straight line. 1.3 is a conservative average for
# regional driving; a larger factor would only make "can't reach home" fire
# sooner.
ROAD_DISTANCE_FACTOR = 1.3
# Keep a margin so a car that would arrive nearly empty still counts as
# needing an en-route charge.
RANGE_BUFFER_KM = 20.0
# An answered arrival time that passes by more than this without the car
# arriving is treated as "not coming", without asking again.
LATE_ARRIVAL_GRACE = timedelta(hours=1)
# Destinations within this radius of the site count as navigating home.
HOME_RADIUS_KM = 0.5

MODE_AUTO = "auto"
MODE_NOT_HOME = "not_home"
MODE_HOME_AT = "home_at"
OVERRIDE_MODES = (MODE_NOT_HOME, MODE_HOME_AT)


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in km."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6371.0 * math.asin(min(1.0, math.sqrt(a)))


def _finite(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


@dataclass(frozen=True)
class EVReturnOverride:
    """The owner's answer for one car. Cleared when the car arrives home."""

    mode: str
    until: datetime
    set_at: datetime

    def active(self, now: datetime) -> bool:
        if self.mode == MODE_NOT_HOME:
            return now < self.until
        # A home-at answer stays meaningful until its grace has passed; after
        # that it reads as "not coming" until the car arrives or it expires.
        return now < self.until + timedelta(hours=24)

    def to_dict(self) -> dict:
        return {
            "mode": self.mode,
            "until": self.until.isoformat(),
            "set_at": self.set_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: Any) -> "EVReturnOverride | None":
        if not isinstance(data, Mapping) or data.get("mode") not in OVERRIDE_MODES:
            return None
        try:
            until = datetime.fromisoformat(str(data["until"]))
            set_at = datetime.fromisoformat(str(data["set_at"]))
        except (KeyError, TypeError, ValueError):
            return None
        if until.tzinfo is None or set_at.tzinfo is None:
            return None
        return cls(str(data["mode"]), until, set_at)


@dataclass(frozen=True)
class EVReturnTelemetry:
    """Read-only car facts. Distances are km; any field may be unknown."""

    latitude: float | None = None
    longitude: float | None = None
    range_km: float | None = None
    soc: float | None = None
    navigating_home: bool = False
    nav_eta: datetime | None = None
    nav_distance_km: float | None = None


@dataclass(frozen=True)
class EVReturnArrival:
    """Shaped like EVArrivalForecast where consumers read it (arrival, soc)."""

    arrival: datetime
    soc: float
    source: str

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "expected_arrival": self.arrival.isoformat(),
            "expected_arrival_soc": round(self.soc, 2),
            "forecast_only": True,
        }


@dataclass(frozen=True)
class EVReturnDecision:
    """How an away car takes part in tonight's home plan.

    ``include`` False means no home demand at all. ``arrival`` set means demand
    starts at that time. ``use_learned`` means the learned weekday forecast
    (if any) decides, which is the existing behaviour for that source.
    """

    away: bool
    include: bool
    source: str
    reason: str
    arrival: EVReturnArrival | None = None
    use_learned: bool = False
    distance_km: float | None = None

    def to_dict(self) -> dict:
        data = {
            "away": self.away,
            "counts_as_home_demand": self.include,
            "source": self.source,
            "reason": self.reason,
        }
        if self.arrival is not None:
            data.update(self.arrival.to_dict())
        if self.distance_km is not None:
            data["distance_from_home_km"] = round(self.distance_km, 1)
        return data


PRESENT = EVReturnDecision(False, True, "present", "Vehicle is home or its location is unknown")


def distance_home_km(telemetry: EVReturnTelemetry, home_lat: float, home_lon: float) -> float | None:
    lat, lon = _finite(telemetry.latitude), _finite(telemetry.longitude)
    hlat, hlon = _finite(home_lat), _finite(home_lon)
    if None in (lat, lon, hlat, hlon):
        return None
    return haversine_km(lat, lon, hlat, hlon)


def range_shortfall(distance_km: float | None, range_km: float | None) -> bool:
    """True when the car cannot drive home without charging on the way."""
    if distance_km is None or range_km is None or range_km < 0:
        return False
    return distance_km * ROAD_DISTANCE_FACTOR + RANGE_BUFFER_KM > range_km


def estimate_arrival_soc(
    telemetry: EVReturnTelemetry, road_km: float | None,
) -> float | None:
    """SOC after driving ``road_km`` at the car's own range-per-percent."""
    soc, range_km = _finite(telemetry.soc), _finite(telemetry.range_km)
    if soc is None:
        return None
    if road_km is None or range_km is None or range_km <= 0 or soc <= 0:
        return soc
    return max(0.0, soc - road_km * soc / range_km)


def resolve_ev_return(
    *,
    now: datetime,
    away: bool,
    telemetry: EVReturnTelemetry,
    home_lat: float,
    home_lon: float,
    override: EVReturnOverride | None = None,
    learned_available: bool = False,
) -> EVReturnDecision:
    """Return how an away car contributes to the home plan. Pure function."""
    if not away:
        return PRESENT
    distance = distance_home_km(telemetry, home_lat, home_lon)
    road_km = distance * ROAD_DISTANCE_FACTOR if distance is not None else None
    nav_road_km = _finite(telemetry.nav_distance_km)
    eta = telemetry.nav_eta if telemetry.navigating_home else None
    if eta is not None and (eta.tzinfo is None or eta < now - timedelta(minutes=15)):
        eta = None

    def arrival_at(when: datetime, source: str, km: float | None) -> EVReturnArrival:
        soc = estimate_arrival_soc(telemetry, km)
        return EVReturnArrival(max(when, now), 0.0 if soc is None else soc, source)

    if override is not None and override.active(now):
        if override.mode == MODE_NOT_HOME:
            return EVReturnDecision(
                True, False, "owner_not_home",
                f"Owner says not home until {override.until.isoformat()}",
                distance_km=distance)
        if eta is not None:
            return EVReturnDecision(
                True, True, "navigation", "Navigating home; car ETA replaces the owner's time",
                arrival=arrival_at(eta, "navigation", nav_road_km if nav_road_km is not None else road_km),
                distance_km=distance)
        if now > override.until + LATE_ARRIVAL_GRACE:
            return EVReturnDecision(
                True, False, "owner_home_at_overdue",
                "Owner's expected arrival time passed without the car arriving",
                distance_km=distance)
        return EVReturnDecision(
            True, True, "owner_home_at",
            f"Owner expects the car home at {override.until.isoformat()}",
            arrival=arrival_at(override.until, "owner_home_at", road_km),
            distance_km=distance)

    if eta is not None:
        return EVReturnDecision(
            True, True, "navigation", "Navigating home",
            arrival=arrival_at(eta, "navigation", nav_road_km if nav_road_km is not None else road_km),
            distance_km=distance)

    if range_shortfall(distance, _finite(telemetry.range_km)):
        return EVReturnDecision(
            True, False, "range_shortfall",
            "Not enough range to reach home; the car must charge on the way",
            distance_km=distance)

    if learned_available:
        return EVReturnDecision(
            True, True, "learned", "Weekday trip history predicts the return",
            use_learned=True, distance_km=distance)

    return EVReturnDecision(
        True, False, "no_return_evidence",
        "Away with no expected return; home demand starts when it arrives",
        distance_km=distance)


def select_home_demand_plan(
    decision: EVReturnDecision | None, *, current_plan: Any,
    historical_plan: Any, return_plan: Any,
) -> tuple[Any, bool]:
    """Return ``(plan, forecast_only)`` for one car's home charging demand.

    No decision (present car, or decisions unavailable) keeps the existing
    behaviour: the learned estimate while it exists, else the live plan.
    """
    if decision is not None and decision.away:
        if not decision.include:
            return None, False
        if decision.arrival is not None:
            return return_plan, return_plan is not None
        return historical_plan, historical_plan is not None
    return historical_plan or current_plan, historical_plan is not None
