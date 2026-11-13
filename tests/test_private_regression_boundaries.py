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
from private_regression.sandbox import CandidateFailure,parse_output
from regression_runner.runtime import component_digest


def test_daemon_start_failure_is_infrastructure_and_real_candidate_exit_is_attributable(monkeypatch):
    from private_regression import sandbox
    for state in ({'StartedAt':'0001-01-01T00:00:00Z','Error':''},
                  {'StartedAt':'2026-11-13T00:00:00Z','Error':'synthetic daemon failure'}):
        monkeypatch.setattr(sandbox,'docker',lambda *args:json.dumps([{'State':state}]).encode())
        with pytest.raises(sandbox.SandboxError) as raised:
            sandbox.candidate_started('synthetic')
        assert not isinstance(raised.value,CandidateFailure)
    monkeypatch.setattr(sandbox,'docker',lambda *args:json.dumps([{'State':{'StartedAt':'2026-11-13T00:00:00Z','Error':'','ExitCode':23}}]).encode())
    assert sandbox.candidate_started('synthetic')['ExitCode']==23


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


def test_public_image_freeze_is_readable_by_unprivileged_worker_without_changing_checkout(tmp_path):
    from private_regression.build import freeze
    source=tmp_path/'source';component=source/'custom_components/power_sync'
    component.mkdir(parents=True);component.chmod(0o700)
    original=component/'manifest.json';original.write_text('{}');original.chmod(0o600)
    destination=tmp_path/'frozen'
    digest,_=freeze(source,destination)
    assert digest==component_digest(component)
    assert (destination/'custom_components/power_sync/manifest.json').stat().st_mode&0o777==0o644
    assert (destination/'custom_components/power_sync').stat().st_mode&0o777==0o755
    assert original.stat().st_mode&0o777==0o600
    assert component.stat().st_mode&0o777==0o700


def test_public_freeze_rejects_links_before_copy_and_late_links_before_chmod(tmp_path,monkeypatch):
    from private_regression import build
    source=tmp_path/'source';component=source/'custom_components/power_sync';component.mkdir(parents=True)
    for name,level in (('linked-parent','parent'),('linked-root','root')):
        linked=tmp_path/name;linked.mkdir()
        if level=='parent':(linked/'custom_components').symlink_to(source/'custom_components',target_is_directory=True)
        else:
            (linked/'custom_components').mkdir();(linked/'custom_components/power_sync').symlink_to(component,target_is_directory=True)
        with pytest.raises(ValueError,match='source_symlink'):build.freeze(linked,tmp_path/(name+'-output'))
        assert not (tmp_path/(name+'-output')).exists()
    secret=tmp_path/'private';secret.write_text('synthetic-private-canary');secret.chmod(0o600)
    link=component/'linked';link.symlink_to(secret)
    with pytest.raises(ValueError,match='source_symlink'):build.freeze(source,tmp_path/'early')
    assert not (tmp_path/'early').exists()
    link.unlink();(component/'manifest.json').write_text('{}')
    original=build.shutil.copytree
    def racing(src,dest,*args,**kwargs):
        if Path(src)==component:link.symlink_to(secret)
        return original(src,dest,*args,**kwargs)
    monkeypatch.setattr(build.shutil,'copytree',racing)
    with pytest.raises(ValueError,match='symlink_in_frozen_tree'):build.freeze(source,tmp_path/'late')
    assert (tmp_path/'late/custom_components/power_sync/linked').is_symlink()
    assert secret.stat().st_mode&0o777==0o600


def test_cleanup_pins_daemon_and_group_signal_uses_held_pidfd(monkeypatch):
    from private_regression import sandbox
    monkeypatch.setattr(sandbox,'daemon_identity',lambda:'other-daemon')
    monkeypatch.setattr(sandbox,'docker',lambda *args:pytest.fail('must not trust an empty listing on another daemon'))
    with pytest.raises(sandbox.SandboxError,match='cleanup_daemon_changed'):sandbox.purge('a'*32,'expected-daemon')
    calls=[]
    monkeypatch.setattr(sandbox.signal,'pidfd_send_signal',lambda *args:calls.append(args))
    sandbox.kill_group(73)
    assert calls==[(73,sandbox.signal.SIGKILL,None,4)]
    monkeypatch.setattr(sandbox,'no_swap',lambda:None)
    monkeypatch.setattr(sandbox.os,'getpgrp',lambda:sandbox.os.getpid()+1)
    with pytest.raises(sandbox.SandboxError,match='private_process_group_required'):sandbox.Sandbox('a'*32,__import__('time').time()+100)


def test_reaper_refuses_cross_namespace_owner_liveness(monkeypatch):
    from private_regression import sandbox
    calls=[]
    def docker(*args):
        calls.append(args)
        if args[0]=='ps':return b'public-container\n'
        if args[0]=='inspect':return json.dumps([{'Config':{'Labels':{sandbox.LABEL+'.pid_ns':'different-namespace',sandbox.LABEL+'.deadline':'0'}}}]).encode()
        pytest.fail('cross-namespace reaper must not remove this container')
    monkeypatch.setattr(sandbox,'docker',docker)
    sandbox.reap()
    assert [call[0] for call in calls]==['ps','inspect']


def test_cleanup_process_inventory_only_matches_our_exact_job_markers():
    from private_regression.sandbox import private_argv
    job='a'*64
    assert private_argv(['/python','/opt/job.py','--private-job-id',job],job)
    assert private_argv(['/docker','start','-a','-i','ppr-'+job+'-01234567'],job)
    assert not private_argv(['/python','controller.py','--recover-job',job],job)
    assert not private_argv(['/python','sandbox.py','watch',job],job)
    assert not private_argv(['/docker','start','-a','-i','ppr-'+('b'*64)+'-01234567'],job)
    assert not private_argv(['/python','unrelated.py','--private-job-id',job],job)


def test_candidate_output_nan_unknown_shape_and_arbitrary_fields_fail_closed():
    for value in ({}, {'outcome':'pass'}, {'outcome':float('nan'), 'source':{}, 'schedule':[], 'solver':None}, {'household_text':'never release'}):
        with pytest.raises(CandidateFailure):
            output({'source':{}}, value, 'a'*64)


@pytest.mark.parametrize('raw',[b'{"number":1e999}',b'{"number":NaN}',b'['*4000+b'0'+b']'*4000],ids=['overflow_exponent','nan','excessive_depth'])
def test_nonfinite_exponent_and_excessive_nesting_are_candidate_failures(raw):
    with pytest.raises(CandidateFailure):
        parse_output(raw)


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
