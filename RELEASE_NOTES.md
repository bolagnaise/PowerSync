<!-- release: v2.12.768 -->

## What's Changed

**Use Amber retail forecast prices in the optimizer**
PowerSync now feeds Amber `advancedPrice` retail forecasts into the LP import price forecast instead of the raw wholesale `perKwh` values for future price intervals. This keeps `sensor.power_sync_lp_import_price_forecast` and optimizer decisions aligned with the configured Amber forecast type and the prices shown in Amber/PowerSync tariff views.

**Avoid wholesale fallback when Amber retail forecast data is missing**
If an Amber forecast or current interval is missing retail `advancedPrice` data, PowerSync now carries forward the last valid retail price rather than silently optimizing against wholesale pricing. Non-Amber dynamic providers continue to use their existing `perKwh` behaviour.

Update available via HACS
