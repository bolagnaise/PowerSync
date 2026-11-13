"""Participant lookup uses verified links and live permission, never cached access."""
import importlib.util
import json
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('_diagnostics_enrollments_test', ROOT / 'scripts/diagnostics_enrollments.py')
enrollments = importlib.util.module_from_spec(spec)
spec.loader.exec_module(enrollments)
REPORTER = '123456789012345678'
REFERENCE = 'https://discord.com/channels/1443943629130567874/123456789012345678/234567890123456789'


@pytest.fixture
def setup(tmp_path):
    private, public = enrollments.wire.keypair()
    config = {'profile': 'codex', 'root': 'a' * 64, 'grant': 'b' * 32, 'private_key': private, 'authority_key': public}
    config_path = tmp_path / 'client.json'
    config_path.write_text(json.dumps(config))
    config_path.chmod(0o600)

    class Client:
        denied = False
        grant = config['grant']
        purpose = 'ha_read'
        def __init__(self, value, profile):
            self.config = value
            assert profile == 'codex'
        def command(self, command):
            assert command == {'type': 'describe'}
            if self.denied:
                raise ValueError('revoked')
            return {'kind': self.purpose, 'binding': {'grant_id': self.grant, 'client': 'codex:key',
                'proof_key': public, 'actor': 'operator', 'revision': 'c' * 32, 'epoch': 1}}
        def close(self):
            pass
    return tmp_path / 'private-index', config_path, config, Client


def test_enroll_and_lookup_recheck_permission_without_retaining_credentials(setup):
    directory, config_path, config, client = setup
    row = enrollments.enroll(directory, REPORTER, REFERENCE, config_path, client)
    assert row['purpose'] == 'ha_read' and row['reporter_id'] == REPORTER
    raw = (directory / 'enrollments.json').read_text()
    assert config['private_key'] not in raw and config['authority_key'] not in raw
    assert os.stat(directory).st_mode & 0o777 == 0o700
    assert os.stat(directory / 'enrollments.json').st_mode & 0o777 == 0o600
    assert enrollments.lookup(directory, REPORTER, client)['records'][0]['authority_status'] == 'active'
    client.denied = True
    result = enrollments.lookup(directory, REPORTER, client)['records'][0]
    assert result['authority_status'] == 'unavailable_or_no_longer_authorized' and 'checked_at' not in result
    assert enrollments.lookup(directory, '987654321098765432', client)['records'] == []


def test_different_reporter_cannot_claim_existing_installation(setup):
    directory, config_path, _, client = setup
    enrollments.enroll(directory, REPORTER, REFERENCE, config_path, client)
    with pytest.raises(ValueError, match='owner_conflict'):
        enrollments.enroll(directory, '987654321098765432', REFERENCE, config_path, client)


def test_denied_or_mismatched_grant_is_not_enrolled(setup):
    directory, config_path, _, client = setup
    client.denied = True
    with pytest.raises(ValueError):
        enrollments.enroll(directory, REPORTER, REFERENCE, config_path, client)
    assert not directory.exists()
    client.denied = False
    client.grant = 'c' * 32
    with pytest.raises(ValueError, match='identity_mismatch'):
        enrollments.enroll(directory, REPORTER, REFERENCE, config_path, client)


def test_changed_configuration_or_purpose_does_not_authorize_a_lookup(setup):
    directory, config_path, _, client = setup
    enrollments.enroll(directory, REPORTER, REFERENCE, config_path, client)
    client.purpose = 'optimizer_contribution'
    assert enrollments.lookup(directory, REPORTER, client)['records'][0]['authority_status'] != 'active'


def test_invalid_proof_or_insecure_paths_cannot_enroll(setup, tmp_path):
    directory, config_path, _, client = setup
    with pytest.raises(ValueError):
        enrollments.enroll(directory, 'display-name', REFERENCE, config_path, client)
    with pytest.raises(ValueError):
        enrollments.enroll(directory, REPORTER, 'https://example.com/', config_path, client)
    directory.mkdir(mode=0o755)
    with pytest.raises(ValueError, match='private_directory'):
        enrollments.enroll(directory, REPORTER, REFERENCE, config_path, client)
    directory.chmod(0o700)
    target = tmp_path / 'unrelated.json'
    target.write_text('leave unchanged')
    (directory / 'enrollments.json').symlink_to(target)
    with pytest.raises(OSError):
        enrollments.enroll(directory, REPORTER, REFERENCE, config_path, client)
    assert target.read_text() == 'leave unchanged'
