"""The "is the car coming home?" question: when it is sent and what it offers."""
from __future__ import annotations

import asyncio
import ast
import importlib
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
COMPONENT_ROOT = ROOT / "custom_components" / "power_sync"
_SENTINEL = object()
_NAMES = (
    "power_sync", "power_sync.optimization", "power_sync.optimization.ev_return",
    "power_sync.optimization.ev_return_prompt", "power_sync.automations",
    "power_sync.automations.actions", "power_sync.ev_charging_services",
)
TZ = timezone(timedelta(hours=10))
NOW = datetime(2026, 11, 28, 16, 0, tzinfo=TZ)
STEP = timedelta(minutes=30)
TIMES = [NOW + STEP * i for i in range(12)]


@pytest.fixture()
def mods():
    saved = {name: sys.modules.get(name, _SENTINEL) for name in _NAMES}
    for name in _NAMES:
        sys.modules.pop(name, None)
    for name, path in (("power_sync", COMPONENT_ROOT), ("power_sync.optimization", COMPONENT_ROOT / "optimization")):
        module = types.ModuleType(name)
        module.__path__ = [str(path)]
        sys.modules[name] = module
    prompt = importlib.import_module("power_sync.optimization.ev_return_prompt")
    ev_return = importlib.import_module("power_sync.optimization.ev_return")
    try:
        yield prompt, ev_return
    finally:
        for name, value in saved.items():
            if value is _SENTINEL:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value


def _result(battery_w, export_w, import_w=None, feasible=True):
    actions = [SimpleNamespace(battery_charge_w=max(0, b), battery_discharge_w=max(0, -b)) for b in battery_w]
    return SimpleNamespace(feasible=feasible, schedule=SimpleNamespace(actions=actions),
                           grid_export_w=list(export_w), grid_import_w=list(import_w or [0] * len(export_w)))


def test_threshold_normalizes_to_slider_steps(mods):
    prompt, _ = mods
    assert prompt.normalize_threshold(None) == 1.0
    assert prompt.normalize_threshold("2.3") == 2.5
    assert prompt.normalize_threshold(-4) == 0
    assert prompt.normalize_threshold(99) == 20
    assert prompt.normalize_threshold(float("nan")) == 1.0
    configs = [{"vehicle_id": "vin1", "return_prompt_threshold": 0}]
    assert prompt.threshold_for(configs, "1", "VIN1") == 0
    assert prompt.threshold_for(configs, "other") == 1.0


def test_value_excludes_the_cars_own_cheapest_energy(mods):
    prompt, _ = mods
    # With the car, the battery skips a 5 kWh export at 40c; car needs 10 kWh, cheapest import 10c.
    assert prompt.answer_value(5.0, 2.0, 10.0, [0.1, 0.3, 0.5]) == pytest.approx(2.0)
    assert prompt.answer_value(1.0, 2.0, 10.0, [0.1]) == 0


def test_divergence_finds_the_export_window(mods):
    prompt, _ = mods
    held = _result([0] * 12, [0] * 12)
    exporting = _result([0, 0, 0, 0, -5000, -5000, -5000, 0, 0, 0, 0, 0],
                        [0, 0, 0, 0, 5000, 5000, 5000, 0, 0, 0, 0, 0])
    assert prompt.divergence(held, exporting, 12) == (4, 6)
    assert prompt.divergence(held, held, 12) is None


def test_question_buttons_follow_the_window(mods):
    prompt, _ = mods
    q = prompt.build_question(vehicle_id="1", name="W3RT1E", soc=59, value=4.2,
                              window_start=NOW + timedelta(hours=2), window_end=NOW + timedelta(hours=4),
                              now=NOW)
    assert [o["label"] for o in q.options] == ["Before 6pm", "After 8pm", "Not tonight"]
    assert q.title == "W3RT1E is away (59%). When will it be home?"
    assert q.body == ("Home before 6pm, after 8pm, or not tonight? "
                      "Knowing could change tonight's plan by about $4.20.")
    soon = prompt.build_question(vehicle_id="1", name="W3RT1E", soc=None, value=2,
                                 window_start=NOW + timedelta(minutes=10),
                                 window_end=NOW + timedelta(hours=1, minutes=30), now=NOW)
    assert [o["id"] for o in soon.options] == ["after", "not_tonight"]
    assert prompt.clock_label(NOW.replace(hour=17, minute=30)) == "5:30pm"


