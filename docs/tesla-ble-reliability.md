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
recovery by itself; the general command-history action also needs the complete producer reset and readback transaction.

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
native power reports; estimated power is excluded. Freshness alone does not add recovery. The optional yoziru recovery adapter
below supplies its own producer hold and correlated native samples. The upstream
PRs remain separate merge dependencies; the immutable owner-fork revisions can
be built without waiting for those merges.

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
- Which whole-inventory recovery action applies to Stop-only or mixed charging history.
- Whether the configured bridges expose the companion recovery protocol.

This action reads existing HA data only. It does not refresh telemetry, restart
bridges, change settings, clear journals or unlock control.

## Recovering uncertain charging commands

The companion firmware's protocol-2 whole-inventory transaction can now reconcile
Start, Stop, current and charge-limit history through
`power_sync.recover_ble_command_history`. The existing Stop-only action remains
available. See the [complete recovery procedure](tesla-ble-stop-recovery.md).

Recovery freezes writers, drains actual HA submissions, restarts the original
bridges into persistent read-only mode, obtains advancing native stopped/zero
reports from every declared vehicle, and saves the exact release before firmware
unlock. It preserves original unknown outcomes and never replays an old Start.
A failed read, changed identity, interrupted Stop or save failure retains the hold.

This is operational state reconciliation, not an OEM assertion that no historical
vehicle command can ever produce a delayed effect. It follows the stateful
command recovery model also used for native providers. Normal setting admission,
current regulation and command confirmation remain in force. Freshness firmware
alone does not implement this transaction; the Pedro companion is not compatible
with the yoziru family. Use the corresponding adapter below; an integration
update alone cannot add firmware recovery capability.

### Yoziru recovery adapter

Preserve the existing board, VIN, BLE address, device name, entity IDs and keys.
Replace the component source with the matching immutable revision:

```yaml
external_components:
  - source:
      type: git
      url: https://github.com/bolagnaise/esphome-tesla-ble
      ref: 1a807b0b8abe55452ebbbece3e7c68d28155ace6
    components: [tesla_ble_vehicle, tesla_ble_listener]

esp32:
  framework:
    components:
      - name: tesla-ble
        source: https://github.com/bolagnaise/tesla-ble-yoziru.git
        ref: 544a8fb2a35ea141946a640117b605fe17141cf3

packages:
  powersync_recovery: !include /config/esphome/stop-recovery-yoziru.yaml
```

Use `esphome/stop-recovery-yoziru.yaml` from the same PowerSync source as this
guide. It includes the paired freshness package; remove a separate freshness
include to avoid duplicate IDs. Keep the existing `tesla_ble_vehicle_id` used by
the client package. The companion creates the same HA proof/request entity names
and uses the same complete-inventory action as Pedro.

The component refuses recovery builds with an incompatible TeslaBLE library.
Its optional callback accepts only an active matching native request UUID;
unsolicited notifications and completed-request replay cannot renew recovery.
The original producers are frozen before reboot, the read-only hold survives
NVS, and firmware commit failure remains held. See
[component PR287](https://github.com/yoziru/esphome-tesla-ble/pull/287) and
[library PR126](https://github.com/yoziru/tesla-ble/pull/126).

ESP32 and ESP32-S3 builds with ESPHome 2026.10.2 pass. The source/build fixtures
use dummy identities and must not be uploaded to a real bridge. Build each
bridge from its own preserved configuration. No firmware upload or live recovery
is implied by those checks.
