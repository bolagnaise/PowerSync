# Tesla BLE charging freshness and confirmation

PowerSync needs real vehicle samples even when charging power stays constant.
Home Assistant can suppress equal ESPHome measurements, so an ordinary HA
`last_updated` timestamp cannot provide that guarantee. The optional packages
below publish native receipt timestamps and paired raw values on every valid
sample. No timer, requested current or calculated power renews them.

PowerSync still expires power after 90 seconds without fresh data. With a
`<sensor>_sample` entity, it also requires the raw value and displayed value to
agree before accepting that sample's timestamp. This handles either delivery
order between the two HA entities. Invalid samples, clocks and mismatched
filters produce unavailable evidence rather than a fabricated fresh reading.
Existing timestamp-only packages and older HA-timestamp installations remain
compatible; their capability is reported separately.

## Choose the firmware family

Preserve the existing device name, entity IDs, VIN, BLE address, board,
credentials, keys and pairing. Build from each bridge's own YAML. Do not flash a
sample configuration or a test binary: build fixtures use dummy identities.
Updating HACS alone cannot add the ESPHome entities below.

### PedroKTFC

Keep the existing `base.yml`, `common.yml`, `project.yml`, and `client.yml`
packages pinned to PedroKTFC commit
`651b4c25dcb24ce8d16d625a51868576e83bf2fe`. Add this package after them:

```yaml
packages:
  powersync_charging_freshness:
    url: https://github.com/bolagnaise/PowerSync/
    ref: v2.12.1652
    files:
      - esphome/charging-freshness-pedro.yaml
```

This family uses component keys `charge_state`, `charge_power` and
`charging_state`. It adds Charge level, Charge power and Charging state
`received` and `sample` entities. Keep their default entity names aligned with
the corresponding measurement names.

The PowerSync [Stop recovery companion](tesla-ble-stop-recovery.md) now includes
this package automatically. Use either the standalone freshness package or the
companion's inclusion. Replace a separate upstream `charging-freshness.yml`
include rather than stacking duplicate callbacks. Freshness does not grant
permission to recover an uncertain Start.

### yoziru

Use the native-power receipt implementation from
[upstream PR287](https://github.com/yoziru/esphome-tesla-ble/pull/287), refreshed
against upstream main. Its tested immutable component revision is
`ebb8aa7c9f50391a4e1f85010ca19220e0fbc6a5` in
`bolagnaise/esphome-tesla-ble`. Keep that family's package/API configuration;
it is incompatible with the Pedro recovery component.

Pin your existing external component source to that revision, preserving its
component list, then include PowerSync's package after yoziru's `common.yml`
and `client.yml`:

```yaml
packages:
  powersync_charging_freshness:
    url: https://github.com/bolagnaise/PowerSync/
    ref: v2.12.1652
    files:
      - esphome/charging-freshness-yoziru.yaml
```

This family uses `charger_power`, preserves `tesla_charger_power_sensor`, and
adds Charger Power Received and Charger Power Sample. It opts into repeated
native power reports; estimated power is excluded. It does not add the Pedro
Stop recovery protocol or reliable receipt timestamps for every other field.
The upstream PR remains a separate publication/merge dependency.

Both combined packages were compile checked for ESP32 and ESP32-S3 with
ESPHome 2026.10.2. Compilation is not installation or hardware verification.

## Command confirmation

After one charging write, PowerSync allows up to 35 seconds from service
handover for fresh readback. This covers the Pedro queue's existing 30-second
command ceiling plus a readback allowance; it is a finite observation window,
not a guarantee that every vehicle responds within it. A pre-write health
probe retains its separate 10-second budget.

The wait can press the exact same-device Force Data Update button at most
three times. It never repeats Start, Rate, Limit or Stop. It checks the original
owner, request deadline, producer metadata and entity bindings throughout.
A Stop, expired request or changed identity ends the wait. Start still needs
fresh Charging state plus actual current or native power; a switch echo alone
cannot confirm it. A timeout remains an uncertain outcome and preserves the
journal and safety hold.

## Inspect a hold without commands

Call `power_sync.get_ble_recovery_status` with response data enabled and, when
needed, the exact PowerSync `entry_id`. It reports:

- Each configured bridge's receipt capability and current power freshness.
- The retained attempt/hazard operations from the authoritative history union.
- Whether Stop-only recovery is a candidate requiring full inventory/proof.
- `vehicle_execution_fence_unproven` for positive or unknown retained history.

This action reads existing HA data only. It does not refresh telemetry, restart
bridges, change settings, clear journals or unlock control.

## General recovery remains an open requirement

The existing v2 action remains Stop-only. An uncertain Start, Rate or Limit
cannot be released merely by installing freshness firmware or extending the
confirmation window. A safe general recovery needs three separate facts:

1. All original producers and retry builders can no longer emit old work.
2. No old signed envelope can be accepted later.
3. Work already accepted by the vehicle has completed or been cancelled.

A persistent lock and verified bridge reboot address producer work. Tesla's
[protocol specification](https://github.com/teslamotors/vehicle-command/blob/main/pkg/protocol/protocol.md)
allows out-of-order infotainment counters. A higher-counter Stop is therefore
not a general cancellation watermark. The pinned Pedro library's five-second
expiry uses a wall-clock/session offset; it does not retrospectively establish
a real five-second bound for an older uncaptured command.

A fresh authenticated epoch change can invalidate old envelopes. The reference
[verifier](https://github.com/teslamotors/vehicle-command/blob/main/internal/authentication/verifier.go)
can rotate epochs without an application reset, so epoch change alone does not
prove cancellation of already accepted charging work. Public APIs reviewed do
not establish that execution fence. This is an evidence gap, not an observed
case of a Tesla executing indefinitely delayed work.

To complete general recovery, qualify an OEM-supported execution-reset/drain
procedure or a supported acceptance-to-final-effect bound for the relevant
vehicle/software profile. The evidence must cover forwarded and persistent
work, preserve pairing, identify completion and specify surviving settings.
Then bind that evidence to the original producer inventory, reconcile fresh
native settings/zero draw, durably retire covered liabilities, and unlock the
same recovery generation. An operator checkbox, a new Stop acknowledgement,
repeated zeros, a touchscreen reboot or unplugging alone cannot supply the
missing contract. Preserve existing holds until it is established.
