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

Non-hybrid GoodWe solar-only models such as DT/MS/XS do not support PowerSync battery control.

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

## Setup checklist

1. Enable Modbus TCP on the GoodWe inverter or LAN / WiFiLAN Kit-20 module.
2. Assign a stable local IP address to the inverter or communication module.
3. If using LAN / Kit-20, install and configure the GoodWe Experimental Home Assistant integration.
4. Confirm Home Assistant has the EMS mode and EMS power limit entities.
5. In PowerSync, select **TCP / LAN Kit-20**, set port **502**, and enter the entity mode prefix.

## Troubleshooting

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
