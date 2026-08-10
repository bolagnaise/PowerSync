<!-- release: v2.12.1061 -->

## What's Changed

**Tesla Smart Schedule now follows the charger each vehicle is using**

PowerSync now resolves the active EVSE capability from VIN-scoped Tesla vehicle
telemetry instead of treating a stored per-vehicle current as the physical
charger limit. Two vehicles can therefore use different charging sources at the
same time, and their current limits follow the physical connection if the
vehicles swap chargers. An explicitly paired Tesla BLE bridge contributes only
to its paired vehicle; Fleet-only vehicles remain independent.

Smart Schedule uses the active connection's safe current, voltage, and phase
semantics together with the vehicle's configured battery capacity when it
calculates required energy, charging power, and window duration. A 10 A
single-phase connection is planned and commanded as 10 A single-phase rather
than using a higher stored vehicle limit. A lower configured Home Power/site
limit still wins over the connected charger's capability.

The active capability is refreshed while charging. If the EVSE pilot or site
limit drops, PowerSync lowers the VIN-scoped command before the next rate
decision and regenerates Smart Schedule planning when the effective capability
changes. If the vehicle-to-charger association is unplugged, unavailable, or
conflicting across telemetry sources, Tesla planning and commands use a
conservative 5 A ceiling and never override the entity's reported limit.
Direct charger devices that do not identify their connected VIN are not guessed
or assigned by vehicle order.

Existing single-vehicle behavior, explicit Fleet/BLE pairing, Fleet fallback,
BLE-only loadpoints, generic and OCPP chargers, multi-phase equipment, ownership
guards, and command-neutral unload cleanup are preserved. Regression coverage
includes two simultaneous vehicles on different charger limits, vehicles
swapping charging sources, lower site limits, unplugged and ambiguous telemetry,
mid-session cap changes, BLE-only compatibility, 1/3-phase conversion, and an
explicit battery-capacity override on a low-current single-phase connection.

The focused identity, lifecycle, planner, and charging-control gate passes on
Python 3.12. Read-only Home Assistant telemetry was inspected to validate the
VIN-scoped capability path; no physical charging-command canary was performed
for this release.

Update available via HACS
