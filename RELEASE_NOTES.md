<!-- release: v2.12.761 -->

## What's Changed

**Protect below-reserve battery holds before planned recovery charging**
When the optimizer is already below the configured reserve and close to the hardware floor, it now preserves LP-planned hold slots before an upcoming grid-charge recovery window instead of remapping those slots back to native self-consumption. This prevents batteries from continuing to drain through the floor while waiting for a later scheduled charge.

Update available via HACS
