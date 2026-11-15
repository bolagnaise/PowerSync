"""Weekday commute predictions use only evidence available at solve time."""
from datetime import datetime, timedelta, timezone
import importlib.util
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

import pytest


PATH = Path(__file__).resolve().parents[1] / "custom_components/power_sync/optimization/ev_history.py"
SPEC = importlib.util.spec_from_file_location("ev_history_test", PATH)
history = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = history
SPEC.loader.exec_module(history)
Point = history.EVHistoryPoint
NOW = datetime(2026, 11, 16, 12, tzinfo=timezone.utc)


def trips(now=NOW, *, loss=10, weeks=(1, 2, 3)):
    points = []
    for weeks_ago in weeks:
        day = now - timedelta(weeks=weeks_ago)
        points += [Point(day.replace(hour=7), 85, True),
                   Point(day.replace(hour=8), 75, False),
                   Point(day.replace(hour=12), 75, False),
                   Point(day.replace(hour=15, minute=45), 75, False),
                   Point(day.replace(hour=16), 75 - loss, True)]
    return points


def forecast(points=None, **kwargs):
    return history.forecast_arrival(trips() if points is None else points,
        now=kwargs.pop("now", NOW), current_soc=kwargs.pop("current_soc", 75),
        home=kwargs.pop("home", False), **kwargs)


def test_remaining_commute_is_added_once_and_return_is_not_a_charging_start():
    estimate = forecast()
    assert estimate.soc == 65
    assert estimate.remaining_drive_soc == 10
    assert estimate.arrival == NOW.replace(hour=16)
    assert estimate.sample_days == 3
    # A second rolling solve uses actual SOC, never its previous projection.
    assert forecast(now=NOW + timedelta(minutes=5)).soc == 65
    assert forecast(current_soc=70).soc == 60


@pytest.mark.parametrize("home", [True, None])
def test_actual_arrival_or_unknown_presence_never_uses_history(home):
    assert forecast(home=home) is None


def test_missed_return_expires_without_sliding_to_tomorrow():
    assert forecast(now=NOW.replace(hour=17)) is None


def test_single_day_and_duplicate_rows_are_not_independent_evidence():
    one_day = trips(weeks=(1,))
    assert forecast(one_day * 5) is None


def test_current_and_future_observations_cannot_train_the_forecast():
    assert forecast(trips(weeks=(0, -1, -2))) is None


def test_offsite_charging_is_not_learned_as_driving_consumption():
    points = trips()
    for index, point in enumerate(points):
        if point.timestamp.hour == 15:
            points[index] = Point(point.timestamp, 90, False)
    assert forecast(points) is None


def test_known_zero_driving_loss_remains_a_valid_prediction():
    assert forecast(trips(loss=0)).remaining_drive_soc == 0
    assert forecast(trips(loss=0)).soc == 75


@pytest.mark.parametrize("soc", [None, True, float("nan"), float("inf"), -1, 101])
def test_invalid_live_soc_does_not_create_a_forecast(soc):
    assert forecast(current_soc=soc) is None


def test_timezone_and_dst_use_local_return_hour_and_distinct_days():
    local = datetime(2026, 11, 2, 12, tzinfo=ZoneInfo("America/New_York"))
    points = [Point(p.timestamp.astimezone(timezone.utc), p.soc, p.home)
              for p in trips(local)]
    estimate = forecast(points, now=local)
    assert estimate.arrival.hour == 16
    assert estimate.arrival.utcoffset() == timedelta(hours=-5)
    assert estimate.soc == 65


def test_missing_arrival_soc_and_unknown_presence_break_training():
    points = [Point(p.timestamp, None if p.home else p.soc, p.home)
              for p in trips()]
    assert forecast(points) is None
    points = [Point(p.timestamp, p.soc, None if p.timestamp.hour == 15 else p.home)
              for p in trips()]
    assert forecast(points) is None


def test_overnight_trip_preserves_its_own_next_day_arrival():
    now = NOW.replace(hour=23)
    points = []
    for week in (1, 2, 3):
        departed = (now - timedelta(weeks=week)).replace(hour=22)
        points += [Point(departed, 75, False), Point((departed + timedelta(days=1)).replace(hour=6), 65, True)]
    estimate = forecast(points, now=now)
    assert estimate.arrival == (now + timedelta(days=1)).replace(hour=6)
    assert estimate.soc == 65


def test_missing_soc_during_trip_is_not_a_zero_consumption_observation():
    points = trips()
    points += [Point((NOW - timedelta(weeks=week)).replace(hour=14), None, False) for week in (1, 2, 3)]
    assert forecast(points) is None


import json


@pytest.mark.parametrize('case', json.loads((Path(__file__).parent / 'fixtures/ev_history.json').read_text()), ids=lambda case: case['name'])
def test_portable_history_contract(case):
    now = datetime.fromtimestamp(case['now'] / 1000, timezone.utc).astimezone(ZoneInfo(case['timezone']))
    points = [Point(datetime.fromtimestamp(p['at'] / 1000, timezone.utc), p['soc'], p['home']) for p in case['points']]
    actual = history.forecast_arrival(points, now=now, current_soc=case['soc'], home=case['home'])
    if case['expected'] is None:
        assert actual is None
    else:
        assert actual.arrival.timestamp() * 1000 == case['expected']['arrival']
        assert actual.soc == case['expected']['soc']
        assert actual.sample_days == case['expected']['sampleDays']
