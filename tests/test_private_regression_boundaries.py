"""Trusted scoring/output boundaries, mixed failures and manifest framing."""
import importlib
import json
from pathlib import Path
import sys
from types import ModuleType
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'scripts'))
sys.path.insert(0, str(ROOT/'custom_components/power_sync'))
from private_regression.job import aggregate, output
from private_regression.sandbox import CandidateFailure
from regression_runner.runtime import component_digest


def test_failure_wins_invalid_and_unknown_status_never_passes():
    assert aggregate(['pass','fail','invalid']) == 'fail'
    assert aggregate(['pass',None]) == 'invalid'
    assert aggregate([]) == 'invalid'
    assert aggregate(['pass','pass']) == 'pass'
    assert aggregate(['pass','insufficient']) == 'insufficient'


def test_length_framed_source_digest_separates_embedded_nul_collision(tmp_path):
    left, right = tmp_path/'left', tmp_path/'right'
    left.mkdir();right.mkdir()
    (left/'a').write_bytes(b'X\0b\0Y')
    (right/'a').write_bytes(b'X');(right/'b').write_bytes(b'Y')
    assert component_digest(left) != component_digest(right)


def test_candidate_output_nan_unknown_shape_and_arbitrary_fields_fail_closed():
    for value in ({}, {'outcome':'pass'}, {'outcome':float('nan'), 'source':{}, 'schedule':[], 'solver':None}, {'household_text':'never release'}):
        with pytest.raises(CandidateFailure):
            output({'source':{}}, value, 'a'*64)


def test_reference_redaction_keeps_the_real_replay_identical():
    from replay_optimizer import capture_synthetic
    from regression_runner.runtime import replay
    from regression_runner import trusted_contract as contract
    component = ROOT/'custom_components/power_sync'
    # Loader modules are restored: the full suite must not inherit fake HA.
    saved = {key:value for key,value in sys.modules.items() if key=='power_sync' or key.startswith('power_sync.') or key=='homeassistant' or key.startswith('homeassistant.')}
    try:
        bundle = capture_synthetic(component, 'flat-tariff', 'AUD')
        original = replay(bundle, component)
        redacted = {key:value for key,value in bundle.items() if key!='digest'}
        redacted.update(outcome='exception', result={'schedule':[], 'feasible':None, 'solver':None, 'exception':'other'})
        redacted['digest'] = contract.digest(redacted)
        repeated = replay(redacted, component)
        for key in ('outcome','solver','schedule','exception'):
            assert original.get(key) == repeated.get(key)
        assert output(bundle, original, bundle['source']['component_sha256'])['schedule'] == original['schedule']
        malformed = {**original,'schedule':[{**original['schedule'][0],'battery_charge_w':float('nan')}]+original['schedule'][1:]}
        with pytest.raises(CandidateFailure):
            output(bundle, malformed, bundle['source']['component_sha256'])
    finally:
        for key in tuple(sys.modules):
            if key=='power_sync' or key.startswith('power_sync.') or key=='homeassistant' or key.startswith('homeassistant.'):
                sys.modules.pop(key)
        sys.modules.update(saved)
