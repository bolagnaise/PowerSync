<!-- release: v2.12.797 -->

## What's Changed

**Flow Power below-reserve import recovery**
PowerSync now checks the buy price before using paid grid imports to recover a below-reserve battery for a future Flow Power Happy Hour export. If the current import price is above the round-trip-adjusted value of that future export, the optimiser will leave the battery to recover from cheaper windows or solar instead of force-charging during a bad-price period.

Update available via HACS
