<!-- release: v2.12.806 -->

## What's Changed

**Generic charger EV sensors**
Generic charger setups now create the PowerSync EV sensor family on startup. This restores `sensor.power_sync_ev_power` and its `is_charging` attribute for switch/entity based EV charger configurations, so automations can react to charger start/stop state without needing a Tesla, OCPP, Zaptec, Sigenergy, or SolarEdge-specific charger path.

Update available via HACS
