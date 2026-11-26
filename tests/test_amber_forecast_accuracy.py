"""Amber forecast accuracy: earlier export forecasts compared with settled prices.

Ticket #191 asked for a single-glance view of whether forecast export spikes
actually happen, to decide on Amber Forecast Confidence. These tests pin the
display-only tracker behind the dashboard's Amber forecast accuracy card.
"""

from __future__ import annotations

import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
PATH = ROOT / "custom_components/power_sync/amber_forecast_accuracy.py"
spec = importlib.util.spec_from_file_location("amber_forecast_accuracy_under_test", PATH)
acc = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = acc
spec.loader.exec_module(acc)

START = datetime(2026, 11, 20, 0, 0, tzinfo=timezone.utc)


def iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def forecast_entry(start: datetime, export_cents: float, minutes: int = 30, **extra) -> dict:
    # Amber feedIn perKwh is negative when you are paid to export.
    entry = {
        "type": "ForecastInterval", "channelType": "feedIn", "duration": minutes,
        "startTime": iso(start + timedelta(seconds=1)), "endTime": iso(start + timedelta(minutes=minutes)),
        "perKwh": -export_cents, "advancedPrice": {"low": -export_cents / 2, "predicted": -export_cents, "high": -export_cents * 2},
    }
    entry.update(extra)
    return entry


def actual_entry(start: datetime, export_cents: float, minutes: int = 5) -> dict:
    return {
        "type": "ActualInterval", "channelType": "feedIn", "duration": minutes,
        "startTime": iso(start + timedelta(seconds=1)), "endTime": iso(start + timedelta(minutes=minutes)),
        "perKwh": -export_cents,
    }


def settle(start: datetime, export_cents: float) -> list[dict]:
    return [actual_entry(start + timedelta(minutes=5 * i), export_cents) for i in range(6)]


def run_history(prices: dict[int, tuple[float, float]], hours: int, ordinary: float = 8.0):
    """Simulate 5-minute refreshes. prices: period index -> (forecast, settled) cents."""
    tracker = acc.AmberForecastAccuracyTracker()
    periods = hours * 2
    for step in range(periods * 6 + 1):
        now = START + timedelta(minutes=5 * step)
        forecast = []
        for period in range(periods + 12):
            start = START + timedelta(minutes=30 * period)
            if start > now:
                forecast.append(forecast_entry(start, prices.get(period, (ordinary, ordinary))[0]))
        settled = []
        for period in range(periods):
            start = START + timedelta(minutes=30 * period)
            if start + timedelta(minutes=30) <= now:
                settled += settle(start, prices.get(period, (ordinary, ordinary))[1])
        tracker.observe(forecast, settled, now)
    return tracker, START + timedelta(minutes=30 * periods)


def test_forecast_is_kept_only_from_its_lead_window_and_sign_is_export_earnings():
    tracker = acc.AmberForecastAccuracyTracker()
    period = START + timedelta(hours=6)
    # 6 h ahead: outside both lead windows, so nothing is recorded.
    tracker.observe([forecast_entry(period, 40)], [], period - timedelta(hours=6))
    assert tracker.as_dict()["buckets"] == {}
    # 4 h 30 m ahead: the 4-hour forecast. Later refreshes in the window win.
    tracker.observe([forecast_entry(period, 40)], [], period - timedelta(minutes=290))
    tracker.observe([forecast_entry(period, 35)], [], period - timedelta(minutes=250))
    # 3 h ahead: between windows, ignored.
    tracker.observe([forecast_entry(period, 99)], [], period - timedelta(hours=3))
    # 1 h 10 m ahead: the 1-hour forecast; 50 m ahead is too late.
    tracker.observe([forecast_entry(period, 20)], [], period - timedelta(minutes=70))
    tracker.observe([forecast_entry(period, 5)], [], period - timedelta(minutes=50))
    record = tracker.as_dict()["buckets"][iso(period)]
    assert record["f240"] == 0.35
    assert record["f60"] == 0.20


def test_forecast_type_follows_the_configured_amber_forecast():
    tracker = acc.AmberForecastAccuracyTracker(forecast_type=lambda: "high")
    period = START + timedelta(hours=2)
    tracker.observe([forecast_entry(period, 30)], [], period - timedelta(minutes=70))
    assert tracker.as_dict()["buckets"][iso(period)]["f60"] == 0.60


def test_five_minute_forecasts_average_into_the_half_hour_and_settled_needs_coverage():
    tracker = acc.AmberForecastAccuracyTracker()
    period = START + timedelta(hours=2)
    entries = [forecast_entry(period + timedelta(minutes=5 * i), 10 + 10 * i, minutes=5) for i in range(6)]
    tracker.observe(entries, [], period - timedelta(minutes=65))
    assert tracker.as_dict()["buckets"][iso(period)]["f60"] == 0.35
    # Only 20 minutes settled: not enough to compare yet.
    end = period + timedelta(minutes=30)
    tracker.observe([], [actual_entry(period + timedelta(minutes=5 * i), 12) for i in range(4)], end)
    assert tracker._settled(tracker.as_dict()["buckets"][iso(period)]) is None
    # Settled entries repeat across refreshes without double counting.
    tracker.observe([], settle(period, 12), end)
    tracker.observe([], settle(period, 12), end + timedelta(minutes=5))
    assert abs(tracker._settled(tracker.as_dict()["buckets"][iso(period)]) - 0.12) < 1e-9


