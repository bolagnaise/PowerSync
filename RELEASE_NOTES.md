<!-- release: v2.12.1128 -->

## What's Changed

**Away Teslas no longer appear as a second charging Wall Connector**
PowerSync now reconciles duplicate Fleet or Teslemetry registry rows by physical vehicle identity before applying a VIN-less Wall Connector observation. When every known Tesla is away and a DIN- or serial-only connector reports state `2` at `0 W`, stale connected and charging flags are fenced so the canonical EV sensor keeps the single away vehicle instead of publishing a second onsite loadpoint.

**Real connector load and return-home transitions remain intact**
A genuinely distinct Wall Connector with positive measured power remains a separate physical loadpoint, and a newer Home Assistant home-location observation reopens the normal connector attribution path. The regression coverage includes the reporter's exact DIN-only payload, both duplicate registry orders, canonical sensor counts and selected identity, positive connector power, and a newer home transition.

**Control behavior is unchanged**
This release changes Tesla telemetry and display normalization only. It does not start or stop charging or alter vehicle, charger, battery, inverter, optimizer, ownership, or Monitoring Mode command paths.

Update available via HACS
