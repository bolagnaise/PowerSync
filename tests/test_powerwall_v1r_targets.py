"""Literal idle and bounded SOC targets use the owned V1R lease lifecycle."""
import asyncio
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from conftest import load_power_sync_submodule
from test_powerwall_fixed_output import fixed_config, fixed_snapshot
from test_powerwall_native_site_limits import fixture as limits_fixture

requests = load_power_sync_submodule("powerwall_local.battery_output", "v1r_targets")
module = load_power_sync_submodule("powerwall_local.battery_dispatch", "v1r_targets")


@pytest.mark.parametrize("power,target,soc", [(-1800, 80, 60), (1800, 40, 60), (1800, 1, 2)])
def test_target_percent_is_directional_and_one_percent_stays_one(power, target, soc):
    requests.check_target_soc(power, target, soc)
    with pytest.raises(requests.BatteryOutputError, match="target reached"):
        requests.check_target_soc(power, target, target)


@pytest.mark.parametrize("power,target", [(0, 80), (-1800, 0), (1800, 101), (1800, True), (1800, 1.5)])
def test_invalid_targets_are_not_ignored(power, target):
    with pytest.raises(requests.BatteryOutputError):
        requests.validate_output_request({"request_id": "test", "expected_control_revision": "a" * 64,
            "power_w": power, "duration_seconds": 60, "target_soc": target})


@pytest.mark.parametrize("power,target,options", [(-1800, 90, {"optimization_grid_charge_soc_cap": .8}),
    (1800, 10, {"optimization_backup_reserve": .2})])
def test_unreachable_target_is_refused_without_clamping(power, target, options):
    with pytest.raises(ValueError, match="ceiling|reserve"):
        module.check_battery_soc_target(power, target, fixed_snapshot(), fixed_config(**options))


@pytest.mark.asyncio
async def test_zero_uses_real_power_lease_and_owned_resume_without_grid_charge_changes():
    client = NS(get_snapshot=AsyncMock(return_value=fixed_snapshot(soc=100, grid_charging_enabled=False)),
        dispatch_battery_power=AsyncMock(return_value=True), resume_battery_operation=AsyncMock(return_value=True))
    persist, stopped = AsyncMock(), AsyncMock()
    c = module.LocalBatteryDispatch(client, lambda: fixed_config(optimization_allow_grid_charge=False),
        lambda mode: True, persist, stopped)
    try:
        result = await c.start("charge", 0, 60, target_basis="battery_output", prepare_grid_charging=True)
        assert result["battery_power_w"] == 0
        assert client.dispatch_battery_power.await_args.args[0] == 0
        assert c.grid_charge_restore is None
        assert persist.await_count and c.pending
    finally:
        await c.stop()
    client.resume_battery_operation.assert_awaited_once()
    stopped.assert_awaited_once()
    assert not c.pending


@pytest.mark.asyncio
@pytest.mark.parametrize("power,target,start,end", [(-1800, 80, 60, 81), (1800, 40, 60, 39)])
async def test_target_reached_on_renewal_releases_without_another_power_command(monkeypatch, power, target, start, end):
    sample = fixed_snapshot(soc=start)
    client = NS(get_snapshot=AsyncMock(side_effect=lambda: sample), dispatch_battery_power=AsyncMock(return_value=True),
        resume_battery_operation=AsyncMock(return_value=True))
    c = module.LocalBatteryDispatch(client, fixed_config, lambda mode: True, AsyncMock(), AsyncMock())
    original_sleep = asyncio.sleep

    async def advance(_):
        nonlocal sample
        sample = fixed_snapshot(soc=end)
        await original_sleep(0)

    monkeypatch.setattr(module.asyncio, "sleep", advance)
    check = lambda snapshot, _: module.check_battery_soc_target(power, target, snapshot, fixed_config())
    try:
        await c.start("charge" if power < 0 else "discharge", abs(power), 120,
                      target_basis="battery_output", snapshot_check=check)
        await asyncio.wait_for(c._task, 1)
        assert client.dispatch_battery_power.await_count == 1
        client.resume_battery_operation.assert_awaited_once()
        assert not c.pending and c.completion_reason == "target_reached"
    finally:
        await c.stop()


@pytest.mark.asyncio
async def test_confirmed_explicit_edit_supersedes_only_its_legacy_approval():
    c, request, gateway, writes, behavior, load, changes = limits_fixture()
    await c.execute(await request(operation="approve", import_limit_w=8000, export_limit_w=2000,
        confirm_installation_limits=True, document_reference="Legacy record", approval_date="2026-11-15"), actor="owner")
    result = await c.execute(await request(export_limit_w=6000))
    assert result["success"] and gateway["site_info"]["min_site_meter_power_ac"] == -6
    state = await load()
    assert "export_limit_w" not in state["approval"]
    assert state["approval"]["import_limit_w"]["max_w"] == 8000
    assert state["operational"]["export_limit_w"] == 6000


@pytest.mark.asyncio
async def test_unknown_edit_retains_legacy_cap_until_independent_desired_readback():
    c, request, gateway, writes, behavior, load, changes = limits_fixture()
    await c.execute(await request(operation="approve", export_limit_w=2000, confirm_installation_limits=True,
        document_reference="Legacy record", approval_date="2026-11-15"), actor="owner")
    behavior["ack"] = False
    with pytest.raises(ValueError):
        await c.execute(await request(export_limit_w=6000))
    assert (await load())["approval"]["export_limit_w"]["max_w"] == 2000
    behavior["now"] += 50
    result = await c.execute(await request(operation="reconcile"))
    assert result["success"]
    assert "export_limit_w" not in (await load())["approval"]


def test_discharge_target_uses_configured_floor_when_native_reserve_is_unknown():
    module.check_battery_soc_target(1800, 40, fixed_snapshot(soc=60, backup_reserve_percent=None), fixed_config())
    with pytest.raises(ValueError, match="reserve"):
        module.check_battery_soc_target(1800, 10, fixed_snapshot(soc=60, backup_reserve_percent=None), fixed_config())


@pytest.mark.asyncio
async def test_real_runtime_fixed_zero_never_becomes_max_or_changes_grid_permission():
    from test_powerwall_battery_dispatch import load_routing, call
    import time
    ns, controller, client, saved = load_routing()
    ns["check_battery_soc_target"] = module.check_battery_soc_target
    ns["timezone"] = __import__("datetime").timezone
    operation = {"power_w": 0, "operation_id": "zero-owned", "expires_at": time.time() + 120}
    try:
        result = await ns["_try_local_powerwall_force_locked"]("charge", call(source="optimizer"), 2, 0, fixed_output=operation)
        assert result["battery_power_w"] == 0
        assert client.dispatch_battery_power.await_args.args[0] == 0
        assert saved["force_mode_state"]["power_target_basis"] == "battery_output"
        assert saved["force_mode_state"]["battery_output_operation_id"] == "zero-owned"
        assert controller.grid_charge_restore is None
    finally:
        await controller.stop()
    client.resume_battery_operation.assert_awaited_once()
    assert saved["force_mode_state"] is None