def test_spikes_that_happened_and_spikes_that_did_not_are_counted_as_events():
    prices = {
        10: (60, 55), 11: (70, 66),   # one two-period spike that settled
        30: (80, 9),                  # forecast spike that never came
        50: (50, 10),                 # another miss
        70: (8, 90),                  # unforecast spike
    }
    tracker, now = run_history(prices, hours=48)
    summary = tracker.summary(now, confidence_enabled=False)
    one_hour = summary["leads"]["60"]
    assert summary["baseline_per_kwh"] == 0.18  # 8c ordinary + 10c margin
    assert one_hour["predicted_spikes"] == 3
    assert one_hour["happened"] == 1 and one_hour["missed"] == 2
    assert one_hour["hit_rate"] == 33
    assert one_hour["unforecast_spike_periods"] == 1
    events = sorted((e["start"], e["happened"]) for e in summary["events"] if e["lead_minutes"] == 60)
    assert events == [
        (iso(START + timedelta(hours=5)), True),
        (iso(START + timedelta(hours=15)), False),
        (iso(START + timedelta(hours=25)), False),
    ]
    assert one_hour["advice"] == "consider_confidence"
    assert tracker.summary(now, confidence_enabled=True)["leads"]["60"]["advice"] == "confidence_suits"
    # Chart points carry forecast and settled values in $/kWh.
    point = next(p for p in summary["points"] if p["t"] == iso(START + timedelta(hours=15)))
    assert point == {"t": point["t"], "actual": 0.09, "f60": 0.8, "f240": 0.8}


def test_mostly_happening_spikes_advise_keeping_confidence_off():
    prices = {10: (60, 58), 30: (70, 66), 50: (50, 49), 60: (40, 9)}
    tracker, now = run_history(prices, hours=48)
    lead = tracker.summary(now, confidence_enabled=False)["leads"]["60"]
    assert (lead["happened"], lead["missed"], lead["hit_rate"]) == (3, 1, 75)
    assert lead["advice"] == "keep_off"
    assert tracker.summary(now, confidence_enabled=True)["leads"]["60"]["advice"] == "consider_off"


def test_advice_waits_for_two_days_and_three_spikes():
    tracker, now = run_history({10: (60, 9), 20: (60, 9), 30: (60, 9)}, hours=24)
    lead = tracker.summary(now, confidence_enabled=False)["leads"]["60"]
    assert lead["missed"] == 3
    assert lead["advice"] == "collecting"


def test_record_survives_a_restart_and_old_periods_are_pruned():
    tracker, now = run_history({10: (60, 9)}, hours=12)
    saved = json.loads(json.dumps(tracker.as_dict()))
    restored = acc.AmberForecastAccuracyTracker()
    restored.load(saved)
    assert restored.summary(now)["leads"] == tracker.summary(now)["leads"]
    later = now + timedelta(days=acc.RETENTION_DAYS + 1)
    restored.observe([], [], later)
    assert restored.as_dict()["buckets"] == {}
    restored.load({"buckets": {"not-a-time": {}, iso(now): "bad"}, "first_seen": 5})
    assert restored.as_dict() == {"buckets": {}, "first_seen": None}


def test_malformed_entries_and_naive_clock_are_ignored():
    tracker = acc.AmberForecastAccuracyTracker(forecast_type=lambda: 1 / 0)
    period = START + timedelta(hours=2)
    bad = [None, {"channelType": "general", "type": "ForecastInterval"},
           forecast_entry(period, 30, advancedPrice=None, perKwh=float("nan")),
           {"channelType": "feedIn", "type": "ForecastInterval", "perKwh": -5}]
    assert tracker.observe(bad, bad, period.replace(tzinfo=None)) is False
    tracker.observe(bad, bad, period - timedelta(minutes=70))
    assert tracker.as_dict()["buckets"] == {}


def test_amber_coordinator_records_accuracy_without_failing_the_price_refresh():
    source = (ROOT / "custom_components/power_sync/coordinator.py").read_text()
    body = source[source.index("class AmberPriceCoordinator"):source.index("class LocalvoltsPriceCoordinator")]
    assert 'self._record_forecast_accuracy(result["forecast"], forecast_5min)' in body
    helper = body[body.index("def _record_forecast_accuracy"):body.index("async def _async_update_data")]
    assert "except Exception" in helper and "tracker.observe(" in helper
    init = (ROOT / "custom_components/power_sync/__init__.py").read_text()
    assert "await _attach_amber_forecast_accuracy(hass, entry, amber_coordinator)" in init


