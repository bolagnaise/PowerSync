"""Create/bind an owner-only client key file; never print its private key."""
import argparse
import json
import os
from pathlib import Path
from . import wire
from .bridge import private_file


def write_new(path, value):
    path = Path(path)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        raw = wire.json_bytes(value)
        with os.fdopen(descriptor, 'wb') as output:
            output.write(raw);output.flush();os.fsync(output.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def main():
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest='action', required=True)
    create = commands.add_parser('create')
    create.add_argument('--profile', choices=('codex','claude'), required=True)
    create.add_argument('--output', required=True)
    bind = commands.add_parser('bind')
    bind.add_argument('--key-file', required=True)
    bind.add_argument('--output', required=True)
    for field in ('root','grant','authority-key'):
        bind.add_argument('--'+field, required=True)
    args = parser.parse_args()
    if args.action == 'create':
        private, public = wire.keypair()
        write_new(args.output, {'profile':args.profile, 'private_key':private, 'public_key':public})
        print(json.dumps({'profile':args.profile,'public_key':public}))
    else:
        config = private_file(args.key_file)
        if not wire.ID.fullmatch(args.root) or not wire.ID.fullmatch(args.grant):
            raise wire.WireError('invalid_identity')
        wire.unb64(args.authority_key, size=32)
        write_new(args.output, {**config,'root':args.root,'grant':args.grant,'authority_key':args.authority_key})
        print(json.dumps({'status':'bound','profile':config['profile']}))


if __name__ == '__main__':
    main()
