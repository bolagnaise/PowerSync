<!-- release: v2.12.1538 -->
## What's Changed

**Tesla battery export**
PowerSync now recognises Tesla/Teslemetry's empty energy-setting acknowledgement. Scheduled and manual Tariff discharge can continue past this supported reply instead of stopping with an unreadable export-response error.

An acknowledgement confirms submission only. Native Max and the Grid Export control still require matching setting readback; check live battery and grid power to confirm discharge. Update through HACS and restart Home Assistant.

Update available via HACS
