<!-- release: v2.12.824 -->

## What's Changed

**Ignore duplicate Tesla BLE entries when Fleet/Teslemetry already has the car**
PowerSync now suppresses SOC-less Tesla BLE fallback entries whenever a real Tesla vehicle is already discovered. This prevents mixed Fleet/Teslemetry + BLE setups from showing a second pseudo-vehicle with unknown SOC, and avoids that duplicate taking price-level charging decisions while the real Tesla entry has valid SOC and plug state.

Update available via HACS
