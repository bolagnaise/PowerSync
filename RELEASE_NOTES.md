<!-- release: v2.12.769 -->

## What's Changed

**Amber LP export pricing uses retail feed-in forecasts**
PowerSync now resolves Amber feed-in forecast intervals from `advancedPrice` before feeding export prices into the LP optimiser and LP export price sensor. This keeps the optimiser aligned with the Amber app and the TOU chart instead of using raw AEMO wholesale `perKwh` values for future export slots.

Update available via HACS
