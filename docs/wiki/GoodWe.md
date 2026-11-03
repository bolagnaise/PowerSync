# GoodWe

PowerSync supports GoodWe ET/EH/BT/BH and ES/EM/BP hybrid inverter-battery systems through two control paths:

- **Direct GoodWe control** uses the GoodWe local protocol, typically UDP port 8899. This path controls force charge/discharge by writing GoodWe operation modes directly.
- **Entity mode** uses the GoodWe Experimental Home Assistant integration's EMS entities. This is the required path for LAN / WiFiLAN Kit-20 setups that expose Modbus TCP on port 502 but do not support UDP control.

## Supported models

| Model family | Notes |
|---|---|
| ET / EH | Three-phase hybrid systems |
| BT / BH | Three-phase battery inverter systems |
| ES / EM / BP | Single-phase hybrid or battery inverter systems |

Non-hybrid GoodWe solar-only models such as DT/MS/XS do not support PowerSync battery control. A standalone MS inverter can nevertheless be added under **AC-coupled inverter curtailment** through GoodWe Experimental entities.

## Standalone MS AC inverter

For an AC-coupled GoodWe MS that is separate from the site's battery inverter:

1. Configure the inverter in the GoodWe Experimental Home Assistant integration.
2. In PowerSync, open **AC-coupled inverter curtailment**, choose **GoodWe (Home Assistant entities)** and enter the common entity prefix. For `sensor.goodwe_pv_power`, enter `goodwe`.
3. PowerSync validates these writable controls before saving:
   - `number.<prefix>_grid_export_limit`
   - `switch.<prefix>_grid_export_limit_switch`
4. PowerSync reads `sensor.<prefix>_pv_power`, optional `pv1_power` through `pv3_power`, and `today_s_pv_generation` for status and daily production.

Curtailment saves the exact prior number and switch values, sets the export allowance to 0 W, verifies both entity states, and rolls back if either write fails. Restore reapplies the saved values; the snapshot is persisted so an interrupted Home Assistant restart can also recover it.

## Connection modes

### Direct UDP control

Use this when the inverter or dongle supports GoodWe local UDP control:

| Field | Value |
|---|---|
| Protocol | UDP direct control |
| Port | 8899 |
| GoodWe entity mode prefix | Leave blank |

If force charge/discharge fails with a message like `expected work_mode=3 but read back 0`, UDP control is not taking effect. For LAN / Kit-20 systems, use entity mode instead of trying to force UDP. PowerSync will automatically use a detected EMS entity pair for TCP / LAN Kit-20 setups, even if the control selector was left on Direct IP control.

### TCP / LAN Kit-20 with entity mode

Use this when the inverter is connected through LAN or WiFiLAN Kit-20 and Modbus TCP is enabled:

| Field | Value |
|---|---|
| Protocol | TCP / LAN Kit-20 |
| Port | 502 |
| GoodWe entity mode prefix | The prefix from the GoodWe Experimental HA EMS entities |

Entity mode requires the GoodWe Experimental Home Assistant integration to expose both EMS controls:

| Required entity | Example when prefix is `goodwe` |
|---|---|
| EMS mode select | `select.goodwe_ems_mode` |
| EMS power limit number | `number.goodwe_ems_power_limit` |

In PowerSync, enter only the prefix. For the examples above, enter:

```text
goodwe
```

Do not enter the full entity ID.

When the prefix is set, PowerSync routes force charge/discharge through those Home Assistant EMS entities:

- Force charge prefers EMS mode `charge_pv`, which keeps PV contributing first and uses grid power for the shortfall; if unavailable, PowerSync falls back to `charge_battery`
- Force discharge prefers EMS mode `discharge_battery`, which targets battery discharge power directly
- Hold SoC uses EMS mode `conserve`, which blocks on-grid battery discharge but still allows excess solar to charge the battery
- If the GoodWe HA integration does not expose the preferred EMS modes, PowerSync falls back to the older supported modes where available
- Restore normal sets EMS mode to `auto`
- Requested power is sent to the EMS power limit entity

Telemetry, backup reserve, and export-limit operations still use PowerSync's GoodWe connection.
Hold SoC requires the EMS entity path; PowerSync does not issue an unverified direct-UDP hold command.

## Solar curtailment method (direct control)

Direct GoodWe control curtails uneconomic export in one of two ways. Choose it
under **GoodWe connection** → **Solar curtailment method**. Saving reloads the
integration.

- **Export limit 0 W (default).** Enables the export limiter (47509) and sets
  a 0 W limit (47510), then restores the saved limiter values.
- **EMS load following (BuyPower 0 W).** Puts the inverter in GoodWe's EMS
  BuyPower mode (47511 = 9) with a 0 W grid set point (47512). The inverter
  balances against its own meter: PV keeps supplying the house and charging the
  battery, surplus PV is reduced, and the battery covers any shortfall. Release
  restores the saved EMS mode and set point. This is GoodWe's documented
  third-party EMS mode (Third-Party EMS Protocol MODBUS V1.1.1, Table 1-4). It
  does not change the export limiter, Off-grid mode, fallback or meter settings.

