<!-- release: v2.12.604 -->

## What's Changed

**EPEX export value can now come from a Home Assistant sensor**
EPEX users can keep PowerSync's EPEX day-ahead import pricing while supplying their actual feed-in value through a Home Assistant sensor. This is useful for local contracts where export payout differs from EPEX, such as OTE/Tedom-style export pricing. The sensor can provide a live value or a `price_values` forecast, and PowerSync will fall back to the existing EPEX export rate behavior if the sensor is missing or invalid.

**Profit Max export reserve now waits for the future export window**
PowerSync now scopes the higher Profit Max export reserve to the future export-bridge window instead of applying that reserve immediately across the whole optimizer horizon. This lets the battery keep operating normally before the export opportunity while still protecting the energy needed for the planned export period.

Update available via HACS
