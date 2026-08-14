<!-- release: v2.12.1102 -->

## What's Changed

**Show configured vehicle names instead of VINs**

Vehicle status, command, and dashboard widget paths now consistently prefer each saved display name over raw provider identifiers. Multi-vehicle Tesla Fleet/BLE and app-managed charger setups retain their physical identity for routing while presenting the friendly name configured in PowerSync.

**Remove every measured charger from Home Load exactly once**

PowerSync now aggregates Tesla Fleet, BLE and Wall Connector telemetry together with generic or Home Assistant-native chargers, OCPP, Zaptec, Sigenergy EVAC/EVDC, SolarEdge, and app-managed power entities by physical loadpoint. Duplicate providers for the same vehicle are coalesced, distinct simultaneous chargers are summed, and signed EVDC V2X flow is handled correctly. The normalized non-EV Home Load is shared by live sensors, local Powerwall fallback, optimizer forecasts and headroom, charging actions, recorder history, and daily load totals. Stale or incomplete active-charger telemetry fails closed instead of being mislabeled as household demand.

Update available via HACS
