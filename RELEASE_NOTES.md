<!-- release: v2.12.746 -->

## What's Changed

**Fix TOU schedule day labels**

PowerSync now keeps wall-clock TOU schedule tiles on the current day instead of rolling elapsed half-hour periods to Tomorrow. This fixes the mobile/dashboard TOU Schedule display where a past slot, such as 12:30 while viewing around 13:30, could be labeled Tomorrow even though it belonged to today.

Update available via HACS
