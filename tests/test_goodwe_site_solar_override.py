"""GoodWe whole-site solar override.

A GoodWe hybrid only measures the panels wired to it.  With an AC-coupled array
(e.g. Enphase microinverters) the inverter's PV figure under-reports production
and its house-consumption figure under-reports load by the same amount.  When
``custom_solar_power_entity`` is configured the GoodWe coordinator uses that
whole-site sensor for solar, adds the AC-coupled surplus back to load, and marks
the sample invalid (``solar_power_valid=False``) rather than silently using the
half-blind figure when the sensor is unreadable.
"""

from __future__ import annotations

import ast
import asyncio
import importlib
import math
import sys
import textwrap
import types
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
COMPONENT = ROOT / "custom_components" / "power_sync"
COORDINATOR_PATH = COMPONENT / "coordinator.py"
INIT_PATH = COMPONENT / "__init__.py"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _load_override_module():
    """Import inverters/entity_override.py under a stub ``power_sync`` package."""
    names = (
        "power_sync",
        "power_sync.inverters",
        "power_sync.inverters.entity_override",
    )
    saved = {name: sys.modules.get(name) for name in names}

    power_sync = types.ModuleType("power_sync")
    power_sync.__path__ = [str(COMPONENT)]
    sys.modules["power_sync"] = power_sync
    inverters = types.ModuleType("power_sync.inverters")
    inverters.__path__ = [str(COMPONENT / "inverters")]
    sys.modules["power_sync.inverters"] = inverters
    sys.modules.pop("power_sync.inverters.entity_override", None)

    module = importlib.import_module("power_sync.inverters.entity_override")

    def restore() -> None:
        for name, previous in saved.items():
            if previous is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous

    return module, restore


@pytest.fixture()
def override():
    module, restore = _load_override_module()
    try:
        yield module
    finally:
        restore()


class _State:
    def __init__(self, state, unit: str | None = None) -> None:
        self.state = state
        self.attributes = {"unit_of_measurement": unit} if unit else {}


class _States:
    def __init__(self, states: dict) -> None:
        self._states = states

    def get(self, entity_id):
        return self._states.get(entity_id or "")


class _Hass:
    def __init__(self, states: dict) -> None:
        self.states = _States(states)


# --------------------------------------------------------------------------- #
# read_power_entity_kw
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    ("state", "unit", "expected"),
    [
        ("6088", "W", 6.088),
        ("6.088", "kW", 6.088),
        ("0.006", "MW", 6.0),
        ("6.088", None, 6.088),
        ("0", "W", 0.0),
        (" 1500 ", "W", 1.5),
    ],
)
def test_read_power_entity_kw_converts_units(override, state, unit, expected):
    hass = _Hass({"sensor.site_solar": _State(state, unit)})
    assert override.read_power_entity_kw(hass, "sensor.site_solar") == pytest.approx(expected)


@pytest.mark.parametrize(
    "state",
    ["unknown", "unavailable", "none", "None", "", "abc", "nan", "inf", "-inf", "-5"],
)
def test_read_power_entity_kw_rejects_unusable_states(override, state):
    hass = _Hass({"sensor.site_solar": _State(state, "W")})
    assert override.read_power_entity_kw(hass, "sensor.site_solar") is None


@pytest.mark.parametrize("entity_id", [None, "", "   ", "sensor.missing"])
def test_read_power_entity_kw_missing_entity_is_none(override, entity_id):
    hass = _Hass({"sensor.site_solar": _State("1000", "W")})
    assert override.read_power_entity_kw(hass, entity_id) is None


# --------------------------------------------------------------------------- #
# apply_site_solar_override (pure maths)
# --------------------------------------------------------------------------- #

def test_override_adds_ac_coupled_surplus_to_solar_and_load(override):
    # GoodWe sees 2.5 kW of its own panels; Enphase adds 6.1 kW; GoodWe's load
    # figure (1.9 kW) is missing exactly that 6.1 kW of AC-coupled production.
    solar, load, valid = override.apply_site_solar_override(2.5, 1.9, 8.6)
    assert solar == pytest.approx(8.6)
    assert load == pytest.approx(8.0)
    assert valid is True


