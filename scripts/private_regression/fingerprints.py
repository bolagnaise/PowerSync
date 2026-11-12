"""Immutable trusted scorer and data-contract identities for paired jobs."""
import hashlib
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]


def digest(paths,root=ROOT):
    value=hashlib.sha256()
    for name in sorted(paths):
        raw=(root/name).read_bytes();path=name.encode()
        value.update(len(path).to_bytes(8,'big')+path+len(raw).to_bytes(8,'big')+raw)
    return value.hexdigest()


def scorer_digest():
    paths=['scripts/private_regression/'+name+'.py' for name in ('controller','job','sandbox','images','fingerprints')]
    paths += ['scripts/regression_runner/'+name+'.py' for name in ('scoring','compare','trusted_contract','plant','evaluation','temporal','campaign')]
    paths += ['custom_components/power_sync/regression/contract.py']
    return digest(paths)


def schema_digest():
    return digest(['custom_components/power_sync/regression/'+name+'.py' for name in ('contract','environment','temporal_capture','plant_capture')])
