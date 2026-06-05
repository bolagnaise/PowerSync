<!-- release: v2.12.595 -->

## What's Changed

**GoodWe curtailment disable restores export headroom**
PowerSync now restores GoodWe EMS power limits to the inverter's normal maximum when returning to auto/general mode, instead of leaving the EMS power limit at 0W after disabling curtailment or reloading options. The disable path also attempts a GoodWe restore when the cached curtailment state was lost, so SolarGo is not left showing a stale zero export limit.

**Home Assistant weather entity can be cleared**
The optional Home Assistant weather entity selector now treats the saved entity as a suggestion rather than a forced default, allowing users to clear it and fall back to OpenWeatherMap or no Home Assistant weather source.

Update available via HACS
