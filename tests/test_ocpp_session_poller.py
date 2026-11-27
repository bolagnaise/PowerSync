"""Regression: the OCPP session poller must meter HACS kW power correctly.

HACS OCPP converts charger ``W`` readings to ``kW`` before publishing them.
The poller previously read the state as watts and divided by 1000 again, so a
7 kW charge was recorded as 0.007 kW and session energy and cost were
understated about 1000x.
"""

from __future__ import annotations

import ast
import asyncio
import logging
import re
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

from conftest import load_source_node


ROOT = Path(__file__).resolve().parent.parent / "custom_components" / "power_sync"
INIT = ROOT / "__init__.py"


class _SessionManager:
    def __init__(self):
        self.active_sessions = {}
        self.started = []
        self.updates = []
        self.ended = []

    async def start_session(self, vehicle_id, mode):
        self.started.append((vehicle_id, mode))
        self.active_sessions[vehicle_id] = SimpleNamespace(id="s1")

    async def update_session(self, **kwargs):
        self.updates.append(kwargs)

    async def end_session(self, vehicle_id, reason):
        self.ended.append((vehicle_id, reason))
        self.active_sessions.pop(vehicle_id, None)


def _poll(monkeypatch, states, sm, prices=(41.5, 6.0)):
    ps = types.ModuleType("power_sync")
    ps.__path__ = [str(ROOT)]
    automations = types.ModuleType("power_sync.automations")
    automations.__path__ = [str(ROOT / "automations")]
    ocpp_status = types.ModuleType("power_sync.automations.ocpp_status")
    exec(compile((ROOT / "automations/ocpp_status.py").read_text(),
                 str(ROOT / "automations/ocpp_status.py"), "exec"), ocpp_status.__dict__)
    session_mod = types.ModuleType("power_sync.automations.ev_charging_session")
    session_mod.get_session_manager = lambda: sm
    pricing = types.ModuleType("power_sync.automations.ev_pricing")
    price_calls = []

    def get_current_ev_prices(hass, entry_id):
        price_calls.append(entry_id)
        return prices

    pricing.get_current_ev_prices = get_current_ev_prices
    registry = SimpleNamespace(entities={
        entity_id: SimpleNamespace(entity_id=entity_id, platform="ocpp")
        for entity_id in states
    })
    ha_helpers = types.ModuleType("homeassistant.helpers")
    ha_helpers.entity_registry = SimpleNamespace(async_get=lambda hass: registry)
    for name, module in {
        "power_sync": ps,
        "power_sync.automations": automations,
        "power_sync.automations.ocpp_status": ocpp_status,
        "power_sync.automations.ev_charging_session": session_mod,
        "power_sync.automations.ev_pricing": pricing,
        "homeassistant.helpers": ha_helpers,
    }.items():
        monkeypatch.setitem(sys.modules, name, module)

    node = load_source_node(INIT, "_poll_ocpp_sessions")
    namespace = {
        "__package__": "power_sync",
        "DOMAIN": "power_sync",
        "_LOGGER": logging.getLogger(__name__),
        "_re": re,
        "_OCPP_SUFFIXES": re.compile(
            r"^(sensor|switch|number)\.(\w+?)_(status_connector|status|availability|charge_control|current_power|energy_meter)$",
            re.IGNORECASE,
        ),
        "entry": SimpleNamespace(entry_id="entry-1"),
    }
    state_objects = {
        entity_id: SimpleNamespace(entity_id=entity_id, state=value, attributes=attrs)
        for entity_id, (value, attrs) in states.items()
    }
    namespace["hass"] = SimpleNamespace(
        data={"power_sync": {"entry-1": {}}},
        states=SimpleNamespace(get=lambda entity_id: state_objects.get(entity_id)),
    )
    exec(compile(ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[])),
                 str(INIT), "exec"), namespace)
    asyncio.run(namespace["_poll_ocpp_sessions"]())
    return price_calls


@pytest.mark.parametrize(
    ("power_state", "attrs", "expected_kw"),
    [
        ("7.2", {"unit_of_measurement": "kW"}, 7.2),
        ("7200", {"unit_of_measurement": "W"}, 7.2),
        ("7.2", {}, 7.2),
        ("7200", {}, 7.2),
    ],
)
def test_poller_records_charging_power_in_kw(monkeypatch, power_state, attrs, expected_kw):
    sm = _SessionManager()
    sm.active_sessions["ocpp_wallbox"] = SimpleNamespace(id="s0")
    _poll(monkeypatch, {
        "sensor.wallbox_status_connector": ("Charging", {}),
        "sensor.wallbox_current_power": (power_state, attrs),
    }, sm)

    assert len(sm.updates) == 1
    update = sm.updates[0]
    assert update["vehicle_id"] == "ocpp_wallbox"
    assert update["power_kw"] == pytest.approx(expected_kw)
    assert update["amps"] == int(expected_kw * 1000 / 230)
    assert sm.ended == []


def test_poller_uses_shared_ev_prices(monkeypatch):
    sm = _SessionManager()
    calls = _poll(monkeypatch, {
        "sensor.wallbox_status_connector": ("Charging", {}),
        "sensor.wallbox_current_power": ("7.2", {"unit_of_measurement": "kW"}),
    }, sm, prices=(41.5, 6.0))

    assert calls == ["entry-1"]
    assert sm.started == [("ocpp_wallbox", "ocpp")]
    assert sm.updates[0]["import_price_cents"] == 41.5
    assert sm.updates[0]["export_price_cents"] == 6.0


def test_kw_power_keeps_session_open_while_finishing(monkeypatch):
    """Power above 50 W in kW units must hold a Finishing session open."""
    sm = _SessionManager()
    sm.active_sessions["ocpp_wallbox"] = SimpleNamespace(id="s0")
    _poll(monkeypatch, {
        "sensor.wallbox_status_connector": ("Finishing", {}),
        "sensor.wallbox_current_power": ("1.4", {"unit_of_measurement": "kW"}),
    }, sm)

    assert sm.ended == []


def test_ocpp_power_w_helper_handles_units_and_bad_states():
    module = types.ModuleType("ocpp_status_probe")
    exec(compile((ROOT / "automations/ocpp_status.py").read_text(), "ocpp_status", "exec"),
         module.__dict__)
    w = module.ocpp_power_w
    state = lambda value, unit=None: SimpleNamespace(  # noqa: E731
        state=value, attributes={"unit_of_measurement": unit} if unit else {})
    assert w(state("7.2", "kW")) == pytest.approx(7200)
    assert w(state("80", "W")) == 80
    assert w(state("0.08", "kW")) == pytest.approx(80)
    assert w(state("unavailable", "kW")) == 0.0
    assert w(state("nan", "kW")) == 0.0
    assert w(state("abc")) == 0.0
    assert w(None) == 0.0


def test_poller_does_not_run_before_session_manager_exists():
    source = INIT.read_text()
    start = source.index("async def _poll_ocpp_sessions")
    register = source.index("_track_ocpp_interval(", start)
    manager = source.index("set_session_manager(session_manager)")
    assert "await _poll_ocpp_sessions()" not in source[start:manager]
    assert register < manager
