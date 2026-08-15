<!-- release: v2.12.1112 -->

## What's Changed

**Kept Home Load numeric after EV snapshot refreshes**
PowerSync now timestamps canonical EV load snapshots after their observations have been collected. Fresh Tesla and Wall Connector readings can no longer appear momentarily future-dated and be rejected as unavailable, preventing Home Load from becoming unknown or displaying as zero after a Home Assistant restart while an idle connector is still reported active.

**Preserved fail-closed protection for genuinely incomplete EV metering**
The existing stale, future-skewed, and unavailable-meter safeguards remain unchanged. A focused restart-time regression now verifies that an active zero-power EV observation produces complete normalization and retains the measured household load.

Update available via HACS