def test_gates(mods):
    prompt, ev_return = mods
    uncertain = ev_return.EVReturnDecision(True, False, "no_return_evidence", "")
    settled = ev_return.EVReturnDecision(True, False, "owner_not_home", "")
    assert prompt.should_evaluate(now=NOW, decision=uncertain, threshold=1, record=None)
    assert not prompt.should_evaluate(now=NOW, decision=settled, threshold=1, record=None)
    assert not prompt.should_evaluate(now=NOW, decision=uncertain, threshold=0, record=None)
    asked = {"asked_until": (NOW + timedelta(hours=18)).isoformat()}
    assert not prompt.should_evaluate(now=NOW, decision=uncertain, threshold=1, record=asked)
    recent = {"checked_at": (NOW - timedelta(minutes=10)).isoformat()}
    assert not prompt.should_evaluate(now=NOW, decision=uncertain, threshold=1, record=recent)
    start = NOW + timedelta(hours=1)
    assert prompt.should_ask(now=NOW, value=1.0, threshold=1.0, window_start=start)
    assert not prompt.should_ask(now=NOW, value=0.99, threshold=1.0, window_start=start)
    assert not prompt.should_ask(now=NOW, value=5, threshold=1.0, window_start=NOW + timedelta(hours=2))


_METHODS = ("_maybe_ask_ev_return", "_ev_return_counterfactual")


def _probe(mods, pushes):
    automations = types.ModuleType("power_sync.automations")
    automations.__path__ = []
    actions = types.ModuleType("power_sync.automations.actions")

    async def send(hass, title, message, **kwargs):
        pushes.append((title, message, kwargs))
    actions._send_expo_push = send
    services = types.ModuleType("power_sync.ev_charging_services")
    services._default_not_home_until = lambda now: now + timedelta(hours=18)
    sys.modules.update({"power_sync.automations": automations,
                        "power_sync.automations.actions": actions,
                        "power_sync.ev_charging_services": services})
    source = (COMPONENT_ROOT / "optimization" / "coordinator.py").read_text()
    segments = [ast.get_source_segment(source, node) for node in ast.walk(ast.parse(source))
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in _METHODS]
    assert len(segments) == len(_METHODS)
    body = "\n\n".join("\n".join("    " + line for line in seg.splitlines()) for seg in segments)
    ns = {"__name__": "power_sync.optimization._prompt_probe", "__package__": "power_sync.optimization",
          "dt_util": SimpleNamespace(now=lambda: NOW), "timedelta": timedelta,
          "currency_for_entry": lambda entry, hass: "AUD",
          "currency_metadata": lambda code: {"currency": code},
          "_LOGGER": SimpleNamespace(debug=lambda *a, **k: None)}
    exec(compile("class Probe:\n" + body + "\n", "<probe>", "exec"), ns)
    return ns["Probe"]


def _coordinator(mods, pushes, configs):
    _, ev_return = mods
    Probe = _probe(mods, pushes)
    store = SimpleNamespace(_data={"vehicle_charging_configs": configs})

    async def save():
        store.saved = True
    store.async_save = save
    c = Probe()
    c._entry = SimpleNamespace(entry_id="e1")
    c._config = SimpleNamespace(interval_minutes=30)
    c.hass = SimpleNamespace(data={"power_sync": {"e1": {"automation_store": store}}})
    c._price_timestamps = lambda n: TIMES[:n]
    decision = ev_return.EVReturnDecision(True, False, "no_return_evidence", "")
    c._ev_return_decisions = {"1": decision, "VIN1": decision}
    c._ev_return_status = {"1": {"vin": "VIN1", "soc": 59.0}}
    # The car's home Tesla schedule: 7 kW from 18:00 for 3 slots.
    c._ev_return_counterfactual_w = {"VIN1": [0, 0, 0, 0, 7000, 7000, 7000, 0, 0, 0, 0, 0]}
    return c, store


