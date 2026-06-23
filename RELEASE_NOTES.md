<!-- release: v2.12.692 -->

## What's Changed

**Fix Chip mode with Export Boost**
Chip mode now checks its minimum export-price threshold against the real unboosted export price, even when Export Boost is enabled. This prevents Export Boost from making a below-threshold export interval look profitable enough for Chip mode to allow, while still allowing boosted exports when the actual export price is at or above the configured Chip mode threshold.

Update available via HACS
