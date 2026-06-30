"""Tests for normalized EV loadpoint status helpers."""

from __future__ import annotations

import sys
import types
from datetime import datetime, timedelta
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent / "custom_components" / "power_sync"
_ps = types.ModuleType("power_sync")
_ps.__path__ = [str(ROOT)]
sys.modules["power_sync"] = _ps

_automations = types.ModuleType("power_sync.automations")
_automations.__path__ = [str(ROOT / "automations")]
sys.modules["power_sync.automations"] = _automations

from power_sync.automations.loadpoint_status import (  # noqa: E402
    build_generic_charger_observation,
    build_loadpoint_status,
    charging_state_plugged_status,
    coalesce_ev_widget_data,
)
from power_sync.automations.generic_charger_soc import (  # noqa: E402
    resolve_generic_charger_soc,
)


class _State:
    def __init__(self, state):
        self.state = state


class _States:
    def __init__(self, states):
        self._states = states

    def get(self, entity_id):
        return self._states.get(entity_id)


class _Hass:
    def __init__(self, states):
        self.states = _States(states)


def test_charging_state_plugged_status_matches_idle_connected_tesla_states():
    assert charging_state_plugged_status("Stopped") is True
    assert charging_state_plugged_status("Complete") is True
    assert charging_state_plugged_status("Connected") is True
    assert charging_state_plugged_status("No Power") is True
    assert charging_state_plugged_status("Disconnected") is False
    assert charging_state_plugged_status("unknown") is None


def test_generic_charger_soc_resolver_prefers_primary_sensor():
    hass = _Hass({
        "sensor.car_a_soc": _State("61"),
        "sensor.car_b_soc": _State("72"),
    })

    assert resolve_generic_charger_soc(hass, {
        "generic_charger_soc_entity": "sensor.car_a_soc",
        "generic_charger_soc_entity_2": "sensor.car_b_soc",
    }) == 61


def test_generic_charger_soc_resolver_falls_back_when_primary_unavailable():
    hass = _Hass({
        "sensor.car_a_soc": _State("unavailable"),
        "sensor.car_b_soc": _State("72"),
    })

    assert resolve_generic_charger_soc(hass, {
        "generic_charger_soc_entity": "sensor.car_a_soc",
        "generic_charger_soc_entity_2": "sensor.car_b_soc",
    }) == 72


def test_generic_charger_soc_resolver_falls_back_when_primary_invalid():
    hass = _Hass({
        "sensor.car_a_soc": _State("125"),
        "sensor.car_b_soc": _State("47.9"),
    })

    assert resolve_generic_charger_soc(hass, {
        "generic_charger_soc_entity": "sensor.car_a_soc",
        "generic_charger_soc_entity_2": "sensor.car_b_soc",
    }) == 47.9


def test_generic_charger_soc_resolver_returns_none_when_all_invalid():
    hass = _Hass({
        "sensor.car_a_soc": _State("not-a-number"),
        "sensor.car_b_soc": _State("-1"),
    })

    assert resolve_generic_charger_soc(hass, {
        "generic_charger_soc_entity": "sensor.car_a_soc",
        "generic_charger_soc_entity_2": "sensor.car_b_soc",
    }) is None


def test_widget_data_removes_active_wall_connector_when_named_ev_is_active():
    widgets = [
        {
            "vehicle_name": "TESSY",
            "is_connected": True,
            "is_charging": True,
            "current_soc": 79,
            "current_power_kw": 3.1,
        },
        {
            "vehicle_name": "Wall Connector",
            "is_connected": True,
            "is_charging": True,
            "current_soc": 0,
            "current_power_kw": 3.54,
        },
    ]

    assert [w["vehicle_name"] for w in coalesce_ev_widget_data(widgets)] == ["TESSY"]


def test_widget_data_keeps_wall_connector_without_named_active_ev():
    widgets = [
        {
            "vehicle_name": "Wall Connector",
            "is_connected": True,
            "is_charging": True,
            "current_power_kw": 3.54,
        },
    ]

    assert coalesce_ev_widget_data(widgets) == widgets


