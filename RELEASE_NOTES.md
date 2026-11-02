<!-- release: v2.12.1602 -->

## Tesla charging status after sleep or reload

Sleeping or offline Tesla vehicles no longer appear to be charging from retained readings after a Home Assistant reload. This also prevents those readings from leaving Home Load unavailable when it includes EV charging.

Fresh readings from a matching BLE bridge or Wall Connector can still show current charging. Disabled connectivity sensors no longer hide live charging data, and accepted Wall Connector readings update the loadpoint's telemetry status and observation time.

Update through HACS and restart Home Assistant.