Use EMS load following when the export limit is accepted and read back but the
inverter keeps exporting once the battery is full. This has been seen on the
ESA/EHA-G20. Requirements and safeguards:

- A working GoodWe meter on the grid connection. BuyPower controls to the meter
  reading.
- EMS fallback (47505) must read **10**, the factory default: keep the last
  mode. PowerSync refuses to start load following if it reads another value or
  cannot be read. It never changes the fallback.
- If the inverter was already left in BuyPower 0 W, or its EMS mode was never
  set, release returns it to self-use (EMS mode 1).
- If SEMS, an EMS entity command or a force mode has already replaced the hold,
  PowerSync releases its claim without overwriting that setting.
- Force charge and force discharge release the hold first. Curtailment does
  not resume until the force mode ends.
- An interrupted release is persisted across restarts and retried with the
  saved EMS baseline, even if you have since switched methods.

The card still reports **Active** only after fresh telemetry shows export below
the 250 W tolerance.

## Whole-site solar for AC-coupled arrays

If the property has solar on both the GoodWe hybrid and a separate AC-coupled
array, select **Whole-site solar power sensor (all PV)** on **Battery connection
method & sensors** during setup. Choose a sensor reporting the total instantaneous
production of all arrays, including the GoodWe PV strings. The sensor can use W,
kW or MW. Leave the field empty when all solar is connected to the GoodWe.

Existing installations can select, replace or clear the sensor through the
**Battery connection method & sensors** options form or the direct GoodWe
connection options. Saving reloads the integration and applies the selection to
both direct and Home Assistant entity telemetry.

PowerSync uses the whole-site reading, with the inverter's own PV as a lower
bound, and calculates household load from solar, grid and battery power. This
also handles negative inverter house-consumption readings that can occur with
AC-coupled generation. Corrected readings feed solar nowcasting, forecast
learning and daily energy accumulation.

If the selected sensor is missing, unavailable, non-numeric, non-finite or
negative, the displayed readings remain inverter-only. PowerSync marks the solar
sample invalid, skips solar nowcasting and forecast learning for that sample,
and skips its daily energy accumulation. Clearing the optional field restores
the normal inverter-only telemetry.

## Setup checklist

1. Enable Modbus TCP on the GoodWe inverter or LAN / WiFiLAN Kit-20 module.
2. Assign a stable local IP address to the inverter or communication module.
3. If using LAN / Kit-20, install and configure the GoodWe Experimental Home Assistant integration.
4. Confirm Home Assistant has the EMS mode and EMS power limit entities.
5. In PowerSync, select **TCP / LAN Kit-20**, set port **502**, and enter the entity mode prefix.

## Troubleshooting

### DC curtailment remains Pending

For direct GoodWe control, PowerSync verifies the export-limit setting and
requires fresh site telemetry below the 250 W export tolerance before reporting
Active. An accepted zero-watt setting can still remain Pending if the inverter
continues exporting. Entity-only battery profiles without an export-limit
actuator also remain Pending.

Automatic price and timer checks run one GoodWe curtailment or restore transition
at a time. A newer price callback waits for the current transition and then
evaluates the updated state. An interrupted write remains Pending and keeps its
retry backoff.

For an ESA still exporting after a verified limit, switch **Solar curtailment
method** to **EMS load following (BuyPower 0 W)**. If export continues, check the
native meter/CT selection and grid-connected power-limit configuration with the
installer.
PowerSync does not expose GoodWe Off-grid mode as an automatic curtailment
fallback.

### Force charge/discharge does not work on LAN / Kit-20

- Confirm PowerSync is using TCP port 502.
- Confirm the GoodWe Experimental integration can force charge/discharge from Home Assistant.
- Confirm the entity mode prefix matches the entity IDs. For `select.goodwe_ems_mode`, the prefix is `goodwe`.
- Check Home Assistant logs for `GoodWe EMS control`.

### Error says UDP port 8899 is required

That message means PowerSync is still using direct operation-mode control instead of entity mode. LAN / Kit-20 setups commonly support Modbus TCP only, so configure the GoodWe entity mode prefix and retry.

### Entity prefix validation fails

PowerSync checks for both required entities before saving the prefix. If validation fails:

- Make sure the GoodWe Experimental integration is installed and loaded.
- Search Home Assistant entities for `_ems_mode` and `_ems_power_limit`.
- Enter only the common prefix, not the full entity ID.

### Port changes back or connection fails

For TCP / LAN Kit-20, the port should be 502. For direct UDP control, the port should be 8899. If you change the protocol in PowerSync, confirm the port matches the selected path before saving.
