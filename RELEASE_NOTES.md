<!-- release: v2.12.334 -->

## What's Changed

**Fix duplicate EV charging display**
PowerSync now collapses Tesla bridge telemetry, such as Wall Connector or Tesla BLE rows, when a named EV already represents the active charging session. This prevents the mobile app and EV widget API from showing two EVs charging when only one vehicle is plugged in.

**Keep standalone charger visibility**
Standalone charger rows are still shown when there is no named active EV to account for the charging session, so charger-only setups continue to surface their real charging state.

Update available via HACS