FRONTEND = ROOT / "custom_components/power_sync/frontend"


def test_dashboard_card_shows_counts_chart_markers_and_advice():
    from test_dashboard_board import BOARD_PATH, _board_script

    def _run_board(body: str) -> dict:
        script = _board_script(body).replace(
            "PsBatteryPlanCard, psApiCache", "PsBatteryPlanCard, PsForecastAccuracyCard, psApiCache", 1,
        )
        result = subprocess.run(["node", "-e", script, str(BOARD_PATH)], capture_output=True, text=True, timeout=20)
        assert result.returncode == 0, result.stderr[-2000:]
        return json.loads(result.stdout.strip().splitlines()[-1])

    tracker, now = run_history({10: (60, 55), 30: (80, 9), 50: (50, 10), 70: (8, 90)}, hours=48)
    summary = tracker.summary(now, confidence_enabled=False)
    attrs = {k: summary[k] for k in ("leads", "points", "events", "hours_tracked", "baseline_per_kwh")}
    out = _run_board(r"""
  class El {
    constructor() { this.children = []; this.attrs = {}; this.clientWidth = 320; this.clientHeight = 180; this.classList = { contains: () => false }; }
    appendChild(el) { this.children.push(el); el.parentElement = this; }
    insertBefore(el) { this.appendChild(el); }
    setAttribute(k, v) { this.attrs[k] = v; }
    querySelector(sel) { return this.children.find(e => '.' + e.className === sel); }
    getBoundingClientRect() { return { left: 0, width: 320 }; }
  }
  context.document.createElement = () => new El();
  context.document.createElementNS = () => new El();
  class FixedDate extends Date {
    constructor(...args) { super(...(args.length ? args : [NOW])); }
    static now() { return new FixedDate().getTime(); }
  }
  context.Date = FixedDate;
  const card = new T.PsForecastAccuracyCard();
  card._hass = { states: {
    'sensor.power_sync_amber_forecast_accuracy': { state: '33', attributes: ATTRS },
    'switch.power_sync_amber_forecast_confidence': { state: 'off', attributes: {} },
  } };
  card._config = { entities: { accuracy: 'sensor.power_sync_amber_forecast_accuracy', confidence: 'switch.power_sync_amber_forecast_confidence' } };
  card._render();
  const html1 = card.innerHTML;
  const svg = new El(), parent = new El(); parent.appendChild(svg);
  card.querySelector = () => svg;
  card._draw();
  const circles = svg.children.filter(c => c.attrs.cx !== undefined);
  const marks = circles.map(c => c.attrs.fill === c.attrs.stroke);
  card._lead = '240'; card._render();
  console.log(JSON.stringify({ html1, html4: card.innerHTML, marks, paths: svg.children.filter(c => c.attrs.d).length }));
""".replace("ATTRS", json.dumps(attrs)).replace("NOW", str(int(now.timestamp() * 1000))))
    html = out["html1"]
    assert 'aria-pressed="true">1 h before' in html
    assert ">3</span>" in html and "Forecast spikes" in html
    assert "33<small>%</small>" in html
    assert "Most forecast spikes didn’t happen" in html
    assert "18.0c/kWh" in html
    assert "Amber Forecast Confidence is off" in html
    assert 'aria-pressed="true">4 h before' in out["html4"]
    assert out["paths"] >= 2
    assert sorted(out["marks"]) == [True, True, True]


def test_strategy_adds_the_card_only_when_the_accuracy_sensor_exists():
    from test_dashboard_board import _strategy_script, STRATEGY_PATH

    def run(with_sensor: bool) -> dict:
        script = _strategy_script(False)
        if with_sensor:
            script = script.replace(
                "  const hass = {",
                "states['sensor.power_sync_amber_forecast_accuracy'] = s(40, { leads: {} });"
                "states['switch.power_sync_amber_forecast_confidence'] = s('off');\n  const hass = {",
            )
        script = script.replace(
            "      keys: cards.map(c => c.key),",
            "      keys: cards.map(c => c.key), fcacc: cards.find(c => c.key === 'fcacc') || null,",
        )
        result = subprocess.run(["node", "-e", script, str(STRATEGY_PATH)], check=True, capture_output=True, text=True)
        return json.loads(result.stdout.strip().splitlines()[-1])

    assert "fcacc" not in run(False)["keys"]
    out = run(True)
    card = out["fcacc"]
    assert card["native"] == "ps-forecast-accuracy-card"
    assert card["title"] == "Amber forecast accuracy"
    assert card["config"]["entities"] == {
        "accuracy": "sensor.power_sync_amber_forecast_accuracy",
        "confidence": "switch.power_sync_amber_forecast_confidence",
    }
    assert "'ps-forecast-accuracy-card': PsForecastAccuracyCard" in (FRONTEND / "power-sync-dashboard.js").read_text()
