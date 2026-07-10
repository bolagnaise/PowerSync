<!-- release: v2.12.812 -->

## What's Changed

**Stop Sungrow reserve-floor restore storms**
PowerSync now recognises the effective Sungrow hardware/BMS reserve when minimum-SOC registers are unavailable, including the protected 5% fallback used by affected SH firmware. Normal grid import at the battery floor is no longer mistaken for a stale discharge cap, inferred recovery attempts are rate-limited, and explicit forced-mode evidence can still trigger an immediate restore.

**Prevent duplicate money-event battery control**
AEMO spike and saving-session managers now stay disabled while Smart Optimization is active, preventing legacy managers from issuing overlapping battery commands alongside the optimizer's own event overlays.

Update available via HACS
