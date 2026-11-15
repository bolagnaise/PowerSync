"""Persistent native limits: approval, exact CAS, readback and recovery."""
import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json

import pytest
from conftest import load_power_sync_submodule

m = load_power_sync_submodule("powerwall_local.native_site_limits", "native_site_limits_test")
NOW = datetime(2026, 11, 15, tzinfo=timezone.utc).timestamp()


@pytest.mark.parametrize("source", [
    '{"site_info":{},"value":9007199254740993}',
    '{"site_info":{},"value":1.0000000000000001}',
    '{"site_info":{},"value":1e400}',
    '{"site_info":{},"value":1e-400}',
    '{"site_info":{},"value":NaN}',
    '{"site_info":{},"value":1,"value":2}',
])
def test_lossy_or_ambiguous_configuration_is_refused(source):
    with pytest.raises(ValueError):
        m.parse_config_json(source)


def test_semantic_json_preserves_unrelated_values_without_requiring_lexical_spelling():
    source = '{"site_info":{},"value":5.0,"nested":[0.1,true,null,{"text":"12.00001"}]}'
    assert m.parse_config_json(source) == json.loads(source)
    assert type(m.parse_config_json(source)["value"]) is float


def fixture():
    gateway = {"site_info": {"max_site_meter_power_ac": 12, "min_site_meter_power_ac": -5,
                             "on_grid_solar_curtailment_enabled": False},
               "unrelated": {"array": [None, True, 5.0, "value"]}}
    stored, pending, writes, changes = {}, None, [], []
    behaviour = {"ack": True, "apply": True, "before_validate": None, "now": NOW, "online": True, "owned": False}
    identity = {"site_id": "site", "gateway": "gateway", "pairing": "key"}

    async def load():
        return deepcopy(stored)

    async def save(value, unresolved):
        nonlocal stored, pending
        stored = deepcopy(value)
        if unresolved != "preserve":
            pending = deepcopy(unresolved)

    async def read():
        if not behaviour["online"]:
            raise RuntimeError("offline")
        return {"config": deepcopy(gateway), "hash": hashlib.sha256(json.dumps(gateway, sort_keys=True).encode()).hexdigest()}

    async def write(updates, validate, stage, before_write):
        if behaviour["before_validate"]:
            behaviour["before_validate"]()
        document = await read()
        assert validate(document["config"], bytes.fromhex(document["hash"])) is True
        await stage(NOW + 10)
        assert stored["transaction"]["phase"] == "submitted"
        assert pending["id"] == stored["transaction"]["operation_id"]
        before_write()
        writes.append(deepcopy(updates))
        if behaviour["apply"]:
            for key, value in updates.items():
                gateway["site_info"][key.split(".")[1]] = value
        return behaviour["ack"]

    async def changed():
        changes.append(True)

    def guard(owned):
        return not behaviour["owned"] and (pending is None or owned and pending.get("kind") == "native_site_limits")

    control = m.NativeSiteLimits(identity=lambda: identity, load=load, save=save,
        read=read, write=write, guard=guard, lock=asyncio.Lock(), changed=changed, clock=lambda: behaviour["now"])
    counter = 0

    async def request(**values):
        nonlocal counter
        counter += 1
        status = await control.read()
        return {"request_id": f"request-{counter}", "expected_config_revision": status["configuration_revision"],
                "expected_approval_revision": status["approval_revision"], **values}

    return control, request, gateway, writes, behaviour, load, changes


@pytest.mark.asyncio
async def test_tightening_is_persistent_and_does_not_change_permissions_or_other_fields():
    control, request, gateway, writes, behaviour, load, changes = fixture()
    other = deepcopy(gateway["unrelated"])
    result = await control.execute(await request(export_limit_w=2000))
    assert result["success"] and result["transaction"]["phase"] == "readback_confirmed"
    assert result["physical_enforcement_verified"] is False
    assert writes == [{"site_info.min_site_meter_power_ac": -2}]
    assert gateway["site_info"]["max_site_meter_power_ac"] == 12
    assert gateway["site_info"]["on_grid_solar_curtailment_enabled"] is False
    assert gateway["unrelated"] == other
    assert changes == [True]
    assert (await control.read())["limits"]["export_limit_w"]["value_w"] == 2000


@pytest.mark.asyncio
async def test_authenticated_mixed_tightening_and_relaxation_writes_one_document_without_paperwork():
    control, request, gateway, writes, *_ = fixture()
    result = await control.execute(await request(import_limit_w=10000, export_limit_w=6000))
    assert result["success"]
    assert writes == [{"site_info.max_site_meter_power_ac": 10, "site_info.min_site_meter_power_ac": -6}]
    assert gateway["site_info"]["max_site_meter_power_ac"] == 10
    assert gateway["site_info"]["min_site_meter_power_ac"] == -6