def test_unreadable_override_returns_inverter_figures_and_marks_invalid(override):
    solar, load, valid = override.apply_site_solar_override(2.5, 1.9, None)
    assert (solar, load) == (2.5, 1.9)
    assert valid is False


def test_override_never_reports_less_solar_than_the_inverter_itself(override):
    # A lagging site sensor must not push solar below what the GoodWe measures.
    solar, load, valid = override.apply_site_solar_override(2.5, 1.9, 2.0)
    assert solar == pytest.approx(2.5)
    assert load == pytest.approx(1.9)
    assert valid is True


def test_override_at_night_changes_nothing(override):
    solar, load, valid = override.apply_site_solar_override(0.0, 0.6, 0.0)
    assert (solar, load, valid) == (0.0, 0.6, True)


# --------------------------------------------------------------------------- #
# GoodWeEnergyCoordinator._async_update_data (real method, stand-in objects)
# --------------------------------------------------------------------------- #

def _find_class_method(tree: ast.Module, class_name: str, method_name: str):
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == method_name:
                    return item
    raise AssertionError(f"{class_name}.{method_name} not found")


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def __call__(self, *args):
        self.calls.append(args)


class _FakeController:
    def __init__(self, runtime: dict) -> None:
        self.runtime = runtime

    async def connect(self) -> bool:
        return True

    def get_runtime_data(self) -> dict:
        return dict(self.runtime)


class _FakeEnergyAcc:
    _last_update = datetime(2026, 10, 3, tzinfo=timezone.utc)

    def as_dict(self) -> dict:
        return {}

    async def async_restore(self) -> None:  # pragma: no cover - never reached
        return None


def _run_update(*, override_entity: str, states: dict, runtime: dict | None = None):
    """Run the real, patched ``_async_update_data`` and return (result, accumulator calls)."""
    module, restore = _load_override_module()
    try:
        source = COORDINATOR_PATH.read_text(encoding="utf-8")
        node = _find_class_method(ast.parse(source), "GoodWeEnergyCoordinator", "_async_update_data")
        method_source = textwrap.dedent(ast.get_source_segment(source, node, padded=True))

        accumulator = _Recorder()
        logger = SimpleNamespace(
            debug=lambda *a, **k: None,
            info=lambda *a, **k: None,
            warning=lambda *a, **k: None,
            error=lambda *a, **k: None,
        )

        class UpdateFailed(Exception):
            pass

        namespace = {
            "__package__": "power_sync",
            "__name__": "power_sync.coordinator",
            "_LOGGER": logger,
            "dt_util": SimpleNamespace(utcnow=lambda: datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc)),
            "_get_current_prices": lambda hass, entry_id: (0.20, 0.05),
            "_update_energy_accumulator_with_ev_load": accumulator,
            "UpdateFailed": UpdateFailed,
            "asyncio": asyncio,
            "math": math,
        }
        exec(method_source, namespace)  # noqa: S102 - extracting the real method under test

        runtime_data = {
            "telemetry_ready": True,
            "solar_power": 2.5,   # GoodWe's own PV strings only
            "grid_power": -0.4,
            "battery_power": 0.0,
            "load_power": 1.9,    # GoodWe house_consumption: misses the AC-coupled array
            "battery_level": 80.0,
            "rated_power_w": 10000,
            "entity_telemetry": True,
        }
        if runtime:
            runtime_data.update(runtime)

        controller = _FakeController(runtime_data)
        fake_self = SimpleNamespace(
            hass=_Hass(states),
            _entry_id="entry1",
            _energy_acc=_FakeEnergyAcc(),
            _using_entity_telemetry=True,
            _telemetry_validated=True,
            _telemetry_controller=controller,
            _controller=None,
            _connected=True,
            _solar_override_entity=override_entity,
            _native_integration_enabled=lambda: True,
            _native_stale_data=lambda: None,
        )
        result = asyncio.run(namespace["_async_update_data"](fake_self))
        return result, accumulator.calls
    finally:
        restore()


