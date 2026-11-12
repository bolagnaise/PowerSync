#!/usr/bin/env python3
"""Offline local solver capture, same-version replay and A/A comparison.

Use only reviewed component roots with private inputs. Workers have bounded
runtime/output and an I/O tripwire, not an OS security sandbox. No HA instance,
provider, broker, credentials or device command is needed.

Examples (Python 3.12 with requirements-test.txt):
  python scripts/replay_optimizer.py capture --case flat-tariff --currency AUD --output /tmp/case.json
  python scripts/replay_optimizer.py replay /tmp/case.json
  python scripts/replay_optimizer.py compare /tmp/case.json --candidate-root /reviewed/custom_components/power_sync
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import resource
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
from regression_runner import trusted_contract as contract
from regression_runner.compare import classify, source_coverage, changed_component_paths
from regression_runner.runtime import load_component, replay, source_binding

ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "custom_components/power_sync"


class OfflineIOError(RuntimeError):
    """A forbidden worker operation; never includes request contents."""


def _limits():
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_CPU, (120, 120))
    resource.setrlimit(resource.RLIMIT_FSIZE, (contract.MAX_BYTES, contract.MAX_BYTES))
    if sys.platform.startswith("linux"):
        resource.setrlimit(resource.RLIMIT_AS, (2 * 1024**3, 2 * 1024**3))


def runner_digest():
    paths = [Path(__file__).resolve(), ROOT / "custom_components/power_sync/regression/contract.py",
             *sorted((ROOT / "scripts/regression_runner").glob("*.py"))]
    value = hashlib.sha256()
    for path in paths:
        value.update(path.relative_to(ROOT).as_posix().encode() + b"\0" + path.read_bytes() + b"\0")
    return value.hexdigest()


def worker(request):
    """Always use a fresh process; never mix baseline/candidate module caches."""
    encoded = contract.canonical(request)
    if len(encoded) > contract.MAX_BYTES:
        raise contract.ContractError("request_limit")
    with tempfile.TemporaryDirectory(prefix="powersync-replay-") as directory:
        output, errors = Path(directory) / "result", Path(directory) / "errors"
        env = {"PATH": os.defpath, "PYTHONHASHSEED": "0", "OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1"}
        with output.open("wb") as stdout, errors.open("wb") as stderr:
            process = subprocess.run([sys.executable, "-I", str(Path(__file__).resolve()), "--worker"],
                input=encoded, stdout=stdout, stderr=stderr, env=env, cwd=directory,
                timeout=180, preexec_fn=_limits, check=False)
        if output.stat().st_size > contract.MAX_BYTES:
            raise contract.ContractError("worker_output_limit")
        if process.returncode:
            # Raw stderr can contain household data. Never echo it to a report.
            raise contract.ContractError("worker_failed")
        return json.loads(output.read_bytes())


def capture_synthetic(root, name, currency):
    clock = datetime(2026, 11, 7, tzinfo=timezone.utc)
    module = load_component(Path(root), clock)
    spec = importlib.util.spec_from_file_location("trusted_synthetic_benchmark", ROOT / "scripts/benchmark_lp_optimizer.py")
    benchmark = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = benchmark
    spec.loader.exec_module(benchmark)
    cases = benchmark._cases(module)
    if name not in cases:
        raise contract.ContractError("unknown_synthetic_case")
    case = cases[name]
    optimizer = module.BatteryOptimizer(**{
        "capacity_wh": 13500, "max_charge_w": 7000, "max_discharge_w": 7000,
        "backup_reserve": 0.2, "interval_minutes": 5, "horizon_hours": 48,
        **(case.config or {}),
    })
    for key, value in (case.attributes or {}).items():
        setattr(optimizer, key, value)
    # Import from the selected component only during explicit local capture.
    from power_sync.regression.capture import CaptureRecorder, record_cycle
    recorder = CaptureRecorder(source_binding(Path(root), module), currency=currency)
    with record_cycle(optimizer, recorder):
        optimizer.optimize(**case.inputs)
    report = recorder.export()
    if not report["cycles"]:
        raise contract.ContractError("synthetic_capture_failed")
    return report["cycles"][0]["attempts"][0]


def main(argv=None):
    if argv == ["--worker"] or (argv is None and sys.argv[1:] == ["--worker"]):
        # Process-lifetime hook belongs only in this disposable worker. Install
        # before candidate imports, and never in the parent or a pytest process.
        def audit(event, arguments):
            if event in ("socket.connect", "socket.getaddrinfo", "subprocess.Popen", "os.system"):
                raise OfflineIOError("offline_replay_forbidden_io")
        sys.addaudithook(audit)
        request = json.loads(sys.stdin.buffer.read(contract.MAX_BYTES + 1))
        try:
            if request["operation"] == "capture":
                result = capture_synthetic(request["component_root"], request["case"], request["currency"])
            elif request["operation"] == "replay":
                result = replay(request["bundle"], request["component_root"], allow_changed_source=request.get("candidate", False))
            else:
                raise contract.ContractError("unsupported_operation")
            print(json.dumps(result, allow_nan=False))
            return 0
        except Exception as error:
            code = "forbidden_io" if isinstance(error, OfflineIOError) else (str(error) if isinstance(error, contract.ContractError) else "execution_error")
            print(json.dumps({"worker_error": code}))
            return 0
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    capture = commands.add_parser("capture")
    capture.add_argument("--case", default="flat-tariff")
    capture.add_argument("--currency", required=True)
    capture.add_argument("--component-root", type=Path, default=COMPONENT)
    capture.add_argument("--output", type=Path, required=True)
    for name in ("validate", "replay", "compare"):
        sub = commands.add_parser(name)
        sub.add_argument("bundle", type=Path)
        if name != "validate":
            sub.add_argument("--component-root", type=Path, default=COMPONENT)
            sub.add_argument("--terminal-price", type=float, default=0.0)
            sub.add_argument("--output", type=Path)
        if name == "compare":
            sub.add_argument("--candidate-root", type=Path, required=True)
            sub.add_argument("--changed-path", action="append", default=[])
    args = parser.parse_args(argv)
    try:
        if args.command == "capture":
            result = worker({"operation": "capture", "component_root": str(args.component_root.resolve()), "case": args.case, "currency": args.currency})
            if "worker_error" in result:
                raise contract.ContractError(result["worker_error"])
            contract.validate_bundle(result)
        else:
            bundle = contract.loads_bundle(args.bundle.read_bytes())
            if args.command == "validate":
                print(json.dumps({"status": "valid", "layer": bundle["layer"], "digest": bundle["digest"]}))
                return 0
            request = {"operation": "replay", "bundle": bundle, "component_root": str(args.component_root.resolve())}
            baseline = worker(request)
            again = worker(request)
            if "worker_error" in baseline or "worker_error" in again:
                raise contract.ContractError(baseline.get("worker_error", again.get("worker_error")))
            control = classify(bundle, baseline, again, terminal_price=args.terminal_price)
            target = baseline if args.command == "replay" else worker({**request, "component_root": str(args.candidate_root.resolve()), "candidate": True})
            if "worker_error" in target:
                raise contract.ContractError(target["worker_error"])
            reference = {"outcome": bundle["outcome"], "schedule": bundle["result"]["schedule"], "solver": bundle["result"]["solver"]}
            comparison = classify(bundle, reference if args.command == "replay" else baseline, target, terminal_price=args.terminal_price)
            changed = getattr(args, "changed_path", [])
            if args.command == "compare":
                changed = sorted(set(changed) | set(changed_component_paths(args.component_root, args.candidate_root)))
            coverage = source_coverage(changed)
            status = "pass" if comparison["outcome"] in ("exact", "equivalent") else "fail"
            if comparison["outcome"] == "unsupported" or "not-covered" in coverage.values():
                status = "insufficient"
            if control["outcome"] == "unsupported":
                status = "insufficient"
            elif control["outcome"] not in ("exact", "equivalent"):
                status = "invalid"
            result = {"status": status, "a_a": control, "comparison": comparison, "source_coverage": coverage,
                      "bundle_digest": bundle["digest"], "baseline_source": baseline["source"], "candidate_source": target["source"],
                      "runner_sha256": runner_digest(), "profile": "offline_default_solver",
                      "thresholds": {"common_score": 1e-6, "terminal_energy_kwh": 0.001, "soc": 0.0002},
                      "performance": [{k: r.get(k) for k in ("elapsed_seconds", "max_rss_kib")} for r in (baseline, again, target)],
                      "not_covered": ["live_hardware", "counterfactual_daily_savings"]}
        if getattr(args, "output", None):
            # Explicit local export only; no implicit transcript or corpus upload.
            descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(contract.canonical(result) + b"\n")
        else:
            print(json.dumps(result, allow_nan=False))
        return int(result.get("status", "pass") != "pass")
    except (contract.ContractError, ValueError, TypeError, KeyError, IndexError, OSError, subprocess.TimeoutExpired) as error:
        code = str(error) if isinstance(error, contract.ContractError) else type(error).__name__
        print(json.dumps({"status": "invalid", "error": code}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
