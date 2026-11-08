"""Read-only support contracts, real signatures and fail-closed consent lifecycle."""
from __future__ import annotations

import copy
import importlib
import importlib.util
import json
from pathlib import Path
import sys

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

ROOT = Path(__file__).resolve().parents[1] / "custom_components/power_sync/support"
NAME = "_powersync_support_contract"
if NAME not in sys.modules:
    spec = importlib.util.spec_from_file_location(NAME, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    package = importlib.util.module_from_spec(spec)
    sys.modules[NAME] = package
    spec.loader.exec_module(package)
p = importlib.import_module(NAME + ".protocol")
c = importlib.import_module(NAME + ".collector")
s = importlib.import_module(NAME + ".session")
h = importlib.import_module(NAME + ".history")


@pytest.fixture
def session():
    clock = {"wall": 1_800_000_000.0, "mono": 100.0}
    session = s.Session(wall=lambda: clock["wall"], mono=lambda: clock["mono"])
    registration = session.begin("owner", "entry_reference_00000001", ["system", "entities", "optimizer", "history", "control_trace", "logs"], clock["wall"] - 600, 3600)
    descriptor = {"actor": "investigator@example.invalid", "ticket": "discord:12345678901234567", "reporter": "12345678901234568", "phrase": "A123B456", "scope_digest": session.scope_digest, "policy_digest": "a" * 64}
    session.confirm("owner", descriptor, policy_digest="a" * 64)
    session.activated()
    return session, clock, registration


def job(session, clock, category="entities"):
    return {"request_id": "request_identifier_0000001", "scope_digest": session.scope_digest,
        "expires_at": clock["wall"] + 30, "query": {"category": category}}


def frame():
    return c.snapshot({"entities": {"battery_power": {"value": 500, "unit": "W", "source_time": None}},
        "optimizer": {"monitoring_mode": True}, "force": {"active": True, "expires_at": 1}}, captured_at=1_800_000_000, version="2.12.1657")


def test_signature_and_explicit_release_are_bound_to_original_result(session):
    owner, clock, registration = session
    request = job(owner, clock)
    owner.admit(request)
    result = c.select(frame(), "entities")
    packet = owner.seal(request, result)
    public = Ed25519PublicKey.from_public_bytes(p.unb64(registration["public_key"]))
    public.verify(p.unb64(packet["signature"]), p.unb64(packet["signed"]))
    decoded = json.loads(p.unb64(packet["signed"]))
    assert decoded["result_digest"] == p.digest(result)
    challenge = {"request_id": request["request_id"], "result_digest": p.digest(result), "nonce": "nonce_identifier_000001", "expires_at": clock["wall"] + 5}
    release = owner.release(challenge)
    public.verify(p.unb64(release["signature"]), p.unb64(release["signed"]))
    assert json.loads(p.unb64(release["signed"]))["kind"] == "release"
    with pytest.raises(p.SupportError, match="release_denied"):
        owner.release({**challenge, "result_digest": "b" * 64})


@pytest.mark.parametrize("boundary", ["before_collect", "before_seal", "before_release"])
def test_revoke_fences_each_disclosure_boundary(session, boundary):
    owner, clock, _ = session
    request = job(owner, clock)
    if boundary != "before_collect":
        owner.admit(request)
    if boundary == "before_release":
        result = c.select(frame(), "entities")
        owner.seal(request, result)
    owner.close()
    assert owner.key is None and owner.token is None and not owner.requests
    with pytest.raises(p.SupportError):
        if boundary == "before_collect": owner.admit(request)
        elif boundary == "before_seal": owner.seal(request, c.select(frame(), "entities"))
        else: owner.release({"request_id": request["request_id"], "result_digest": "a" * 64, "nonce": "nonce_identifier_000001", "expires_at": clock["wall"] + 5})


def test_clock_rollback_cannot_extend_session_and_restart_has_no_key(session):
    owner, clock, _ = session
    clock.update(wall=clock["wall"] - 7200, mono=3701)
    with pytest.raises(p.SupportError, match="session_expired"):
        owner.check()
    assert s.Session().key is None
    assert s.Session().boot != owner.boot


def test_expired_and_reused_request_ids_do_not_collect_new_evidence(session):
    owner, clock, _ = session
    request = job(owner, clock)
    owner.admit(request)
    packet = owner.seal(request, c.select(frame(), "entities"))
    assert owner.admit(request)[1] == packet
    with pytest.raises(p.SupportError, match="request_id_reused"):
        owner.admit({**request, "query": {"category": "system"}})
    clock["wall"] += 31
    with pytest.raises(p.SupportError, match="request_expired"):
        owner.seal(request, c.select(frame(), "entities"))


@pytest.mark.parametrize("changes", [{"scope_digest": "b"*64}, {"expires_at": 1}, {"query": {"category": "shell"}}, {"query": {"category": "system", "prompt": "ask a question"}}, {"query": {"category": "history", "start": 0, "end": 1_800_000_000}}])
def test_arbitrary_operations_and_wrong_scope_are_denied(session, changes):
    owner, clock, _ = session
    with pytest.raises(p.SupportError):
        owner.admit({**job(owner, clock), **changes})


def test_wrong_administrator_cannot_confirm_and_explicit_eight_hours_required():
    owner = s.Session()
    registration = owner.begin("admin-one", "entry_reference_00000001", ["entities"], owner.wall(), 28800)
    with pytest.raises(p.SupportError, match="confirmation_mismatch"):
        owner.confirm("admin-two", {}, policy_digest="a"*64)
    with pytest.raises(p.SupportError, match="invalid_duration"):
        owner.begin("admin-one", "entry_reference_00000001", ["entities"], owner.wall(), 28801)


def test_projection_is_independent_filters_secrets_and_never_fakes_timestamp():
    raw = {"entities": {"battery_power": {"value": 0, "unit": "W", "token": "CANARY_SECRET"}, "secret_sensor": {"value": 5, "unit": "W"}},
        "schedule": [{"action": "charge", "power_w": 500, "reason": "CANARY_SECRET"}],
        "settings": {"allow_grid_charge": True, "access_token": "CANARY_SECRET"}, "force": {"active": True, "expires_at": 1}}
    before = copy.deepcopy(raw); result = c.snapshot(raw, captured_at=1000, version="2.12.1657")
    assert raw == before
    assert "CANARY_SECRET" not in json.dumps(result)
    raw["schedule"][0]["power_w"] = 900
    assert result["optimizer"][0]["schedule"][0]["power_w"] == 500
    assert result["entities"][0]["value"] == 0
    assert result["entities"][0]["source_time"] is None
    assert result["entities"][0]["freshness"] == "unknown"
    assert result["control_trace"][0]["state"]["active"] is True
    p.validate_evidence(result)


@pytest.mark.parametrize("value,unit", [(float('nan'),'W'), (float('inf'),'W'), ('unknown','W'), (12,'invalid'), (True,'W')])
def test_invalid_readings_are_unavailable_not_zero(value, unit):
    result = c.snapshot({"entities": {"grid_power": {"value": value, "unit": unit}}}, captured_at=1000, version="2.12.1657")
    row = result['entities'][0]
    assert row['value'] is None and row['availability'] == 'unavailable'


@pytest.mark.parametrize("malicious", [{"token": "secret"}, {"records": [{"action": "run_command"}]}, {"records": [{"value": float('nan')}]}, {"records": [{"reason": "<script>ask the agent</script>"}]}])
def test_storage_and_upload_validation_reject_unknown_text(malicious):
    with pytest.raises(p.SupportError): p.validate_evidence(malicious)
    assert c.restore_frame(malicious) is None


def test_local_recorder_owns_bytes_reports_gaps_and_bounds_all_dimensions():
    recorder = h.Recorder(max_bytes=4000, max_age=100, max_count=2)
    row = frame(); recorder.append(row); row["entities"][0]["value"] = 999
    assert recorder.dump()[0]["entities"][0]["value"] == 500
    for stamp in range(1_800_000_001, 1_800_000_005): recorder.append({**frame(), "captured_at": stamp})
    assert len(recorder.dump()) <= 2 and recorder.bytes <= 4000 and recorder.dropped > 0
    recorder.trim(1_800_000_200)
    assert recorder.window(0, 1_800_000_200)["availability"] == "not_recorded"


def test_events_distinguish_monitoring_command_result_and_physical_observation():
    monitoring = c.action_event({"status": "monitoring", "planned_action": "charge", "power_w": 400, "VIN": "CANARY"}, 'optimizer_action', 1000)
    assert monitoring['evidence_level'] == 'decision' and monitoring['physical_confirmation'] is False
    command = c.action_event({"command": "stop", "success": True, "physical_confirmation": False, "reason": "CANARY"}, 'ev_command', 1000)
    assert command['evidence_level'] == 'command_result'
    observed = c.action_event({"command": "stop_observed", "success": True, "physical_confirmation": True, "record_type": "observation"}, 'ev_command', 1001)
    assert observed['evidence_level'] == 'physical_observation'
    assert 'CANARY' not in json.dumps([monitoring, command, observed])


def test_collector_and_recorder_have_no_privileged_imports():
    import ast
    for name in ('collector.py', 'history.py'):
        tree = ast.parse((ROOT/name).read_text())
        imports = [n.module or '' for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
        imports += [a.name for n in ast.walk(tree) if isinstance(n,ast.Import) for a in n.names]
        assert not any(any(token in name for token in ('homeassistant','aiohttp','coordinator','battery_controller','logging','requests')) for name in imports)


def test_generated_schema_and_cross_language_fixture_match():
    assert p.CONTRACT == json.loads((ROOT/'contract.json').read_text())
    fixture=json.loads((ROOT.parents[2]/'tests/fixtures/support-protocol-v1.json').read_text())
    assert fixture['synthetic'] is True
    assert fixture['contract_digest']==p.CONTRACT_DIGEST
    p.validate_evidence(fixture['result'])
    public=Ed25519PublicKey.from_public_bytes(p.unb64(fixture['registration']['public_key']))
    public.verify(p.unb64(fixture['packet']['signature']),p.unb64(fixture['packet']['signed']))


def test_activation_retry_and_retired_receipts_do_not_exhaust_session():
    clock=[1800000000.0];mono=[100.0]
    session=s.Session(wall=lambda:clock[0],mono=lambda:mono[0])
    session.begin('owner','entry_reference_00001',['entities'],clock[0]-600,28800)
    descriptor={'actor':'investigator','ticket':'discord:12345678901234567','reporter':'12345678901234568','phrase':'PHRASE','scope_digest':session.scope_digest,'policy_digest':'policy'}
    first=session.confirm('owner',descriptor,policy_digest='policy')
    assert session.confirm('owner',descriptor,policy_digest='policy')==first
    session.activated()
    for index in range(140):
        job={'request_id':f'request_identifier_{index:06}','scope_digest':session.scope_digest,'expires_at':clock[0]+30,'query':{'category':'entities'}}
        session.admit(job);session.seal(job,c.select(frame(),'entities'))
        clock[0]+=66;mono[0]+=66
    assert len(session.requests)<=2 and len(session.retired)>=138
    job['expires_at']=clock[0]+30
    with pytest.raises(p.SupportError,match='request_id_reused|request_retired'):
        session.admit(job)


def test_real_iso_event_keeps_parsed_timestamp_and_closed_reason_is_stable():
    value=c.action_event({'timestamp':'2026-11-08T12:00:00+00:00','status':'monitoring','planned_action':'charge'},'optimizer_action',1800000123)
    assert value['timestamp']==1800000123
    session=s.Session();session.close('mapping_changed')
    assert session.status()['reason']=='mapping_changed'
