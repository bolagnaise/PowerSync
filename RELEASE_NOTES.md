<!-- release: v2.12.635 -->

## What's Changed

**Planned EV load forecast sensor**
Smart Optimization can now include forecast-only EV demand from any Home Assistant sensor with a `planned_load` attribute. This lets Node-RED, MQTT, templates, read-only chargers, and dumb EVSE setups tell PowerSync about expected EV charging load without handing charger control to PowerSync.

**Optimizer load forecast and API debug fields**
The configured planned EV load is added to the optimizer load forecast before the LP solve, and `sensor.power_sync_lp_load_forecast` continues to show the combined load seen by the optimizer. The optimization API and Load Forecast sensor attributes now expose `planned_ev_load_forecast_w`, `planned_ev_load_peak_kw`, and `planned_ev_load_kwh` for debugging.

Update available via HACS
