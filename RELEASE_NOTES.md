<!-- release: v2.12.853 -->

## What's Changed

**Network export monitoring now starts cleanly when optional entities are unset**

Fixed a startup and periodic-refresh error that occurred when the optional network export schedule entity was not configured. PowerSync now skips empty schedule and PCC entity lookups while preserving embedded schedule data and configured entity overrides.

Update available via HACS
