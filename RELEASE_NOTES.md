<!-- release: v2.12.1109 -->

## What's Changed

**Excluded remote Tesla charging from the home site in named zones**
Home Assistant device trackers report a named zone instead of `not_home` when a vehicle is at a saved location. PowerSync now treats every known location other than the literal home zone as off-site, so charging at another property is no longer counted as local EV power or subtracted from Home Load.

**Kept every Tesla telemetry path on the same site-presence decision**
The off-site gate now follows a physical vehicle through Fleet/Teslemetry observations, paired Tesla BLE telemetry, app-configured Tesla power entities, Wall Connector attribution, Home Assistant sensors, and the canonical mobile display snapshot. This prevents a secondary provider from restoring the remote charging value after the primary vehicle observation excluded it.

Update available via HACS
