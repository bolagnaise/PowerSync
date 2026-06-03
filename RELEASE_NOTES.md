<!-- release: v2.12.549 -->

## What's Changed

**Optimizer schedule charts now show normal home battery use below the calculated reserve**
PowerSync now separates the calculated optimizer reserve from the battery's real hardware reserve when building the 24-hour schedule data. Forced export still respects the optimizer reserve, but ordinary self-consumption can now continue toward the hardware reserve in the SOC and battery-power charts, matching what the battery is expected to do while supplying the house.

**Flow Power No Idle charts now include home-load discharge**
When Flow Power No Idle converts optimizer hold periods into self-consumption, the schedule now estimates the normal home-load discharge and SOC movement from the latest load and solar forecasts. This prevents the dashboard and mobile schedule from flatlining at the reserve with `Powering Home` shown as zero after export windows.

Update available via HACS
