<!-- release: v2.12.1194 -->

## What's Changed

**Smart Schedule now refreshes Tesla charger capability after a charger swap**
When an exact Tesla VIN moves from a lower-current UMC to a uniquely identified Wall Connector, PowerSync now detects stale or conflicting pilot limits, attempts a passive refresh and vehicle wake, then uses one bounded minimum-current negotiation only if the idle telemetry still cannot identify the active charger limit. The plan is regenerated as soon as fresh VIN-scoped capability is proven, avoiding an unnecessarily early low-power schedule that can miss the departure target.

The refresh transaction is isolated per Wall Connector serial, so multiple vehicles and multiple Wall Connectors remain independent. Monitoring mode, ambiguous associations, manual or external ownership, already-charging vehicles, and vehicles at their target remain command-neutral. PowerSync stops only the exact VIN whose probe was physically confirmed, and persists that proof for fail-safe restart recovery. UMC charging continues to use the capability reported by the car and does not receive a Wall Connector probe.

Update available via HACS
