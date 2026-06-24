<!-- release: v2.12.711 -->

## What's Changed

**Fix Fronius/BYD AC inverter curtailment decisions**

PowerSync now uses cached site coordinator live data before falling back to Tesla live-status calls when evaluating AC-coupled inverter curtailment. This fixes Fronius GEN24 storage / BYD installs where the curtailment check could fail to read live status, treat the battery as able to absorb solar, and restore the Fronius inverter during low-value export periods instead of keeping it curtailed.

Update available via HACS