@pytest.mark.asyncio
async def test_owner_can_restore_a_known_owned_baseline_without_paperwork():
    control, request, gateway, writes, *_ = fixture()
    result = await control.execute(await request(export_limit_w=2000))
    restored = await control.execute(await request(operation="restore", snapshot_id=result["snapshots"][0]["snapshot_id"]))
    assert restored["success"]
    assert len(writes) == 2 and gateway["site_info"]["min_site_meter_power_ac"] == -5


@pytest.mark.asyncio
async def test_explicit_approval_writes_no_hardware_then_allows_only_owned_restore():
    control, request, gateway, writes, *_ = fixture()
    result = await control.execute(await request(export_limit_w=2000))
    snapshot_id = result["snapshots"][0]["snapshot_id"]
    approved = await control.execute(await request(operation="approve", export_limit_w=5000,
        confirm_installation_limits=True, document_reference="Installer connection agreement", approval_date="2026-11-15"), actor="owner")
    assert approved["hardware_command_sent"] is False
    assert len(writes) == 1
    restored = await control.execute(await request(operation="restore", snapshot_id=snapshot_id))
    assert restored["success"]
    assert gateway["site_info"]["min_site_meter_power_ac"] == -5


@pytest.mark.asyncio
async def test_compare_before_restore_preserves_an_external_change():
    control, request, gateway, writes, *_ = fixture()
    result = await control.execute(await request(export_limit_w=2000))
    gateway["site_info"]["min_site_meter_power_ac"] = -1
    with pytest.raises(ValueError, match="externally modified"):
        await control.execute(await request(operation="restore", snapshot_id=result["snapshots"][0]["snapshot_id"]))
    assert len(writes) == 1


@pytest.mark.asyncio
async def test_precondition_checks_the_exact_document_used_for_the_write():
    control, request, gateway, writes, behaviour, load, _ = fixture()
    data = await request(export_limit_w=2000)
    behaviour["before_validate"] = lambda: gateway["site_info"].update(min_site_meter_power_ac=-1)
    with pytest.raises(ValueError, match="revision conflict"):
        await control.execute(data)
    assert not writes
    assert (await load())["transaction"]["phase"] == "rejected"


@pytest.mark.asyncio
@pytest.mark.parametrize("ack,apply", [(False, False), (False, True), (True, False)])
async def test_unknown_or_mismatched_write_retains_recovery_and_does_not_retry(ack, apply):
    control, request, gateway, writes, behaviour, load, changes = fixture()
    behaviour.update(ack=ack, apply=apply)
    with pytest.raises(ValueError):
        await control.execute(await request(export_limit_w=2000))
    assert (await load())["transaction"]["phase"] == "recovery_required"
    with pytest.raises(ValueError, match="reconciliation required"):
        await control.execute(await request(export_limit_w=1000))
    assert len(writes) == 1
    assert changes == [True]


@pytest.mark.asyncio
async def test_request_id_reuse_never_replays_a_cap_command():
    control, request, gateway, writes, *_ = fixture()
    data = await request(export_limit_w=2000)
    await control.execute(data)
    result = await control.execute(data)
    assert result["replayed"] and len(writes) == 1
    with pytest.raises(ValueError, match="request id conflict"):
        await control.execute({**data, "export_limit_w": 1000})


@pytest.mark.parametrize("value", [True, "5000", -1, 1000001, 5000.5, float("nan")])
def test_site_limit_values_have_one_strict_watts_contract(value):
    with pytest.raises(ValueError):
        m.plan_changes({"site_info": {"max_site_meter_power_ac": 12, "min_site_meter_power_ac": -5}},
                       {"export_limit_w": value}, {}, {})


def test_unknown_native_value_does_not_become_unlimited_permission():
    config = {"site_info": {"max_site_meter_power_ac": 1e9}}
    assert m.native_fields(config)["import_limit_w"]["value_w"] is None
    updates, baseline = m.plan_changes(config, {"export_limit_w": 5000}, {}, {})
    assert updates == {"site_info.min_site_meter_power_ac": -5}
    assert baseline["export_limit_w"]["present"] is False
    assert "site_info.max_site_meter_power_ac" not in updates


@pytest.mark.asyncio
async def test_approval_can_be_reduced_or_revoked_offline_without_retiring_unknown_write():
    control, request, gateway, writes, behaviour, load, changes = fixture()
    behaviour["ack"] = False
    with pytest.raises(ValueError):
        await control.execute(await request(export_limit_w=2000))
    data = await request(operation="approve", import_limit_w=0, export_limit_w=None,
        confirm_installation_limits=True, document_reference="Revised installation approval", approval_date="2026-11-15")
    behaviour.update(online=False, owned=True)
    result = await control.execute(data, actor="owner")
    assert result["hardware_command_sent"] is False
    state = await load()
    assert state["transaction"]["phase"] == "recovery_required"
    assert m.recorded_limits_w(state, state["identity"]) == (0, 2000)
    assert len(writes) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("apply,terminal,watts", [(True, "readback_confirmed", 2000), (False, "rejected", 5000)])
