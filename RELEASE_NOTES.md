<!-- release: v2.12.878 -->

## What's Changed

**Show custom external-controller telemetry in the mobile app**

Custom external-controller setups now mirror the five selected Home Assistant telemetry entities through PowerSync's normalized live-energy sensors. The mobile dashboard can now show Solar, Grid, Home, Battery, and SOC values instead of `--`, while Monitoring Mode remains planner-only and sends no hardware commands.

The Solar Energy history view now uses the same accumulated telemetry instead of reporting that the system is still loading forever. Energy history starts recording after the update and reload; it cannot backfill earlier history.

Update available via HACS
