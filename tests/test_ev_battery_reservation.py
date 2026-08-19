"""Regression tests for home-battery import reservation during EV charging.

Covers the case where the optimizer grid-charges the home battery while a
managed EV session is live: the EV must not consume the import headroom the
battery's plan needs, and no branch may command EV load that pushes site import
past the meter limit.
"""

from __future__ import annotations

import ast
import importlib
import sys
import types
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parent.parent / "custom_components" / "power_sync"


def _install_ha_stubs() -> None:
    ha_root = sys.modules.setdefault("homeassistant", types.ModuleType("homeassistant"))
    ha_config_entries = sys.modules.setdefault(
        "homeassistant.config_entries", types.ModuleType("homeassistant.config_entries")
    )
    ha_core = sys.modules.setdefault("homeassistant.core", types.ModuleType("homeassistant.core"))
    ha_exceptions = sys.modules.setdefault(
        "homeassistant.exceptions", types.ModuleType("homeassistant.exceptions")
    )
    ha_helpers = sys.modules.setdefault(
        "homeassistant.helpers", types.ModuleType("homeassistant.helpers")
    )
    ha_storage = sys.modules.setdefault(
        "homeassistant.helpers.storage", types.ModuleType("homeassistant.helpers.storage")
    )
    ha_update = sys.modules.setdefault(
        "homeassistant.helpers.update_coordinator",
        types.ModuleType("homeassistant.helpers.update_coordinator"),
    )
    ha_er = sys.modules.setdefault(
        "homeassistant.helpers.entity_registry",
        types.ModuleType("homeassistant.helpers.entity_registry"),
    )
    ha_dr = sys.modules.setdefault(
        "homeassistant.helpers.device_registry",
        types.ModuleType("homeassistant.helpers.device_registry"),
    )
    ha_event = sys.modules.setdefault(
        "homeassistant.helpers.event", types.ModuleType("homeassistant.helpers.event")
    )
    ha_util = sys.modules.setdefault("homeassistant.util", types.ModuleType("homeassistant.util"))
    ha_dt = sys.modules.setdefault("homeassistant.util.dt", types.ModuleType("homeassistant.util.dt"))

    ha_core.HomeAssistant = type("HomeAssistant", (), {})
    ha_config_entries.ConfigEntry = type("ConfigEntry", (), {})
    ha_exceptions.ConfigEntryNotReady = type("ConfigEntryNotReady", (Exception,), {})
    ha_er.async_get = lambda hass: getattr(hass, "entity_registry", SimpleNamespace(entities={}))
    ha_dr.async_get = lambda hass: getattr(hass, "device_registry", SimpleNamespace(devices={}))
    ha_storage.Store = type("Store", (), {"__init__": lambda self, *args, **kwargs: None})
    ha_update.DataUpdateCoordinator = type(
        "DataUpdateCoordinator",
        (),
        {
            "__class_getitem__": classmethod(lambda cls, item: cls),
            "__init__": lambda self, *args, **kwargs: None,
        },
    )
    ha_event.async_track_time_interval = lambda *args, **kwargs: (lambda: None)
    ha_event.async_track_time_change = lambda *args, **kwargs: (lambda: None)
    ha_event.async_track_point_in_time = lambda *args, **kwargs: (lambda: None)
    ha_dt.now = getattr(ha_dt, "now", lambda *args, **kwargs: datetime.now())
    ha_dt.utcnow = getattr(ha_dt, "utcnow", lambda *args, **kwargs: datetime.utcnow())

    ha_helpers.entity_registry = ha_er
    ha_helpers.device_registry = ha_dr
    ha_helpers.storage = ha_storage
    ha_helpers.update_coordinator = ha_update
    ha_helpers.event = ha_event
    ha_util.dt = ha_dt
    ha_root.helpers = ha_helpers
    ha_root.util = ha_util


_install_ha_stubs()

_ps = types.ModuleType("power_sync")
_ps.__path__ = [str(ROOT)]
sys.modules["power_sync"] = _ps

_automations = types.ModuleType("power_sync.automations")
_automations.__path__ = [str(ROOT / "automations")]
sys.modules["power_sync.automations"] = _automations

if not hasattr(sys.modules.get("power_sync.const"), "CONF_EV_PROVIDER"):
    sys.modules.pop("power_sync.const", None)
sys.modules.pop("power_sync.automations.actions", None)
actions = importlib.import_module("power_sync.automations.actions")


class _Entry:
    entry_id = "entry-1"
    data = {}
    options = {}


def _hass(coordinator=None):
    entry_data = {}
    if coordinator is not None:
        entry_data["optimization_coordinator"] = coordinator
    return SimpleNamespace(data={"power_sync": {"entry-1": entry_data}})


def _coordinator(*, battery_charge_w=0.0, action="charge", power_w=0.0, enabled=True):
    scheduled = SimpleNamespace(
        action=action,
        power_w=power_w,
        battery_charge_w=battery_charge_w,
    )
    return SimpleNamespace(
        _enabled=enabled,
        _get_current_action=lambda: scheduled,
    )


