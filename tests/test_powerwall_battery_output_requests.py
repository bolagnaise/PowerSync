"""Fixed-output request lifecycle never aliases Force or replays power."""
from copy import deepcopy
import asyncio

import pytest
from conftest import load_power_sync_submodule

m = load_power_sync_submodule("powerwall_local.battery_output", "output_requests_test")


def fixture():
    stored, calls = {}, []
    current = {"active": False, "any_active": False, "release_pending": False}
    policy = {"revision": "a" * 64, "allowed": True, "save_fail": False, "start_fail": False, "stop_fail": False}
    now = [1000]

    async def load():
        return deepcopy(stored)

    async def save(value):
        nonlocal stored
        if policy["save_fail"]:
            raise RuntimeError("storage")
        stored = deepcopy(value)

    async def start(operation, context):
        assert stored["operation"]["phase"] == "prepared"
        assert operation["expires_at"] == 1030
        calls.append(("start", deepcopy(operation)))
        if policy["start_fail"]:
            current["release_pending"] = True
            raise RuntimeError("unknown")
        current.update(active=True, any_active=True, operation_id=operation["operation_id"], commanded_w=operation["power_w"])
        policy["revision"] = "b" * 64
        return {"success": True}

    async def stop(operation, context):
        calls.append(("stop", operation["operation_id"]))
        if policy["stop_fail"]:
            current.update(active=False, release_pending=True)
            raise RuntimeError("resume unconfirmed")
        current.update(active=False, any_active=False, release_pending=False)
        policy["revision"] = "c" * 64

    def controller():
        return m.BatteryOutputControl(load=load, save=save, revision=lambda: policy["revision"],
            status=lambda: dict(current), start=start, stop=stop, allowed=lambda: policy["allowed"], clock=lambda: now[0])

    request = {"power_w": 1800, "duration_seconds": 30, "request_id": "output-one", "expected_control_revision": policy["revision"]}
    return controller, request, current, policy, calls, load, now


@pytest.mark.asyncio
async def test_saved_intent_exact_deadline_and_duplicate_do_not_extend_or_resubmit():
    factory, request, current, policy, calls, load, now = fixture()
    c = factory()
    result = await c.dispatch(request)
    assert result["operation"]["commanded_w"] == 1800
    assert result["operation"]["tracking"] == "unknown"
    now[0] += 20
    replay = await c.dispatch(request)
    assert replay["replayed"] and replay["operation"]["expires_at"] == 1030
    assert len(calls) == 1
    # Reconstructing the request layer reads receipts and never starts power.
    restarted = factory()
    assert (await restarted.read())["operation"]["operation_id"] == result["operation"]["operation_id"]
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("change,expected", [
    ({"power_w": 1900}, "request_id_conflict"),
    ({"duration_seconds": 60}, "request_id_conflict"),
])
async def test_same_identity_cannot_change_payload(change, expected):
    factory, request, *rest = fixture()
    c = factory()
    await c.dispatch(request)
    with pytest.raises(m.BatteryOutputError, match=expected.replace("_", " ")):
        await c.dispatch({**request, **change})


@pytest.mark.asyncio
async def test_unavailable_conflicting_or_recovery_requests_do_not_call_hardware():
    factory, request, current, policy, calls, *_ = fixture()
    c = factory()
    for update, policy_update in [
        ({}, {"allowed": False}),
        ({"any_active": True}, {"allowed": True}),
        ({"release_pending": True}, {}),
    ]:
        current.update(update); policy.update(policy_update)
        with pytest.raises(m.BatteryOutputError):
            await c.dispatch(request)
    assert calls == []


@pytest.mark.asyncio
async def test_storage_failure_prevents_submission_and_unknown_start_remains_fenced():
    factory, request, current, policy, calls, load, _ = fixture()
    policy["save_fail"] = True
    with pytest.raises(RuntimeError, match="storage"):
        await factory().dispatch(request)
    assert calls == []
    policy.update(save_fail=False, start_fail=True)
    with pytest.raises(RuntimeError, match="unknown"):
        await factory().dispatch(request)
    assert (await load())["operation"]["phase"] == "recovery_required"
    replay = await factory().dispatch(request)
    assert replay["replayed"] and not replay["success"]
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_stop_is_owned_idempotent_and_cannot_stop_successor():
    factory, request, current, policy, calls, *_ = fixture()
    c = factory()
    result = await c.dispatch(request)
    stop = {"operation_id": result["operation"]["operation_id"], "request_id": "stop-one", "expected_control_revision": policy["revision"]}
    current["operation_id"] = "successor"
    with pytest.raises(m.BatteryOutputError, match="ownership changed"):
        await c.end(stop)
    assert len(calls) == 1
    current["operation_id"] = stop["operation_id"]
    assert (await c.end(stop))["success"]
    assert (await c.end(stop))["replayed"]
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_unconfirmed_stop_retains_recovery_state():
    factory, request, current, policy, calls, load, _ = fixture()
    c = factory()
    result = await c.dispatch(request)
    policy["stop_fail"] = True
    with pytest.raises(RuntimeError, match="resume unconfirmed"):
        await c.end({"operation_id": result["operation"]["operation_id"], "request_id": "stop", "expected_control_revision": policy["revision"]})
    assert (await load())["operation"]["phase"] == "recovery_required"
    assert (await c.read())["operation"]["phase"] == "recovery_required"


@pytest.mark.asyncio
async def test_concurrent_duplicate_is_one_submission():
    factory, request, current, policy, calls, *_ = fixture()
    c = factory()
    results = await asyncio.gather(c.dispatch(request), c.dispatch(request))
    assert len(calls) == 1 and sum(bool(r.get("replayed")) for r in results) == 1


@pytest.mark.asyncio
async def test_superseded_history_is_bounded_and_does_not_block_after_33_takeovers():
    factory, request, current, policy, calls, load, _ = fixture()
    c = factory()
    for i in range(35):
        if i:
            current.update(operation_id="legacy-successor", active=True, any_active=True)
        await c.dispatch({**request, "request_id": f"request-{i}", "takeover": True,
                          "expected_control_revision": policy["revision"]})
    assert len(calls) == 35
    assert len((await load())["requests"]) <= 32
    assert sum(r["phase"] == "active" for r in (await load())["requests"].values()) == 1


@pytest.mark.asyncio
async def test_policy_revision_change_does_not_prevent_an_owned_stop():
    factory, request, current, policy, calls, *_ = fixture()
    c = factory()
    result = await c.dispatch(request)
    old_revision = policy["revision"]
    policy["revision"] = "d" * 64
    result = await c.end({"operation_id": result["operation"]["operation_id"], "request_id": "stop-after-policy", "expected_control_revision": old_revision})
    assert result["success"] and not result["operation"]["active"]


@pytest.mark.parametrize("field,value", [
    ("power_w", True), ("power_w", "1800"), ("power_w", 1.8),
    ("power_w", -10001), ("duration_seconds", False), ("duration_seconds", 1.5),
    ("duration_seconds", 86401), ("takeover", "yes"), ("request_id", ""),
])
def test_strict_contract_rejects_lossy_values(field, value):
    _, request, *_ = fixture()
    with pytest.raises(m.BatteryOutputError):
        m.validate_output_request({**request, field: value})
