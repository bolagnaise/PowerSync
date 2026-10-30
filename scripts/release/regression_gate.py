#!/usr/bin/env python3
"""Match Git content to successful, actually executed trusted-main pytest jobs.

No artifacts, local receipt files or Actions caches are accepted as evidence.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

WORKFLOW = ".github/workflows/regression.yml"
JOB = "Full regression suite"
TEST_STEP = "Run complete parallel regression suite"
MANIFEST = "custom_components/power_sync/manifest.json"
METADATA = {"README.md", "CHANGELOG.md", "RELEASE_NOTES.md",
            ".github/discord-release-last-posted.txt"}
VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9_.-]+)?\Z")


def git(*args):
    return subprocess.check_output(["git", *args])


def no_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate manifest field: {key}")
        result[key] = value
    return result


def normalized_manifest(content):
    data = json.loads(content, object_pairs_hook=no_duplicates)
    if not isinstance(data, dict) or not isinstance(data.get("version"), str):
        raise ValueError("Manifest must be an object with a version string")
    if not VERSION.fullmatch(data["version"]):
        raise ValueError("Manifest version is not a release version")
    del data["version"]
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def documentation(path):
    # No extension-wide exemption: executable/config/unknown paths fail closed.
    return path in METADATA or (path.startswith("docs/") and path.endswith(".md"))


def fingerprint(ref="HEAD"):
    digest = hashlib.sha256(b"powersync-regression-v1:ubuntu-24.04:python-3.12.12\0")
    found_manifest = False
    for record in git("ls-tree", "-rz", "--full-tree", ref).split(b"\0"):
        if not record:
            continue
        metadata, raw_path = record.split(b"\t", 1)
        mode, kind, oid = metadata.split()
        path = raw_path.decode("utf-8")
        # Symlinks/submodules never gain the documentation exemption.
        if mode == b"100644" and documentation(path):
            continue
        if path == MANIFEST:
            if mode != b"100644" or kind != b"blob":
                raise ValueError("Manifest must be a regular, non-executable file")
            oid = hashlib.sha256(normalized_manifest(git("show", f"{ref}:{path}"))).hexdigest().encode()
            found_manifest = True
        digest.update(mode + b" " + kind + b" " + oid + b"\t" + raw_path + b"\0")
    if not found_manifest:
        raise ValueError("Missing integration manifest")
    return digest.hexdigest()


def api(endpoint, *, data=None):
    command = ["gh", "api", endpoint]
    if data is not None:
        command += ["--method", "POST", "--input", "-"]
    result = subprocess.run(command, input=None if data is None else json.dumps(data).encode(),
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
    return json.loads(result.stdout) if result.stdout.strip() else None


def ancestor(older, newer):
    return subprocess.run(["git", "merge-base", "--is-ancestor", older, newer],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0


def trusted_run(run, repo_id, workflow_id, target):
    sha = run.get("head_sha", "")
    return (run.get("workflow_id") == workflow_id
            and run.get("path", "").split("@")[0] == WORKFLOW
            and run.get("event") in {"push", "workflow_dispatch"}
            and run.get("head_branch") == "main"
            and run.get("head_repository", {}).get("id") == repo_id
            and re.fullmatch(r"[0-9a-f]{40}", sha) is not None
            and ancestor(sha, target))


def executed_full_suite(jobs):
    return any(job.get("name") == JOB and job.get("conclusion") == "success"
               and any(step.get("name") == TEST_STEP and step.get("status") == "completed"
                       and step.get("conclusion") == "success" for step in job.get("steps", []))
               for job in jobs)


def evidence(repo, target="HEAD"):
    """Return (successful run, matching active run) using GitHub API proof."""
    target = git("rev-parse", target).decode().strip()
    wanted = fingerprint(target)
    repository = api(f"repos/{repo}")
    branch = api(f"repos/{repo}/branches/main")
    if not branch.get("protected"):
        raise RuntimeError("Evidence requires protected main; refusing reuse/publication")
    workflow_id = api(f"repos/{repo}/actions/workflows/regression.yml")["id"]
    runs = api(f"repos/{repo}/actions/workflows/{workflow_id}/runs?branch=main&per_page=100")["workflow_runs"]
    active = None
    for run in runs:
        if not trusted_run(run, repository["id"], workflow_id, target):
            continue
        try:
            if fingerprint(run["head_sha"]) != wanted:
                continue
        except (subprocess.CalledProcessError, ValueError):
            continue
        if run["status"] != "completed":
            if str(run["id"]) != os.environ.get("GITHUB_RUN_ID"):
                active = run
            continue
        if run.get("conclusion") != "success":
            continue
        jobs = api(f"repos/{repo}/actions/runs/{run['id']}/jobs?filter=latest&per_page=100")["jobs"]
        if not executed_full_suite(jobs):
            continue
        current = api(f"repos/{repo}/actions/runs/{run['id']}")
        if (current.get("status") == "completed" and current.get("conclusion") == "success"
                and current.get("run_attempt") == run.get("run_attempt")
                and current.get("head_sha") == run["head_sha"]):
            return run, active
    return None, active


def cancel_obsolete(repo, target="HEAD"):
    """Cancel older main verification only when its relevant content differs.

    A release bump while testing must not cancel the matching in-progress run.
    Never cancel PRs, publication jobs, or a newer concurrently pushed revision.
    """
    target = git("rev-parse", target).decode().strip()
    wanted = fingerprint(target)
    repository = api(f"repos/{repo}")
    workflow_id = api(f"repos/{repo}/actions/workflows/regression.yml")["id"]
    runs = api(f"repos/{repo}/actions/workflows/{workflow_id}/runs?branch=main&per_page=100")["workflow_runs"]
    for run in runs:
        if (str(run["id"]) == os.environ.get("GITHUB_RUN_ID")
                or run.get("status") == "completed"
                or not trusted_run(run, repository["id"], workflow_id, target)):
            continue
        if fingerprint(run["head_sha"]) != wanted:
            api(f"repos/{repo}/actions/runs/{run['id']}/cancel", data={})
            print(f"Cancelled obsolete regression run {run['id']}", flush=True)
    return wanted


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["fingerprint", "plan", "find", "ensure"])
    parser.add_argument("--ref", default="HEAD")
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", "bolagnaise/PowerSync"))
    parser.add_argument("--wait-seconds", type=int, default=3300)
    args = parser.parse_args()
    if args.command == "fingerprint":
        print(fingerprint(args.ref))
        return
    if args.command == "plan":
        value = cancel_obsolete(args.repo, args.ref)
        if output := os.environ.get("GITHUB_OUTPUT"):
            with open(output, "a") as stream:
                stream.write(f"fingerprint={value}\n")
        print(f"Regression fingerprint: {value}")
        return
    deadline = time.monotonic() + args.wait_seconds
    dispatched = False
    while True:
        run, active = evidence(args.repo, args.ref)
        if run:
            print(f"Full regression evidence: {run['html_url']}", flush=True)
            if output := os.environ.get("GITHUB_OUTPUT"):
                with open(output, "a") as stream:
                    stream.write(f"reuse=true\nrun_id={run['id']}\n")
            return
        if args.command == "find":
            if output := os.environ.get("GITHUB_OUTPUT"):
                with open(output, "a") as stream:
                    stream.write("reuse=false\n")
            print("No matching successful full-suite evidence; tests must run.")
            return
        if time.monotonic() >= deadline:
            raise RuntimeError("No matching successful full-suite evidence before timeout; publication blocked")
        # Dispatch only when there is no matching queued/running run. This also
        # covers bot pushes, missing/deleted runs and manual release retries.
        if not active and not dispatched:
            branch = api(f"repos/{args.repo}/branches/main")
            target = git("rev-parse", args.ref).decode().strip()
            if branch["commit"]["sha"] != target:
                raise RuntimeError("Main advanced; prepare/retry the release from current main")
            api(f"repos/{args.repo}/actions/workflows/regression.yml/dispatches", data={"ref": "main"})
            dispatched = True
            print("Requested full regression run on main.", flush=True)
        print("Waiting for successful matching full regression evidence...", flush=True)
        time.sleep(min(20, max(0, deadline - time.monotonic())))


if __name__ == "__main__":
    main()