def test_loadpoint_status_merges_wall_connector_into_single_active_tesla():
    loadpoints = build_loadpoint_status(
        {
            "VIN_TESS": {
                "active": True,
                "vehicle_name": "TESSY",
                "current_amps": 32,
                "target_amps": 32,
                "charging_started": True,
                "params": {
                    "dynamic_mode": "battery_target",
                    "charger_type": "tesla",
                    "voltage": 240,
                    "phases": 1,
                },
            }
        },
        [
            {
                "vehicle_id": "VIN_TESS",
                "vehicle_name": "TESSY",
                "charger_type": "tesla",
                "ev_power_kw": 7.0,
                "ev_soc": 70,
                "is_connected": True,
                "is_charging": True,
            },
            {
                "vehicle_id": "wall_connector",
                "vehicle_name": "Wall Connector",
                "ev_power_kw": 3.4,
                "is_connected": True,
                "is_charging": True,
            },
        ],
    )

    assert [loadpoint["vehicle_name"] for loadpoint in loadpoints] == ["TESSY"]
    assert loadpoints[0]["current_power_kw"] == 3.4
    assert loadpoints[0]["commanded_power_kw"] == 7.68
    assert loadpoints[0]["status"] == "charging"


def test_loadpoint_status_keeps_wall_connector_when_tesla_match_is_ambiguous():
    loadpoints = build_loadpoint_status(
        {},
        [
            {
                "vehicle_id": "VIN_TESS",
                "vehicle_name": "TESSY",
                "charger_type": "tesla",
                "ev_power_kw": 0.0,
                "is_connected": True,
                "is_charging": False,
            },
            {
                "vehicle_id": "VIN_THEO",
                "vehicle_name": "THEO",
                "charger_type": "tesla",
                "ev_power_kw": 0.0,
                "is_connected": True,
                "is_charging": False,
            },
            {
                "vehicle_id": "wall_connector",
                "vehicle_name": "Wall Connector",
                "ev_power_kw": 3.4,
                "is_connected": True,
                "is_charging": True,
            },
        ],
    )

    assert [loadpoint["vehicle_name"] for loadpoint in loadpoints] == [
        "TESSY",
        "THEO",
        "Wall Connector",
    ]


def test_dynamic_state_reports_commanded_no_power_when_observed_power_is_zero():
    loadpoints = build_loadpoint_status(
        {
            "VIN123": {
                "active": True,
                "vehicle_name": "Blue Car",
                "current_amps": 16,
                "target_amps": 16,
                "charging_started": True,
                "params": {
                    "dynamic_mode": "solar_surplus",
                    "charger_type": "ocpp",
                    "voltage": 230,
                    "phases": 1,
                },
            }
        },
        [
            {
                "vehicle_id": "VIN123",
                "vehicle_name": "Blue Car",
                "ev_power_kw": 0,
                "is_connected": True,
                "is_charging": False,
            }
        ],
    )

    assert loadpoints[0]["owner"] == "powersync"
    assert loadpoints[0]["owner_mode"] == "solar_surplus"
    assert loadpoints[0]["charger_type"] == "ocpp"
    assert loadpoints[0]["current_amps"] == 16
    assert loadpoints[0]["actual_charging"] is False
    assert loadpoints[0]["status"] == "commanded_no_power"
    assert "Commanded 16A" in loadpoints[0]["blocking_reason"]
    assert loadpoints[0]["confidence"] == "observed"


def test_solar_surplus_start_delay_timer_is_exposed():
    started_at = datetime.now() - timedelta(seconds=45)

    loadpoints = build_loadpoint_status(
        {
            "VIN123": {
                "active": True,
                "current_amps": 0,
                "target_amps": 0,
                "charging_started": False,
                "high_surplus_start": started_at,
                "params": {
                    "dynamic_mode": "solar_surplus",
                    "sustained_surplus_minutes": 3,
                },
            }
        }
    )

    timer = loadpoints[0]["delay_timer"]
    assert timer["phase"] == "start"
    assert timer["label"] == "Start delay"
    assert timer["duration_seconds"] == 180
    assert 0 <= timer["remaining_seconds"] <= 180


def test_solar_surplus_stop_delay_timer_is_exposed():
    started_at = datetime.now() - timedelta(seconds=75)

    loadpoints = build_loadpoint_status(
        {
            "VIN123": {
                "active": True,
                "current_amps": 8,
                "target_amps": 8,
                "charging_started": True,
                "low_surplus_start": started_at,
                "params": {
                    "dynamic_mode": "solar_surplus",
                    "stop_delay_minutes": 5,
                },
            }
        }
    )

    timer = loadpoints[0]["delay_timer"]
    assert timer["phase"] == "stop"
    assert timer["label"] == "Stop delay"
    assert timer["duration_seconds"] == 300
    assert 0 <= timer["remaining_seconds"] <= 300


