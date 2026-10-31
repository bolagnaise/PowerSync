# Two-vehicle Tesla charging investigation — 31 October 2026

This supplements the [site-control validation](tesla-control-validation-2026-10-31.md).
It records the owner's approved brief start/stop trial after the second car
returned home. The trial used unpublished candidate `2.12.1575.dev8`, packaged
from `7a2c6e2fd67f9c4886774468c28c38b0de5e542b`. Subsequent source fixes were
not installed during this trial. All times below are UTC.

**Latest installed checkpoint:** `2.12.1576.dev10`, source
`2a63050d9c311b89191d49b23073ce8136ed4fa8`. The first trial below is historical;
the successful Fleet overlap and reload trials are recorded separately. The
owner's current baseline is 5% native reserve and Self-Powered mode, with
Monitoring enabled, Fleet-only commands and all automatic EV modes disabled.
The PR is draft and unreleased.

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

During the later authorized Fleet repair, the owner identified the application's
allowed origin as `m2wun.fleetkey.cc`. That address hosted a different public
key from HA's current key, and Tesla's partner readback denied this application
access to that domain. It was not supplied as a pairing link. A replacement
alias, `5hfco.fleetkey.cc`, now hosts the existing HA public key. Registration
and independent Tesla readback at 07:49:57 both matched that same key; HA's
private key was unchanged. The owner was asked to approve this verified key
on W3RT1E. Pairing, a successful command and physical charging remain separate
checks, subsequently completed in the Fleet trial below. The first attempted
`.fleetkey.net` alias was refused because
the application permits the `.fleetkey.cc` root; that refused registration is
not treated as a working repair.

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

At the end of this first trial, two-car overlap, stopping one while the other
continues, and physical shared-capacity allocation remained unverified. The
later Fleet trial below verifies overlap and stop isolation. Neither trial
exercises Smart Schedule's two-VIN export-policy leases.

Sanitized test records are retained locally in
`/tmp/powersync-tesla-control-evidence/two-vehicle-live-20261031.json`.
Vehicle identifiers and credentials are omitted from this report.

## Successful Fleet trial after key repair

At 07:51:25 the same application's read-only `fleet_status` returned both cars
in `key_paired_vins`, with no unpaired vehicle. Candidate `2.12.1576.dev9`,
from source `5f9015decd7307701719689c06a90da129de2e48`, was then installed.
All 265 packaged files matched; HA configuration check passed and Core restarted.
Monitoring Mode, the owner's 5% native reserve and Self-Powered mode remained.
Provider selection was changed to Fleet-only, retaining the BLE configuration
while excluding its unresolved command queue from these live actions.

For this trial only, Home Power limited each vehicle to 5 A and site import to
12 A at the configured single-phase voltage. SOC targets stayed 70%/80%.

| Time | Event | Highest proof |
| --- | --- | --- |
| 07:58:25–07:58:46 | TESSY three-minute Boost | API succeeded; deadline remained 08:01:25.684638, measured from the request rather than the slower response. |
| 07:59:49–07:59:52 | W3RT1E three-minute Boost | API succeeded with its separate 08:02:49.122383 deadline; TESSY's deadline remained intact. |
| 08:00:35 | Independent fresh Fleet `vehicle_data`, charge-state only | Both vehicles reported Charging, actual current 5 A each, 233/229 V, and charger power 1 kW each. Targets remained 70%/80%. |
| 08:01:23–08:01:24 | Explicit TESSY stop | API returned success at 08:01:24.114. A fresh Fleet sample at 08:01:25.030 reported TESSY Stopped/0 A while W3RT1E still reported Charging/5 A. |
| 08:02:49–08:02:52 | W3RT1E's original Boost deadline | PowerSync recorded a successful stop at 08:02:50.754 with reason `Boost duration elapsed`. Fresh Fleet samples at 08:02:52 reported both Stopped/0 A. |
| 08:05:16 | Fresh HA Fleet readback | Both canonical loadpoints were connected idle, with no owner, expiry or measured draw. |
| 08:07–08:08 | Restoration | Every Home Power setting matched the saved original. Fleet accepted requested-current restoration to 15 A/10 A; fresh physical readback is recorded separately. |

The cars physically charged together, and stopping the first left the second
charging until its own deadline. Grid import remained approximately zero while
the home batteries supplied the home and EV demand in Self-Powered mode. This
does not test saturation of the import limit, native charging flexibility,
native-Max exclusion during EV charging, or Smart Schedule export leases.

This UI review also reproduced two source defects. Fleet-only discovery lost
explicit VIN/BLE associations and rendered four rows for two cars; the fix
preserves read-only identity without enabling BLE commands. Separately, the
vehicle-list view allowed a `charge_rate` sensor in km/h to overwrite a real
charger-power sensor in kW. It now accepts fresh finite power units, normalizes
them to kW and chooses deterministically. Both defects have unchanged-baseline
failures and focused regressions; the final combined artifact is checked again
before release. Fleet remains the working command route while BLE firmware
deployment is unresolved.

## Installed two-car display and reload trial

Candidate `2.12.1576.dev10` includes the Fleet identity and power-unit repairs.
All 265 installed files matched archive SHA-256
`61a74e0d7209e4ebcac594e99678fcfee23f01408844c4cf4ea00adc37d9a7ad`.
HA configuration check and startup passed. The EV charging selector showed
exactly two canonical cars after restart, rather than four provider rows.

With the same temporary 5 A per-car and 12 A site limits, W3RT1E's two-minute
Boost was requested at 08:20:04.680 and accepted at 08:20:08.724. Its original
deadline was 08:22:04.972882. A fresh HA Fleet update at 08:21:09 showed Charging
and 1 kW in both vehicle-list and canonical loadpoint APIs. The normal HA
`homeassistant.reload_config_entry` service ran from 08:21:53.329 to
08:22:00.468. The earlier unsupported REST reload request failed immediately
and did not reload the integration.

