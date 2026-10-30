# PowerSync HACS Release Procedure

This tracked file is the authoritative procedure on both the MacBook and Tower.
Claude or Codex initiating the authorized release owns it through completion.
Host skills link here; host-specific CLI, checkout and interpreter paths remain
local. Publication does not imply installation or live behavior.

## Preflight

1. **Baseline.** `git fetch origin --tags`, then reconcile ancestry: local `main` can be far behind `origin/main`. Read the current manifest version (`custom_components/power_sync/manifest.json`) and the newest published tag. The target is a patch bump (e.g. 2.12.1553 → 2.12.1554) unless the user specified a version.
2. **Preserve unrelated dirty work.** If the checkout has unrelated changes or is on another branch, release from an isolated exact-commit worktree of `origin/main` instead of stashing.
3. **Prove the change.** Confirm the implementation and its focused plus adjacent regression tests are present and passing (`python3.12 -m pytest <files> -q -p no:cacheprovider`). Main/PR pushes automatically run `PowerSync Regression` through the audited two-process runner. Local full runs use `python scripts/release/run_regression.py --workers 4` from a dedicated environment with `requirements-test.txt`; use `--workers 1` for serial diagnosis. Never bypass completeness checks, restart crashed workers, select a subset, or use rerun-failed masking for publication. Push implementation work ahead of release preparation so this can finish in the background. Publication requires successful full-suite evidence for the exact relevant content; local green output alone is not publication evidence. Read [regression-release-gate.md](regression-release-gate.md).
4. **Gather changes.** `git log <previous tag>..HEAD -- custom_components/` (skip release commits) and read the actual diffs. Don't copy commit subjects.
5. **Write `RELEASE_NOTES.md`** in the current published format (check the last 2–3 releases with `gh release view vX.Y.Z`):

   ```
   <!-- release: vX.Y.Z -->

   ## User-facing change title

   What was wrong or what's new, and the user-visible impact, in plain language.

   Scope details: which brands or modes it covers, and anything the user must do. Update through HACS and restart Home Assistant.
   ```

   - The first line must be exactly `<!-- release: vX.Y.Z -->`, matching the manifest. Otherwise the workflow fails with `RELEASE_NOTES.md must start with <!-- release: vX.Y.Z -->`.
   - Write for end users: explain the benefit, group related commits under one `##` heading, and leave out internal refactors with no user-visible effect.
6. **Bump** `custom_components/power_sync/manifest.json` to the same version.
7. **Run the read-only preflight** and resolve every FAIL before committing:

   ```bash
   python3.12 scripts/release/release_preflight.py --repo <repo> --target-version X.Y.Z
   ```

   It checks the manifest/marker/target match, that the target is newer than the newest tag and its tag is absent, that the branch is `main` and not behind `origin/main`, and that dirty paths are limited to the notes and manifest. Pass `--allow-path <path>` for each implementation file that is deliberately part of this release.

If preflight turns up real implementation or regression uncertainty, resolve it before publishing (an independent read-only review is fine). Routine release bookkeeping needs no review.

## Publish and prove

1. **Commit only the intended files** and push `main`, using `chore(release): vX.Y.Z` (fix commits usually land separately first). Never force-push. If the push is rejected, `git fetch && git rebase origin/main`, rerun the narrow tests and the preflight, then push again.
2. **The manifest bump and the marked notes must be in the same push.** `Create Release on Version Bump` only triggers on `manifest.json` changes, so a notes-only follow-up will not retrigger it.
3. **Own the gate and any failure.** Monitor `PowerSync Regression` and `Create Release on Version Bump` through completion. The release workflow waits for matching trusted successful full-suite evidence before tagging, publishing or notifying. It can request a missing run automatically. A failure is work for the initiating agent: inspect the logs, reproduce/isolate it, classify product defect versus test pollution/environment failure, fix the verified cause within the authorized release scope, push and obtain fresh matching success. Never skip/remove tests, relax assertions, or call dispatching CI completion. Ask only for a genuine external blocker or decision, describing the evidence and investigation already completed. There is no separate unattended repair service. Also check HACS/hassfest and Pages, and confirm the release body/tag with `gh release view vX.Y.Z`.
4. **Prove the archive.** Download the published source archive (`gh release download vX.Y.Z --archive=zip`, or the tag zipball). Check that it unpacks cleanly, that its `manifest.json` has the target version, and that the expected implementation markers are present. Do not start any installation until publication and this archive proof are done.
5. **Fast-forward the cleanup.** The workflow auto-commits `docs(release): clear RELEASE_NOTES.md after vX.Y.Z`. Run `git pull --ff-only`, then prove `HEAD == origin/main`, the expected manifest version, and a clean checkout. If the cleanup commit never lands, clear `RELEASE_NOTES.md` yourself, push that exact commit message, and verify the resulting run.
6. **Notify reporters only after publication is proven,** and only when that is authorized (see the `powersync-discord-triage` skill). Re-read the ticket before sending, avoid duplicates, and verify the sent message. The automated Discord release announcement does not count as that reply.

If `gh` is unavailable, use `git fetch`, `git ls-remote` and the GitHub REST API with `curl`; never infer publication from the push alone. If a Pages rerun fails on duplicate `github-pages` artifacts, use a fresh workflow dispatch. If deployment stays externally queued, report that as a bounded exception without calling the release unpublished.

## Outcome levels

Report each channel at the highest level you have proven:

- `source-validated`: code and focused validation passed.
- `published`: tag, release, workflows and archive are verified.
- `installed`: the exact artifact and manifest version are verified on a target HA instance.
- `live-verified`: post-restart logs, readback or telemetry show the requested behavior.

A tag, workflow or archive alone is never "installed" or "live". For authorized installation or live checks on the user's HA instance, see AGENTS.md and the live-HA access notes.

## Troubleshooting

- **Release run failed on marker/version mismatch or needs recovery:** investigate and fix the cause, push the correction, then dispatch the existing gated workflow from current `main`. The gate reuses only matching successful trusted-main full execution; absent evidence starts/waits for tests. Do not create tags or releases directly with `git tag`, `gh release create`, or the API. Those commands bypass the gate:

  ```bash
  gh workflow run release.yml --ref main
  ```

  Monitor this dispatched run and resolve failures as above. Then pull the cleanup with `git pull --ff-only` and prove the published archive and final state. If the target tag already exists, inspect the existing publication; do not create or replace tags to work around a failure.
- **Inspecting staged release files:** use `git diff HEAD -- <paths>`, not `git diff -- <paths>`.
