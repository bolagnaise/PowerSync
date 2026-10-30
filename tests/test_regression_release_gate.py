"""Release evidence must follow exact content and actual trusted full execution."""
import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("regression_gate_probe", ROOT / "scripts/release/regression_gate.py")
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)


@pytest.fixture
def repository(tmp_path, monkeypatch):
    def git(*args):
        return subprocess.check_output(["git", "-C", str(tmp_path), *args], stderr=subprocess.DEVNULL)
    git("init", "-q")
    git("config", "user.name", "Regression test")
    git("config", "user.email", "test@example.invalid")
    monkeypatch.setattr(gate, "git", git)

    def commit(files):
        for name, content in files.items():
            path = tmp_path / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        git("add", ".")
        git("commit", "-qm", "fixture")
        return git("rev-parse", "HEAD").decode().strip()
    commit({gate.MANIFEST: json.dumps({"version": "1.0.0", "requirements": ["lib==1"], "domain": "power_sync"}),
            "runtime.py": "value = 1\n", "requirements-test.txt": "pytest==9.0.3\n"})
    return commit


@pytest.mark.parametrize("name", ["README.md", "CHANGELOG.md", "RELEASE_NOTES.md",
                                   "docs/guide.md", ".github/discord-release-last-posted.txt"])
def test_named_documentation_and_cleanup_reuse_content(repository, name):
    before = gate.fingerprint()
    repository({name: "release documentation\n"})
    assert gate.fingerprint() == before


def test_version_only_normalization_keeps_every_other_field(repository):
    before = gate.fingerprint()
    repository({gate.MANIFEST: '{"domain":"power_sync","version":"1.0.1","requirements":["lib==1"]}'})
    assert gate.fingerprint() == before
    repository({gate.MANIFEST: '{"domain":"power_sync","version":"1.0.2","requirements":["lib==2"]}'})
    assert gate.fingerprint() != before


@pytest.mark.parametrize("name", ["runtime.py", "tests/probe.py", "tests/conftest.py", "pytest.ini",
    "requirements-test.txt", ".github/workflows/regression.yml", ".github/workflows/release.yml",
    "scripts/release/regression_gate.py", "docs/executable.py", "unknown.md", "unknown.txt",
    "custom_components/power_sync/translations/en.json"])
def test_executable_dependency_test_workflow_and_unknown_changes_invalidate(repository, name):
    before = gate.fingerprint()
    repository({name: "one line changed\n"})
    assert gate.fingerprint() != before


@pytest.mark.parametrize("source", ['{}', '[]', '{"version":1}', '{"version":"bad"}',
    '{"version":"1.0.0","version":"1.0.1"}',
    '{"version":"1.0.0","nested":{"key":1,"key":2}}'])
def test_invalid_manifest_cannot_supply_a_fingerprint(source):
    with pytest.raises((ValueError, TypeError)):
        gate.normalized_manifest(source)


def test_executable_documentation_is_not_exempt(repository):
    repository({"README.md": "text\n"})
    before = gate.fingerprint()
    gate.git("update-index", "--chmod=+x", "README.md")
    gate.git("commit", "-qm", "mode")
    assert gate.fingerprint() != before


@pytest.fixture
def proof(repository, monkeypatch):
    sha = gate.git("rev-parse", "HEAD").decode().strip()
    run = dict(id=17, workflow_id=10, path=gate.WORKFLOW, event="push", head_branch="main",
               head_repository={"id": 42}, head_sha=sha, run_attempt=1, status="completed",
               conclusion="success", html_url="https://example.invalid/run/17")
    jobs = [{"name": gate.JOB, "conclusion": "success", "steps": [
        {"name": gate.TEST_STEP, "status": "completed", "conclusion": "success"}]}]
    state = dict(runs=[run], jobs=jobs, protected=True, current_sha=sha, calls=[])
    monkeypatch.setattr(gate, "ancestor", lambda older, newer: older == sha and newer == sha)

    def api(endpoint, *, data=None):
        state["calls"].append((endpoint, data))
        if endpoint.endswith("/branches/main"):
            return {"protected": state["protected"], "commit": {"sha": state["current_sha"]}}
        if endpoint.endswith("/actions/workflows/regression.yml"):
            return {"id": 10}
        if "/runs?" in endpoint:
            return {"workflow_runs": copy.deepcopy(state["runs"])}
        if "/jobs?" in endpoint:
            return {"jobs": state["jobs"]}
        if endpoint.endswith("/runs/17"):
            return state.get("current", state["runs"][0])
        if endpoint.endswith("/dispatches"):
            return None
        return {"id": 42}
    monkeypatch.setattr(gate, "api", api)
    return state


def test_trusted_success_uses_job_records_without_artifacts(proof):
    run, active = gate.evidence("owner/repo")
    assert run["id"] == 17 and active is None
    assert not any("artifact" in endpoint for endpoint, _ in proof["calls"])


@pytest.mark.parametrize("field,value", [("event", "pull_request"), ("head_branch", "feature"),
    ("head_repository", {"id": 99}), ("workflow_id", 99), ("path", "other.yml"),
    ("head_sha", "f" * 40), ("conclusion", "failure"), ("conclusion", "cancelled"),
    ("conclusion", "skipped"), ("conclusion", "timed_out")])
def test_untrusted_wrong_stale_failed_or_cancelled_evidence_is_rejected(proof, field, value):
    proof["runs"][0][field] = value
    assert gate.evidence("owner/repo")[0] is None


@pytest.mark.parametrize("conclusion", ["skipped", "failure", "cancelled", None])
def test_skipped_or_failed_full_step_is_not_passing_evidence(proof, conclusion):
    proof["jobs"][0]["steps"][0]["conclusion"] = conclusion
    assert gate.evidence("owner/repo")[0] is None


