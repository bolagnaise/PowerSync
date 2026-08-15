<!-- release: v2.12.1106 -->

## What's Changed

**Kept Home Load available during Tesla Wall Connector charging**
PowerSync now uses the Wall Connector's direct, vehicle-scoped power reading to fill the same vehicle's stale or incomplete EV observation. This keeps `sensor.power_sync_home_load` available and excludes active Tesla charging power, while still failing closed when a separate active charger has no usable measurement.

**Aligned Tesla Wall Connector telemetry with site-wide EV tracking**
Raw Wall Connector records now feed the canonical EV snapshot, preventing a valid direct meter from being discarded by an incomplete aggregate snapshot.

Update available via HACS
