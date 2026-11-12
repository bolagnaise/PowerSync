"""Disposable trusted scoring process. Its inputs never enter persistent files."""
import ctypes
import json
import os
from pathlib import Path
import signal
import re
import math
import hashlib
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "custom_components/power_sync"))
from diagnostics_access.private_capture import aad
from diagnostics_access import wire
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from regression_runner import trusted_contract as contract
from regression_runner.runtime import component_digest
from regression_runner.compare import classify, fidelity, source_coverage, changed_component_paths, run_status, temporal_surface, temporal_changed_methods
from private_regression.sandbox import Sandbox, no_swap, CandidateFailure
from private_regression.fingerprints import scorer_digest,schema_digest


def output(bundle, value, component):
    allowed = {'outcome', 'source', 'schedule', 'solver', 'exception', 'elapsed_seconds', 'max_rss_kib'}
    if type(value) is not dict or value.keys()-allowed or not {'outcome', 'source', 'schedule', 'solver'} <= value.keys():
        raise CandidateFailure('candidate_output_invalid')
    expected = {**bundle['source'], 'component_sha256': component}
    if value['source'] != expected:
        raise CandidateFailure('candidate_output_invalid')
    reconstructed = {key: item for key, item in bundle.items() if key != 'digest'}
    reconstructed['source'] = expected
    reconstructed['outcome'] = value['outcome']
    reconstructed['result'] = {'schedule': value['schedule'], 'solver': value['solver'],
        'feasible': None if value['outcome'] == 'exception' else value['outcome'] == 'feasible', 'exception': value.get('exception')}
    try:
        reconstructed['digest'] = contract.digest(reconstructed)
        contract.validate_bundle(reconstructed)
    except Exception:
        raise CandidateFailure('candidate_output_invalid') from None
    return {key: value.get(key) for key in ('outcome', 'schedule', 'solver', 'exception')}


def aggregate(statuses):
    if not statuses or any(status not in ('pass', 'fail', 'invalid', 'insufficient') for status in statuses):
        return 'invalid'
    if 'fail' in statuses:
        return 'fail'
    if 'invalid' in statuses:
        return 'invalid'
    return 'insufficient' if 'insufficient' in statuses else 'pass'


def temporal_output(value):
    try:
        return _temporal_output(value)
    except (ValueError,TypeError,KeyError,OverflowError,MemoryError,RecursionError):
        raise CandidateFailure('candidate_temporal_output_invalid') from None


def _temporal_output(value):
    from regression_runner.temporal import METHODS, STATE
    if type(value) is not dict or set(value) != {'profile','source','methods','commands','returns','state','elapsed_virtual_seconds','restarts'} or value['profile'] != 'reserve-recovery-temporal-v1' or value['methods'] != sorted(METHODS) or not re.fullmatch('[a-f0-9]{64}',value['source']):
        raise CandidateFailure('candidate_temporal_output_invalid')
    if type(value['state']) is not dict or set(value['state']) != STATE:
        raise CandidateFailure('candidate_temporal_output_invalid')
    for key in ('reserve','held_reserve','configured_reserve'):
        item=value['state'][key]
        if item is not None and (type(item) not in (int,float) or not math.isfinite(item) or not 0<=item<=100):
            raise CandidateFailure('candidate_temporal_output_invalid')
    if value['state']['last_action'] not in ('idle','self_consumption','charge','export',None) or any(type(value['state'][key]) is not bool for key in ('ev_preserve','scheduled_ev_state','monitoring')):
        raise CandidateFailure('candidate_temporal_output_invalid')
    for field in ('commands','returns'):
        rows=value[field]
        if type(rows) is not list or len(rows)>10000:
            raise CandidateFailure('candidate_temporal_output_invalid')
        for row in rows:
            keys={'at','generation','operation','reserve'} if field=='commands' else {'at','generation','value'}
            if type(row) is not dict or set(row)!=keys or any(type(row[key]) is not int or not 0<=row[key]<=604800 for key in ('at','generation')):
                raise CandidateFailure('candidate_temporal_output_invalid')
            item=row['reserve'] if field=='commands' else row['value']
            if item is not None and type(item) is not bool and (type(item) not in (int,float) or not math.isfinite(item) or not 0<=item<=100):
                raise CandidateFailure('candidate_temporal_output_invalid')
            if field=='commands' and row['operation']!='set_backup_reserve':
                raise CandidateFailure('candidate_temporal_output_invalid')
    return {key:value[key] for key in ('commands','returns','state')}


