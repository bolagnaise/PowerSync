<!-- release: v2.12.767 -->

## What's Changed

**Export surplus battery energy during intentional export windows**
PowerSync now treats configured battery export windows as priority export periods when planning the optimizer schedule. If the battery has surplus energy above the calculated safety floor, it can export during those windows even when the import price in the same slot is slightly higher than the feed-in price, matching the intended behaviour for export programs and other allowed export periods.

**Keep enough charge until the next refill opportunity**
Before exporting, the optimizer now calculates an export-only bridge floor from the end of each export window through to the next viable charge opportunity, using forecast home load, forecast solar surplus, and cheap grid-charge slots. Runtime export commands use the same floor, so active force-discharge/export behaviour is cancelled instead of extended when projected SOC would fall below the bridge reserve.

**Preserve real tariff savings**
The export priority bonus is internal to schedule selection only. Predicted cost and savings continue to use the actual import, export, and program bonus prices, so this change does not inflate displayed savings with synthetic planning values.

Update available via HACS
