## What's Changed

**Solax setup: EV charger no longer interferes with prefix auto-detection**
Users with a Solax EV charger alongside their hybrid inverter were still seeing a failed connection after the v2.12.115 fix, because the EV charger also exposes a `charger_use_mode` entity — so auto-detection found multiple candidates and couldn't determine which was the inverter. The discovery logic now requires both `select.*_charger_use_mode` **and** `sensor.*_battery_capacity` to exist under the same prefix. The EV charger satisfies the first but not the second, so it's correctly excluded and the hybrid inverter prefix auto-fills as expected.

Update available via HACS
