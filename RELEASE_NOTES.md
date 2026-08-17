<!-- release: v2.12.1124 -->

## What's Changed

**Tesla location now applies to the physical VIN across duplicate devices**
Home Assistant can retain more than one Fleet or Teslemetry device-registry row for the same car. PowerSync now reconciles those rows as one physical identity before attaching exact-VIN Wall Connector telemetry, so an unlocated duplicate can no longer make an away Tesla appear connected at home.

**Newer home transitions and charging power remain source-time ordered**
Home and away observations are resolved by their real source timestamps, with ambiguous or untimestamped away state failing closed. A genuinely newer home observation restores Wall Connector attribution, while older duplicate charging power cannot override a newer stop, zero-power sample, or current home reading in either the canonical or fallback EV status path.

**Canonical EV status exposes the resolved site presence**
The normalized loadpoint API and `sensor.power_sync_ev_power` attributes now retain the active vehicle's `site_presence`, making home-versus-away attribution directly observable. A separate home Wall Connector remains its own loadpoint and keeps its physical power instead of being hidden or assigned to the away vehicle.

**Control and hardware behavior are unchanged**
This correction is limited to telemetry identity, location, power normalization, and display status. It does not start or stop charging and does not alter vehicle, charger, battery, inverter, optimizer, or Monitoring Mode command paths.

Update available via HACS
