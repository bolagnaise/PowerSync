# Two-vehicle Tesla charging investigation — 31 October 2026

This supplements the [site-control validation](tesla-control-validation-2026-10-31.md).
It records the owner's approved brief start/stop trial after the second car
returned home. The trial used unpublished candidate `2.12.1575.dev8`, packaged
from `7a2c6e2fd67f9c4886774468c28c38b0de5e542b`. Subsequent source fixes were
not installed during this trial. All times below are UTC.

## Starting conditions

PowerSync resolved two distinct physical vehicles, each with its own paired
BLE prefix and Tesla Fleet entities. Both were home, plugged in and stopped.
TESSY was at 67% with a 70% charge target; W3RT1E was at 74% with an 80% target.
Scheduled, price-level and global solar-surplus charging were disabled. No
PowerSync EV session owned either car. Monitoring Mode remained enabled;
explicit manual EV controls use their separate authorized command path.

The native gateway reported a 14.9 kW import limit and a −10.1 kW export
limit. Home batteries were approaching full and their charging rate was
tapering. Battery charging is not a fixed household load: Tesla documents
that its site import limit first reduces Powerwall charging, and may then
discharge it to keep site import below the limit. Its export limit first
reduces discharge, then increases charging, then curtails solar.
[Tesla Site Limit Feature](https://energylibrary.tesla.com/docs/Public/EnergyStorage/Powerwall/General/Compliance/PowerControlSystemFeatures/en-us/GUID-6ABE100F-7C90-408E-ABD4-D7249BBF32F3.html).

The test temporarily enabled the managed-EV 5 A Home Power cap. It did not
change either SOC target or enable an automatic charging mode.

## Live results

| Time | Event | Evidence and limit |
| --- | --- | --- |
| 06:34:33–06:35:48 | TESSY manual start, two-minute duration | API succeeded. Subsequent fresh charging state, 5 A measured current and approximately 1.2–1.3 kW draw confirmed physical charging. |
| 06:37:36–06:38:25 | W3RT1E manual start, three-minute duration | BLE wake confirmation timed out. The Tesla Fleet fallback rejected its command key: “Vehicle did not recognize the key used to authorize command.” API correctly returned failure; no charging was proved. |
| 06:37:48–06:39:28 | TESSY deadline and stop | Deadline remained attached to TESSY. Completion was delayed while the second start and stop/readback work serialized. An explicit stop was requested at 06:38:40. BLE measured zero current/power at 06:39:00; ownership cleared at 06:39:28 with the timed-stop reason. This does not establish a lost Manual timer or identify which stop request caused the physical transition. |
| 06:39:43–06:43:01 | One W3RT1E retry after a fresh awake report | BLE start was dispatched, but neither charging state nor draw was freshly confirmed. The confirmation timeout triggered an exact-vehicle compensating stop. BLE stop was unconfirmed and Fleet again rejected the key. No second start provider was used after the ambiguous BLE dispatch. |
| 06:43:46 | Fresh Tesla Fleet refresh for W3RT1E | Charging state was Stopped after the ambiguous attempt. Earlier unchanged zero-power BLE values alone were insufficient proof. |
| 06:43:51 | Home Power restoration | Every supported Home Power setting exactly matched the saved original; the temporary 5 A cap was disabled. |
| 06:44:35 | TESSY current restoration | Fleet accepted 15 A and its current-setting entity read 15 A. The old BLE setting was 32 A, above its presently advertised 15 A maximum, so it was not blindly restored. BLE's current-setting entity remained stale at 5 A; this is not a fresh BLE readback. |
| 06:45–06:47 | Final state | Both cars reported Stopped, no EV ownership or timers remained, and SOC targets were still 70%/80%. Original battery settings and the FOUR4FREE tariff hash matched the saved baseline. |

The second car's available Charge switch belongs to `tesla_fleet`. The key
rejection therefore concerns that fallback integration's command key. Its
BLE connection signal was on, but charging telemetry was stale and commands
were not confirmed. Discovery, bridge connectivity, vehicle authorization
and physical charging are separate observations.

The owner subsequently confirmed both expected keys were paired. A separate
read-only `fleet_status` request at 06:54:35, using the same Home Assistant
Tesla Fleet account as the failed fallback, returned TESSY in
`key_paired_vins` and W3RT1E in `unpaired_vins`. This narrows the rejection
to that application's key; it does not establish that the owner's PowerSync
or BLE keys are missing. No key was reset, removed or re-paired.

A 25-second read-only bridge log subscription at 06:55 showed successful
vehicle-status responses on the second bridge. Its reported ESPHome version
was 2026.6.5 and project version 2026.8.0. Status communication alone did not
prove charging-command authorization or fresh charging samples. No further
start command or firmware change was issued for this log capture.

At 07:02 the gateway reported 5% reserve and `self_consumption`, replacing
the earlier 100%/`autonomous` baseline. The owner confirmed they changed
these settings. Further work must preserve that new baseline; it must not
restore the earlier values. No PowerSync force, native-Max journal or
settings-recovery obligation was active when this change was checked.

The two-car overlap, stopping one while the other continues, and physical
shared-capacity allocation remain **unverified**. These Manual controls also
do not exercise Smart Schedule's two-VIN export-policy leases.

Sanitized test records are retained locally in
`/tmp/powersync-tesla-control-evidence/two-vehicle-live-20261031.json`.
Vehicle identifiers and credentials are omitted from this report.

## Reproduced source defects

The Boost handler used one entry-wide stop timer. Starting vehicle B cancelled
vehicle A's timer without stopping A. The extracted production handler and
real ownership helpers reproduced this against both the candidate source and
published `v2.12.1576` source. The first fix assigns timers to canonical
physical vehicles and checks runtime, state and lease identity after taking
the EV mutation gate. Fourteen focused behavioral regressions passed, and
the two-vehicle reproduction failed against the unchanged handler.

A separate budget probe reproduced an unissued optimizer forecast being
treated as a committed battery load. With measured grid import 6 kW, battery
charging 1 kW and a 15 kW site limit, a cached 10 kW charge plan created 9 kW
of fictitious pending demand even with optimization disabled and Monitoring
Mode enabled. An otherwise identical EV increase was admitted when only
that forecast was removed. Repairs must preserve actual device commitments,
prospective demand from both vehicles, verified site limits and native Max
exclusivity.

Boost now saves an exact-vehicle stop obligation before any provider action,
including the original absolute deadline and pairing identity. Reload recovery
only stops; it does not replay a start, charge-limit or current command. A
replacement keeps the previous accepted receipt and timer until acknowledged.
Cancellation, an unknown result and a crash before a provider reply retain
cleanup. A queued start or positive-current command must still be within its
deadline at the final provider boundary, after admission and journal waits.
Stop and zero-current cleanup remain available after expiry.

The budget fixes remove unissued optimizer forecasts from firm demand. Actual
charging and accepted prospective demand still count. A separate conservative
allowance recognizes measured native battery charging that Tesla can reduce:
it requires fresh signed configuration and meter data, the same physical site,
verified native autonomous mode, no competing control owner, and a native import
limit no higher than the effective PowerSync limit. It never credits forecast
charging or future battery discharge. Self-Powered mode receives no such credit
under this initial policy. Local dispatch must still physically yield before
its capacity becomes available; native Max remains exclusive.

Manual Fleet failures now expose a fixed actionable key-rejection message
without including raw provider text or private identifiers. Provider acceptance,
fresh readback and physical charging remain distinct statuses.

## Separate BLE firmware finding

A host C++ probe extracted the upstream bridge's real queue-insertion and
action-capture code. An active data poll, queued start and later stop execute
as stop followed by the older start in both tagged v2026.8.1 and current main
`faddd99150ff90c4aafc9737cf3c8c35b15aa3b2`.
[Pinned upstream source](https://github.com/PedroKTFC/esphome-tesla-ble/blob/faddd99150ff90c4aafc9737cf3c8c35b15aa3b2/components/tesla_ble_vehicle/tesla_ble_vehicle.cpp).
This is an upstream code-confirmed defect. The installed bridge reports
2026.8.0 but has no established source hash; the original live failure cannot
be attributed to that queue defect from the available observations.

A minimal review patch removes superseded unstarted charging-switch commands,
preserving in-progress commands and unrelated queue order. Both original
snapshots fail five of seven host cases; patched snapshots pass all fourteen
combined cases. This is not a complete firmware build or on-device proof.
The report, source extracts, patch and results are retained in
`/tmp/powersync-ble-queue-order-evidence/`.

The nominal 30-second command timeout resets when an item first starts
processing. A separate BLE transmit queue is not cleared by ordinary command
cleanup. Consequently, waiting 30 seconds after a Home Assistant submission
does not prove that the bridge has drained. PowerSync cannot revoke an action
already handed to this external queue. A firmware fix and exact installed
configuration are required before claiming that boundary repaired locally.

## Validation and release boundary

The follow-up source has focused regressions for two-car timers, durable
pre-dispatch intent, reload recovery, provider cancellation, queued expiry,
native battery flexibility and Fleet diagnostics. Full combined regression and
installation receipts are recorded separately when complete. Candidate dev8
does not contain these follow-ups. The PR remains draft and publication is held
until the required local control paths, including two-car overlap, are proved.
