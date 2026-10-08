<!-- release: v2.12.1361 -->

## What's Changed

**Refresh Sigenergy export ceilings when the solved PCC target changes**
PowerSync now tracks the applied Sigenergy whole-site grid-export ceiling
separately from the battery-to-grid contribution shown in the Action Plan. An
active optimizer export is refreshed when the solved PCC ceiling changes,
including legacy active states without the new marker, while network-envelope
clamps and failed-write retry state remain fail-closed.

Update available via HACS
