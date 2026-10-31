# Tesla EV transport recovery and native site coexistence

This continues the 31 October Tesla control review in PR #139. The earlier
control-path and two-car validation reports describe previous checkpoints;
their installation and physical results do not validate this candidate.

## Ownership and charging plans

The existing per-vehicle controller owns the charging target, allocation and
deadline. Transport recovery does not introduce a second scheduler. An
uncertain command retains that plan, the possible vehicle draw and the
original finite Boost deadline. The UI reports recovery pending rather than
claiming that the car is charging. Periodic recovery uses the controller's
latest allocation and bounded retry interval. Manual Stop, expiry, unload,
pairing changes and a replacement owner revoke Start authority.

Persisted transport attempts and finite stop obligations survive reload.
Restoring those records grants observation, isolation and cleanup authority;
it never replays a saved Start. An unresolved earlier attempt cannot be
overwritten by a later Stop, a successful read or a replacement lease.

## Transport selection and evidence

Configured ESPHome BLE remains first. A proven pre-dispatch failure permits
the next configured route. An uncertain dispatched command first gets an
exact vehicle observation. Fresh Charging with measured draw can be adopted
without another Start, while any outstanding producer hazard is retained.

For ESPHome automatic fallback, PowerSync saves the exact alternate HA
vehicle identity before dispatch. It durably quarantines the failed bridge,
turns off that bridge's BLE connection and requires fresh OFF observations
from both the control and physical connection status. A replaced alternate
entity, missing OFF observation or changed pairing prevents handoff. No
firmware reboot, key change or arbitrary queue-expiry timer proves isolation.
The failed bridge stays quarantined; PowerSync does not automatically turn
its radio back on while its old attempt remains unresolved.

Fleet and native Teslemetry recovery use an explicit exact-vehicle coordinator
refresh when needed. This works with automatic Fleet polling disabled and
does not introduce recurring paid idle polling. An unchanged provider source
timestamp is not a fresh observation. Such explicit reads and permitted Fleet
fallback can incur provider charges.

Legacy `tesla_bluetooth`, modern `teslemetry_bluetooth` command-only entities,
native Teslemetry and HA Tesla Fleet are distinct adapters. Registry platform,
config entry, physical vehicle and operation identity must agree. A mapped
BLE display alias resolves to its own VIN; an unmapped explicit alias cannot
fall through to the first vehicle. Command-only numbers are never actual
current readback. Modern native commands are resolved by unique ID, so renamed
entity IDs retain their vehicle binding.

With Both selected, a modern command-only target backed by an exact native
Teslemetry entity uses that native entity's existing BLE-first/cloud-fallback
router. Strict local mode can use the separate command-only adapter. Its
validated provider acknowledgement and fresh physical confirmation are distinct
outcomes; a completed acknowledged producer does not leave a phantom queue
hazard simply because telemetry is delayed. Verified pre-write errors permit
fallback, while ambiguous mutating-command errors retain their attempt.

The provider contract was checked against [the modern adapter source](https://github.com/Teslemetry/hass-teslemetry-bluetooth/blob/a3a672d4116dfa9b29a68c73238e5380399736c2/custom_components/teslemetry_bluetooth/vehicle.py)
and [tesla-fleet-api 1.20.0's Bluetooth contract](https://github.com/Teslemetry/python-tesla-fleet-api/blob/v1.20.0/docs/bluetooth_vehicles.md).

## Native battery charge and discharge

Tesla native Max battery charge and export can coexist with managed EV
charging and EV rate-only control when the current site proves that capability.
The proof binds the active session, direction, token, adapter, runtime, paired
site, fresh local telemetry/configuration and hardware limits within configured
software limits. Every EV start/increase revalidates this proof after awaits.
Persisted capability flags alone cannot authorize a command.

The site's native controller prioritizes household and EV loads. Admission may
spend measured battery charging that the verified native site can reduce;
it cannot spend forecast battery charging or count battery discharge as extra
import capacity. Reductions remain possible when a proof expires. SOC,
reserve, export permission, settings recovery and foreign ownership fences
continue to apply. Other inverter brands need their own verified equivalent
capability before this Tesla-specific coexistence contract can apply.

## Verification boundaries

Focused regressions cover exact two-car routing, stale provider data, command
outcomes, durable persistence and cancellation, isolation, fallback identity,
pending plans, original deadlines and both native battery directions. Release
also requires the audited complete suite and trusted protected-main evidence
under the tracked regression gate.

The local HA verification must separately record the installed artifact hash,
Core check/startup, command acceptance, fresh vehicle/settings readback and
physical power. Both local cars and ESPHome/Fleet paths are available. Neither
legacy nor modern Teslemetry Bluetooth is installed on this instance; their
adapter contracts can be tested in software but cannot be called physically
verified here. Live receipts will be added after installing the frozen candidate.
