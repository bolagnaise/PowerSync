# Recover native Tesla charging after a lost response

A Fleet, Teslemetry or Tessie timeout can occur before or after a command executes.
PowerSync keeps the original attempt as `sent_unknown`. It now separates that
historical uncertainty from the operational decision to resume charging control.

## Automatic recovery

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

## Read-only inspection

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
[Tesla BLE recovery](tesla-ble-stop-recovery.md). Reconciliation never adopts another
provider, vehicle, disabled entity or changed pairing to escape a retained hold.