def test_external_observed_charger_is_kept_without_dynamic_state():
    loadpoints = build_loadpoint_status(
        {},
        [
            {
                "charger_id": "garage_ocpp",
                "vehicle_name": "Garage OCPP",
                "charger_type": "ocpp",
                "ev_power_kw": 7.2,
                "is_connected": True,
                "is_charging": True,
            }
        ],
        {"surplus_kw": 1.0},
    )

    assert loadpoints == [
        {
            "loadpoint_id": "garage_ocpp",
            "vehicle_id": None,
            "vehicle_name": "Garage OCPP",
            "charger_type": "ocpp",
            "connected": True,
            "actual_charging": True,
            "status": "charging",
            "owner": "external",
            "owner_mode": None,
            "source": "grid",
            "current_power_kw": 7.2,
            "commanded_power_kw": None,
            "current_amps": 0,
            "target_amps": 0,
            "soc": None,
            "target_soc": None,
            "allocated_surplus_kw": 0.0,
            "blocking_reason": None,
            "session_id": None,
            "last_command": None,
            "confidence": "observed",
            "source_mode": None,
            "duration_minutes": None,
            "expires_at": None,
            "quick_control": False,
        }
    ]


def test_dynamic_state_prefers_business_owner_mode_over_control_mode():
    loadpoints = build_loadpoint_status(
        {
            "VIN123": {
                "active": True,
                "current_amps": 16,
                "target_amps": 16,
                "charging_started": True,
                "params": {
                    "dynamic_mode": "battery_target",
                    "owner_mode": "price_level_recovery",
                    "voltage": 230,
                    "phases": 1,
                },
            }
        }
    )

    assert loadpoints[0]["owner_mode"] == "price_level_recovery"


def test_loadpoint_status_includes_ownership_last_command():
    loadpoints = build_loadpoint_status(
        {
            "VIN123": {
                "active": True,
                "current_amps": 0,
                "target_amps": 0,
                "params": {"dynamic_mode": "solar_surplus"},
            }
        },
        None,
        None,
        {
            "VIN123": {
                "owner": "powersync",
                "owner_mode": "manual",
                "session_id": "sess-1",
                "last_command": {
                    "command": "start",
                    "at": "2026-05-01T00:00:00+00:00",
                    "source": "powersync",
                    "success": True,
                    "reason": "Manual start",
                },
            }
        },
    )

    assert loadpoints[0]["owner"] == "powersync"
    assert loadpoints[0]["owner_mode"] == "manual"
    assert loadpoints[0]["session_id"] == "sess-1"
    assert loadpoints[0]["last_command"]["command"] == "start"


def test_loadpoint_status_includes_quick_control_metadata():
    loadpoints = build_loadpoint_status(
        {
            "VIN123": {
                "active": True,
                "current_amps": 16,
                "target_amps": 16,
                "charging_started": True,
                "params": {
                    "dynamic_mode": "manual",
                    "owner_mode": "manual",
                    "source_mode": "grid_allowed",
                    "duration_minutes": 90,
                    "expires_at": "2026-05-01T01:30:00+00:00",
                    "quick_control": True,
                },
            }
        },
    )

    assert loadpoints[0]["owner_mode"] == "manual"
    assert loadpoints[0]["source_mode"] == "grid_allowed"
    assert loadpoints[0]["duration_minutes"] == 90
    assert loadpoints[0]["expires_at"] == "2026-05-01T01:30:00+00:00"
    assert loadpoints[0]["quick_control"] is True


def test_observed_ocpp_loadpoint_uses_ownership_alias():
    loadpoints = build_loadpoint_status(
        {},
        [
            {
                "charger_id": "garage_ocpp",
                "vehicle_name": "Garage OCPP",
                "charger_type": "ocpp",
                "ev_power_kw": 0.0,
                "is_connected": True,
            }
        ],
        None,
        {
            "ocpp_garage_ocpp": {
                "owner": "powersync",
                "owner_mode": "manual",
                "session_id": "sess-ocpp",
                "last_command": {
                    "command": "start_manual",
                    "at": "2026-05-01T00:00:00+00:00",
                    "source": "powersync",
                    "success": True,
                    "reason": "Manual OCPP start",
                },
            }
        },
    )

    assert loadpoints[0]["loadpoint_id"] == "garage_ocpp"
    assert loadpoints[0]["owner"] == "powersync"
    assert loadpoints[0]["owner_mode"] == "manual"
    assert loadpoints[0]["session_id"] == "sess-ocpp"
    assert loadpoints[0]["last_command"]["command"] == "start_manual"


