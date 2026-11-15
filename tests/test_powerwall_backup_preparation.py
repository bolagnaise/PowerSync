"""Advanced backup preparation reuses owned service/recovery semantics."""
from copy import deepcopy
from unittest.mock import AsyncMock

import pytest

from conftest import load_power_sync_submodule
from test_powerwall_fixed_output import fixed_config, fixed_snapshot

m = load_power_sync_submodule("powerwall_local.backup_preparation", "backup_preparation_test")
transport = load_power_sync_submodule("powerwall_local.transport", "backup_preparation_test")


def fixture():
    saved, calls, live = {}, [], {"operation": None, "readable": True, "can_start": True}
    behavior = {"fail": False, "allowed": True}

    async def load(): return deepcopy(saved)
    async def save(value): saved.clear(); saved.update(deepcopy(value))
    async def status(): return deepcopy(live)

    async def start(minutes, context, requested_at):
        assert saved["start"]["phase"] == "prepared"
        calls.append(("start", minutes, requested_at))
        if behavior["fail"]:
            live["operation"] = {"operation_id": "owned", "phase": "recovery_required", "active": True, "ends_at": requested_at + minutes * 60, "can_cancel": True}
            raise RuntimeError("unknown")
        live["operation"] = {"operation_id": "owned", "phase": "active", "active": True, "ends_at": requested_at + minutes * 60, "can_cancel": True}

    async def stop(operation_id, context):
        calls.append(("stop", operation_id))
        live["operation"] = None

    async def reconcile(context):
        if live["operation"]:
            live["operation"]["phase"] = "active"
    control = lambda: m.BackupPreparationControl(load=load, save=save, status=status, start=start, stop=stop, allowed=lambda: behavior["allowed"], reconcile=reconcile)
    return control, behavior, calls, live, saved


@pytest.mark.asyncio
async def test_start_is_durable_and_reconstruction_or_duplicate_cannot_extend_it():
    factory, behavior, calls, live, saved = fixture()
    body = {"request_id": "start", "duration_minutes": 60}
    first = await factory().execute(body)
    deadline = first["operation"]["ends_at"]
    replay = await factory().execute(body)
    assert replay["replayed"] and replay["operation"]["ends_at"] == deadline
    assert len(calls) == 1
    with pytest.raises(ValueError, match="conflict"):
        await factory().execute({**body, "duration_minutes": 30})
    with pytest.raises(ValueError, match="changed"):
        await factory().execute({"request_id": "stop", "operation": "cancel", "operation_id": "successor"})
    assert len(calls) == 1
    behavior["allowed"] = False
    await factory().execute({"request_id": "stop", "operation": "cancel", "operation_id": "owned"})
    assert calls[-1] == ("stop", "owned"), "owned cleanup survives loss of acquisition permission"


@pytest.mark.asyncio
async def test_unknown_start_is_not_replayed_and_cleanup_is_required_before_a_new_start():
    factory, behavior, calls, live, saved = fixture()
    behavior["fail"] = True
    with pytest.raises(RuntimeError):
        await factory().execute({"request_id": "start", "duration_minutes": 60})
    assert saved["start"]["phase"] == "prepared"
    replay = await factory().execute({"request_id": "start", "duration_minutes": 60})
    assert replay["replayed"] and not replay["success"] and len(calls) == 1
    assert (await factory().read())["recovery_required"]
    with pytest.raises(ValueError, match="owned or unavailable"):
        await factory().execute({"request_id": "new", "duration_minutes": 60})
    await factory().execute({"request_id": "reconcile", "operation": "reconcile"})
    assert not (await factory().read())["recovery_required"]
    assert calls == [("start", 60, saved["start"]["started_at"])]


@pytest.mark.parametrize("minutes", [True, None, "60", 0, 1441, 1.5])
@pytest.mark.asyncio
async def test_invalid_backup_window_never_calls_the_service(minutes):
    factory, behavior, calls, live, saved = fixture()
    with pytest.raises(ValueError):
        await factory().execute({"request_id": "start", "duration_minutes": minutes})
    assert not calls and not saved


def test_backup_does_not_enable_grid_charging_or_bypass_recorded_native_caps():
    m.validate_backup_preparation(fixed_config(), fixed_snapshot())
    for options, sample in [({"optimization_allow_grid_charge": False}, {}), ({}, {"grid_charging_enabled": False}),
                           ({"optimization_grid_charge_soc_cap": .8}, {}), ({}, {"imported": 0}),
                           ({"native_site_import_cap_w": 1000}, {}), ({"native_site_export_cap_w": 0}, {})]:
        with pytest.raises(ValueError):
            m.validate_backup_preparation(fixed_config(**options), fixed_snapshot(**sample))


def test_manual_backup_fingerprint_distinguishes_replacement_with_the_same_end():
    first = {"start_time": 1000, "duration_seconds": 600, "priority": (1 << 64) - 1}
    assert transport.manual_backup_matches_window({"manual_backup": first}, 1600, first) is True
    replacement = {**first, "start_time": 1100, "duration_seconds": 500}
    assert transport.manual_backup_matches_window({"manual_backup": replacement}, 1600, first) is False
    assert transport.manual_backup_matches_window(None, 1600, first) is None
