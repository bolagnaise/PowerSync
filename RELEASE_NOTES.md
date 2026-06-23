<!-- release: v2.12.703 -->

## What's Changed

**Flow Power current price spike correction**
Flow Power tariff schedules no longer replace the active 30-minute all-in tariff slot with the raw 5-minute KWatch wholesale current interval. This keeps `sensor.power_sync_current_import_price`, Sigenergy tariff display values, and the PowerSync electricity price chart aligned with Flow Power's Actual Price view during volatile intervals.

Update available via HACS
