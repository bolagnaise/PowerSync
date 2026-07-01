<!-- release: v2.12.744 -->

## What's Changed

**Flow Power pricing TWAP correction**
PowerSync now uses the rolling raw wholesale TWAP for Flow Power import and forecast PEA pricing instead of the Flow Power portal account TWAP. Portal account TWAP remains available on sensors, while portal BPEA and GST still feed the pricing calculation when available. This avoids double-counting average network tariff effects in the v2 Flow Power formula.

Update available via HACS
