# Regression evidence before publication

`PowerSync Regression` runs the entire serial suite on main/PR changes with
Python 3.12.12, Ubuntu 24.04 and `requirements-test.txt`. Newer revisions cancel
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
