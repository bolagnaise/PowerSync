"""Stepped or delayed SOC must not discard measured charging origins."""

from datetime import datetime, timedelta, timezone

import pytest

from test_battery_export_allowed_slots import (
    opt_module,
    _charging_solve_coordinator,
    _provenance_coordinator,
    _run_to_acquisition_cost,
)


def _opening(opt_module):
    c = _provenance_coordinator(opt_module)
    c._battery_provenance_available = True
    c._battery_provenance_solar_kwh = 1.0
    c._battery_provenance_grid_kwh = 7.0
    c._battery_provenance_unknown_kwh = 2.0
    c._battery_provenance_grid_cost = 0.7
    c._battery_provenance_last_soc = 10 / 27
    c._battery_provenance_last_capacity_wh = 27000
    return c


def _charge(c, target, solar=0.002, grid=0.004):
    c._record_battery_provenance_interval(
        solar_charge_kwh=solar, grid_charge_kwh=grid,
        grid_charge_cost=grid * 0.1, discharge_kwh=0.0,
        current_soc=target / 27, capacity_wh=27000, telemetry_gap=False,
    )


def test_stepped_soc_retains_cumulative_measured_sources(opt_module):
    c = _opening(opt_module)
    for i in range(30):
        # Fresh samples alternate small and large SOC increases. The pair
        # stores 0.010 kWh from 0.012 kWh measured, with no missing telemetry.
        _charge(c, 10 + i * 0.010 + 0.001)
        _charge(c, 10 + (i + 1) * 0.010)
    ratio = 10.3 / 10.36
    assert c._battery_provenance_unknown_kwh == pytest.approx(2 * ratio)
    assert c._battery_provenance_grid_kwh == pytest.approx(7.24 * ratio)
    assert c._battery_provenance_grid_cost == pytest.approx(0.724 * ratio)
    assert c._battery_provenance_total_kwh() == pytest.approx(10.3)


def test_charge_soc_ahead_can_catch_up_without_relabelling_opening_unknown(opt_module):
    c = _opening(opt_module)
    _charge(c, 10.010)
    assert c._battery_provenance_unknown_kwh > 2
    _charge(c, 10.011)
    assert c._battery_provenance_unknown_kwh <= 2
    assert c._battery_provenance_total_kwh() == pytest.approx(10.011)


@pytest.mark.parametrize("idle_target", [10.001, 10.006])
def test_charge_pause_retains_sources_until_delayed_soc_catches_up(opt_module, idle_target):
    """Ticket #456: a zero-power update is not an unmeasured boundary."""
    c = _opening(opt_module)
    _charge(c, 10.001)
    _charge(c, idle_target, solar=0, grid=0)
    c._reconcile_battery_provenance(10.006 / 27, 27000, reason="optimizer_run")

    assert c._battery_provenance_unknown_kwh == pytest.approx(2.0)
    assert c._battery_provenance_solar_kwh == pytest.approx(1.002)
    assert c._battery_provenance_grid_kwh == pytest.approx(7.004)
    assert c._battery_provenance_grid_cost == pytest.approx(0.7004)
    assert c._battery_provenance_total_kwh() == pytest.approx(10.006)


def test_charge_pause_does_not_attribute_soc_beyond_measured_energy(opt_module):
    c = _opening(opt_module)
    _charge(c, 10.001)
    _charge(c, 10.010, solar=0, grid=0)

    assert c._battery_provenance_unknown_kwh == pytest.approx(2.004)
    assert c._battery_provenance_solar_kwh == pytest.approx(1.002)
    assert c._battery_provenance_grid_kwh == pytest.approx(7.004)
    assert c._battery_provenance_total_kwh() == pytest.approx(10.010)


def test_free_charge_pause_keeps_zero_acquisition_cost_through_real_solve(
    opt_module, monkeypatch,
):
    """Fresh power/SOC callbacks followed by a solve must retain free charging."""
    clock = {
        "now": datetime(2026, 11, 12, 12, 0, tzinfo=timezone.utc),
        "data": {"battery_level": 50.0, "battery_power": -6.0,
                 "grid_power": 4.0, "solar_power": 2.0},
    }
    c = _charging_solve_coordinator(opt_module, monkeypatch, clock)
    c.battery_system = "sigenergy"
    c._last_display_import_prices = [0.0]
    c._track_actual_cost(force=True)
    c._battery_provenance_available = True
    c._battery_provenance_grid_kwh = 13.5
    c._battery_provenance_last_soc = 0.5
    c._battery_provenance_last_capacity_wh = 27000

    clock["now"] += timedelta(seconds=60)
    clock["data"].update(battery_level=13.51 / 27 * 100,
                         last_update=clock["now"])
    c._on_cost_energy_update()
    clock["now"] += timedelta(seconds=60)
    clock["data"].update(battery_level=13.6 / 27 * 100, battery_power=0.0,
                         grid_power=0.0, last_update=clock["now"])
    c._on_cost_energy_update()
    _run_to_acquisition_cost(c)

    assert c._actual_charge_kwh_today == pytest.approx(0.1)
    assert c._battery_provenance_total_kwh() == pytest.approx(13.6)
    assert c._battery_provenance_solar_kwh == pytest.approx(2 / 60)
    assert c._battery_provenance_grid_kwh == pytest.approx(13.5 + 4 / 60)
    assert c._battery_provenance_unknown_kwh == pytest.approx(0.0)
    assert clock["acquisition_cost"] == pytest.approx(0.0)


@pytest.mark.parametrize('boundary', ['gap', 'discharge', 'capacity', 'reload'])
def test_charge_episode_does_not_cross_unmeasured_boundaries(opt_module, boundary):
    c = _opening(opt_module)
    _charge(c, 10.010)
    if boundary == 'reload':
        c._reconcile_battery_provenance(10.010 / 27, 27000, reason='startup_restore')
    elif boundary == 'capacity':
        c._reconcile_battery_provenance(10.010 / 28, 28000, reason='optimizer_run')
    else:
        c._record_battery_provenance_interval(
            solar_charge_kwh=0, grid_charge_kwh=0, grid_charge_cost=0,
            discharge_kwh=0.001 if boundary == 'discharge' else 0,
            current_soc=10.010 / 27, capacity_wh=27000,
            telemetry_gap=boundary == 'gap',
        )
    opening_unknown = c._battery_provenance_unknown_kwh
    _charge(c, 10.011)
    # The previous episode's unexplained SOC excess is no longer eligible
    # for source attribution using this new episode's measured charge.
    assert c._battery_provenance_unknown_kwh > 2
    assert c._battery_provenance_unknown_kwh <= opening_unknown
