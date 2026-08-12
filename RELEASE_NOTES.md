<!-- release: v2.12.1082 -->

## What's Changed

**Keep Smart Schedule previews aligned with automatic execution**
Smart Schedule previews now use the same vehicle-scoped planner as the automatic executor. On systems with more than one PowerSync config entry, the preview no longer bypasses the home battery optimiser allocation or advertises free-grid capacity that the executable plan has already reserved for the home battery.

Update available via HACS
