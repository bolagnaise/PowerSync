<!-- release: v2.12.1585 -->

## Prevent stale SOC samples from diluting battery provenance

When battery power updates more often than state of charge, the optimizer now defers provenance reconciliation against an unchanged SOC sample until that sample catches up. This prevents valid measured solar/grid energy from being repeatedly moved into unknown during normal cycling, while telemetry gaps and later authoritative SOC changes remain conservative.

Update through HACS and restart Home Assistant.
