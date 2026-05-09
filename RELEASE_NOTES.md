<!-- release: v2.12.349 -->

## What's Changed

**EV dashboard visibility for self-scheduled Tesla charging**
PowerSync now creates the EV power and battery sensors whenever Tesla vehicle telemetry is present, even if PowerSync is not controlling EV charging. This lets the Home Assistant energy-flow dashboard show a Tesla that is charging from the vehicle's own schedule instead of hiding it because EV control was disabled.

**Charge history for observed Tesla sessions**
Tesla charging that starts outside PowerSync, such as a car schedule or Tesla-side charging, is now tracked as an observed charging session. The app can build charge history from live Wall Connector and vehicle telemetry without requiring PowerSync to have started the charge.

Update available via HACS