def test_allocated_surplus_marks_powersync_session_as_solar():
    loadpoints = build_loadpoint_status(
        {
            "solar_car": {
                "active": True,
                "current_amps": 10,
                "target_amps": 10,
                "charging_started": True,
                "allocated_surplus_kw": 2.4,
                "params": {"voltage": 240, "phases": 1},
            }
        },
        None,
        {"surplus_kw": 0.0},
    )

    assert loadpoints[0]["current_power_kw"] == 2.4
    assert loadpoints[0]["status"] == "charging"
    assert loadpoints[0]["source"] == "solar"
    assert loadpoints[0]["confidence"] == "commanded"


def test_dynamic_state_uses_observed_power_for_matched_vehicle():
    loadpoints = build_loadpoint_status(
        {
            "LRW3F7FS1NC484342": {
                "active": True,
                "vehicle_name": "N3bula",
                "current_amps": 10,
                "target_amps": 10,
                "charging_started": True,
                "params": {
                    "dynamic_mode": "solar_surplus",
                    "voltage": 240,
                    "phases": 3,
                },
            }
        },
        [
            {
                "vehicle_id": "LRW3F7FS1NC484342",
                "vehicle_name": "N3bula",
                "charger_type": "tesla",
                "ev_power_kw": 2.4,
                "ev_soc": 78,
                "is_connected": True,
                "is_charging": True,
            }
        ],
    )

    assert loadpoints[0]["current_power_kw"] == 2.4
    assert loadpoints[0]["commanded_power_kw"] == 7.2
    assert loadpoints[0]["status"] == "charging"
    assert loadpoints[0]["confidence"] == "observed"


def test_tesla_ble_bridge_merges_with_single_named_tesla_vehicle():
    loadpoints = build_loadpoint_status(
        {},
        [
            {
                "vehicle_id": "VIN_TESS",
                "vehicle_name": "TESSY",
                "charger_type": "tesla",
                "ev_power_kw": 0.0,
                "ev_soc": None,
                "is_connected": False,
                "is_charging": False,
            },
            {
                "vehicle_id": "ble_teslable",
                "vehicle_name": "Tesla BLE (teslable)",
                "charger_type": "tesla",
                "ev_power_kw": 0.0,
                "ev_soc": 78,
                "is_connected": True,
                "is_charging": False,
            },
        ],
    )

    assert len(loadpoints) == 1
    assert loadpoints[0]["loadpoint_id"] == "VIN_TESS"
    assert loadpoints[0]["vehicle_id"] == "VIN_TESS"
    assert loadpoints[0]["vehicle_name"] == "TESSY"
    assert loadpoints[0]["soc"] == 78
    assert loadpoints[0]["connected"] is True


def test_tesla_ble_bridge_merges_after_duplicate_fleet_devices_are_deduped():
    vin = "LRWYHCEK3PC907290"
    loadpoints = build_loadpoint_status(
        {},
        [
            {
                "vehicle_id": vin,
                "vehicle_name": "TESSY",
                "charger_type": "tesla",
                "ev_power_kw": 0.0,
                "ev_soc": None,
                "is_connected": True,
                "is_charging": False,
            },
            {
                "vehicle_id": vin,
                "vehicle_name": "TESSY",
                "charger_type": "tesla",
                "ev_power_kw": 0.0,
                "ev_soc": None,
                "is_connected": False,
                "is_charging": False,
            },
            {
                "vehicle_id": "ble_teslable",
                "vehicle_name": "Tesla BLE (teslable)",
                "charger_type": "tesla",
                "ev_power_kw": 0.0,
                "ev_soc": 78,
                "is_connected": True,
                "is_charging": False,
            },
        ],
    )

    assert len(loadpoints) == 1
    assert loadpoints[0]["loadpoint_id"] == vin
    assert loadpoints[0]["vehicle_name"] == "TESSY"
    assert loadpoints[0]["soc"] == 78
    assert loadpoints[0]["connected"] is True


