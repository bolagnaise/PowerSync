<!-- release: v2.12.1488 -->

## What's Changed

**Powerwall Max discharge restoration checks**
PowerSync now validates the fresh gateway export limit before saving the settings used to restore Max discharge. Missing or malformed capture data stops the action, and invalid saved limits or cleanup readback keep restoration pending for retry instead of treating it as complete. This builds on v2.12.1487's fix for explicitly unset gateway limits, while retaining temporary export caps, existing reserves and exact restoration after discharge settles.

Update available via HACS
