<!-- release: v2.12.811 -->

## What's Changed

**Generic EV charger telemetry**
Generic charger setups now feed the configured charger power, status, and SOC entities into the PowerSync EV sensor family consistently. `sensor.power_sync_ev_power` remains a kW power sensor, but its value and `is_connected` / `is_charging` attributes now reflect measured generic charger power and connected-idle states instead of staying stale or disconnected after the sensor is created.

Update available via HACS
