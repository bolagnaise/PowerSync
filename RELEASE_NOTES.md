<!-- release: v2.12.1213 -->

## What's Changed

**Keep Amber's active export price aligned with the plan**
When Amber has finalised the active interval and omits its advanced forecast,
PowerSync now uses that interval's settled retail `perKwh` value rather than
filling the current slot with a later forecast. This keeps the current action
plan and its price statistics aligned with the live Amber and Home Assistant
price readings, while retaining configured advanced forecasts for future slots.

Update available via HACS
