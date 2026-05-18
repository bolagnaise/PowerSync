<!-- release: v2.12.421 -->

## What's Changed

**Prevent Tesla export pauses at tariff boundaries**
PowerSync now gives optimiser-owned Tesla force-export tariffs a slightly longer private tariff window when an export slot ends right on a Tesla 30-minute TOU boundary. The optimiser countdown and restore timing still follow the LP schedule, but the uploaded Powerwall tariff can cover the next Tesla bucket so Powerwall systems do not briefly pause and import house load while Tesla applies the refreshed tariff.

Update available via HACS
