<!-- release: v2.12.1126 -->

## What's Changed

**Wall Connector scenes no longer show an away vehicle**
When an identified Tesla is away and its home Wall Connector reports zero power with a lagging connected state, PowerSync now keeps the charger topology without presenting a vehicle at home. The built-in energy-flow card selects the vehicle-free scene instead of showing a parked car beside `Wall Connector / 0 W / --%`.

**Canonical away presence now fences stale display telemetry**
The energy-flow projection treats `site_presence: away` as authoritative for vehicle presence, charging state, and displayed EV power. This prevents a secondary or stale presence signal from restoring an away vehicle in the scene while preserving genuine onsite Wall Connector load and plugged-in idle vehicles.

**Charging control and hardware behavior are unchanged**
This correction is limited to Wall Connector presence normalization and dashboard rendering. It does not start or stop charging and does not alter vehicle, charger, battery, inverter, optimizer, or Monitoring Mode command paths.

Update available via HACS