def test_reused_workflow_success_is_not_mistaken_for_actual_full_execution(proof):
    proof["jobs"][0]["steps"] = [{"name": "Find trusted matching evidence", "status": "completed", "conclusion": "success"}]
    assert gate.evidence("owner/repo")[0] is None


def test_content_change_invalidates_success_even_when_sha_is_on_main(proof, repository, monkeypatch):
    repository({"runtime.py": "value = 2\n"})
    monkeypatch.setattr(gate, "ancestor", lambda *args: True)
    assert gate.evidence("owner/repo")[0] is None


def test_docs_and_version_only_commit_reuse_actual_main_execution(proof, repository, monkeypatch):
    repository({"docs/guide.md": "Updated instructions\n", gate.MANIFEST:
                '{"version":"1.0.1","requirements":["lib==1"],"domain":"power_sync"}'})
    monkeypatch.setattr(gate, "ancestor", lambda *args: True)
    assert gate.evidence("owner/repo")[0]["id"] == 17


def test_missing_jobs_or_runs_cannot_supply_evidence(proof):
    proof["jobs"] = []
    assert gate.evidence("owner/repo")[0] is None
    proof["runs"] = []
    assert gate.evidence("owner/repo") == (None, None)


def test_rerun_changed_during_lookup_invalidates_evidence(proof):
    proof["current"] = dict(proof["runs"][0], run_attempt=2, status="in_progress", conclusion=None)
    assert gate.evidence("owner/repo")[0] is None


def test_protected_main_is_required(proof):
    proof["protected"] = False
    with pytest.raises(RuntimeError, match="protected main"):
        gate.evidence("owner/repo")


def test_matching_active_run_is_waited_for_not_accepted(proof):
    proof["runs"][0].update(status="in_progress", conclusion=None)
    run, active = gate.evidence("owner/repo")
    assert run is None and active["id"] == 17


def test_timeout_blocks_publication(proof, monkeypatch):
    proof["runs"] = []
    monkeypatch.setattr(sys, "argv", ["gate", "ensure", "--wait-seconds", "0"])
    with pytest.raises(RuntimeError, match="publication blocked"):
        gate.main()
    assert not any(data for _, data in proof["calls"])


def test_main_advancing_blocks_recovery_dispatch(proof, monkeypatch):
    proof["runs"] = []
    proof["current_sha"] = "f" * 40
    monkeypatch.setattr(sys, "argv", ["gate", "ensure"])
    with pytest.raises(RuntimeError, match="Main advanced"):
        gate.main()
    assert not any(data for _, data in proof["calls"])


@pytest.mark.parametrize("active", [None, {"id": 20}])
def test_ensure_waits_for_new_success_and_only_dispatches_if_needed(proof, monkeypatch, active):
    run = proof["runs"][0]
    states = iter([(None, active), (run, None)])
    monkeypatch.setattr(gate, "evidence", lambda *args: next(states))
    monkeypatch.setattr(gate.time, "sleep", lambda *args: None)
    monkeypatch.setattr(sys, "argv", ["gate", "ensure"])
    gate.main()
    dispatched = [data for _, data in proof["calls"] if data]
    assert dispatched == ([{"ref": "main"}] if active is None else [])


def test_metadata_push_does_not_cancel_matching_in_progress_work(proof, repository, monkeypatch):
    proof["runs"][0].update(status="in_progress", conclusion=None)
    repository({"RELEASE_NOTES.md": "version bump\n"})
    monkeypatch.setattr(gate, "ancestor", lambda *args: True)
    gate.cancel_obsolete("owner/repo")
    assert not any(endpoint.endswith("/cancel") for endpoint, _ in proof["calls"])


def test_new_code_cancels_only_obsolete_main_regression(proof, repository, monkeypatch):
    proof["runs"][0].update(status="in_progress", conclusion=None)
    repository({"runtime.py": "value = 2\n"})
    monkeypatch.setattr(gate, "ancestor", lambda *args: True)
    gate.cancel_obsolete("owner/repo")
    assert [data for endpoint, data in proof["calls"] if endpoint.endswith("/cancel")] == [{}]


def test_older_planning_job_never_cancels_newer_or_foreign_run(proof):
    proof["runs"][0].update(status="in_progress", conclusion=None, head_sha="f" * 40)
    gate.cancel_obsolete("owner/repo")
    assert not any(endpoint.endswith("/cancel") for endpoint, _ in proof["calls"])


def test_release_and_recovery_paths_require_gate_before_publication():
    text = (ROOT / ".github/workflows/release.yml").read_text()
    assert "workflow_dispatch:" in text
    assert "needs: regression-gate" in text
    assert "regression_gate.py ensure" in text
    assert text.index("CURRENT_MAIN=") < text.index("git tag ")
    assert "cancel-in-progress: false" in text


@pytest.mark.parametrize("tests,skipped,errors,failures,valid", [
    (8500, 0, 0, 0, True), (0, 0, 0, 0, False), (8499, 0, 0, 0, False),
    (8600, 1, 0, 0, False), (8600, 0, 1, 0, False), (8600, 0, 0, 1, False)])
def test_coverage_receipt_rejects_partial_skipped_and_failed_suites(tmp_path, tests, skipped, errors, failures, valid):
    spec = importlib.util.spec_from_file_location("result_check_probe", ROOT / "scripts/release/check_regression_results.py")
    check = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(check)
    path = tmp_path / "results.xml"
    path.write_text(f'<testsuites><testsuite tests="{tests}" skipped="{skipped}" errors="{errors}" failures="{failures}"/></testsuites>')
    if valid:
        check.check(path)
    else:
        with pytest.raises(RuntimeError):
            check.check(path)
