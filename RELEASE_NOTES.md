<!-- release: v2.12.1273 -->

## What's Changed

**Protected vehicle IDs in Auto Schedule diagnostics**
Auto Schedule status logging now redacts vehicle IDs even when they are passed as structured logging data, so enabling debug logs does not expose a full VIN.

**Completed Daily Cost Tracking support for dynamic tariff providers**
Daily Cost Tracking now uses the current import and feed-in prices supplied by Localvolts, Octopus, and EPEX, matching the existing optimization and EV-pricing paths. Invalid or non-finite dynamic prices remain unavailable rather than being used for cost calculations.

Update available via HACS
