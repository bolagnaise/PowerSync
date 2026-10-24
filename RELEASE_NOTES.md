<!-- release: v2.12.1494 -->

## What's Changed

**Recover from an empty Powerwall gateway reply**
Paired Powerwall charge/discharge now retries an empty telemetry reply once before reporting a failure. Live readings and gateway limits are both checked again; if valid data is still unavailable, control remains blocked. No settings changes are needed.

Update available via HACS