def test_tesla_ble_bridge_stays_separate_when_vehicle_match_is_ambiguous():
    loadpoints = build_loadpoint_status(
        {},
        [
            {
                "vehicle_id": "VIN_TESS",
                "vehicle_name": "TESSY",
                "charger_type": "tesla",
                "ev_power_kw": 0.0,
                "ev_soc": 78,
                "is_connected": True,
                "is_charging": False,
            },
            {
                "vehicle_id": "VIN_THEO",
                "vehicle_name": "THEO",
                "charger_type": "tesla",
                "ev_power_kw": 0.0,
                "ev_soc": 44,
                "is_connected": False,
                "is_charging": False,
            },
            {
                "vehicle_id": "ble_teslable",
                "vehicle_name": "Tesla BLE (teslable)",
                "charger_type": "tesla",
                "ev_power_kw": 0.0,
                "ev_soc": 78,
                "is_connected": True,
                "is_charging": False,
            },
        ],
    )

    assert [loadpoint["vehicle_name"] for loadpoint in loadpoints] == [
        "TESSY",
        "THEO",
        "Tesla BLE (teslable)",
    ]


def test_tesla_ble_bridge_does_not_merge_with_non_tesla_charger():
    loadpoints = build_loadpoint_status(
        {},
        [
            {
                "charger_id": "ocpp_evse_1",
                "vehicle_name": "Garage OCPP",
                "charger_type": "ocpp",
                "ev_power_kw": 0.0,
                "is_connected": True,
                "is_charging": False,
            },
            {
                "vehicle_id": "ble_teslable",
                "vehicle_name": "Tesla BLE (teslable)",
                "charger_type": "tesla",
                "ev_power_kw": 0.0,
                "ev_soc": 78,
                "is_connected": True,
                "is_charging": False,
            },
        ],
    )

    assert [loadpoint["vehicle_name"] for loadpoint in loadpoints] == [
        "Garage OCPP",
        "Tesla BLE (teslable)",
    ]


def test_default_session_merges_with_single_observed_charging_vehicle():
    generic_observation = build_generic_charger_observation(
        vehicle_name="EV",
        switch_state="on",
        amps_value=None,
        status_state="connected",
        soc_value="53",
    )

    loadpoints = build_loadpoint_status(
        {
            "_default": {
                "active": True,
                "vehicle_name": "EV",
                "current_amps": 0,
                "target_amps": 0,
                "params": {"dynamic_mode": "solar_surplus"},
            }
        },
        [
            {
                "vehicle_id": "VIN_TESS",
                "vehicle_name": "Tess",
                "charger_type": "tesla",
                "ev_power_kw": 3.4,
                "ev_soc": 55,
                "is_connected": True,
                "is_charging": True,
            },
            generic_observation,
            {
                "vehicle_id": "VIN_THEO",
                "vehicle_name": "Theo",
                "charger_type": "tesla",
                "ev_power_kw": 0.0,
                "ev_soc": 21,
                "is_connected": False,
                "is_charging": False,
            },
        ],
    )

    assert [loadpoint["vehicle_name"] for loadpoint in loadpoints] == ["Tess", "Theo"]
    assert loadpoints[0]["loadpoint_id"] == "VIN_TESS"
    assert loadpoints[0]["vehicle_id"] == "VIN_TESS"
    assert loadpoints[0]["current_power_kw"] == 3.4
    assert loadpoints[0]["owner"] == "powersync"
    assert loadpoints[0]["owner_mode"] == "solar_surplus"
    assert loadpoints[0]["confidence"] == "observed"


def test_smart_schedule_solar_surplus_session_reports_solar_source_after_consuming_surplus():
    loadpoints = build_loadpoint_status(
        {
            "_default": {
                "active": True,
                "vehicle_name": "EV",
                "current_amps": 0,
                "target_amps": 0,
                "params": {
                    "dynamic_mode": "solar_surplus",
                    "owner_mode": "smart_schedule_solar_surplus",
                    "charger_type": "sigenergy",
                },
            }
        },
        [
            {
                "vehicle_id": "sigenergy_charger",
                "vehicle_name": "Sigenergy charger",
                "charger_type": "sigenergy",
                "ev_power_kw": 5.93,
                "ev_soc": 41,
                "is_connected": True,
                "is_charging": True,
                "current_amps": 0,
            }
        ],
        site={"surplus_kw": 0.0},
    )

    assert loadpoints[0]["loadpoint_id"] == "sigenergy_charger"
    assert loadpoints[0]["owner_mode"] == "smart_schedule_solar_surplus"
    assert loadpoints[0]["source"] == "solar"
    assert loadpoints[0]["current_power_kw"] == 5.93
    assert loadpoints[0]["current_amps"] == 0


