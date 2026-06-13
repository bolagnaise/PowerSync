<!-- release: v2.12.640 -->

## What's Changed

**Fix Sigenergy EVDC charger endpoint resolution**
PowerSync now resolves Sigenergy EV charger reads through the dedicated charger configuration and app-managed vehicle charger settings before falling back to the inverter Modbus host. This prevents multi-stack EVDC installs from repeatedly polling the gateway or plant inverter instead of the EVDC stack, so EVDC status, plug detection, and optimizer energy reads use the configured charger endpoint.

**Allow Sigenergy charger slave ID 247**
The Sigenergy EV charger slave ID field now accepts 247, matching valid Modbus IDs and the main Sigenergy Modbus configuration range. App-created vehicle charger configs also preserve Sigenergy host, port, slave ID, charger type, and EVDC power-limit entities on first save.

Update available via HACS
