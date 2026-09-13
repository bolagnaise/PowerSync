<!-- release: v2.12.1282 -->

## What's Changed

**Fronius curtailment now holds safely when site telemetry is unavailable**
During an uneconomic export interval, PowerSync no longer treats a missing Fronius live-status sample as proof that the battery is absorbing solar. If an AC-coupled inverter is already curtailed, its confirmed limit is retained until fresh telemetry positively supports restoring normal output. This avoids an unintended restore during a stale or unavailable coordinator snapshot.

Update available via HACS
