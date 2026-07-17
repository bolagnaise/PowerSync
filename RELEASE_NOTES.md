<!-- release: v2.12.880 -->

## What's Changed

**Restore custom external-controller setup after the mobile telemetry update**

Custom external-controller entries now load successfully again after updating. A redundant setup-time constant import introduced in v2.12.878 could shadow the selected grid-power entity setting and abort integration setup before telemetry or optimization started.

This release keeps the v2.12.878 mobile live-flow and energy-history telemetry bridge while removing the startup failure for all custom external-controller electricity providers.

Update available via HACS
