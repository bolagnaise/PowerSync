# Tesla EV charging

- [Native Tesla Charge on Solar](#teslemetry-native-solar)
- [Rate management inside Tesla charge windows](#tesla-native-window-rate)
- [Tesla Start confirmation](#tesla-start-confirmation)
- [Tesla EV transport recovery and native site coexistence](#tesla-transport-recovery)

<a id="teslemetry-native-solar"></a>

## Native Tesla Charge on Solar

PowerSync adds explicit Home Assistant actions for reading and managing Tesla's native Charge on Solar through PowerSync Fleet or the selected Teslemetry vehicle provider. With the matching mobile app changes, **EVs & Chargers > Vehicles > Tesla Charge on Solar** provides an explicit settings read, enabled setting, lower and upper charge limits, and a confirmed apply action. Developer Tools → Actions remains available. The actions, cost, eligibility and recovery procedure are documented in the wiki [Services Reference](https://github.com/bolagnaise/PowerSync/wiki/Services-Reference#native-tesla-charge-on-solar).

The native controller is distinct from PowerSync Solar Surplus and Smart Schedule. PowerSync Fleet uses its own OAuth connection and paired virtual key through the private signing gateway; Teslemetry uses its documented custom GET/POST route. Both operate the native feature and its two limits. Existing installations make no native requests until explicitly requested. The selected Teslemetry vehicle provider takes precedence; otherwise the configured PowerSync account supplies Fleet credentials. The separate Home Assistant Tesla Fleet integration cannot sign these custom commands.

The module persists a VIN control hold before each native request, serializes handover against in-flight PowerSync EV actions and dynamic updates, and retains the hold on rejected or uncertain requests. Only complete disabled readback releases it. Unresolved commands are blocked while a hold exists, including unidentified charger paths. An explicitly different identified vehicle remains separate. Reads are explicit, incur provider costs and have a 15-second minimum interval. Teslemetry may wake the car; the PowerSync Fleet extension sends no wake command. Writes are blocked in monitoring mode; active PowerSync EV sessions block reads and writes.

Enabling requires confirmation of Tesla native setup, compatible Tesla/Powerwall hardware and firmware, no site/export limits, other controllers disarmed and request costs accepted. Native mode does not enforce PowerSync price, phase allocation or grid-import constraints. The local hold cannot fence another integration or product, so it must be disarmed before native activation. There is no automatic hardware activation, restoration, retry or background native polling.

The mobile screen reads local provider and ownership metadata when opened; it does not contact the vehicle for native settings. **Read Tesla settings** and **Apply Tesla settings** are explicit requests. Confirmed readback is shown separately from an unconfirmed control hold. The independently developed `no_solar_installed` setting prevents new native activation, while explicit reads and disabling remain available subject to the existing monitoring/session gates. Changing solar installation settings never disables hardware or releases an uncertain native hold.

### PowerSync Fleet provider

Register the vehicle in PowerSync Cloud, grant Tesla vehicle-data and charging permissions, and install the PowerSync virtual key. Complete native Charge on Solar site pairing in the Tesla app first. PowerSync does not create or remove the vehicle-to-Powerwall association. An unregistered or unpaired vehicle cannot use this path.

HA sends one authenticated transaction to `/v1/vehicles/{vin}/native-solar`. Cloud resolves the VIN inside the authenticated account, serializes it with Cloud vehicle controls, and keeps its own persistent ownership hold. A write includes fresh settings readback in that transaction; HA must not make a second read that would collide with the Cloud cooldown. Both products retain their holds after an uncertain response. Cloud accepts HA writes only with an authenticated actuating-mode HA session; ordinary Cloud activation remains blocked when HA is active.

The private gateway independently encodes the native protocol messages and uses the official vehicle-command session, key, VIN mutex, counter and response authentication. These commands are absent from Tesla's public command API. The extension builds against checksum-pinned official vehicle-command v0.4.1; protocol fields are documented in the extended [CarServer schema](https://github.com/Teslemetry/tesla-protocol/blob/main/proto/command/car_server.proto). No Teslemetry account or token is needed. Existing service IDs retain `teslemetry` for compatibility.

Deploy the extended gateway before Cloud and HA changes. Compile/protocol fixtures and mocked provider tests do not establish live firmware compatibility or physical solar charging. No vehicle command was sent during implementation verification.

### Vehicle schedules in battery optimization

**EVs & Chargers > Vehicles > Plan Tesla charge schedules** controls each VIN's imported Tesla home charge windows independently of Smart EV Charging. It defaults on, including when Smart EV Charging is disabled or unconfigured. An away or unplugged vehicle keeps future home windows; its current known unavailable slot is excluded. Planning uses the configured home charger rate when a live away/unplugged charger cannot be identified, while execution keeps its own availability and charger-safety gates.

Turn this forecast switch off for a car you do not expect to charge at home during a longer absence. The preference is stored per VIN and PowerSync entry, survives restarts, excludes cached native schedule demand immediately on the next solve and suppresses further schedule reads for that VIN. Other cars and PowerSync's own Smart Schedule plans remain independent. Turning it on restores the bounded schedule cache/refresh path. Actual observed home EV consumption remains part of the site's measured load.

The authenticated `/api/power_sync/ev/tesla_schedule_planning` endpoint returns saved inclusion preferences on GET. POST accepts `entry_id`, `vin` and strict boolean `enabled`; it persists before publishing the new preference and queues an advisory reoptimization. Both operations are local. They never change Tesla schedules, charging controls, native Charge on Solar ownership or the household Smart Optimization setting. No solar installed does not prohibit planning ordinary grid-charge windows.


The battery optimizer imports location-specific Tesla vehicle charging schedules through loaded Home Assistant Teslemetry or Tesla Fleet integrations, even when PowerSync Smart EV charging is disabled or unconfigured. Online vehicle schedule/charge-state reads are limited to once per 15 minutes per VIN per optimizer entry. This is separate from the native-solar settings endpoint and can incur provider charges. Known sleeping vehicles are skipped and no wake command is issued. Missing/unsupported responses are unavailable evidence rather than confirmed empty schedules; successful empty responses replace the previous schedules.

Home-location schedules are expanded on the Home Assistant local clock, including recurring days, overnight windows, one-time schedules and finish-by-only schedules. Forecast power uses requested charging current and available/configured charger limits; remaining energy uses SOC, charge limit and the shared capacity resolver (including its labelled 60 kWh default estimate). Cached schedules and charge state are limited to 24 hours; older-but-bounded charge state is explicitly described as last known. This is predicted demand, not measured charging or a guarantee of the vehicle's actual execution time.

For the same VIN, overlapping Tesla and PowerSync windows are combined once per slot and share one remaining-energy budget. Distinct cars still add. Tesla-controlled charging is fixed load in the battery solve, so the optimizer reserves site-import headroom and includes it in costs, battery planning and published EV demand. Other managed cars retain joint optimization. An explicitly configured planned-EV-load sensor remains authoritative over automatic forecasts.

When a native-solar ownership hold exists, competing PowerSync windows do not become forecast demand. Confirmed native settings restrict scheduled any-source charging to the lower solar limit; opportunistic excess-solar charging above it has no guaranteed fixed window. Tesla's newer Powerwall/Wall Connector Smart Charging plans are not imported because they have no documented public schedule-read API. Existing native ownership protections remain the physical-command authority.

<a id="tesla-native-window-rate"></a>

## Rate management inside Tesla charge windows

Available in the Home Assistant integration from v2.12.1482.

PowerSync can optionally adjust a Tesla vehicle's charging current while the car is already charging at home in a recurring Tesla schedule with explicit start and end times. Tesla continues to start and stop charging and enforce its charge limit. PowerSync does not wake the car, edit its schedule, change its charge limit, or pause a Tesla-owned session.

**Plan Tesla charge schedules** remains a forecast setting, enabled by default. The separate **Adjust rate in Tesla windows** setting grants current-control permission for that vehicle and defaults off. Turning forecast inclusion on does not enable rate control.

### Requirements and limits

- A paired local Powerwall with fresh whole-site measurements and a known site import limit, plus an enabled PowerSync optimiser.
- A loaded Tesla Fleet or Teslemetry vehicle integration supplies current vehicle data and the saved schedule. Existing supported Tesla BLE, Teslemetry Bluetooth or Fleet current entities supply the actual amp command.
- A unique vehicle/provider mapping, fresh electrical metadata, and an explicit recurring home window. One-time, start-only and finish-by-only schedules remain forecast-only.
- Grid-charging permission, gateway limits and other controller ownership still apply. PowerSync keeps the original positive current setting as its maximum; this permission does not authorize exceeding that setting.

Current is calculated from the remaining vehicle energy and time before the window ends. Battery charging, home load, other EV demand and pending commands share the site budget. If the minimum positive charging current does not fit, PowerSync cannot stop a Tesla-owned session; its status reports the limitation. A target can remain unreachable because of site capacity, vehicle limits or a short window.

The first implementation supports Tesla vehicle current control. Native rate management is withheld where PowerSync has configured another charger controller, or existing managed charging demand has not been reconciled. Existing controllers continue operating while the new feature is unavailable. A pending native current restoration must finish before an unsupported controller can take over.

### Battery and forecast coordination

Before increasing EV demand into capacity currently used by a local grid-following battery charge, PowerSync records the prospective EV demand, lowers that exact battery charging session, and waits for two fresh site readings showing sufficient headroom. It rechecks authority and measurements before sending the EV command. Manual battery charging also respects reserved EV demand; a legacy tariff charge cannot enforce this shared wattage budget.

An eligible active native window uses one fixed EV forecast derived from the same current, remaining-energy and deadline calculation as rate control. Existing native scheduling forecasts remain in use elsewhere. Losing authority, recovery or a manual takeover removes the controlled forecast and requests a new household plan. Forecasts and acknowledged current settings are separate from measured charging power.

Keep enforced gateway connection limits configured. Software coordination uses sampled measurements and asynchronous provider commands; it is not a replacement for electrical protection or the gateway's enforced limits.

### Restoration and manual changes

Disabling rate management, leaving a supported window, stopping the optimiser, or restarting Home Assistant ends active control. PowerSync restores only the original current it owns and checks available import headroom before increasing it. An offline vehicle, uncertain command or insufficient capacity can leave restoration pending; the journal survives restart and does not replay a charging target.

After the original current setting is acknowledged, its prospective demand stays reserved until two fresh, aligned vehicle and site readings confirm physical settlement. Battery charging cannot consume that headroom while the car ramps up. Waiting for settlement does not resend the current command.

A manual current change or another controller taking ownership supersedes PowerSync. PowerSync does not overwrite that change or reacquire the same native window. Native Charge on Solar and native-window rate management are mutually exclusive across PowerSync entries for the same vehicle.

### Home Assistant API

Authenticated `GET /api/power_sync/ev/tesla_schedule_planning?vin=<VIN>` returns each loaded entry's forecast setting, rate permission, availability reason and current status. `POST` accepts `entry_id`, `vin`, `enabled`, and the optional explicit boolean `rate_management`. A rate permission requires forecast inclusion. These settings requests do not themselves establish physical charging proof.

The mobile control appears under **EVs & Chargers > Vehicles** with a matching app build (1.0.7 build 454 or later). **Refresh rate status** reads PowerSync's local status without waking or polling the vehicle.

This document describes the Home Assistant implementation. Cloud rate control and mobile build delivery require separate implementation and validation; their publication must not be inferred from an HA source change.

<a id="tesla-start-confirmation"></a>

## Tesla Start confirmation

PowerSync checks fresh physical charging evidence after a Tesla Start. A newer
native reading replaces the initial command observation only when its VIN,
provider, config entry, device and producer identity all match. Stale, future,
pre-command and unchanged-timestamp readings cannot confirm a new Start.

In Fleet API or Both mode, an old Home Assistant cache can trigger a bounded
read-only request through the already selected Fleet connection for that exact
vehicle. BLE-only mode does not acquire this fallback. This read does not wake
the car or repeat the Start command, and ownership, provider binding and request
deadline checks still apply before and after the request.

Charging state and positive measured current or power remain necessary. Command
acceptance, a UI state and a later independent physical reading are separate
proof levels. A failed confirmation must not be described as proof that the
vehicle never started.

The corresponding user documentation is in the wiki's Tesla Vehicle Charging
section of EV Smart Charging. No provider, automatic EV mode or monitoring
setting changes are required for this correction.

<a id="tesla-transport-recovery"></a>

## Tesla EV transport recovery and native site coexistence

This continues the 31 October Tesla control review in PR #139. The earlier
control-path and two-car validation reports describe previous checkpoints;
their installation and physical results do not validate this candidate.

### Ownership and charging plans

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
Known terminal command outcomes also retain exact completion proofs. An older
automation-store projection cannot turn a completed or proven-unsent command
back into a pending attempt after synchronization or reload. These proofs do
not erase an uncertain submission or replace its physical cleanup obligation.

Finite manual requests capture their absolute deadline before readiness or
provider awaits. The cleanup obligation is persisted and armed before positive
dispatch, with a generation token and original producer/pairing binding.
Cleanup uses a separate per-vehicle lane, so a stalled positive request or
another car's Stop cannot strand expiry. A late accepted Start receives tracked
compensating cleanup and cannot restore charging authority or extend its timer.
Automatic Boost cleanup retains native solar ownership holds; the separate
lane does not permit it to take over a foreign controller.

When passive HA telemetry cannot confirm an expired Boost or finite manual
session has stopped,
cleanup may read its exact native Fleet or Teslemetry charge-state endpoint.
This is available only when the configured provider policy permits that
fallback. A source timestamp after the cleanup boundary, a stopped state,
zero actual current and zero power are all required. The read does not wake
the vehicle, change a setting or extend the deadline. Runtime, provider,
device, pairing, generation and ownership fences must remain current before
the stopped proof can retire the cleanup obligation.
The same observer serves both expiry timers and reload cleanup. Fresh passive
charging evidence follows the ordinary BLE Stop path, while fresh passive
stopped evidence needs no cloud request. Malformed persisted obligation
families remain held and retained without arming callbacks or aborting the
otherwise healthy integration setup.

### Transport selection and evidence

Configured ESPHome BLE remains first. A proven pre-dispatch failure permits
the next configured route. An uncertain dispatched command first gets an
exact vehicle observation. Fresh Charging with measured draw can be adopted
without another Start, while any outstanding producer hazard is retained.

Native entity uniqueness is scoped to the selected provider, configuration
entry and vehicle device before duplicate checks. Fleet and Teslemetry can
publish the same VIN-derived unique IDs without masking each other's controls.
Multiple candidates within the exact authorized scope still fail closed.

Tessie native charging switches and current/limit numbers use the same exact
provider, configuration-entry, device and VIN checks. A completed Price-Level
session can therefore submit its cleanup Stop through Tessie instead of being
rejected before the service call. This compatibility repair does not retire an
uncertain earlier command or qualify Tessie for the Fleet/Teslemetry-only native
cloud recovery observer described above.

For an uncertain ESPHome mutation, PowerSync retains the exact attempt and
quarantines its producer. It supports two recovery mechanisms: an explicit
nonce-bound producer-fence receipt, or a qualified stock-node restart followed
by independent vehicle reconciliation. A bridge's advertisement/presence sensor
cannot supply either proof. A successful service call or reconnect alone is
also insufficient.

A quarantine belongs to its originating attempt and exact producer. Reusing
a bridge prefix does not transfer a previous quarantine's retirement proof.
Merges preserve distinct historical hazards and remain stable when the same
projection is replayed. Stock recovery selects current liabilities for its
exact VIN and producer under the journal lock; an old or incomplete hazard
reference cannot retire a newer command's quarantine.

Before a new BLE mutation, the exact bridge must demonstrate a live vehicle
session. A known charging state with fresh measured current or power supplies
this evidence; zero is valid. Qualified stock bridges may instead establish
transport health from an advancing LastUpdate source value after a bounded
probe in the same established API connection. The original native subscription
must already have a non-stale baseline, and registry, producer, radio and
connection identities remain pinned across every wait. A reconnect or a cached
value receiving a new HA delivery timestamp cannot qualify.

For these qualified versions, LastUpdate is published after processing an
authenticated CarServer vehicle-data response. Unchanged charge fields need
not be republished to establish transport health. Its text has no timezone;
source-value advancement is checked without pretending that it is UTC. This
health evidence never refreshes retained charging power, confirms a command,
or replaces site admission. Wake advertisement/presence alone remains
insufficient. A bounded wake/data-refresh failure is a known pre-dispatch
failure, so Both mode can use the exact configured native fallback without
creating a queued BLE mutation. Fleet data cannot certify BLE session health.

BLE wake allows up to 120 seconds for a fresh vehicle awake or charge-data
response before considering fallback. If the same registered bridge freshly
reports the car still asleep after the first request, it may retry Wake once
within that original timeout. This handles a stock bridge discarding a Wake
queued before its GATT connection exists. Advertisement presence alone cannot
trigger the retry. Ownership, registry identity and the original charging
deadline remain effective during the wait; cancellation or expiry cannot turn
a late wake into a new Start. The charge-data health check still precedes every
mutation. This timing and route selection are PowerSync software behaviour and
do not require a firmware upgrade.

Cancellation while an instrumented ESPHome helper is still waking or preparing
its first mutation records `not_sent`. Once it reaches a mutating service
boundary, cancellation remains uncertain; a later current-range retry cannot
erase that uncertainty. An uninstrumented producer or a restart before the
outcome was saved also keeps the conservative recovery obligation.

BLE current and charge-limit attempts also persist their exact registry target
and bounded possible setting values before dispatch. The final service value
must remain inside that saved range after readiness and journal writes; a
changing entity range cannot enlarge an already recorded effect. A structured
current-range retry is covered by the same saved bound. Only HA's exact
structured prewrite range rejection permits that retry; range-like error text
alone leaves the first submission uncertain. A later refusal or admission
exception cannot remove an earlier possible handoff. Older attempts without
these values remain uncertain; today's requested current cannot reconstruct
what an earlier command submitted.

One supported isolation mechanism uses an
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
bridges do not expose it. An outstanding uncertain command must remain
quarantined pending verified producer recovery. The original node configuration
and rollback artifact are needed before changing bridge firmware; their absence
does not require rebuilding or pairing a bridge that can communicate normally.
No bridge firmware has been flashed during this investigation.

The supported stock restart path verifies the configured VIN, exact enabled
registry entities, ESPHome runtime, inspected source profile and host submission
contract. It revokes the old command generation, drains that vehicle's actual
provider submission tasks, and records each phase before effects. A real node
disconnect/reconnect, newly published low uptime and a subsequent advancing
uptime sample must all belong to that same producer. Missing uptime, an
unsupported profile, a rebind or a failed save leaves recovery held. Failure
also attempts to contain the radio; it does not claim containment succeeded.
This requests the bridge's **BLE Connection** switch off and logs that reason.
An off switch after a failed recovery is therefore a containment action, not
evidence that the car failed to wake. A connected BLE status alone does not
establish fresh vehicle charge data.
Original YAML or a firmware rebuild is not required for this supported Restart
operation.

The initial stock profiles cover the two reviewed project/ESPHome pairs:
`PedroKTFC.esphome-tesla-ble` 2026.2.0 / ESPHome 2026.1.5, and project 2026.8.0 /
ESPHome 2026.6.5. Host submission qualification is currently HA 2026.10.4 with
`aioesphomeapi` 46.6.0. These are supported source and runtime profiles, not a
claim of cryptographic binary attestation. Other versions remain held unless
another implemented producer-isolation mechanism qualifies. Profile expansion
requires source review and the same runtime checks.

A reboot retires RAM queues; it cannot undo a command already accepted by the
car. Recovery separately reads the exact configured native Fleet or Teslemetry
cloud charge-state endpoint using HA's existing authenticated client and refresh
lock. It consumes the returned cloud document rather than the coordinator's
mixed stream cache or the vehicle's command router. Requested amps, charge
limit, charge state and measured power require native source timestamps and
field provenance. Two advancing observations must agree on requested settings
and settled charging state. Recovery does not issue Start or change a limit to
manufacture convergence. The original delivery outcome stays unknown, while
separate retirement and present-state evidence allow future ordinary control.
If the second read has not acquired a newer document yet, recovery may retry
it twice with five-second pauses within a 40-second total budget. Every read
keeps the same source and policy fences. An actual changed setting or charging
state still refuses reconciliation immediately.

If that independent read is unavailable because the car is asleep, recovery
first uses the ordinary bounded BLE Wake and reads again. When the configured
provider permits cloud fallback, a remaining failure can request one Wake on
the exact native cloud client and wait up to 60 seconds using its vehicle-info
endpoint. It then requires fresh independent charge-state observations; Wake
acceptance alone never releases the incident. This fallback bypasses the
Teslemetry command router, keeps the same VIN and provider binding, and does
not change charging settings or extend an existing session deadline. BLE-only
configuration does not enable the paid cloud Wake fallback.

Teslemetry's ordinary `vehicle_data` response can come from its server cache.
When cloud fallback is permitted, an independent recovery read may request one
explicit native Teslemetry refresh. A returned acknowledgement is insufficient:
the exact vehicle's charge-state document must still pass the source timestamp,
requested-setting and convergence checks. The refresh can consume Teslemetry
credits. Command readback recovery uses the same bounded opt-in only after an
ordinary read returns no fresh observation, while retaining its original
deadline and exact native source. Periodic observation does not force refreshes,
and BLE-only configuration does not enable them. Both exact unsigned and signed
native Fleet cloud clients are supported; command routers are not independent
readers.

The recovery sidecar (`power_sync.ev_transport_recovery.<entry_id>`) is
independent of the automation store's legacy projection. It retains incidents,
generations, attempts, liabilities and essential finite stop obligations.
Startup hydrates it before charging callbacks. A durable enrollment marker
prevents a missing established sidecar from looking like a first installation.
A malformed store or persistence failure holds new positive control. If an
older public build already removed the original journal, recovery records a
new evidence-loss incident with surviving facts and missing fields; it never
invents an old attempt ID or numerical target. That incident requires confirmed
coverage of all plausible old producers before it can be reconciled.

Automatic recovery preserves the existing controller, current plan allocation
and absolute deadline. A request interrupted by recovery stays pending; the
existing controller retries under fresh authority. Stop and expiry remain in
their existing independent cleanup lanes. Every actual provider handover
rechecks generation, owner, original deadline and registry binding after
persistence. Cancellation of a waiter does not cancel or forget an already
handed-over child service call. Reload never grants authority to replay a saved
Start.

If a new cleanup journal save fails, a narrowly scoped Stop can still use an
existing exact durable cleanup obligation or unresolved producer liability. Its
real service task remains tracked and shielded; a later fresh cleanup retry can
proceed once that task finishes. Service acceptance retains the original stop
obligation and uncertain outcomes until physical confirmation and durable
cleanup. This exception cannot dispatch Start, Rate or Limit, select another
producer, or replace a newer owner.

The `power_sync.recover_ev_transport` service also exposes this qualified
recovery for an exact VIN and optional existing incident. It does not clear a
journal, confirm missing producer scope or bypass normal charging admission.

Source qualification: [2026.2.0 queue declarations](https://github.com/PedroKTFC/esphome-tesla-ble/blob/3906351db4bba7e533ee7c7af13b460306ad5d65/components/tesla_ble_vehicle/tesla_ble_vehicle.h#L578),
[2026.8.0 source](https://github.com/PedroKTFC/esphome-tesla-ble/tree/00ff23e74da1ec6beebca486898d48745862cd07),
[HA Teslemetry cloud coordinator](https://github.com/home-assistant/core/blob/2026.10.4/homeassistant/components/teslemetry/coordinator.py),
[HA Fleet cloud coordinator](https://github.com/home-assistant/core/blob/2026.10.4/homeassistant/components/tesla_fleet/coordinator.py).
Physical results for this candidate are recorded separately; source and unit
coverage do not certify the installed bridges or the complete local test matrix.

Fleet and native Teslemetry recovery use an explicit exact-vehicle coordinator
refresh when needed. This works with automatic Fleet polling disabled and
does not introduce recurring paid idle polling. An unchanged provider source
timestamp is not a fresh observation. Such explicit reads and permitted Fleet
fallback can incur provider charges.

Native requested-current evidence comes from one exact provider's charge-state
document and its source acquisition time. An unchanged HA number entity does
not make a fresh native document stale. Explicit admission can refresh that
source when needed, while routine telemetry remains passive. A BLE command may
use this observation only through its own durable attempt's pinned native
fallback; the command itself remains on BLE. Old power reservations still need
two advancing, aligned observations and no live owner or submission task before
release. Stopped retirement additionally requires an explicit settled state,
zero measured current and zero power. A provider or runtime rebind during
persistence retains the original reservation.

Before a Fleet mutation, PowerSync refreshes the exact registered provider
vehicle once. This prevents a cached Online state from suppressing the Fleet
integration's own wake-up while automatic polling is disabled. A refreshed
asleep/offline state leaves wake-up to the command entity, including accounts
without a separate Wake button. An Online result requires newer charge data;
an unchanged or failed refresh cannot authorize a new positive command.
Cleanup Stop remains available without that metadata-read prerequisite.

Native Fleet and Teslemetry service writes also save their intent before
submission. An ambiguous response or cancellation preserves the exact producer
and original cleanup obligation across reload. A successful service return is
provider acknowledgement, with physical charging confirmed separately. A
completed native fallback retains its exact producer through the active session,
including initial current changes, later limits and Stop. The session anchor
stays separate from each switch/number mutation identity. Later sessions return
to the configured BLE preference when no outstanding attempt or isolation hazard
needs the native route. After reload, resuming Manual or Solar rate control
requires an explicit handoff of that same producer to the restored owner.
Expired Manual and Boost recovery uses a stop-only handoff, which cannot
authorize Start, Rate or Limit commands. Neither handoff replays Start or clears
an unresolved attempt. Only a structured HA number validation known to precede
the provider permits the bounded current-range retry.

Before a redundant BLE current setter, a fresh exact native requested-current
report may establish that the capped target already holds. This is an observed
no-op, recorded as no BLE submission, without Wake, paid polling or a new
uncertain site reservation. Actual draw, writable-number polling and a generic
bridge update cannot establish that requested-current setting.

The vehicle list merges charging fields using their own timestamps; a bridge
status/SoC update does not refresh retained Charging or power. Quarantined BLE
data cannot override a newer native stopped report or drive automation charging
triggers. The car's identity/SOC/plug metadata remains available separately from
unknown charging power.

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

### Native battery charge and discharge

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

The public Restore normal response distinguishes completed restoration, a
manual native restore still awaiting physical settling, and nothing to restore.
Requesting an HA service response does not request strict synchronous physical
confirmation. Internal strict confirmation and background cleanup retain their
existing failure, retry and ownership rules.