After the successful reload, PowerSync recorded the timed stop at
08:22:05.874877, retaining the original deadline. Independent Fleet vehicle
readback at 08:22:21 showed W3RT1E Stopped/0 A; TESSY was asleep. Every Home
Power setting was restored at 08:23:09.165, and Fleet accepted W3RT1E's original
10 A request. Fresh readback at 08:24:30 confirmed Stopped/0 A, requested 10 A
and target 80%. TESSY had earlier fresh Stopped/0 A, requested 15 A and target
70% readback at 08:11:29, with no subsequent charging command to that car.

The house picture remained a separate defect: its generated configuration
populated only the selected EV's entity slot even though the canonical API
reported two home, plugged-in cars. The existing renderer already has two-car
scenes. The follow-up connects generated house pictures to read-only canonical
loadpoints, retaining manual entity configuration for standalone cards.
Generated classic and board layouts use the same opt-in read-only path. Actual
JavaScript regressions cover two stopped/charging cars, selector independence,
separate power paths, all weather/day/night scenes, duplicate identities,
away/unplugged cars, unavailable power and failed-read fallback. Polling is
bounded to 30 seconds, with one in-flight read, a 90-second browser-cache limit
and rejection of replies from a previous connection or configuration. The
focused and adjacent package passed 113 tests before integration.

The final storage review also found two Boost stop obligations retained after
the live trials. The observation consumer required a known-state marker that
the real discovery producer removed. A focused follow-up carries private,
timestamped charging-state evidence to that consumer; stale, unknown or other
vehicle observations cannot retire a stop obligation. Neither journal is
manually cleared. Installation and fresh recovery readback of these follow-ups
are recorded in the PR receipts when complete.
Private evidence keeps the original charging-state timestamp; a fresh meter,
Wall Connector sample or repeated HA report cannot renew an old state. Explicit
Stopped/Complete/Disconnected may prove a stop. Starting, Pending, unknown and
equal-time conflicting provider evidence remain uncertain. Default/public
vehicle rows retain their existing schema.

The corresponding dashboard wiki text is prepared in local wiki commit
`bf631f3` on `docs/tesla-connected-vehicle-picture`. It is held with this
unreleased feature rather than being published as existing release behavior.

Sanitized records: `fleet-two-vehicle-trial-20261031.json` and
`fleet-reload-boost-trial-20261031.json` in the local evidence directory.

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

A subsequent isolated **full generic ESP32 firmware compile** passed for both
the unchanged pinned v2026.8.1 source and the queue-patched source, using
ESPHome 2026.8.1 and the same pinned TeslaBLE library, configuration and partition
table. The receipt is `/tmp/powersync-ble-firmware-lab/validation-summary.json`.
These builds use dummy credentials and identifiers and are not production
images. No firmware was flashed. The actual second bridge's YAML/OTA
authentication/layout and a matching rollback remain unavailable; its HA
device page identifies the node but does not provide those build inputs.

## Validation and release boundary

The follow-up source has focused regressions for two-car timers, durable
pre-dispatch intent, reload recovery, provider cancellation, queued expiry,
native battery flexibility and Fleet diagnostics. Full combined regression and
installation receipts are recorded separately when complete. Candidate dev8
does not contain these follow-ups. The PR remains draft and publication is held
until the required local control paths, including two-car overlap, are proved.

At source `2a63050d9c311b89191d49b23073ce8136ed4fa8`, audited local FULL16
passed **9,949 tests, zero skips**, four workers, 517.85 seconds; run ID
`72b0e619932d49dfa7e02c172518d39f`, node-list SHA-256
`addd798b8d6867c224c633879f353780769a878d566101757a2c16c019465cc3`.
PR regression run [55](https://github.com/bolagnaise/PowerSync/actions/runs/41183782946)
independently passed the same 9,949 tests with zero skips in 970.59 seconds;
its audited run ID is `cf9e758ac5b8494baeaf7bfc2848952f`.
These receipts cover that checkpoint. Subsequent code changes require a new
complete audited run and matching installation receipts. PR success does not
replace protected-main evidence required for publication.

At feature source `24c3a70df014399df7665859dce94f35ca8e87fe`, audited local
FULL14 passed **9,892 tests, zero skips**, using four workers in 546.87 seconds.
Its run ID is `3c8f4d8fab964d32bb379fa5e1338932`; node-list SHA-256 is
`69381901ebf6415bee9ac350604481e0e6c134fa36a2e6faeefa66c13e16ef50`.
The combined Boost/ownership/admission package separately passed 562 tests.
These results do not establish installation or two-car physical behavior.

Documentation-impact review: the manual Fleet change adds fixed actionable
failure text; it adds no configuration field or setup path. The diagnostic
behavior is described above. The source documentation signature for
`automations/actions.py` was refreshed after that review; the source-only
documentation check passes. Current-main reconciliation also brought in one
legacy-admission test-fixture correction. Fresh final checks cover that exact
combined repository state.

FULL15 exposed four variants of one supervised-cloud-recovery route failure
after main's paired-IP fixture correction. The unchanged published-main tests
passed; this branch tried to acquire native control after an acknowledged Auto
cloud handoff. The repair honors only a current same-site matching handoff for
targetless requests. It preserves the incident, native suspension, explicit
Local/numeric-target refusals and all cleanup/budget/monitoring fences. The four
original checks remain unchanged, with thirteen additional predicate fences.
FULL15 is failed evidence and is not reused as successful validation.
