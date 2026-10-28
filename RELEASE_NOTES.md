<!-- release: v2.12.1533 -->

## What's Changed

**Powerwall Tariff force discharge**
When Tesla omits the original Grid Export setting, Tariff force discharge now enables battery export instead of stopping at that check. Battery export stays enabled after restoration in this case; readable original settings are still restored exactly for each gateway. Export confirmation and the existing charging, reserve and control protections still apply.

Update through HACS and restart Home Assistant.

Update available via HACS
