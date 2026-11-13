# EV command and headroom recovery

- [Recovering held charging commands on a BLE-only Tesla bridge](#tesla-ble-stop-recovery)
- [Recover native Tesla charging after a lost response](#tesla-native-recovery)
- [Recover retained EV charging headroom](#ev-headroom-recovery)

<a id="tesla-ble-stop-recovery"></a>

## Recovering held charging commands on a BLE-only Tesla bridge

PowerSync can reconcile retained **Start, Stop, current and charge-limit** history without Tesla Fleet or
Teslemetry when every affected ESPHome bridge runs the companion recovery
component below. This is an operator-triggered recovery; updating PowerSync
alone does not clear a held command.
It does not establish why an earlier Stop went unconfirmed or guarantee that
the original communication failure will not recur.

This route covers exact original bridge/device/entity bindings and an explicitly
confirmed complete vehicle inventory. It refuses unknown operations, conflicting identity, omitted vehicles and changed pairing.
The compatibility action `recover_ble_stop_history` remains restricted to Stops;
use `recover_ble_command_history` for an inventory containing positive commands. A legacy
bridge alias remains a journal alias; the VIN you supply is a declaration of the
physical vehicle, not reconstructed historical evidence.

Do not use this procedure if a bridge was moved between vehicles during the held
history, any affected vehicle/producer is missing, or the historical inventory is
uncertain. Do not delete recovery files, re-pair keys, change VIN mappings or
manually toggle the recovery request entity. Preserve a backup of the current
HA recovery files, bridge YAML and working firmware before updating.
Another held recovery incident in the same PowerSync entry also requires
support review before this whole-entry recovery can proceed.

### Install the companion component

Keep EV automations and other writers paused while installing and recovering.
The vehicle must be available to BLE and already report stopped, complete,
disconnected or no-power, with actual current and power both zero. Recovery
reads vehicle state; it does not stop an actively charging vehicle for you.
A PowerSync Stop request interrupts recovery and retains its existing native
provider path where configured. The companion bridge remains protected until
recovery completes; an interrupted run cannot unlock from earlier samples.

The v2.12.1673 companion also handles an unknown sleep status immediately after
the recovery reboot. It uses the bounded wake path until an actual awake status
is available, instead of repeatedly reading a sleeping vehicle without waking it.
Update both PowerSync references below and rebuild the companion for this fix;
an HA-only update cannot change the installed bridge firmware.

The v2.12.1647 companion fixed recovery queries being deferred while a vehicle
is asleep. An explicit recovery can use the existing authenticated wake path
to obtain fresh readings; ordinary background polling still lets sleeping cars
sleep. Existing v2.12.1641 companion installations must update both PowerSync
references below and rebuild each original bridge for this fix. Updating only
the HA integration does not change bridge firmware or release a hold.

1. Update PowerSync through HACS and restart HA.
2. In **each affected bridge's existing YAML**, preserve its device name, VIN,
   BLE address, credentials, board settings and keys. Pin the four PedroKTFC
   packages and add the recovery package as shown below. Replace the existing
   package block rather than importing a second copy of those four packages.
3. Compile and install using ESPHome. The ESP32-S3 configuration was compile
   checked with ESPHome **2026.10.2**; this is not a hardware recovery test.
   Retain the existing pairing. Do not press Pair or Regenerate key.
4. Wait for HA's ESPHome connection to return. Verify both new entities belong
   to the same ESPHome device as its Charger switch:
   `sensor.<prefix>_powersync_recovery_proof` and
   `text.<prefix>_powersync_recovery_request`. If HA assigned different entity
   IDs, restore these names before recovery. Do not change the existing Charger
   identity or create replacement entities to bypass a refusal.

Merge these substitutions with your existing ones:

```yaml
substitutions:
  tesla_ble_component: github://bolagnaise/PowerSync@v2.12.1681
  BLE_library: https://github.com/PedroKTFC/tesla-ble.git#969f05f9dfdd5bfb9f3da5ad6b9f5c2265be133b

packages:
  tesla_packages:
    url: https://github.com/PedroKTFC/esphome-tesla-ble/
    ref: 651b4c25dcb24ce8d16d625a51868576e83bf2fe
    files:
      - packages/base.yml
      - packages/common.yml
      - packages/project.yml
      - packages/client.yml
  powersync_stop_recovery:
    url: https://github.com/bolagnaise/PowerSync/
    ref: v2.12.1681
    files:
      - esphome/stop-recovery.yaml
```

The v2.12.1652 recovery package also includes native charging receipt and paired-value entities automatically; do not add a second freshness package. See [BLE reliability](tesla-ble.md#tesla-ble-reliability) for firmware-family compatibility, the matching yoziru recovery adapter, and the operational recovery policy.

The companion component is separately licensed under AGPL-3.0; see
[its source and notice](../esphome/NOTICE.md). Existing upstream binaries do not
provide this protocol. The pinned packages preserve the upstream controls; the
recovery lock temporarily rejects vehicle mutations, including pairing/key
changes, while it collects proof.

### Confirm the entire affected inventory

In HA **Developer tools → Actions**, run `power_sync.recover_ble_command_history`
using YAML mode. Replace the example prefixes and VINs with your actual values.
The prefix is the existing entity prefix, without `switch.` or `_charger`.
Include **every affected bridge and vehicle** in this entry in one call.

```yaml
action: power_sync.recover_ble_command_history
data:
  entry_id: YOUR_POWERSYNC_ENTRY_ID
  confirm_complete_history: true
  inventory:
    - prefix: tesla_flinn
      vin: YOUR_FIRST_17_CHARACTER_VIN
    - prefix: tesla_yf88
      vin: YOUR_SECOND_17_CHARACTER_VIN
```

Only set `confirm_complete_history: true` when you can confirm those same
physical vehicles account for the entire held history. One bridge used for
multiple historical vehicles is unsupported. Do not guess or omit a vehicle to
make recovery pass. With multiple PowerSync entries, supply the affected entry
ID explicitly.

The action can take several minutes. PowerSync first holds new commands and
waits for outstanding HA submissions. Each bridge saves a unique request and
restarts into a persistent read-only session. It accepts only fresh vehicle
responses matching new queries, and requires repeated explicit stopped state,
zero actual current and zero power. A BLE presence signal, Wi-Fi uptime, old HA
state or a timer cannot substitute for these responses.

All declared bridges must pass before **any** covered record is retired.
PowerSync saves the proof and release before unlocking the bridges. The original
unknown command outcomes remain unknown in history. Recovery does not replay a
saved Start or claim that an earlier Stop succeeded. Normal automation can issue
new commands after successful recovery; resume paused automations deliberately.

### If recovery is interrupted or refused

Leave the records, pairing and bindings intact. The action returns
`success: false` with a reason when required proof or scope is missing. A refusal
is not permission to clear a journal or manually enable the BLE switch.

If all records were durably released but a bridge unlock was interrupted, repeat
the **same complete inventory**. PowerSync checks the completed incident and
collects fresh proof before finishing that exact generation's unlock. It does
not create a new incident or restart a completed bridge. A further bridge reboot,
identity/configuration change or new retained command can require support review.

If HA stopped before release, the incident remains held. Repeating the same
inventory creates a fresh bridge recovery generation and obtains new proof;
previous samples cannot release it. If the vehicle is charging, native fields
are unavailable, or the bridge cannot provide the required proof, recovery stays
held. Keep the returned reason and the recovery records for support.

If another uncertain Stop was recorded during an interrupted attempt, repeating
the same complete inventory includes that additional Stop after checking its
original bridge and vehicle binding. This also covers an original quarantine
preserved under a history key when a later Stop reused its previous key.
PowerSync preserves the earlier scope and
requires fresh proof from every bridge before retiring any record. Missing old
records, changed bindings and unsupported command operations still require support review.

Rollback: keep held bridges and EV automations contained and contact support
before restoring older firmware. A downgrade removes the companion firmware's
persistent read-only enforcement; it is not a recovery method. No customer
firmware installation, charging command or live recovery is performed by the
release itself.

### Operational recovery policy

The general action uses protocol 2 to retire executable bridge work through a
persistent read-only lock and verified reboot, then checks new source-native
stopped/zero reports for every declared vehicle. It records operational
reconciliation while retaining each historical outcome as unknown. It does not
claim a later Stop cancelled an earlier command, or that the vehicle provides a
public queue-drain guarantee. Subsequent controls use normal ownership, current
limits, site headroom and confirmation.

The existing companion firmware already supplies the needed producer reset and
read-only acquisition transaction; no pairing change or protocol upgrade is
required. Firmware without this protocol must use its separately supported stock
recovery route or install the compatible companion. Pedro and yoziru packages
are different firmware families. Use the [yoziru adapter and matching library
pins](tesla-ble.md#tesla-ble-reliability) for that family; do not
flash the Pedro component onto yoziru.

<a id="tesla-native-recovery"></a>

## Recover native Tesla charging after a lost response

A Fleet, Teslemetry or Tessie timeout can occur before or after a command executes.
PowerSync keeps the original attempt as `sent_unknown`. It now separates that
historical uncertainty from the operational decision to resume charging control.

### Automatic recovery

A new charging request can reconcile old native Start, Stop, current or limit
attempts when:

- the journal is loaded and valid, and the original producer identities still match;
- no provider submission is running and configuration, ownership and generation remain unchanged;
- the uncertain commands have settled for at least 60 seconds (120 seconds for Teslemetry, whose command retry window can last 90 seconds);
- two advancing vehicle charge-state reports, separated by at least three seconds, show stopped/complete/disconnected/no-power, zero current and zero power, with valid unchanged requested current and charge limit.

This also works with an older current request whose numerical value was never
saved. It does not reconstruct that value or claim the earlier command succeeded.
Instead, it records the exact attempt IDs and current vehicle observations, saves
the operational recovery, and advances the control generation. Normal command
admission then checks ownership, electrical headroom and the requested settings.

If the car is charging, use the normal **Stop** control before requesting Start.
A charging or contradictory observation keeps recovery pending. A failed recovery
attempt backs off for 60 seconds, persisted across restart; it does not create a
permanent per-session retry latch. Intentional Stop pre-emption permits an
immediate new recovery request. Missing historical submission timing gets one
finite, durable settling interval without backfilling the old record. Stop remains available and interrupts an in-progress reconciliation.

Recovery performs a bounded provider refresh, using the exact independent cloud
reader when the ordinary coordinator retains a stale document. Teslemetry may
require a paid refresh. Tessie recovery uses its source-state GET with caching
disabled. Reconciliation never invokes a wake or charging command by itself. Offline/asleep vehicles can recover after fresh reports become available.

Where an obsolete managed reservation exists, recovery also obtains fresh local
meter samples and feeds the two observations through the existing allocator.
Only the captured car's reservation can settle; other vehicles keep their demand.
Unavailable electrical evidence retains the reservation and normal admission
continues to enforce it.

The original records and unknown outcomes survive restart. Recovery does not
manufacture terminal proofs or delete history. A new uncertain command gets a new
hold and requires new observations.

This is state reconciliation under [Tesla's documented stateful-command timeout
model](https://developer.tesla.com/docs/fleet-api/getting-started/best-practices).
The settling period and repeated observations are operational safeguards, not a
vendor guarantee that no delayed historical command can ever execute. Ordinary
setting confirmation, current regulation and electrical admission remain active.

### Read-only inspection

In **Developer Tools → Actions → PowerSync: Inspect Tesla EV recovery status**:

```yaml
action: power_sync.get_ev_recovery_status
data:
  entry_id: YOUR_POWERSYNC_ENTRY_ID
  vin: YOUR_UPPERCASE_17_CHARACTER_VIN
```

Specify the PowerSync entry when multiple entries are loaded. The response covers
only that VIN. It distinguishes missing/malformed history, producer changes and
native uncertainty, and identifies the fresh reports needed for reconciliation.
The inspection action itself does not refresh, wake, send controls or change history.

BLE history uses the companion firmware recovery described in
[Tesla BLE recovery](ev-command-recovery.md#tesla-ble-stop-recovery). Reconciliation never adopts another
provider, vehicle, disabled entity or changed pairing to escape a retained hold.

<a id="ev-headroom-recovery"></a>

## Recover retained EV charging headroom

An uncertain Tesla charging command can leave a conservative EV reservation
that blocks Powerwall battery grid charging. Previously, automatic recovery
could only wait for the provider's cached charge data to advance. A fresh log
does not make that data advance.

After updating through HACS and restarting Home Assistant, use **Developer Tools
→ Actions → PowerSync: Recover EV charging headroom** for the affected vehicle.
Enable **Allow provider refresh** to authorize this attempt. Teslemetry refreshes
can consume credits. For multiple PowerSync entries, specify the PowerSync
configuration entry ID.

```yaml
action: power_sync.recover_ev_headroom
data:
  vin: YOUR_17_CHARACTER_VIN
  allow_provider_refresh: true
```

The action requests up to two independent native vehicle charge reports and two
local Powerwall meter updates, with a five-second interval so normal two-second
meter polling can satisfy the unchanged three-second acquisition-progress check. Each round
has an eight-second read timeout. There is a one-minute cooldown per vehicle
after an attempted recovery. There are no automatic paid retries or background
changes. The car must be able to provide fresh data; the action does not wake it.

A successful response includes `success: true` and `reservation_cleared: true`.
This means the targeted reservation has been removed durably after verified
readback. It does not start battery charging: retry the intended charge action
or allow the optimizer's next cycle. Other battery or EV safety checks still
apply. Success does not establish historical command execution or actual battery
grid charging.

If a Scheduled Charging session has ended, its expired ownership lease no longer
blocks read-only recovery for the same verified native vehicle source. Unresolved
commands, changed identities and active owners still block recovery. This applies
to existing retained reservations after updating; no store edit is needed.

**PowerSync: Inspect Tesla EV recovery status** now includes `headroom_recovery`.
It reports the command route and read-only settlement route separately, whether
a sidecar hold exists, whether a matching reservation is loaded, and the exact
native-source selection verdict and matching-source count. It does not refresh
the vehicle or meter. `no_retained_history` describes the command journal; it does
not mean no headroom reservation exists. A `native_source_unavailable` response
also includes this projection under `preflight`; send that response if refused.

If the action returns `success: false`, keep the reservation and use the returned
`error` to choose the next step:

| Error | Meaning / next step |
|---|---|
| `provider_refresh_confirmation_required` | Enable Allow provider refresh for this attempt. |
| `reservation_not_found` | This VIN has no retained managed reservation; another guard may be blocking charging. |
| `reservation_scope_mismatch` | Check the VIN and PowerSync entry ID. |
| `native_provider_required` / `native_source_unavailable` | A uniquely identified, enabled Tesla Fleet or Teslemetry native source is required. BLE-only evidence cannot use this action. |
| `local_meter_unavailable` | The configured local Powerwall meter cannot be refreshed. |
| `site_busy` / `recovery_cooldown` | Let the current operation finish; wait one minute before another recovery attempt. |
| `recovery_scope_blocked` / `recovery_scope_changed` / `reservation_identity_changed` | Ownership, an unresolved recovery hold, or source/configuration changes prevent settlement. Resolve that cause; do not reset the store. |
| `fresh_readback_unavailable` | The provider/meter failed, timed out, or did not supply a usable fresh charge report. No wake is attempted. |
| `vehicle_not_stopped` | The charge report does not establish a stopped vehicle with zero current and power. |
| `settlement_not_confirmed` | The readings did not meet the existing timing/alignment and consistency checks. |
| `recovery_failed` | Recovery could not finish, including a possible persistence failure; retain the error response for support. |

No additional diagnostic log is required to try this action. If it cannot finish,
send the response and attempt time first; request further logs only for a specific
unanswered failure.
