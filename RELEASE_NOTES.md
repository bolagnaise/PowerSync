<!-- release: v2.12.1193 -->

## What's Changed

**Smart Schedule previews now follow the active Tesla charger limit**
When a vehicle moves between a higher-current Wall Connector and a lower-current UMC, the Charging Plan now uses the same VIN-scoped live EVSE capability as the executor. This prevents the app from showing a late 32 A plan while PowerSync has already planned the real session at the UMC pilot limit.

The existing safe behavior is preserved when the vehicle is away, asleep, unplugged, or its charger cannot be identified. No test-charge pulse is required, and PowerSync does not start the vehicle merely to refresh the preview.

Update available via HACS