def test_dynamic_session_prefers_observed_current_amps_when_command_state_is_zero():
    loadpoints = build_loadpoint_status(
        {
            "VIN123": {
                "active": True,
                "vehicle_name": "Blue Car",
                "current_amps": 0,
                "target_amps": 0,
                "params": {"dynamic_mode": "solar_surplus"},
            }
        },
        [
            {
                "vehicle_id": "VIN123",
                "vehicle_name": "Blue Car",
                "ev_power_kw": 5.5,
                "is_connected": True,
                "is_charging": True,
                "current_amps": 24,
            }
        ],
    )

    assert loadpoints[0]["current_amps"] == 24
    assert loadpoints[0]["target_amps"] == 24


def test_default_session_is_hidden_when_multiple_observed_vehicles_are_active():
    loadpoints = build_loadpoint_status(
        {
            "_default": {
                "active": True,
                "current_amps": 10,
                "target_amps": 10,
                "charging_started": True,
                "params": {
                    "dynamic_mode": "scheduled",
                    "charger_type": "tesla",
                    "voltage": 240,
                    "phases": 1,
                },
            }
        },
        [
            {
                "vehicle_id": "VIN_TESS",
                "vehicle_name": "Tess",
                "charger_type": "tesla",
                "ev_power_kw": 5.8,
                "ev_soc": 75,
                "is_connected": True,
                "is_charging": True,
            },
            {
                "vehicle_id": "VIN_THEO",
                "vehicle_name": "Theo",
                "charger_type": "tesla",
                "ev_power_kw": 11.7,
                "ev_soc": 19,
                "is_connected": True,
                "is_charging": True,
            },
        ],
    )

    assert [loadpoint["vehicle_name"] for loadpoint in loadpoints] == ["Tess", "Theo"]
    assert {loadpoint["loadpoint_id"] for loadpoint in loadpoints} == {
        "VIN_TESS",
        "VIN_THEO",
    }


def test_generic_charger_observation_reports_commanded_without_power():
    observation = build_generic_charger_observation(
        vehicle_name="Generic EV",
        switch_state="on",
        amps_value="16",
        status_state="connected",
        soc_value="62",
    )

    loadpoints = build_loadpoint_status({}, [observation])

    assert loadpoints[0]["vehicle_name"] == "Generic EV"
    assert loadpoints[0]["charger_type"] == "generic"
    assert loadpoints[0]["connected"] is True
    assert loadpoints[0]["actual_charging"] is False
    assert loadpoints[0]["status"] == "commanded_no_power"
    assert loadpoints[0]["current_amps"] == 16
    assert loadpoints[0]["soc"] == 62


def test_generic_charger_observation_uses_measured_power():
    observation = build_generic_charger_observation(
        vehicle_name="Generic EV",
        switch_state="off",
        amps_value="15",
        status_state="disconnected",
        power_value="3500",
        soc_value="69",
    )

    loadpoints = build_loadpoint_status({}, [observation])

    assert loadpoints[0]["connected"] is True
    assert loadpoints[0]["actual_charging"] is True
    assert loadpoints[0]["status"] == "charging"
    assert loadpoints[0]["current_power_kw"] == 3.5
    assert loadpoints[0]["blocking_reason"] is None
    assert loadpoints[0]["soc"] == 69


def test_generic_charger_loadpoint_uses_fallback_soc_value():
    hass = _Hass({
        "sensor.primary_soc": _State("unknown"),
        "sensor.fallback_soc": _State("71"),
    })
    observation = build_generic_charger_observation(
        vehicle_name="Generic EV",
        switch_state="off",
        status_state="connected",
        soc_value=resolve_generic_charger_soc(hass, {
            "generic_charger_soc_entity": "sensor.primary_soc",
            "generic_charger_soc_entity_2": "sensor.fallback_soc",
        }),
    )

    loadpoints = build_loadpoint_status({}, [observation])

    assert loadpoints[0]["vehicle_name"] == "Generic EV"
    assert loadpoints[0]["soc"] == 71


def test_generic_charger_observation_keeps_configured_idle_loadpoint():
    observation = build_generic_charger_observation(
        switch_state="off",
        amps_value=None,
        status_state="disconnected",
        soc_value=None,
    )

    loadpoints = build_loadpoint_status({}, [observation])

    assert len(loadpoints) == 1
    assert loadpoints[0]["loadpoint_id"] == "generic_ev"
    assert loadpoints[0]["status"] == "idle"
    assert loadpoints[0]["owner"] == "external"
