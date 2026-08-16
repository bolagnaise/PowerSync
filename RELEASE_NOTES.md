<!-- release: v2.12.1117 -->

## What's Changed

**Tesla Home Load stays accurate when charging stops or restarts**
PowerSync now makes a current VIN-scoped Wall Connector reading authoritative for that same physical vehicle, even when the independently refreshed EV snapshot still contains the previous charging value. This prevents `sensor.power_sync_home_load` from being clamped to zero after a charging stop and from temporarily including EV draw when charging restarts.

**Physical charger identity and fail-closed coverage are preserved**
The reconciliation replaces only an exact physical load key. Other measured chargers remain in the site total, a distinct active charger with no usable measurement still keeps Home Load unavailable, and signed V2X observations remain intact. The Tesla cloud coordinator, paired local Powerwall status, and public Home Load sensor now use the same corrected result.

Update available via HACS
