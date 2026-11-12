"""Image entry point. Private stdin/stdout only; never mount the trusted scorer."""
import json
import ctypes
import asyncio
from pathlib import Path
from datetime import datetime,timezone
import resource
import sys

sys.path.insert(0, "/opt/runner")
from regression_runner.runtime import replay, component_digest
from regression_runner import trusted_contract as contract


def main():
    if ctypes.CDLL(None).prctl(4, 0, 0, 0, 0) != 0:
        raise ValueError('private_process_dumpable')
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_CPU, (60, 60))
    resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
    raw = sys.stdin.buffer.read(contract.MAX_BYTES + 1)
    if len(raw) > contract.MAX_BYTES:
        raise ValueError("input_limit")
    request = json.loads(raw)
    root = Path("/opt/custom_components/power_sync")
    if component_digest(root) != request["component_sha256"]:
        raise ValueError("artifact_changed")
    if request.get('mode') == 'health':
        from regression_runner.runtime import load_component
        module=load_component(root,datetime(2026,1,1,tzinfo=timezone.utc))
        from power_sync.regression.environment import binding
        result={'source':binding(root,module)}
    elif request.get('mode') == 'temporal':
        from regression_runner.temporal import replay as temporal
        sequence = request['sequence']
        result = asyncio.run(temporal(root, sequence['initial'], sequence['events']))
    else:
        result = replay(request["bundle"], root, allow_changed_source=True)
    encoded = contract.canonical(result)
    if len(encoded) > contract.MAX_BYTES:
        raise ValueError("output_limit")
    sys.stdout.buffer.write(encoded)


if __name__ == "__main__":
    try:
        main()
    except BaseException:
        sys.exit(2)
