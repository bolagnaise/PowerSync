"""Decide when to ask "is the car coming home?" and what to offer.

The optimizer asks only when the answer changes the plan by at least the
car's own threshold (dollars, default 1.00, 0 = never ask), and only shortly
before the first time slot where the two plans differ. Buttons come from
those slots, so with dynamic prices they follow tonight's windows.

Pure functions; the coordinator runs the second solve and sends the push.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import math
from typing import Any, Mapping, Sequence

DEFAULT_THRESHOLD = 1.0
MAX_THRESHOLD = 20.0
THRESHOLD_STEP = 0.5
ASK_LEAD = timedelta(minutes=90)
RECHECK_INTERVAL = timedelta(minutes=30)
DIVERGENCE_W = 500.0
DIVERGENCE_GAP_SLOTS = 2
UNCERTAIN_SOURCES = frozenset({"no_return_evidence", "learned"})
CATEGORY_ID = "ev_return_question"
NOTIFICATION_TYPE = "ev_return_question"
PROMPTS_KEY = "ev_return_prompts"


def normalize_threshold(value: Any) -> float:
    """Clamp a stored or posted threshold onto the slider's 0.50 steps."""
    if value is None or isinstance(value, bool):
        return DEFAULT_THRESHOLD
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return DEFAULT_THRESHOLD
    if not math.isfinite(number):
        return DEFAULT_THRESHOLD
    number = min(MAX_THRESHOLD, max(0.0, number))
    return round(number / THRESHOLD_STEP) * THRESHOLD_STEP


def threshold_for(configs: Sequence[Mapping[str, Any]] | None, *identifiers: str | None) -> float:
    wanted = {str(i).upper() for i in identifiers if i}
    for config in configs or ():
        if isinstance(config, Mapping) and str(config.get("vehicle_id", "")).upper() in wanted:
            return normalize_threshold(config.get("return_prompt_threshold", DEFAULT_THRESHOLD))
    return DEFAULT_THRESHOLD


def _series(values: Any, n: int) -> list[float]:
    out = []
    for index in range(n):
        try:
            value = float(values[index])
        except (TypeError, ValueError, IndexError):
            value = 0.0
        out.append(value if math.isfinite(value) else 0.0)
    return out


def plan_grid_cost(result: Any, import_prices: Sequence[float],
                   export_prices: Sequence[float], dt_hours: float) -> float:
    """Net grid cost of a solved plan, in the price unit ($/kWh -> $)."""
    n = len(import_prices)
    imports = _series(getattr(result, "grid_import_w", None) or [], n)
    exports = _series(getattr(result, "grid_export_w", None) or [], n)
    ip, ep = _series(import_prices, n), _series(export_prices, len(import_prices))
    return sum((imports[i] * ip[i] - exports[i] * ep[i]) / 1000.0 * dt_hours for i in range(n))


def answer_value(cost_with_car: float, cost_without_car: float, car_kwh: float,
                 import_prices: Sequence[float]) -> float:
    """What the answer is worth beyond buying the car's energy cheaply.

    The car's energy has to be bought somewhere either way. What changes is
    how the rest of the plan bends around it (battery held back from an
    export peak, extra import at a dear time). Pricing the car's energy at
    tonight's cheapest import isolates that.
    """
    cheapest = min((p for p in import_prices if math.isfinite(p)), default=0.0)
    return max(0.0, cost_with_car - cost_without_car - max(0.0, car_kwh) * max(0.0, cheapest))


def _battery_w(result: Any, n: int) -> list[float]:
    actions = getattr(getattr(result, "schedule", None), "actions", None) or []
    out = []
    for index in range(n):
        action = actions[index] if index < len(actions) else None
        charge = float(getattr(action, "battery_charge_w", 0.0) or 0.0)
        discharge = float(getattr(action, "battery_discharge_w", 0.0) or 0.0)
        out.append(charge - discharge)
    return out


