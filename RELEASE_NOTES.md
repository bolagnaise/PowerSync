<!-- release: v2.12.453 -->

## What's Changed

**Restore Smart Optimization safety toggles in setup and options**
Smart Optimization setup and Configure now expose Monitoring Mode and Enable Profit Max directly in the optimization settings. Monitoring Mode explains that commands are blocked while prices, sensors, and plans keep updating, and Profit Max is shown above its target time/SOC controls so Globird and other Smart Optimization users can find the switch next to the settings it affects.

**Improve Sigenergy Modbus command reliability**
Sigenergy control now serializes multi-step Modbus transactions per gateway and retries one reconnect after a transient "not connected" write failure. This prevents overlapping controller instances from disconnecting each other mid-command and makes force charge, force discharge, export-limit, standby, and restore operations more reliable.

**Keep Powerwall cloud commands working without a LAN gateway IP**
Powerwall Local pairing can now keep a signed cloud-capable client available even when no gateway LAN IP is configured. Local TEDAPI polling and local writes are skipped in that cloud-only state, while off-grid and reconnect commands can still use the available signed command path.

**Quiet optional Tesla EV telemetry probes**
Optional Tesla EV power-sensor probes used for observed charging power no longer emit warning logs when no matching EV telemetry entity exists. Required command paths still warn when expected Tesla EV entities are missing.

Update available via HACS
