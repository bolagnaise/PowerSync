# Regression evidence before publication

`PowerSync Regression` runs the entire suite in two worker processes on main/PR
changes with Python 3.12.12, Ubuntu 24.04 and `requirements-test.txt` (including
pinned pytest-xdist/execnet). Newer revisions cancel
obsolete regression runs only when their content differs. A version/docs push
preserves matching in-progress main work, queues behind it, then reuses success.
Only the trusted main planning job has Actions cancellation permission. PRs
always run tests with read-only permissions; their
results are never release evidence. No paid external service is involved.

Before a tag, release or release notification, `release.yml` requires success
from the regression workflow in this repository's protected main history. It
checks the workflow ID/path, event, branch, head repository, ancestry, fingerprint,
latest run attempt and actual successful full-suite job/step through GitHub's API.
A green workflow that merely reused evidence cannot itself become proof of full
execution. Artifacts, caches and local receipt files are never proof. Missing or
deleted evidence causes fresh execution; expired artifacts have no bearing on
GitHub's retained workflow/job records. API failures block release.

The fingerprint hashes every tracked path, mode and content, including scripts,
tests, dependency pins, configuration and workflows. Unknown paths invalidate
evidence. The only exemptions are regular non-executable `README.md`,
`CHANGELOG.md`, `RELEASE_NOTES.md`, `.github/discord-release-last-posted.txt` and
Markdown below `docs/`. Only the manifest's validated version string is removed;
all its other fields, including requirements/dependencies, remain hashed, and
duplicate JSON fields or invalid versions fail. Changing one executable line
always invalidates evidence. Metadata-only release bumps and automated cleanup
can reuse a prior matching actual full-suite success.

The release gate waits for a matching queued/running run or dispatches a missing
one. Failed, skipped, cancelled, timed-out, wrong-content or foreign results are
never accepted. Full execution must include at least 8,500 tests with no errors,
failures or skips; increase this coverage floor as the suite grows. No source
artifact or test selection is substituted for `pytest tests/`.

## Bounded parallel execution

`scripts/release/run_regression.py` uses xdist `loadfile`, disables scope reordering
and worker restarts, and keeps every file on one worker. Every worker performs
the same full collection. Collection order must agree, and each collected node ID
must have exactly one passing setup, call and teardown on one worker, with each
worker retaining collection-relative execution order. Failed/skipped/xfail tests,
collection errors, missing/restarted workers, duplicate/incomplete reports,
timeouts and cancellation fail the run. An old output directory is never reused.

The current-run `audit.json` and `summary.json` are execution checks, not reusable
release evidence. Reuse still requires trusted GitHub workflow/job/step records.
The gate now requires the `Run complete parallel regression suite` step; older
serial workflow results cannot authorize this runner/configuration revision.

CI uses two workers. The MacBook and Tower may use four, bounded at four rather
than CPU-count auto mode. Each worker collects the entire suite and retains its
own Python/module state, so memory grows with worker count. HiGHS solver settings
are unchanged; choose fewer workers on a busy host rather than oversubscribing
native pools. Run only one heavy benchmark per host at a time.

From a clean current checkout, install `requirements-test.txt` into a dedicated
environment and run:

```sh
python scripts/release/run_regression.py --workers 4
```

Install every pinned dependency, including `tzdata`: Tower's standalone Python
does not supply the complete OS timezone database. The package supplies IANA
zones for `zoneinfo` on such hosts; tariff/timezone tests must retain their real
expected values, never skip or substitute UTC to make a run green.

Use `--workers 2` for lower memory use, or `--workers 1` for a fully audited serial
diagnostic fallback. The runner always selects all of `tests/`; it rejects
`PYTEST_ADDOPTS`, `PYTEST_PLUGINS` and disabled plugin autoload so environment
settings cannot silently reduce coverage. For narrow debugging continue using
ordinary `python -m pytest <files>`; those runs cannot authorize publication.

Release jobs are serialized without cancelling active publication. Immediately
before tagging, the release snapshot must still equal remote main. A concurrent
new code push therefore blocks stale publication; prepare/retry on current main.
Recovery uses `gh workflow run release.yml --ref main`, through the same gate.
The initiating Claude/Codex agent monitors runs, investigates and fixes failures,
and obtains new matching successful evidence before publication. See
[release-procedure.md](release-procedure.md). Do not publish tags/releases directly.

This is a workflow/agent control, not a removal of repository administrators'
ability to alter workflows or publish directly. Main is protected; changing
repository protection/administrator permissions is outside this installation.
The lookup examines the newest 100 regression runs. If older proof falls outside
that window, it runs the suite again rather than weakening validation.

## Operational copies

The MacBook checkout is `/Users/benboller/Developer/power-sync`, with dedicated
test Python at `/Users/benboller/.codex/venvs/power-sync-regression/bin/python`;
prefix Mac commands with `rtk proxy`. Use an isolated current-main worktree when
the checkout contains unrelated work. Tower's clean operational
checkout is `/root/codex-workspaces/power-sync-release-gate`, with pinned test
dependencies in `/root/codex-workspaces/power-sync-test-venv`. Fetch and
fast-forward before preparing a release. Tower's older
`/mnt/user/developer/power-sync` is a symlink to `PowerSync` and may contain
unrelated work; its local agent/release guidance routes release operations to
the clean checkout rather than overwriting that work.

Tracked workflows, gate scripts, dependency pins and tests are shared through
Git. Host release skills retain their local routing and support policies while
pointing to the same tracked procedure. Local `PROCEDURE.md` and preflight-script
copies must match the tracked files; do not maintain competing gate policies.
