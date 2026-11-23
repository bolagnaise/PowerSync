<!-- release: v2.12.1807 -->

## EV stop observations in Home Assistant

When PowerSync is waiting to confirm an EV has stopped charging, it now reports each new charging sample once per stop and loadpoint. Rechecking the same cached sample no longer repeats the event for Home Assistant automations and diagnostics. The existing warning remains available, and newer telemetry that shows charging has stopped can still clear it.

Update PowerSync through HACS and restart Home Assistant. No settings changes are needed.
