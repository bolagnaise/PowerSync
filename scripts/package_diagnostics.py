#!/usr/bin/env python3
"""Deterministic public bridge/operator packages; never include local credentials."""
import argparse
import hashlib
import json
from pathlib import Path
import tarfile
import zipfile
import io
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def build(output,*,require_clean=False):
    dirty=bool(subprocess.check_output(['git','-C',str(ROOT),'status','--porcelain','--untracked-files=normal']).strip())
    if require_clean and dirty:
        raise ValueError('release_source_not_clean')
    output.mkdir(parents=True, exist_ok=False)
    files = {name:(ROOT/'custom_components/power_sync'/name).read_bytes() for name in (
        'diagnostics_access/__init__.py','diagnostics_access/wire.py','diagnostics_access/bridge.py',
        'diagnostics_access/provision.py','ha_read/__init__.py','ha_read/reader.py')}
    files['__main__.py'] = b'from diagnostics_access.bridge import main\nmain()\n'
    files['requirements.txt'] = b'cryptography==46.0.7\n'
    target = output/'powersync-diagnostics-bridge.zip'
    with zipfile.ZipFile(target, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for name, raw in sorted(files.items()):
            info = zipfile.ZipInfo(name, (2026,1,1,0,0,0));info.external_attr=0o100644<<16
            archive.writestr(info,raw)
    # Operator sources, including scorer, are separate from candidate images.
    paths = [*sorted((ROOT/'scripts/private_regression').glob('*.py')),*sorted((ROOT/'scripts/private_regression/systemd').glob('*')),
             ROOT/'scripts/private_regression/Dockerfile',*sorted((ROOT/'scripts/regression_runner').glob('*.py')),
             ROOT/'scripts/replay_optimizer.py',ROOT/'scripts/benchmark_lp_optimizer.py',ROOT/'requirements-test.txt',ROOT/'.dockerignore',ROOT/'docs/private-fleet-operations.md']
    with tarfile.open(output/'powersync-private-runner.tar', 'w') as archive:
        for path in paths:
            raw = path.read_bytes();info=tarfile.TarInfo(path.relative_to(ROOT).as_posix());info.size=len(raw);info.mode=0o644
            archive.addfile(info,io.BytesIO(raw))
    receipt = {'format':1, 'version':json.loads((ROOT/'custom_components/power_sync/manifest.json').read_bytes())['version'],
        'source_sha':subprocess.check_output(['git','-C',str(ROOT),'rev-parse','HEAD']).decode().strip(),'source_tree_dirty':dirty,
        'artifacts':{path.name:hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted(output.iterdir())},
        'activation':'disabled', 'raw_policy':'replay_then_delete'}
    (output/'diagnostics-artifacts.json').write_text(json.dumps(receipt,sort_keys=True,indent=2)+'\n')
    return receipt


if __name__ == '__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True);parser.add_argument('--require-clean',action='store_true')
    args=parser.parse_args();print(json.dumps(build(args.output,require_clean=args.require_clean)))
