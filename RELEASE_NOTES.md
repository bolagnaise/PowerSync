<!-- release: v2.12.1636 -->

## Correct Tesla charging status with an idle Wall Connector

Fixes a Tesla appearing as **Charging** with a phantom EV load in the mobile app and dashboard when a single Tesla Wall Connector is idle with nothing plugged in. An idle connector at 0 W no longer refreshes an old vehicle charging reading. Charging status from the Wall Connector now requires measured power.

## Solar charging in dashboard history

The plan history now identifies battery charging from solar when measured solar and grid flows support it. Unavailable readings remain gaps so missing data cannot imply a charging source.

Update through HACS and restart Home Assistant.