# ---------------------------------------------------------------------------
# Reservation resolution
# ---------------------------------------------------------------------------


def test_planned_charge_power_outranks_the_session_hardware_maximum():
    reservation = actions._resolve_battery_reservation_kw(
        session_target_kw=20.0,
        planned_charge_kw=14.7,
    )

    assert reservation == pytest.approx(14.7)


def test_planned_charge_arms_a_session_that_started_with_no_target():
    # Regression: a session started outside a grid window carries 0, which left
    # the EV free to spend the battery's planned grid-charge headroom.
    reservation = actions._resolve_battery_reservation_kw(
        session_target_kw=0.0,
        planned_charge_kw=14.7,
    )

    assert reservation == pytest.approx(14.7)


def test_planned_zero_releases_the_whole_envelope_to_the_ev():
    # The optimizer answered: no battery charge scheduled this interval.
    reservation = actions._resolve_battery_reservation_kw(
        session_target_kw=20.0,
        planned_charge_kw=0.0,
    )

    assert reservation == 0.0


def test_reservation_falls_back_to_session_target_without_a_plan():
    reservation = actions._resolve_battery_reservation_kw(
        session_target_kw=11.0,
        planned_charge_kw=None,
    )

    assert reservation == pytest.approx(11.0)


def test_reservation_clamped_to_the_battery_charge_rate():
    reservation = actions._resolve_battery_reservation_kw(
        session_target_kw=0.0,
        planned_charge_kw=25.0,
        max_battery_charge_rate_kw=14.7,
    )

    assert reservation == pytest.approx(14.7)


def test_unusable_session_target_is_treated_as_no_reservation():
    assert (
        actions._resolve_battery_reservation_kw(
            session_target_kw="not-a-number",
            planned_charge_kw=None,
        )
        == 0.0
    )


# ---------------------------------------------------------------------------
# Reading the plan off the optimization coordinator
# ---------------------------------------------------------------------------


def test_planned_charge_read_from_the_current_schedule_action():
    hass = _hass(_coordinator(battery_charge_w=14700.0))

    assert actions._optimizer_planned_battery_charge_kw(
        hass, _Entry()
    ) == pytest.approx(14.7)


def test_planned_charge_falls_back_to_command_power_for_charge_actions():
    hass = _hass(_coordinator(battery_charge_w=0.0, action="charge", power_w=9000.0))

    assert actions._optimizer_planned_battery_charge_kw(
        hass, _Entry()
    ) == pytest.approx(9.0)


def test_idle_action_reports_no_planned_charge():
    hass = _hass(_coordinator(battery_charge_w=0.0, action="idle", power_w=9000.0))

    assert actions._optimizer_planned_battery_charge_kw(hass, _Entry()) == 0.0


def test_missing_or_disabled_optimizer_reports_unknown():
    assert actions._optimizer_planned_battery_charge_kw(_hass(), _Entry()) is None
    assert (
        actions._optimizer_planned_battery_charge_kw(
            _hass(_coordinator(battery_charge_w=14700.0, enabled=False)),
            _Entry(),
        )
        is None
    )


def test_stale_schedule_reports_unknown():
    coordinator = SimpleNamespace(_enabled=True, _get_current_action=lambda: None)

    assert actions._optimizer_planned_battery_charge_kw(
        _hass(coordinator), _Entry()
    ) is None


# ---------------------------------------------------------------------------
# Composition with the live acceptance learner
# ---------------------------------------------------------------------------


def test_ev_caused_shortfall_is_not_learned_as_battery_taper():
    # Site pinned at the 16.1 kW meter limit: the battery only reaches 10.6 kW
    # of its planned 14.7 kW because the EV holds the difference. With no spare
    # headroom that is an EV-caused shortfall, so the full reserve is protected
    # and the EV is the side that must yield.
    learner: dict = {}
    for _sample in range(4):
        reserve_kw, acceptance_learned = actions._effective_battery_charge_reserve_kw(
            learner,
            battery_power_kw=-10.6,
            battery_soc=48.0,
            target_battery_charge_kw=actions._resolve_battery_reservation_kw(
                session_target_kw=0.0,
                planned_charge_kw=14.7,
            ),
            grid_headroom_kw=0.0,
        )

    assert reserve_kw == pytest.approx(14.7)
    assert acceptance_learned is False


def test_genuine_taper_still_releases_headroom_to_the_ev():
    # Same shortfall, but with real spare import headroom, so the battery is
    # tapering rather than being starved. Two consistent samples confirm it.
    learner: dict = {}
    for _sample in range(2):
        reserve_kw, acceptance_learned = actions._effective_battery_charge_reserve_kw(
            learner,
            battery_power_kw=-10.6,
            battery_soc=48.0,
            target_battery_charge_kw=14.7,
            grid_headroom_kw=3.0,
        )

    assert acceptance_learned is True
    assert reserve_kw == pytest.approx(10.9)


# ---------------------------------------------------------------------------
# Import-limit clamp and planner arming
# ---------------------------------------------------------------------------


