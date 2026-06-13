<!-- release: v2.12.637 -->

## What's Changed

**Scheduled EV charging holds the charger minimum during temporary grid spikes**
Scheduled Charging now keeps an active EV session at the configured minimum charging current instead of stopping the car when a short-lived grid-import spike would otherwise calculate a target below the EVSE minimum. This prevents a scheduled window from ending early and failing to restart after the site load settles, while still leaving the No Grid Import switch as the explicit control for users who want PowerSync to stop charging on import pressure.

Update available via HACS
