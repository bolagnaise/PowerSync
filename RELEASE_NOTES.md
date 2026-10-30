<!-- release: v2.12.1561 -->

## Faster regression checks for future updates

This maintenance release reduces the time needed to run PowerSync's regression checks before future updates. On the same developer Mac, the full suite fell from **19 minutes 34 seconds to 13 minutes 25 seconds**, a **31.4% reduction**, while retaining every existing test and adding five checks for test isolation and source-change detection.

The improvement is limited to the test tools. Battery control, energy scheduling and the Home Assistant dashboard behave as before, and no settings changes are needed.

Update through HACS and restart Home Assistant.
