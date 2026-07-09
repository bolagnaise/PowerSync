<!-- release: v2.12.800 -->

## What's Changed

**Clarified dashboard price displays**
The dashboard now avoids showing a duplicate current-price history chart when a tariff schedule is available, so dynamic tariff users see the tariff schedule as the canonical import/export price view. LP forecast chart legends and markers now report the current forecast point instead of the final point in the 48-hour forecast, which keeps the card header aligned with the visible "now" position and tooltip behavior.

**Reduced planned battery window clutter**
The Planned Battery Windows panel now groups short-gap same-action battery windows into a clearer summary. When the optimizer produces several small charge islands, the summary shows active duration versus total span while calculating price stats only across the active charge/export segments. The detailed 24-hour action list still keeps the exact optimizer schedule.

Update available via HACS
