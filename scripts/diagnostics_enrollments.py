#!/usr/bin/env python3
"""Private operator index. Links locate grants; live authority alone permits reads."""
import argparse
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import re
import stat
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'custom_components/power_sync'))
from diagnostics_access.bridge import Bridge, private_file
from diagnostics_access import wire

DEFAULT_DIRECTORY = Path.home() / '.local/share/powersync-diagnostics'
PROOF = re.compile(r'^https://discord\.com/channels/1443943629130567874/[0-9]{15,22}/[0-9]{15,22}$')


@contextmanager
def locked(directory):
    directory = Path(directory)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open(directory, flags)
    lock = None
    try:
        info = os.fstat(fd)
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError('private_directory_required')
        lock = os.open('index.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=fd)
        info = os.fstat(lock)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError('private_lock_required')
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield fd
    finally:
        if lock is not None:
            os.close(lock)
        os.close(fd)


def read_index(fd):
    try:
        source = os.open('enrollments.json', os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd)
    except FileNotFoundError:
        return {'version': 1, 'records': []}
    with os.fdopen(source, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_size > 1024 * 1024:
            raise ValueError('private_index_required')
        value = wire.loads(stream.read(1024 * 1024 + 1), maximum=1024 * 1024)
    if set(value) != {'version', 'records'} or value['version'] != 1 or type(value['records']) is not list:
        raise ValueError('invalid_index')
    return value


def write_index(fd, value):
    raw = wire.json_bytes(value)
    name = '.enrollments-' + os.urandom(16).hex()
    target = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
    try:
        with os.fdopen(target, 'wb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, 'enrollments.json', src_dir_fd=fd, dst_dir_fd=fd)
        os.fsync(fd)
    finally:
        try:
            os.unlink(name, dir_fd=fd)
        except FileNotFoundError:
            pass


def check(config_path, factory=Bridge):
    config = private_file(config_path)
    client = factory(config, config['profile'])
    try:
        descriptor = client.command({'type': 'describe'})
        binding = descriptor['binding']
        if (binding['grant_id'] != config['grant'] or not binding['client'].startswith(config['profile'] + ':')
                or binding['proof_key'] != wire.keypair(wire.unb64(config['private_key'], size=32))[1]
                or descriptor['kind'] not in ('ha_read', 'optimizer_contribution')):
            raise ValueError('grant_identity_mismatch')
        return {'root': config['root'], 'grant': config['grant'], 'profile': config['profile'],
                'purpose': descriptor['kind'], 'actor': binding['actor'], 'revision': binding['revision'],
                'epoch': binding['epoch'], 'checked_at': int(time.time())}
    finally:
        client.close()


def enroll(directory, reporter, reference, config_path, factory=Bridge):
    if not re.fullmatch(r'[0-9]{15,22}', reporter) or not PROOF.fullmatch(reference):
        raise ValueError('verified_reporter_and_message_reference_required')
    # Human operator verifies the private ticket owner/HA linkage before this step.
    # The index is a locator and never a substitute for the authority check.
    config_path = str(Path(config_path).absolute())
    verified = check(config_path, factory)
    with locked(directory) as fd:
        value = read_index(fd)
        records = value['records']
        if any(item['root'] == verified['root'] and item['reporter_id'] != reporter for item in records):
            raise ValueError('installation_owner_conflict')
        row = {**verified, 'reporter_id': reporter, 'ownership_reference': reference, 'config_path': config_path}
        records[:] = [item for item in records if (item['root'], item['grant']) != (row['root'], row['grant'])]
        records.append(row)
        write_index(fd, value)
    return {key: item for key, item in row.items() if key != 'config_path'}


def lookup(directory, reporter=None, factory=Bridge):
    if reporter is not None and not re.fullmatch(r'[0-9]{15,22}', reporter):
        raise ValueError('invalid_reporter_id')
    with locked(directory) as fd:
        rows = [dict(row) for row in read_index(fd)['records'] if reporter is None or row['reporter_id'] == reporter]
    results = []
    for row in rows:
        output = {key: item for key, item in row.items() if key != 'config_path'}
        try:
            verified = check(row['config_path'], factory)
            if any(verified[key] != row[key] for key in ('root', 'grant', 'profile', 'purpose', 'actor')):
                raise ValueError('grant_identity_mismatch')
            output.update(verified)
            output['authority_status'] = 'active'
        except Exception:
            output['authority_status'] = 'unavailable_or_no_longer_authorized'
            output['last_successful_check_at'] = output.pop('checked_at')
        # Active permission does not prove HA is online or a read will complete.
        results.append(output)
    return {'records': results, 'permission_source': 'live_authority', 'ha_online': 'not_checked'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, default=DEFAULT_DIRECTORY)
    commands = parser.add_subparsers(dest='action', required=True)
    add = commands.add_parser('enroll', help='Record an operator-verified ticket owner/installation link after live grant verification')
    add.add_argument('--reporter-id', required=True)
    add.add_argument('--ownership-reference', required=True, help='Private Discord message proving the verified owner/installation association')
    add.add_argument('--config', required=True, help='Existing owner-only bridge configuration; credentials stay in that file')
    get = commands.add_parser('lookup', help='Find existing links and recheck every matching grant with the live authority')
    get.add_argument('--reporter-id')
    args = parser.parse_args()
    try:
        result = enroll(args.directory, args.reporter_id, args.ownership_reference, args.config) if args.action == 'enroll' else lookup(args.directory, args.reporter_id)
    except Exception:
        parser.exit(1, 'Enrollment operation could not be verified. Check private file permissions, owner linkage and authority availability.\n')
    print(json.dumps(result, sort_keys=True, indent=2))


if __name__ == '__main__':
    main()
