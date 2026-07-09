<!-- release: v2.12.807 -->

## What's Changed

**Sigenergy custom TOU day ranges**
Sigenergy tariff sync now preserves weekday/weekend scopes from custom TOU schedules. Peak periods that are configured for weekdays only are uploaded to the Sigenergy app as weekday-only `weekPrices`, instead of being flattened across all seven days.

**Spread export hardware refresh**
Spread export now keeps the active lower export target when refreshing an optimizer-owned force-discharge window. This prevents a later solve inside the same export window from raising the hardware command to a higher instantaneous target and defeating the spread-export plan.

Update available via HACS
