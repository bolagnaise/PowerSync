# Recovering held Stops on a BLE-only Tesla bridge

PowerSync can recover **Stop-only** retained history without Tesla Fleet or
Teslemetry when every affected ESPHome bridge runs the companion recovery
component below. This is an operator-triggered recovery; updating PowerSync
alone does not clear a held command.
It does not establish why an earlier Stop went unconfirmed or guarantee that
the original communication failure will not recur.

This route covers exact original bridge/device/entity bindings and an explicitly
confirmed complete vehicle inventory. It refuses Start, Rate, Limit, unknown
operations, conflicting identity, omitted vehicles and changed pairing. A legacy
bridge alias remains a journal alias; the VIN you supply is a declaration of the
physical vehicle, not reconstructed historical evidence.

Do not use this procedure if a bridge was moved between vehicles during the held
history, any affected vehicle/producer is missing, or the historical inventory is
uncertain. Do not delete recovery files, re-pair keys, change VIN mappings or
manually toggle the recovery request entity. Preserve a backup of the current
HA recovery files, bridge YAML and working firmware before updating.
Another held recovery incident in the same PowerSync entry also requires
support review before this whole-entry recovery can proceed.

## Install the companion component

Keep EV automations and other writers paused while installing and recovering.
The vehicle must be available to BLE and already report stopped, complete,
disconnected or no-power, with actual current and power both zero. Recovery
reads vehicle state; it does not stop an actively charging vehicle for you.

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
  tesla_ble_component: github://bolagnaise/PowerSync@v2.12.1641
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
    ref: v2.12.1641
    files:
      - esphome/stop-recovery.yaml
```

The companion component is separately licensed under AGPL-3.0; see
[its source and notice](../esphome/NOTICE.md). Existing upstream binaries do not
provide this protocol. The pinned packages preserve the upstream controls; the
recovery lock temporarily rejects vehicle mutations, including pairing/key
changes, while it collects proof.

## Confirm the entire affected inventory

In HA **Developer tools → Actions**, run `power_sync.recover_ble_stop_history`
using YAML mode. Replace the example prefixes and VINs with your actual values.
The prefix is the existing entity prefix, without `switch.` or `_charger`.
Include **every affected bridge and vehicle** in this entry in one call.

```yaml
action: power_sync.recover_ble_stop_history
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
unknown Stop outcomes remain unknown in history. Recovery does not replay a
saved Start or claim that an earlier Stop succeeded. Normal automation can issue
new commands after successful recovery; resume paused automations deliberately.

## If recovery is interrupted or refused

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

Rollback: keep held bridges and EV automations contained and contact support
before restoring older firmware. A downgrade removes the companion firmware's
persistent read-only enforcement; it is not a recovery method. No customer
firmware installation, charging command or live recovery is performed by the
release itself.
