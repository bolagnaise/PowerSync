<!-- release: v2.12.1188 -->

## What's Changed

**Keep Scheduled Sigenergy EV starts within the site import limit**
When Scheduled Charging starts a Sigenergy EVAC charger during a battery grid-charge period, PowerSync now refreshes site telemetry and applies the available shared import headroom before its first charger command. Previously, the initial command could briefly use the charger maximum before the periodic controller corrected it.

If complete live site telemetry is unavailable, or the remaining budget is below the configured minimum charging current, PowerSync now waits without sending a charger command. Existing Smart Schedule and per-phase protections remain unchanged.

Update available via HACS
