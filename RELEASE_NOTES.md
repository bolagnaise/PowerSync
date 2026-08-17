<!-- release: v2.12.1131 -->

## What's Changed

**Cheapest EV charging now preserves Home Assistant power units**
PowerSync now respects whether recorded household load sensors report watts or kilowatts when learning the hourly load profile. Kilowatt sensors are no longer divided by 1,000 a second time, preventing fabricated solar surplus from moving a cheapest-cost vehicle schedule into an earlier paid period.

**Free charging windows remain authoritative**
Zero-cost planned windows now participate in the live price comparison. PowerSync will wait for the planned free period instead of treating an all-free plan as a default 30c schedule and opportunistically starting on a paid rate.

**Waiting status reports the actual next-window price**
Smart Schedule now displays the price of the specific upcoming window. A free 10:00 window is reported as 0c rather than the previous misleading 30c fallback.

Update available via HACS
