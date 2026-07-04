<!-- release: v2.12.759 -->

## What's Changed

**SolarEdge force-charge restore cleanup**
SolarEdge restore now detects when a saved storage-control snapshot would reapply an active remote charge or discharge command. In that case, PowerSync discards the stale forced-dispatch snapshot and explicitly restores self-consumption with zero charge/discharge limits, so pressing Self-Consume or letting a force mode expire does not leave the SolarEdge system stuck in Charging.

Update available via HACS
