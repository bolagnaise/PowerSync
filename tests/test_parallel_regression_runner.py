"""Prove process orchestration cannot turn partial execution into green evidence."""
import copy
import importlib.util
from pathlib import Path
import signal
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("parallel_runner_probe", ROOT / "scripts/release/run_regression.py")
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


def audit(workers=2):
    ids = ["tests/a.py::test_one", "tests/a.py::test_two", "tests/b.py::test_three"]
    return {"run_id": "fresh", "exitstatus": 0, "errors": [],
            "collected": ids if workers == 1 else [],
            "collections": {} if workers == 1 else {f"gw{i}": ids[:] for i in range(workers)},
            "reports": [{"nodeid": node, "when": phase, "outcome": "passed", "xfail": False,
                         "worker": "serial" if workers == 1 else ("gw0" if "/a.py" in node else "gw1")}
                        for node in ids for phase in ("setup", "call", "teardown")]}


@pytest.mark.parametrize("workers", [1, 2, 4])
def test_exactly_once_full_completion_is_required(workers):
    result = runner.validate_audit(audit(workers), workers=workers, run_id="fresh", minimum=3)
    assert result["tests"] == result["passed"] == 3
    assert result["complete"] and result["skipped"] == 0


@pytest.mark.parametrize("failure", ["stale", "failed_exit", "collection_error", "missing_worker",
    "restarted_worker", "different_collection", "duplicate_collection", "missing_report",
    "duplicate_report", "failed", "skipped", "xfail", "foreign_worker", "split_node", "split_file", "reordered"])
def test_partial_stale_duplicate_or_failed_results_are_rejected(failure):
    data = copy.deepcopy(audit())
    if failure == "stale": data["run_id"] = "old"
    elif failure == "failed_exit": data["exitstatus"] = 1
    elif failure == "collection_error": data["errors"] = ["collection failed"]
    elif failure == "missing_worker": del data["collections"]["gw1"]
    elif failure == "restarted_worker": data["collections"]["gw2"] = data["collections"]["gw0"]
    elif failure == "different_collection": data["collections"]["gw1"] = list(reversed(data["collections"]["gw0"]))
    elif failure == "duplicate_collection":
        for ids in data["collections"].values(): ids.append(ids[0])
    elif failure == "missing_report": data["reports"].pop()
    elif failure == "duplicate_report": data["reports"].append(data["reports"][0].copy())
    elif failure in ("failed", "skipped"): data["reports"][1]["outcome"] = failure
    elif failure == "xfail": data["reports"][1]["xfail"] = True
    elif failure == "foreign_worker": data["reports"][1]["worker"] = "gw99"
    elif failure == "split_node": data["reports"][1]["worker"] = "gw1"
    elif failure == "split_file":
        for report in data["reports"][3:6]: report["worker"] = "gw1"
    elif failure == "reordered": data["reports"].reverse()
    with pytest.raises(RuntimeError):
        runner.validate_audit(data, workers=2, run_id="fresh", minimum=3)


@pytest.mark.parametrize("source,success", [
    ("def test_one(): assert True\n", True),
    ("def test_one(): assert False\n", False),
    ("def test_one(:\n", False),
    ("import os\ndef test_one(): os._exit(7)\n", False),
    ("import pytest\ndef test_one(): pytest.skip('not full coverage')\n", False),
    ("import pytest\n@pytest.mark.xfail\ndef test_one(): assert False\n", False),
])
@pytest.mark.parametrize("workers", [1, 2])
def test_real_workers_fail_closed_on_failure_collection_error_exit_skip_and_xfail(tmp_path, source, success, workers):
    repo = tmp_path / "repo"
    (repo / "tests").mkdir(parents=True)
    (repo / "tests/test_probe.py").write_text(source)
    output = tmp_path / "results"
    if success:
        summary = runner.run_suite(repo, workers=workers, output=output, timeout=30, minimum=1)
        assert summary["tests"] == 1 and summary["complete"]
    else:
        with pytest.raises(RuntimeError):
            runner.run_suite(repo, workers=workers, output=output, timeout=30, minimum=1)
        assert not (output / "summary.json").exists()


@pytest.mark.parametrize("option", ["PYTEST_ADDOPTS", "PYTEST_PLUGINS", "PYTEST_DISABLE_PLUGIN_AUTOLOAD"])
def test_environment_cannot_silently_select_partial_tests(tmp_path, monkeypatch, option):
    monkeypatch.setenv(option, "unsafe")
    with pytest.raises(RuntimeError, match="Unset"):
        runner.run_suite(tmp_path, workers=2, output=tmp_path / "output", minimum=1)


def test_old_output_directory_is_not_accepted(tmp_path):
    with pytest.raises(FileExistsError):
        runner.run_suite(tmp_path, workers=2, output=tmp_path, minimum=1)


@pytest.mark.parametrize("interruption", [subprocess.TimeoutExpired("pytest", 1), KeyboardInterrupt()])
def test_timeout_and_cancellation_kill_process_group_and_never_issue_summary(tmp_path, monkeypatch, interruption):
    signals = []

    class Process:
        pid = 12345
        def __init__(self): self.waits = 0
        def wait(self, timeout=None):
            self.waits += 1
            if self.waits == 1: raise interruption
            return -signal.SIGTERM
        def poll(self): return None

    monkeypatch.setattr(runner.subprocess, "Popen", lambda *args, **kwargs: Process())
    monkeypatch.setattr(runner.os, "killpg", lambda pid, sig: signals.append((pid, sig)))
    output = tmp_path / "output"
    with pytest.raises(type(interruption)):
        runner.run_suite(tmp_path, workers=2, output=output, minimum=1)
    assert signals == [(12345, signal.SIGTERM)]
    assert not (output / "summary.json").exists()