SITE_SOLAR = "sensor.total_solar_power"


def test_without_override_behaviour_is_unchanged():
    result, calls = _run_update(override_entity="", states={})

    assert result["solar_power"] == pytest.approx(2.5)
    assert result["load_power"] == pytest.approx(1.9)
    assert "solar_power_valid" not in result
    assert len(calls) == 1  # daily energy still accumulated


def test_override_uses_site_solar_and_corrects_load():
    states = {SITE_SOLAR: _State("8600", "W")}
    result, calls = _run_update(override_entity=SITE_SOLAR, states=states)

    assert result["solar_power"] == pytest.approx(8.6)
    assert result["load_power"] == pytest.approx(8.0)
    assert result["solar_power_valid"] is True
    # accumulator receives the corrected figures
    assert len(calls) == 1
    _acc, _hass, _entry, solar_kw, _grid, _batt, load_kw, _buy, _sell = calls[0]
    assert solar_kw == pytest.approx(8.6)
    assert load_kw == pytest.approx(8.0)


def test_unavailable_override_is_flagged_invalid_and_not_accumulated():
    states = {SITE_SOLAR: _State("unavailable", "W")}
    result, calls = _run_update(override_entity=SITE_SOLAR, states=states)

    assert result["solar_power_valid"] is False
    # the half-blind GoodWe-only number is still reported for display ...
    assert result["solar_power"] == pytest.approx(2.5)
    assert result["load_power"] == pytest.approx(1.9)
    # ... but must not corrupt the daily energy totals
    assert calls == []


def test_missing_override_entity_is_flagged_invalid():
    result, calls = _run_update(override_entity=SITE_SOLAR, states={})

    assert result["solar_power_valid"] is False
    assert calls == []


def test_not_ready_telemetry_still_skips_accumulation_with_override():
    states = {SITE_SOLAR: _State("8600", "W")}
    result, calls = _run_update(
        override_entity=SITE_SOLAR,
        states=states,
        runtime={"telemetry_ready": False},
    )

    assert result["solar_power_valid"] is True
    assert calls == []


def test_site_solar_below_goodwe_pv_is_clamped_to_goodwe_pv():
    states = {SITE_SOLAR: _State("1000", "W")}  # lagging sensor: 1.0 < GoodWe's 2.5
    result, _calls = _run_update(override_entity=SITE_SOLAR, states=states)

    assert result["solar_power"] == pytest.approx(2.5)
    assert result["load_power"] == pytest.approx(1.9)


# --------------------------------------------------------------------------- #
# Wiring
# --------------------------------------------------------------------------- #

def test_goodwe_coordinator_accepts_and_stores_the_override_entity():
    source = COORDINATOR_PATH.read_text(encoding="utf-8")
    init = _find_class_method(ast.parse(source), "GoodWeEnergyCoordinator", "__init__")

    arg_names = [a.arg for a in init.args.args + init.args.kwonlyargs]
    assert "solar_override_entity" in arg_names
    init_source = ast.get_source_segment(source, init)
    assert 'self._solar_override_entity = (solar_override_entity or "").strip()' in init_source


def test_setup_passes_custom_solar_entity_to_the_goodwe_coordinator():
    source = INIT_PATH.read_text(encoding="utf-8")
    call = source.split("goodwe_coordinator = GoodWeEnergyCoordinator(", 1)[1].split("\n    elif is_alphaess:", 1)[0]

    assert "solar_override_entity=entry.options.get(" in call
    assert "CONF_CUSTOM_SOLAR_POWER_ENTITY" in call


def test_override_is_off_by_default():
    source = COORDINATOR_PATH.read_text(encoding="utf-8")
    init = _find_class_method(ast.parse(source), "GoodWeEnergyCoordinator", "__init__")
    defaults = dict(zip([a.arg for a in init.args.args][-len(init.args.defaults):], init.args.defaults))

    default = defaults["solar_override_entity"]
    assert isinstance(default, ast.Constant) and default.value is None