def test_reported_case_asks_before_the_export_window(mods):
    pushes = []
    c, store = _coordinator(mods, pushes, [{"vehicle_id": "VIN1", "display_name": "W3RT1E"}])
    imports = [0.30] * 12
    exports = [0.05, 0.05, 0.05, 0.05, 0.60, 0.60, 0.60, 0.05, 0.05, 0.05, 0.05, 0.05]
    actual = _result([0, 0, 0, 0, -5000, -5000, -5000, 0, 0, 0, 0, 0],
                     [0, 0, 0, 0, 5000, 5000, 5000, 0, 0, 0, 0, 0])
    with_car = _result([0] * 12, [0] * 12, [0, 0, 0, 0, 2000, 2000, 2000, 0, 0, 0, 0, 0])
    loads = []

    async def run_once(reserve, *, load_override=None, ev_plan_override=None):
        loads.append(load_override)
        return with_car

    # 16:00 now; window opens 18:00 -> outside the 90 min lead: evaluated, not sent.
    asyncio.run(c._maybe_ask_ev_return(run_once, actual, imports, exports, [0.5] * 12, [], TIMES, None))
    assert pushes == []
    assert loads[0][4] == pytest.approx(7.5)
    status = c._ev_return_status["1"]
    assert status["question_window"][0] == TIMES[4].isoformat()
    # Without car: earns 3 slots x 5 kW x 0.5 h x 60c = 4.50. With car: imports 3 kWh at 30c = 0.90.
    # Car energy 10.5 kWh x cheapest 30c = 3.15. Value = 0.90 + 4.50 - 3.15.
    assert status["question_value"] == pytest.approx(2.25)
    assert store._data["ev_return_prompts"]["1"]["checked_at"]

    # 45 minutes later (past the 30 min re-check) the window is 75 min away: ask.
    later = NOW + timedelta(minutes=45)
    import power_sync.optimization.ev_return_prompt  # noqa: F401
    c.__class__._maybe_ask_ev_return.__globals__["dt_util"] = SimpleNamespace(now=lambda: later)
    asyncio.run(c._maybe_ask_ev_return(run_once, actual, imports, exports, [0.5] * 12, [], TIMES, None))
    assert len(pushes) == 1
    title, body, kwargs = pushes[0]
    assert title.startswith("W3RT1E is away (59%)")
    assert kwargs["category_id"] == "ev_return_question"
    assert [o["id"] for o in kwargs["data"]["options"]] == ["before", "after", "not_tonight"]
    assert kwargs["data"]["vehicle_id"] == "1"
    assert store._data["ev_return_prompts"]["1"]["asked_until"]

    # Once asked, the same trip is not asked again.
    asyncio.run(c._maybe_ask_ev_return(run_once, actual, imports, exports, [0.5] * 12, [], TIMES, None))
    assert len(pushes) == 1


def test_threshold_off_never_runs_a_second_solve(mods):
    pushes = []
    c, _ = _coordinator(mods, pushes, [{"vehicle_id": "VIN1", "return_prompt_threshold": 0}])

    async def run_once(*args, **kwargs):
        raise AssertionError("second solve must not run")

    asyncio.run(c._maybe_ask_ev_return(run_once, _result([0] * 12, [0] * 12), [0.3] * 12,
                                       [0.05] * 12, [0.5] * 12, [], TIMES, None))
    assert pushes == []