async def test_reconciliation_requires_acceptance_closure_and_fresh_matching_readback(apply, terminal, watts):
    control, request, gateway, writes, behaviour, load, changes = fixture()
    behaviour.update(ack=False, apply=apply)
    with pytest.raises(ValueError):
        await control.execute(await request(export_limit_w=2000))
    with pytest.raises(ValueError, match="acceptance pending"):
        await control.execute(await request(operation="reconcile"))
    behaviour["now"] += 60
    result = await control.execute(await request(operation="reconcile"))
    assert result["reconciled"] is True
    state = await load()
    assert state["transaction"]["phase"] == terminal
    assert m.recorded_limits_w(state, state["identity"])[1] == watts
    assert len(writes) == 1


@pytest.mark.asyncio
async def test_external_change_does_not_retire_an_unknown_cap_write():
    control, request, gateway, writes, behaviour, load, changes = fixture()
    behaviour["ack"] = False
    with pytest.raises(ValueError):
        await control.execute(await request(export_limit_w=2000))
    behaviour["now"] += 60
    gateway["site_info"]["min_site_meter_power_ac"] = -3
    with pytest.raises(ValueError, match="externally modified"):
        await control.execute(await request(operation="reconcile"))
    assert (await load())["transaction"]["phase"] == "recovery_required"
    assert len(writes) == 1


def test_recorded_limits_intersect_zero_approval_native_selection_and_unknown_original():
    identity = {"site_id": "site", "gateway": "gateway", "pairing": "public"}
    state = {"identity": identity, "approval": {"import_limit_w": {"active": True, "max_w": 0, "identity": identity}},
        "operational": {"import_limit_w": 12000, "export_limit_w": 5000}}
    assert m.recorded_limits_w(state, identity) == (0, 5000)
    state["transaction"] = {"phase": "recovery_required", "baseline": {"export_limit_w": {"supported": False}},
        "updates": {"site_info.min_site_meter_power_ac": -2}}
    assert m.recorded_limits_w(state, identity) == (0, 0)
    assert m.recorded_limits_w(state, {**identity, "gateway": "repaired"}) == (0, 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("ack", [True, False])
async def test_1005_watts_round_trip_readback_reconcile_and_owned_restore(ack):
    control, request, gateway, writes, behaviour, load, changes = fixture()
    await control.execute(await request(operation="approve", export_limit_w=5000,
        confirm_installation_limits=True, document_reference="Synthetic agreement", approval_date="2026-11-15"), actor="owner")
    behaviour["ack"] = ack
    data = await request(export_limit_w=1005)
    if ack:
        result = await control.execute(data)
        snapshot_id = result["snapshots"][-1]["snapshot_id"]
        await control.execute(await request(operation="restore", snapshot_id=snapshot_id))
        assert gateway["site_info"]["min_site_meter_power_ac"] == -5
    else:
        with pytest.raises(ValueError):
            await control.execute(data)
        behaviour["now"] += 60
        await control.execute(await request(operation="reconcile"))
        assert (await load())["operational"]["export_limit_w"] == 1005
    assert m.native_fields({"site_info": {"min_site_meter_power_ac": -1.005}})["export_limit_w"]["value_w"] == 1005


@pytest.mark.asyncio
async def test_repair_requires_admin_readback_and_discards_old_approval_without_hardware_write():
    control, request, gateway, writes, behaviour, load, changes = fixture()
    await control.execute(await request(export_limit_w=2000))
    old_identity = deepcopy(control.identity())
    control.identity()["pairing"] = "replacement-key"
    assert (await control.read())["capabilities"]["identity_rebaseline_required"] is True
    assert m.recorded_limits_w(await load(), control.identity()) == (0, 0)
    data = await request(operation="rebaseline", confirm_pairing_change=True)
    with pytest.raises(ValueError, match="administrator"):
        await control.execute(data)
    result = await control.execute(data, actor="admin")
    assert result["hardware_command_sent"] is False
    state = await load()
    assert state["approval"] == {} and state["archives"][-1]["identity"] == old_identity
    assert m.recorded_limits_w(state, control.identity()) == (12000, 2000)
    assert (await control.execute(data, actor="admin"))["replayed"]
    assert len(writes) == 1


@pytest.mark.asyncio
async def test_offline_status_exposes_authority_for_revocation_without_fabricated_readback():
    control, request, gateway, writes, behaviour, load, changes = fixture()
    await control.execute(await request(operation="approve", import_limit_w=10000,
        confirm_installation_limits=True, document_reference="Synthetic agreement", approval_date="2026-11-15"), actor="owner")
    behaviour["online"] = False
    status = await control.read()
    assert status["source"] == "stored_authority" and status["configuration_revision"] == ""
    assert status["capabilities"]["readable"] is False and status["capabilities"]["writable"] is False
    assert status["limits"]["import_limit_w"]["value_w"] is None
    await control.execute({"operation": "approve", "request_id": "offline-revoke", "expected_approval_revision": status["approval_revision"],
        "import_limit_w": None, "confirm_installation_limits": True, "document_reference": "Revoked", "approval_date": "2026-11-15"}, actor="owner")
    assert (await load())["approval"]["import_limit_w"]["active"] is False and not writes
