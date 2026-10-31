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
from the control plus an explicit producer-fence receipt. The common legacy
`ble_status` sensor reports advertisement presence; neither ON nor OFF proves
GATT connection state or that queued commands have stopped. A replaced alternate
entity, missing OFF observation or changed pairing prevents handoff. No
firmware reboot, key change or arbitrary queue-expiry timer proves isolation.
The failed bridge stays quarantined; PowerSync does not automatically turn
its radio back on while its old attempt remains unresolved.

Before a new BLE mutation, the exact bridge must provide fresh known charging
state and measured current or power. Zero is valid health evidence. Wake status
alone is insufficient. A bounded wake/data-refresh failure is a known
pre-dispatch failure, so Both mode can use the exact Fleet fallback without
creating a queued BLE mutation. Fleet data cannot certify BLE session health.

An uncertain dispatched command is different. Automatic handoff requires an
ESPHome `sensor.<prefix>_transport_fence` on the same registry device/config
entry as the command and BLE connection control. Protocol 1 reports `boot_id`,
an echoed `request_id`, an integer `generation`, booleans `radio_enabled`, `connected` and
`producer_stopped`, and integer `queued_commands`, `queued_reads` and
`queued_writes`. Every disable request must cancel all older producer work,
including callbacks/retries, disconnect GATT, drain every queue and publish a
new generation. PowerSync writes a unique persisted request ID to the matching
`text.<prefix>_transport_isolation_request` entity; the producer must process
that request and echo it in the completed fence receipt. This distinguishes
the current barrier from a delayed earlier report. The receipt must be fresh, from the same boot,
with a higher generation, radio/connection off, producer stopped and all three
queues empty. Rebinding, reboot, generation changes or a missing/malformed
receipt revoke the proof. Old presence-only isolation records are not reused.
The registry-bound control must still report OFF, but its unchanged HA value
need not receive a new timestamp: the echoed fence is the fresh producer proof.

This is an explicit bridge contract, not a capability inferred from a sensor
name, project version or accepted switch write. The two installed legacy
bridges do not expose it. Their outstanding uncertain commands remain
quarantined pending verified producer recovery; no firmware has been flashed.
The original node configuration and rollback artifact are still needed before
a safe bridge firmware change can be prepared.

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

If a native export cap exceeds the software cap, acquisition may first claim
preparation ownership and tighten that cap under its saved restore baseline.
This claim permits neither forcing nor EV starts/increases. Fresh exact cap
readback and an atomic upgrade of the same owner/token precede battery or
tariff forcing; cleanup restores the original gateway cap.

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
