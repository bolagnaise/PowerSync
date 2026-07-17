<!-- release: v2.12.877 -->

## What's Changed

**Let No Idle use the battery when Charge By Time can recover later**
With **No Idle** enabled, PowerSync now checks the real charging headroom available before the Charge By Time deadline instead of assuming the optimiser's first provisional charge amount cannot increase. This prevents unnecessary IDLE periods when natural self-consumption can be recovered by a later charge slot while still reaching the configured target.

Genuine deadline holds remain protected when later charge power is insufficient. The reachability check also continues to respect blocked charging windows, grid-charge permissions and power limits, the grid-charge SOC cap, priority export periods, and battery capacity.

Update available via HACS
