"""Ticket #154: stepped SOC must not discard measured charging origins."""

import pytest

from test_battery_export_allowed_slots import opt_module, _provenance_coordinator


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
