<!-- release: v2.12.1784 -->

## Localvolts EV Smart Schedule

EV Smart Schedule now uses Localvolts import and export forecasts. It checks the highest rate across the remaining five-minute intervals in each hour and skips grid charging for hours with missing or stale prices.

Localvolts setup and settings now include a **NEM region** selector. If PowerSync cannot detect your region for automatic tariff sync, choose the region where your meter is located.

Update through HACS and restart Home Assistant.
