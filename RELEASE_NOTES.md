<!-- release: v2.12.1544 -->

## What's Changed

**Powerwall Automatic discharge**
Automatic control could refuse scheduled or Max discharge when Tesla omitted the original Grid Export preference, even though Tariff control supported it. Automatic now handles that case while verifying export limits and requiring Tesla to accept the export request before starting.

The accepted export selection remains in place when its original value was unavailable. Other settings keep their normal restoration checks, and uncertain writes retain recovery state. Update through HACS and restart Home Assistant.

Update available via HACS