def divergence(result_a: Any, result_b: Any, n: int) -> tuple[int, int] | None:
    """First run of slots where battery or export plans differ materially."""
    if n <= 0:
        return None
    bat_a, bat_b = _battery_w(result_a, n), _battery_w(result_b, n)
    exp_a = _series(getattr(result_a, "grid_export_w", None) or [], n)
    exp_b = _series(getattr(result_b, "grid_export_w", None) or [], n)
    differs = [abs(bat_a[i] - bat_b[i]) >= DIVERGENCE_W or abs(exp_a[i] - exp_b[i]) >= DIVERGENCE_W
               for i in range(n)]
    first = next((i for i, d in enumerate(differs) if d), None)
    if first is None:
        return None
    last = gap = first
    for index in range(first + 1, n):
        if differs[index]:
            last, gap = index, index
        elif index - gap > DIVERGENCE_GAP_SLOTS:
            break
    return first, last


def clock_label(when: datetime) -> str:
    hour = when.hour % 12 or 12
    suffix = "am" if when.hour < 12 else "pm"
    return f"{hour}{suffix}" if when.minute == 0 else f"{hour}:{when.minute:02d}{suffix}"


@dataclass(frozen=True)
class ReturnQuestion:
    vehicle_id: str
    title: str
    body: str
    options: tuple[dict, ...]
    value: float
    window_start: datetime
    window_end: datetime
    question_id: str = ""
    expires_at: datetime | None = None

    def push_data(self, entry_id: str) -> dict:
        data = {
            "type": NOTIFICATION_TYPE,
            "entry_id": entry_id,
            "vehicle_id": self.vehicle_id,
            "options": list(self.options),
            "value": round(self.value, 2),
            "question_id": self.question_id,
        }
        if self.expires_at is not None:
            data["expires_at"] = self.expires_at.isoformat()
        return data


def answer_rejection(record: Mapping[str, Any] | None, question_id: str,
                     now: datetime) -> str | None:
    """Why a tapped answer must not be applied, or None when it may.

    A notification can be tapped long after it was sent. Only the newest
    question for the car's current trip, unanswered and unexpired, counts.
    """
    if not record or not question_id or record.get("question_id") != question_id:
        return "This question was replaced or the car's trip has ended."
    if record.get("answered_at"):
        return "This question was already answered."
    try:
        expires = datetime.fromisoformat(str(record.get("expires_at") or record.get("asked_until")))
    except (TypeError, ValueError):
        return "This question has expired."
    if expires.tzinfo is None or now >= expires:
        return "This question has expired."
    return None


def build_question(*, vehicle_id: str, name: str, soc: float | None, value: float,
                   window_start: datetime, window_end: datetime, now: datetime,
                   currency: str = "$", question_id: str = "",
                   expires_at: datetime | None = None) -> ReturnQuestion:
    options = []
    if window_start > now + timedelta(minutes=30):
        options.append({"id": "before", "label": f"Before {clock_label(window_start)}",
                        "mode": "home_at", "time": window_start.isoformat()})
    options.append({"id": "after", "label": f"After {clock_label(window_end)}",
                    "mode": "home_at", "time": window_end.isoformat()})
    options.append({"id": "not_tonight", "label": "Not tonight", "mode": "not_home"})
    level = f" ({round(soc)}%)" if soc is not None else ""
    title = f"{name} is away{level}. When will it be home?"
    # Phone buttons are fixed ("Before", "After", "Not tonight"), so the text
    # names tonight's times.
    choices = (f"before {clock_label(window_start)}, " if options[0]["id"] == "before" else "")
    body = (f"Home {choices}after {clock_label(window_end)}, or not tonight? "
            f"Knowing could change tonight's plan by about {currency}{value:.2f}.")
    return ReturnQuestion(vehicle_id, title, body, tuple(options), value, window_start,
                          window_end, question_id, expires_at)


def should_evaluate(*, now: datetime, decision: Any, threshold: float,
                    record: Mapping[str, Any] | None) -> bool:
    """Cheap gate before spending a second solve on this car."""
    if threshold <= 0 or decision is None or not getattr(decision, "away", False):
        return False
    if getattr(decision, "source", None) not in UNCERTAIN_SOURCES:
        return False
    if record:
        try:
            if record.get("asked_until") and now < datetime.fromisoformat(record["asked_until"]):
                return False
            if record.get("checked_at") and now - datetime.fromisoformat(record["checked_at"]) < RECHECK_INTERVAL:
                return False
        except (TypeError, ValueError):
            pass
    return True


def should_ask(*, now: datetime, value: float, threshold: float,
               window_start: datetime | None) -> bool:
    return (window_start is not None and threshold > 0 and value >= threshold
            and window_start - now <= ASK_LEAD)
