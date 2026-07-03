<!-- release: v2.12.757 -->

## What's Changed

**Restored Tesla reserve after Hold SoC ends**
Resume Auto and Hold SoC expiry now restore the live Tesla backup reserve to the user's saved reserve when Hold SoC was the control being released. This completes the Hold SoC cleanup path so the temporary reserve floor does not remain at the held battery percentage after returning to normal operation.

Update available via HACS