def test_available_power_is_clamped_to_grid_headroom():
    # The battery-surplus branch spends the battery's own grid-charging draw,
    # which is not spare capacity. Guards the clamp that bounds every branch.
    source = (ROOT / "automations" / "actions.py").read_text()

    assert "available_power_kw = min(available_power_kw, grid_headroom_kw)" in source


def _extract_planner_function(name: str):
    """Load one pure planner function without its optimization import chain."""
    source = (ROOT / "automations" / "ev_charging_planner.py").read_text()
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            namespace: dict = {}
            exec(compile(ast.Module([node], []), "<planner>", "exec"), namespace)
            return namespace[name]
    raise AssertionError(f"{name} not found in ev_charging_planner.py")


def test_battery_target_control_arms_for_every_non_solar_source():
    should_control = _extract_planner_function("_should_control_battery_target")

    assert should_control("grid_offpeak", False) is True
    assert should_control("ml_optimized", False) is True
    assert should_control("solar_surplus", False) is False
    assert should_control("grid_offpeak", True) is False


def test_start_path_uses_the_shared_arming_helper():
    # Guards against the condition drifting back inline in _start_charging.
    source = (ROOT / "automations" / "ev_charging_planner.py").read_text()

    assert "control_battery_target = _should_control_battery_target(" in source


# ---------------------------------------------------------------------------
# Following the optimizer's co-planned EV power
# ---------------------------------------------------------------------------


def _ev_coordinator(*, ev_charge_w=0.0, enabled=True, has_field=True):
    fields = {"action": "charge", "power_w": 0.0, "battery_charge_w": 0.0}
    if has_field:
        fields["ev_charge_w"] = ev_charge_w
    return SimpleNamespace(
        _enabled=enabled,
        _get_current_action=lambda: SimpleNamespace(**fields),
    )


def test_planned_ev_power_is_read_from_the_current_action():
    hass = _hass(_ev_coordinator(ev_charge_w=7000.0))

    assert actions._optimizer_planned_ev_charge_kw(
        hass, _Entry()
    ) == pytest.approx(7.0)


def test_no_ev_plan_leaves_the_controller_with_no_ceiling():
    # A zero or absent plan must never strand a plugged-in car at 0 A; the EV
    # planner keeps start/stop authority.
    assert (
        actions._optimizer_planned_ev_charge_kw(
            _hass(_ev_coordinator(ev_charge_w=0.0)), _Entry()
        )
        is None
    )
    assert (
        actions._optimizer_planned_ev_charge_kw(
            _hass(_ev_coordinator(has_field=False)), _Entry()
        )
        is None
    )
    assert actions._optimizer_planned_ev_charge_kw(_hass(), _Entry()) is None


def test_disabled_optimizer_offers_no_ev_ceiling():
    assert (
        actions._optimizer_planned_ev_charge_kw(
            _hass(_ev_coordinator(ev_charge_w=7000.0, enabled=False)), _Entry()
        )
        is None
    )


def test_planned_ev_power_is_applied_as_a_ceiling_not_a_setpoint():
    source = (ROOT / "automations" / "actions.py").read_text()

    assert "planned_ev_charge_kw - current_ev_power_kw," in source
    assert "available_power_kw = min(" in source


# ---------------------------------------------------------------------------
# The import-limited ratchet
# ---------------------------------------------------------------------------


def test_speculative_margin_never_walks_the_car_down():
    # The live failure: 16.1 kW limit with the site pinned at 15.9, battery
    # accepting 13.9 against a learned reserve of 14.2. The 0.3 kW shortfall is
    # entirely the learner's growth margin, which on an import-limited site
    # never fills — so every cycle shed an amp, forever.
    available = actions._battery_target_available_kw(
        16.1 - 15.9, 14.2, 13.9, acceptance_learned=True
    )

    assert available == 0.0


def test_margin_may_still_absorb_spare_headroom():
    # With real headroom the margin is funded from it rather than from the car,
    # so the EV simply gets less of the surplus - it is not pushed down.
    available = actions._battery_target_available_kw(
        5.0, 10.3, 10.0, acceptance_learned=True
    )

    assert available == pytest.approx(4.7)


def test_a_real_shortfall_still_reduces_the_ev():
    # 2.7 kW short is far beyond the speculative margin: genuine competition,
    # and the car must yield.
    available = actions._battery_target_available_kw(
        0.2, 14.7, 12.0, acceptance_learned=True
    )

    assert available == pytest.approx(-2.5)


def test_unlearned_reserve_is_never_treated_as_speculative():
    # Before acceptance is learned the target is the real plan, not intake plus
    # a margin, so even a small gap is genuine and still reduces the EV.
    available = actions._battery_target_available_kw(
        0.2, 14.2, 13.9, acceptance_learned=False
    )

    assert available == pytest.approx(-0.1)


def test_battery_over_its_reserve_leaves_the_ev_the_whole_headroom():
    available = actions._battery_target_available_kw(
        1.5, 14.0, 14.6, acceptance_learned=True
    )

    assert available == pytest.approx(1.5)
