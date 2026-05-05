<!-- release: v2.12.293 -->

## What's Changed

**Pre-charge wake action for sleeping EVs**
PowerSync can now run an optional wake entity before starting a non-Tesla charger. This is aimed at vehicles such as BYD models that can sleep while plugged in and fail the charger handshake unless an air-con, switch, button, script, or similar Home Assistant entity wakes the car first.

**Safer starts for OCPP, generic, Zaptec, and HA-native chargers**
Manual starts, smart schedules, and solar surplus sessions now carry the configured wake entity through the charger start path. PowerSync checks the loadpoint is actually connected where connector status is available, runs the wake sequence, then starts the charger so sleeping vehicles get a chance to respond before the charging handshake begins.

Update available via HACS