def temporal_checks(sandbox,pair,report):
    if type(report) is not dict or report.get('complete') is not True or not 1<=len(report.get('sequences',[]))<=32:
        return ['insufficient']
    statuses=[]
    for sequence in report['sequences']:
        outputs={}
        for arm in ('baseline','candidate'):
            artifact=pair[arm]
            try:
                outputs[arm]=[temporal_output(sandbox.run(artifact['image'],artifact['component'],sequence,mode='temporal')) for _ in range(2)]
            except CandidateFailure:
                outputs[arm]=None
        baseline=outputs['baseline']
        if baseline is None or baseline[0]!=baseline[1] or baseline[0]!=sequence['expected']:
            statuses.append('invalid')
        elif outputs['candidate'] is None or outputs['candidate'][0]!=outputs['candidate'][1] or outputs['candidate'][0]!=baseline[0]:
            statuses.append('fail')
        else:
            statuses.append('pass')
    return statuses


def run(request):
    no_swap()
    pair = request["pair"]
    if pair.get('scorer')!=scorer_digest() or pair.get('schema')!=schema_digest():
        raise ValueError('trusted_scorer_or_schema_changed')
    if hashlib.sha256(wire.json_bytes(pair)).hexdigest()!=request['context']['pair']:
        raise ValueError('pair_context_changed')
    required=pair.get('required_profiles',['solver_v1'])
    if type(required) is not list or not required or not set(required)<={'solver_v1','reserve-recovery-temporal-v1','calibrated-plant-v1'}:
        raise ValueError('unsupported_campaign_profiles')
    # Artifact verification happens before private decryption.
    for arm in ("baseline", "candidate"):
        if component_digest(Path(pair[arm]["root"])) != pair[arm]["component"]:
            raise ValueError("artifact_changed")
    coverage = source_coverage(changed_component_paths(pair["baseline"]["root"], pair["candidate"]["root"]),
                               Path(pair["baseline"]["root"]), Path(pair["candidate"]["root"]))
    context = request["context"]
    associated = aad(context["root"], context["grant"], context["job"], context["pair"])
    raw = wire.unb64(request["encrypted"], maximum=6*1024*1024)
    private = X25519PrivateKey.from_private_bytes(wire.unb64(request["decryption_key"], size=32))
    shared = private.exchange(X25519PublicKey.from_public_bytes(raw[:32]))
    key = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=associated).derive(shared)
    captured = json.loads(AESGCM(key).decrypt(raw[32:44], raw[44:], associated))
    del raw, key, private, shared
    if type(captured) is not dict:
        raise ValueError('incomplete_capture')
    temporal=captured.get('temporal',{})
    coordinator_path='custom_components/power_sync/optimization/coordinator.py'
    if coordinator_path in coverage and temporal.get('complete') is True and 'reserve-recovery-temporal-v1' in pair.get('required_profiles',[]):
        left_path=Path(pair['baseline']['root'])/'optimization/coordinator.py';right_path=Path(pair['candidate']['root'])/'optimization/coordinator.py'
        if temporal_changed_methods(left_path,right_path)<={'_restore_pre_idle_backup_reserve'} and temporal_surface(left_path)==temporal_surface(right_path):
            coverage[coordinator_path]='reserve-recovery-temporal-v1'
    if type(captured) is not dict or len(captured.get("cycles", [])) != 1:
        raise ValueError("incomplete_capture")
    bundles = captured["cycles"][0]["attempts"]
    if not 1 <= len(bundles) <= 32:
        raise ValueError("attempt_limit")
    for bundle in bundles:
        contract.validate_bundle(bundle)
        if bundle['source'] != pair['source'] or bundle['source']['component_sha256'] != pair['baseline']['component'] or bundle['currency']!=pair.get('currency'):
            raise ValueError('runtime_source_changed')
    sandbox = Sandbox(context["job"], request["deadline"])
    statuses, violations, differences = [], 0, []
    executed=None
    def summary():
        if any(type(value) not in (int,float) or not math.isfinite(value) for value in differences):
            statuses.append('invalid');safe=[]
        else:
            safe=differences
        status=aggregate(statuses)
        return {'status':status,'attempts':len(bundles),'violations':min(10000,violations),
            'coverage':'complete' if status in ('pass','fail') and all(value in ('pass','fail') for value in statuses) else 'insufficient',
            'cost_delta':round(max(safe,default=0),4)}
    try:
        for bundle in bundles:
            executed=None
            contract.validate_bundle(bundle)
            if bundle["source"] != pair["source"]:
                raise ValueError("runtime_source_changed")
            results = {}
            for arm in ("baseline", "candidate"):
                artifact = pair[arm]
                try:
                    results[arm] = [output(bundle, sandbox.run(artifact["image"], artifact["component"], bundle), artifact['component']) for _ in range(2)]
                except CandidateFailure:
                    results[arm] = None
            if results['baseline'] is None:
                statuses.append('invalid');continue
            expected = {"outcome": bundle["outcome"], "solver": bundle["result"]["solver"], "schedule": bundle["result"]["schedule"], "exception": bundle["result"]["exception"]}
            a_a = fidelity(bundle, *results["baseline"], exact_only=True)
            reconstruction = fidelity(bundle, expected, results['baseline'][0], exact_only=True)
            if a_a['outcome'] != 'exact' or reconstruction['outcome'] != 'exact':
                statuses.append('invalid');continue
            if results['candidate'] is None:
                statuses.append('fail');continue
            left, right = results["baseline"][0], results["candidate"][0]
            executed=(bundle,left,right)
            expected = {"outcome": bundle["outcome"], "solver": bundle["result"]["solver"], "schedule": bundle["result"]["schedule"], "exception": bundle["result"]["exception"]}
            a_a = fidelity(bundle, *results["baseline"], exact_only=True)
            b_b = fidelity(bundle, *results["candidate"], exact_only=True)
            reconstruction = fidelity(bundle, expected, left, exact_only=True)
            comparison = classify(bundle, left, right)
            statuses.append('fail' if b_b['outcome'] != 'exact' else run_status(comparison, a_a, reconstruction, coverage, b_b))
            score_a, score_b = comparison.get("baseline_score", {}), comparison.get("candidate_score", {})
            violations += len(score_b.get("violations", []))
            if score_a.get('violations'):
                statuses[-1] = 'invalid'
            elif score_a.get("common_score") is not None and score_b.get("common_score") is None:
                statuses[-1] = 'fail'
            elif score_a.get("common_score") is not None and score_b.get("common_score") is not None:
                # Retry attempts describe the same horizon; report the worst
                # delta instead of summing the same energy several times.
                differences.append(score_b["common_score"]-score_a["common_score"])
        if 'reserve-recovery-temporal-v1' in required:
            statuses.extend(temporal_checks(sandbox,pair,captured.get('temporal')))
        if 'calibrated-plant-v1' in required:
            # Physical acceptance requires an independent measured contract;
            # solver-only inputs never become invented observations.
            from regression_runner.evaluation import evaluate_plant
            from regression_runner.plant import PlantError
            committed=[row['schedule'] for row in captured['cycles'][0].get('stages',[]) if row.get('stage')=='committed']
            if pair.get('terminal_price') is None or executed is None or not committed or executed[1]['schedule']!=committed[-1]:
                statuses.append('insufficient')
            else:
                try:
                    physical=evaluate_plant(captured.get('plant'),*executed,terminal_price=pair['terminal_price'])
                    statuses.append(physical['status']);violations+=physical['violations']
                    differences.append(physical['common_score_delta'])
                except Exception:
                    statuses.append('insufficient')
        return summary()
    except Exception:
        statuses.append('invalid')
        return summary()
    finally:
        if not sandbox.close():
            raise ValueError("purge_unconfirmed")


def main():
    no_swap()
    parent = os.getppid()
    # No private-memory orphan if the network controller dies.
    if ctypes.CDLL(None).prctl(1, signal.SIGKILL, 0, 0, 0) != 0 or os.getppid() != parent:
        raise ValueError("parent_guard_unavailable")
    request = json.loads(sys.stdin.buffer.read(8*1024*1024+1))
    print(json.dumps(run(request), allow_nan=False))


if __name__ == "__main__":
    try:
        main()
    except BaseException:
        raise SystemExit(2)
