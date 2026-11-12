"""Freeze a public source artifact, then build immutable private-runner images."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from regression_runner.runtime import component_digest
from private_regression.fingerprints import ROOT,scorer_digest,schema_digest


def freeze(source, destination):
    destination.mkdir(parents=True, exist_ok=False)
    for name in ("requirements-test.txt", ".dockerignore", "scripts/private_regression/Dockerfile", "scripts/private_regression/worker.py",
                 "scripts/regression_runner/__init__.py", "scripts/regression_runner/runtime.py", "scripts/regression_runner/trusted_contract.py", "scripts/regression_runner/temporal.py"):
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT/name, target)
    shutil.copytree(source/"custom_components/power_sync", destination/"custom_components/power_sync",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    component = component_digest(destination/"custom_components/power_sync")
    if component != component_digest(source/"custom_components/power_sync"):
        raise ValueError("source_changed_during_freeze")
    runtime = hashlib.sha256((destination/"scripts/private_regression/worker.py").read_bytes() +
                             (destination/"scripts/regression_runner/runtime.py").read_bytes() +
                             (destination/"scripts/regression_runner/trusted_contract.py").read_bytes() +
                             (destination/"scripts/regression_runner/temporal.py").read_bytes()).hexdigest()
    return component, runtime


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--base", required=True)
    args = parser.parse_args()
    if "@sha256:" not in args.base or len(args.base.rsplit(":",1)[1]) != 64:
        raise ValueError("immutable_base_required")
    component, runtime = freeze(args.source.resolve(), args.output.resolve())
    # Docker's context must be visible to its daemon/CLI host; pass that explicit
    # path rather than mounting a checkout into any private execution container.
    subprocess.run(["docker", "build", "--build-arg", "PYTHON_IMAGE="+args.base,
        "--label", "cc.powersync.component="+component, "--label", "cc.powersync.runtime="+runtime,
        "--iidfile", str(args.output/"image.id"), "-f", str(args.output/"scripts/private_regression/Dockerfile"), str(args.output)], check=True)
    receipt = {"component": component, "runtime": runtime, "scorer":scorer_digest(),"schema":schema_digest(),"base": args.base, "image": (args.output/"image.id").read_text().strip()}
    (args.output/"artifact.json").write_text(json.dumps(receipt, indent=2)+"\n")
