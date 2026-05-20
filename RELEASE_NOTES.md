<!-- release: v2.12.445 -->

## What's Changed

**Sungrow SH diagnostics for zero home-load reports**
Adds a read-only Sungrow diagnostics endpoint that dumps the SH inverter's raw Modbus telemetry registers and decoded values for load, export, battery power, meter power, PV, EMS mode, force command, and export-limit state. This gives support a concrete way to compare SH15T register output during force charge versus self-consumption before changing the live home-load calculation again.

Update available via HACS
