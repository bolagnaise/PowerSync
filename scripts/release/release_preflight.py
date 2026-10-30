#!/usr/bin/env python3
"""Read-only preflight for the PowerSync manifest-driven release contract."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any


VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate PowerSync release metadata and Git baseline without changing it.")
    parser.add_argument("--repo", default=".", help="PowerSync repository path")
    parser.add_argument("--target-version", help="Expected X.Y.Z version; defaults to manifest")
    parser.add_argument("--published-version", help="Published X.Y.Z baseline; defaults to the newest semantic Git tag")
    parser.add_argument(
        "--allow-path",
        action="append",
        default=[],
        help="Additional dirty path explicitly intended for this release (repeatable)",
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON")
    return parser.parse_args()


def git(repo: Path, *args: str) -> tuple[int, str]:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    output = completed.stdout.rstrip("\r\n") if completed.returncode == 0 else completed.stderr.strip()
    return completed.returncode, output


def version_tuple(value: str) -> tuple[int, int, int]:
    return tuple(int(part) for part in value.split("."))  # type: ignore[return-value]


def main() -> int:
    args = parse_args()
    repo = Path(args.repo).expanduser().resolve()
    manifest_path = repo / "custom_components" / "power_sync" / "manifest.json"
    notes_path = repo / "RELEASE_NOTES.md"
    checks: list[dict[str, Any]] = []
    warnings: list[str] = []

    def check(name: str, passed: bool, detail: str) -> None:
        checks.append({"name": name, "passed": passed, "detail": detail})

    manifest_version: str | None = None
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest_version = manifest.get("version")
        check("manifest readable", isinstance(manifest, dict), str(manifest_path))
    except (OSError, json.JSONDecodeError) as exc:
        check("manifest readable", False, str(exc))

    target = args.target_version or manifest_version
    target_valid = bool(target and VERSION_RE.fullmatch(target))
    check("target semantic version", target_valid, str(target))
    check("manifest matches target", bool(target and manifest_version == target), f"manifest={manifest_version}, target={target}")

    try:
        notes = notes_path.read_text(encoding="utf-8")
        first_line = notes.splitlines()[0].strip() if notes.splitlines() else ""
        expected_marker = f"<!-- release: v{target} -->" if target else ""
        check("release notes marker", bool(expected_marker and first_line == expected_marker), f"first_line={first_line!r}")
        check("release notes have content", len([line for line in notes.splitlines()[1:] if line.strip()]) > 0, str(notes_path))
    except OSError as exc:
        check("release notes marker", False, str(exc))
        check("release notes have content", False, str(exc))

    rc, top = git(repo, "rev-parse", "--show-toplevel")
    check("git repository", rc == 0 and Path(top).resolve() == repo, top or "not a Git repository")

    rc, branch = git(repo, "branch", "--show-current")
    check("branch is main", rc == 0 and branch == "main", branch or "detached/unknown")

    rc, origin_sha = git(repo, "rev-parse", "origin/main")
    check("origin/main available", rc == 0, origin_sha or "fetch origin/main and tags")

    tags_rc, tags_output = git(repo, "tag", "--list")
    semantic_tags: list[str] = []
    if tags_rc == 0:
        for tag in tags_output.splitlines():
            match = re.fullmatch(r"v?(\d+\.\d+\.\d+)", tag.strip())
            if match:
                semantic_tags.append(match.group(1))
    latest_tag = max(semantic_tags, key=version_tuple) if semantic_tags else None
    published = args.published_version or latest_tag
    published_valid = published is None or bool(VERSION_RE.fullmatch(published))
    check("published baseline semantic", published_valid, str(published or "no semantic tag baseline found"))
    monotonic = bool(target_valid and published_valid and (published is None or version_tuple(target) > version_tuple(published)))
    check("target newer than published", monotonic, f"target={target}, published={published}")

    ahead: int | None = None
    behind: int | None = None
    if rc == 0:
        relation_rc, relation = git(repo, "rev-list", "--left-right", "--count", "origin/main...HEAD")
        if relation_rc == 0:
            parts = relation.split()
            if len(parts) == 2:
                behind, ahead = int(parts[0]), int(parts[1])
        check("not behind origin/main", behind == 0, f"behind={behind}, ahead={ahead}")

    rc, status = git(repo, "status", "--porcelain=v1")
    changed = [line[3:] for line in status.splitlines() if len(line) >= 4] if rc == 0 else []
    check("git status readable", rc == 0, f"{len(changed)} changed paths")
    allowed_paths = {
        "RELEASE_NOTES.md",
        "custom_components/power_sync/manifest.json",
        *args.allow_path,
    }
    unexpected_dirty = [path for path in changed if path not in allowed_paths]
    check(
        "dirty paths explicitly allowed",
        not unexpected_dirty,
        "none" if not unexpected_dirty else ", ".join(unexpected_dirty),
    )
    if changed:
        warnings.append("review every changed path; unrelated dirty work must not enter the release")
    else:
        warnings.append("working tree is clean; confirm release metadata was already committed and target tag is still absent")

    if target:
        tag_rc, _ = git(repo, "show-ref", "--verify", "--quiet", f"refs/tags/v{target}")
        if tag_rc != 0:
            tag_rc, _ = git(repo, "show-ref", "--verify", "--quiet", f"refs/tags/{target}")
        check("target tag absent", tag_rc != 0, f"v{target} or {target}")

    failures = [item for item in checks if not item["passed"]]
    result = {
        "repo": str(repo),
        "target_version": target,
        "published_version": published,
        "manifest_version": manifest_version,
        "branch": branch if "branch" in locals() else None,
        "origin_main": origin_sha if "origin_sha" in locals() else None,
        "behind": behind,
        "ahead": ahead,
        "changed_paths": changed,
        "unexpected_dirty_paths": unexpected_dirty,
        "checks": checks,
        "warnings": warnings,
        "ready_to_commit_or_push": not failures,
    }

    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(f"repo: {repo}")
        print(f"target: {target or 'unknown'}")
        for item in checks:
            state = "PASS" if item["passed"] else "FAIL"
            print(f"{state}: {item['name']} - {item['detail']}")
        for warning in warnings:
            print(f"WARN: {warning}")
        print(f"ready_to_commit_or_push: {not failures}")

    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
