<!-- release: v2.12.1127 -->

## What's Changed

**Away Teslas no longer inherit a stale zero-power charging state**
When an exact-VIN Wall Connector reports its lagging charging state after the identified Tesla has left home, PowerSync now clears both connected and charging presence when measured connector power is zero. This closes the remaining v2.12.1126 path that could still select `Wall Connector / 0 W` as the active dashboard vehicle.

**Canonical EV status stays aligned with physical site presence**
The correction carries the authoritative away observation through the raw vehicle list, canonical loadpoint status, and `sensor.power_sync_ev_power` projection. Duplicate Fleet or Teslemetry device rows cannot restore onsite presence through the stale connector flag, regardless of registry order.

**Real connector load and control behavior are unchanged**
A genuinely active home Wall Connector with positive measured power remains a separate loadpoint and keeps its physical load. This release changes telemetry and display normalization only; it does not start or stop charging or alter vehicle, charger, battery, inverter, optimizer, ownership, or Monitoring Mode command paths.

Update available via HACS
