Create a new HACS release for PowerSync with detailed, user-facing release notes.

## Steps

1. **Determine the new version.** Read the current version from `custom_components/power_sync/manifest.json`. Bump the patch number (e.g. 2.7.12 → 2.7.13) unless the user specified a version.

2. **Gather changes.** Run `git log` from the previous tag to HEAD for `custom_components/` (excluding "Bump version" commits). Read the actual diffs to understand what changed — don't just copy commit subjects.

3. **Write detailed release notes.** Follow this format and tone (refer to the 2.6.6/2.6.7 releases as examples):

   ```
   ## What's Changed

   **Feature/Fix Title**
   2-3 sentences explaining what changed, why it matters, and any user-visible impact.
   Include technical details that help users understand the improvement.

   **Another Feature/Fix Title**
   Same format. Group related commits into a single section.

   Update available via HACS
   ```

   Guidelines:
   - Write for end users, not developers — explain the benefit, not just the code change
   - Group related commits into logical sections (don't list every commit separately)
   - Use bold section headers for each logical change
   - Include context: what was broken, what's fixed, or what's new
   - Keep it concise but informative — more than one line, less than a paragraph per section
   - Don't include internal refactors unless they have user-visible impact

4. **Show me the release notes** before proceeding. Wait for approval.

5. **After approval:**
   - Bump the version in `manifest.json`
   - Write the release notes to `RELEASE_NOTES.md` in the repo root (overwrite any existing)
   - Commit with message: `Bump version to X.Y.Z`
   - Push to main

The GitHub Actions workflow will automatically create the tag, GitHub release (using the commit messages), and notify Discord. But the Discord notification uses the GitHub release body — so we need the release notes in the GitHub release.

6. **Update the GitHub release** after the workflow creates it. Wait ~10 seconds, then use `gh release edit` to replace the auto-generated notes with our detailed release notes from step 3.

$ARGUMENTS
