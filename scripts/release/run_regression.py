#!/usr/bin/env python3
"""Run every test with bounded processes and reject incomplete execution."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import uuid


def validate_audit(data, *, workers, run_id, minimum=8500):
    if data.get("run_id") != run_id or data.get("exitstatus") != 0 or data.get("errors"):
        raise RuntimeError("Missing/stale/failed regression execution audit")
    collections = data.get("collections", {})
    if workers == 1:
        if collections:
            raise RuntimeError("Unexpected parallel collection in serial diagnostic run")
        expected = data.get("collected", [])
        allowed_workers = {"serial"}
    else:
        if set(collections) != {f"gw{i}" for i in range(workers)}:
            raise RuntimeError("Missing, restarted or unexpected worker collection")
        expected = collections["gw0"]
        if any(ids != expected for ids in collections.values()):
            raise RuntimeError("Worker collection/order mismatch")
        allowed_workers = set(collections)
    if len(expected) < minimum or len(expected) != len(set(expected)):
        raise RuntimeError("Empty, partial or duplicate collected node IDs")
    counts = Counter()
    owners = defaultdict(set)
    files = defaultdict(set)
    calls = defaultdict(list)
    positions = {node: index for index, node in enumerate(expected)}
    for report in data.get("reports", []):
        if (report.get("outcome") != "passed" or report.get("xfail")
                or report.get("worker") not in allowed_workers):
            raise RuntimeError("Failed/skipped/xfail or foreign-worker test outcome")
        counts[(report["nodeid"], report["when"])] += 1
        owners[report["nodeid"]].add(report["worker"])
        files[report["nodeid"].split("::", 1)[0]].add(report["worker"])
        if report["when"] == "call":
            calls[report["worker"]].append(positions.get(report["nodeid"], -1))
    required = Counter({(node, phase): 1 for node in expected
                        for phase in ("setup", "call", "teardown")})
    if counts != required or any(len(value) != 1 for value in owners.values()):
        raise RuntimeError("Missing, incomplete, duplicated or split-worker test execution")
    if any(len(value) != 1 for value in files.values()):
        raise RuntimeError("A test file was split between workers")
    if any(order != sorted(order) for order in calls.values()):
        raise RuntimeError("Worker execution changed collection-relative order")
    return {"tests": len(expected), "workers": workers, "mode": "serial" if workers == 1 else "loadfile",
            "node_ids_sha256": hashlib.sha256(json.dumps(expected).encode()).hexdigest(),
            "passed": len(expected), "skipped": 0, "complete": True}


def terminate_group(process):
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    if process.poll() is None:
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()


def run_suite(repo, *, workers, output, timeout=3000, minimum=8500):
    if workers not in (1, 2, 3, 4):
        raise ValueError("Use 1 (serial diagnostic) or 2–4 workers")
    output.mkdir(parents=True, exist_ok=False)
    run_id = uuid.uuid4().hex
    env = os.environ.copy()
    for option in ("PYTEST_ADDOPTS", "PYTEST_PLUGINS", "PYTEST_DISABLE_PLUGIN_AUTOLOAD"):
        if env.get(option):
            raise RuntimeError(f"Unset {option}; full-suite selection/plugins must be explicit")
    source_root = Path(__file__).resolve().parents[2]
    env["PYTHONPATH"] = str(source_root) + os.pathsep + env.get("PYTHONPATH", "")
    env["PS_REGRESSION_AUDIT"] = str(output / "audit.json")
    env["PS_REGRESSION_RUN_ID"] = run_id
    args = [sys.executable, "-m", "pytest", "tests/", "-q", "-p", "no:cacheprovider",
            "-p", "scripts.release.regression_audit", "--durations=30",
            f"--junitxml={output / 'results.xml'}"]
    if workers > 1:
        args += ["-n", str(workers), "--dist=loadfile", "--no-loadscope-reorder", "--max-worker-restart=0"]
    started = time.monotonic()
    process = subprocess.Popen(args, cwd=repo, env=env, start_new_session=True)
    try:
        result = process.wait(timeout=timeout)
    except BaseException:
        terminate_group(process)
        raise
    if result != 0:
        terminate_group(process)
        raise RuntimeError(f"Full pytest execution failed (exit {result}); see {output}")
    data = json.loads((output / "audit.json").read_text())
    summary = validate_audit(data, workers=workers, run_id=run_id, minimum=minimum)
    summary["seconds"] = round(time.monotonic() - started, 2)
    summary["run_id"] = run_id
    (output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workers", type=int, choices=(1, 2, 3, 4), default=2)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--timeout", type=int, default=3000)
    args = parser.parse_args()
    output = args.output_dir or Path(tempfile.gettempdir()) / f"powersync-regression-{uuid.uuid4().hex}"
    repo = Path(__file__).resolve().parents[2]
    print(f"Full regression output: {output}", flush=True)
    summary = run_suite(repo, workers=args.workers, output=output.resolve(), timeout=args.timeout)
    print("Full regression execution verified: " + json.dumps(summary), flush=True)


if __name__ == "__main__":
    main()
