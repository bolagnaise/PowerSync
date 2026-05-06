<!-- release: v2.12.312 -->

## What's Changed

**Sigenergy Amber tariff upload uses import-only canonical prices**
Sigenergy cloud tariff sync now builds the app-visible upload from the canonical import tariff without constructing the Amber feed-in schedule for that path. This keeps the visible Sigenergy tariff aligned with PowerSync's import/LP price and avoids the lower export price appearing as a second tariff pass in the Sigenergy app.

**Regression coverage for the current slot**
Added coverage for the Sigenergy Amber upload path so the current half-hour slot around the reported evening window mirrors import pricing instead of feed-in pricing.

Update available via HACS
