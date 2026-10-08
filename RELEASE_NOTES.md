<!-- release: v2.12.1359 -->

## What's Changed

**Reject stale Fronius load-following telemetry**
PowerSync now preserves the selected Fronius source entities' observation times
through the live coordinator. Load-following targets and physical-convergence
status are deferred when the load, grid, or battery readings are stale or
incomplete, so a successful PowerSync refresh cannot make retained values look
current.

Update available via HACS
