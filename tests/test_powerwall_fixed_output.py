"""Fixed battery watts are distinct from legacy grid-export targets."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from test_powerwall_battery_dispatch import module, snapshot, config


def fixed_snapshot(*, load=5000, solar=1000, imported=14.9, exported=10,
                   export_rule="battery_ok", **kwargs):
    return snapshot(load_w=load, solar_w=solar, grid_w=load - solar,
                    raw={"config": {"site_info": {
                        "max_site_meter_power_ac": imported,
                        "min_site_meter_power_ac": -exported,
                        "customer_preferred_export_rule": export_rule,
                    }}}, **kwargs)


def fixed_config(**kwargs):
    return config(optimization_max_discharge_w=10000,
                  optimization_max_charge_w=10000,
                  optimization_max_grid_import_w=14900, **kwargs)


@pytest.mark.parametrize("load,solar", [(5000, 1000), (7000, 1000), (3000, 0)])
def test_battery_output_keeps_requested_watts_as_home_and_solar_change(load, solar):
    sample = fixed_snapshot(load=load, solar=solar)
    assert module.battery_watts("discharge", 1800, sample, fixed_config(),
                                target_basis="battery_output") == 1800
    assert module.battery_watts("discharge", 1800, sample, fixed_config(),
                                target_basis="grid_export") == 1800 + load - solar


def test_battery_output_supplies_home_at_zero_native_and_software_export_caps():
    assert module.battery_watts("discharge", 1800, fixed_snapshot(exported=0),
                                fixed_config(optimization_max_grid_export_w=0),
                                target_basis="battery_output") == 1800


@pytest.mark.parametrize("rule", ["never", "pv_only", None])
def test_fixed_home_supply_does_not_enable_battery_export_permission(rule):
    assert module.battery_watts("discharge", 1800, fixed_snapshot(export_rule=rule),
                                fixed_config(), target_basis="battery_output") == 1800
    with pytest.raises(ValueError, match="export"):
        module.battery_watts("discharge", 5000, fixed_snapshot(export_rule=rule),
                             fixed_config(), target_basis="battery_output")


@pytest.mark.parametrize("mode,power,sample,options,reason", [
    ("discharge", 1800, {"load": 1000, "solar": 0, "exported": 0}, {}, "export"),
    ("discharge", 1800, {"imported": 2}, {}, "import"),
    ("discharge", 11000, {}, {}, "discharge"),
    ("charge", 11000, {}, {}, "charge"),
    ("charge", 3000, {"imported": 5}, {}, "import"),
    ("charge", 3000, {}, {"native_ev_unobserved_w": 8000}, "import"),
    ("discharge", 1800, {"soc": 20}, {}, "reserve"),
    ("charge", 1800, {"soc": 100}, {}, "ceiling"),
    ("charge", 1800, {"grid_charging_enabled": False}, {}, "charging"),
    ("discharge", 1800, {"grid_status": "SystemIslandedActive"}, {}, "grid"),
])
def test_infeasible_fixed_output_is_rejected_instead_of_retargeted(mode, power, sample, options, reason):
    with pytest.raises(ValueError, match=reason):
        module.battery_watts(mode, power, fixed_snapshot(**sample), fixed_config(**options),
                             target_basis="battery_output")


def test_fixed_charge_uses_battery_input_and_respects_solar():
    assert module.battery_watts("charge", 2000, fixed_snapshot(load=1000, solar=5000),
                                fixed_config(), target_basis="battery_output") == -2000


@pytest.mark.parametrize("power", [True, False, None, "1800", float("nan"), float("inf"), -1])
def test_fixed_output_rejects_ambiguous_or_invalid_values(power):
    with pytest.raises((ValueError, TypeError)):
        module.battery_watts("discharge", power, fixed_snapshot(), fixed_config(),
                             target_basis="battery_output")


def test_zero_hold_is_not_max_and_remains_explicitly_unverified():
    with pytest.raises(ValueError, match="zero.*hold|Zero.*hold"):
        module.battery_watts("discharge", 0, fixed_snapshot(), fixed_config(),
                             target_basis="battery_output")


@pytest.mark.asyncio
async def test_controller_preserves_basis_and_uses_short_existing_lease():
    client = SimpleNamespace(get_snapshot=AsyncMock(return_value=fixed_snapshot()),
                             dispatch_battery_power=AsyncMock(return_value=True),
                             resume_battery_operation=AsyncMock(return_value=True))
    controller = module.LocalBatteryDispatch(client, fixed_config, lambda mode: True,
                                              AsyncMock(), AsyncMock())
    try:
        result = await controller.start("discharge", 1800, 600, target_basis="battery_output")
        assert result["power_target_basis"] == "battery_output"
        assert result["requested_power_w"] == 1800
        assert result["battery_power_w"] == 1800
        assert client.dispatch_battery_power.await_args.args == (1800, module.LEASE_SECONDS)
        client.get_snapshot.return_value = fixed_snapshot(load=1000, solar=0, exported=0)
        with pytest.raises(ValueError, match="export"):
            await controller._write()
        assert client.dispatch_battery_power.await_count == 1
    finally:
        await controller.stop()
    client.resume_battery_operation.assert_awaited()


@pytest.mark.parametrize("metadata,sample,reason", [
    ({"native_site_import_cap_w": 0}, {}, "import"),
    ({"native_site_export_cap_w": 0}, {"load": 1000, "solar": 0}, "export"),
    ({"native_site_import_cap_w": 2000}, {}, "import"),
])
def test_fixed_output_respects_recorded_hard_limits_including_zero(metadata, sample, reason):
    with pytest.raises(ValueError, match=reason):
        module.battery_watts("discharge", 1800, fixed_snapshot(**sample), fixed_config(**metadata), target_basis="battery_output")


@pytest.mark.asyncio
async def test_fixed_output_cannot_send_a_lease_beyond_its_absolute_deadline(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(module.time, "time", lambda: clock[0])
    receipts, commands = [], []
    async def dispatch(watts, seconds, *, before_submit, before_write):
        await before_submit(1012)
        before_write()
        commands.append((watts, seconds))
        return True
    async def resume(*, before_submit, before_write):
        assert clock[0] >= 1013
        await before_submit(int(clock[0]) + 12)
        before_write()
        return True
    async def sleep(seconds):
        clock[0] += seconds
    # Only the cleanup awaits the fake clock; don't schedule a renewal task.
    monkeypatch.setattr(module.asyncio, "sleep", sleep)
    client = SimpleNamespace(get_snapshot=AsyncMock(return_value=fixed_snapshot()), dispatch_battery_power=dispatch,
        resume_battery_operation=resume)
    async def persist():
        receipts.append((controller.power_acceptance_until, controller.resume_acceptance_until))
    controller = module.LocalBatteryDispatch(client, fixed_config, lambda mode: True, persist, AsyncMock())
    controller._mode, controller._target_basis = "discharge", "battery_output"
    controller._target, controller._deadline, controller.pending = 1800, 1030, True
    assert await controller._write() == 1800
    assert commands == [(1800, 16)]
    assert commands[0][1] + controller.power_acceptance_until <= controller._deadline
    assert receipts[0] == (1013, 0)
    await controller.stop()
    assert clock[0] >= 1026
    assert receipts[-1] == (1013, 1026)
    assert controller.pending is False


@pytest.mark.asyncio
async def test_short_fixed_window_is_refused_without_a_power_command():
    client = SimpleNamespace(get_snapshot=AsyncMock(return_value=fixed_snapshot()),
        dispatch_battery_power=AsyncMock(return_value=True), resume_battery_operation=AsyncMock(return_value=True))
    controller = module.LocalBatteryDispatch(client, fixed_config, lambda mode: True, AsyncMock(), AsyncMock())
    with pytest.raises(ValueError, match="expired"):
        await controller.start("discharge", 1800, 1, target_basis="battery_output")
    client.dispatch_battery_power.assert_not_awaited()
