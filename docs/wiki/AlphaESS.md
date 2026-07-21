# AlphaESS

PowerSync supports AlphaESS SMILE and Storion hybrid inverter-battery systems via Modbus TCP. The AlphaESS Cloud API can supplement Modbus telemetry or run as a monitoring-only connection when local Modbus is unavailable.

## Supported models

| Model | Description |
|---|---|
| SMILE5 | Single Phase Hybrid |
| SMILE-Hi5 | Single Phase Hybrid |
| SMILE-Hi10 | Three Phase Hybrid |
| SMILE-B3 | Three Phase Hybrid |
| SMILE-T10 | Three Phase Hybrid |
| SMILE-G3 | Three Phase Hybrid |
| Storion-T30 | Three Phase Hybrid |

All models use the same Modbus register map and control logic.

## Prerequisites

### Enable Modbus TCP on the inverter

Modbus TCP must be enabled on the inverter before PowerSync can connect. This is done in the AlphaESS app or via the local web interface depending on your firmware version.

- Open the AlphaESS app → **Settings** → **Communication** → enable **Modbus TCP**
- Note the inverter's local IP address — assign a static IP (or DHCP reservation) so it does not change

### Enable Modbus curtailment in firmware (required for DC curtailment)

> **This is the most common reason curtailment does not work on Smile5 systems.**

AlphaESS firmware has a separate "Modbus curtailment" feature that must be enabled before the inverter will physically honour export-limit commands sent over Modbus. Without it, PowerSync can write the export-limit register (0x0800) and the value will appear to change, but the inverter will not throttle PV output.

To enable it:
1. Open the AlphaESS app
2. Go to **Settings** → **Grid** → **Export Limit** (label varies by firmware version)
3. Set the export limit mode to **Modbus** or enable **External Control**
4. Save and confirm

If you cannot find this setting, contact AlphaESS support or check your firmware release notes. Some older firmware versions do not support Modbus-controlled export limiting at all — a firmware upgrade may be required.

---

## Setup in PowerSync

### 1. Choose a connection type

- **Modbus control with optional cloud fallback** is the default and preserves the existing full-control setup.
- **AlphaESS Cloud monitoring only** needs no inverter IP address. It supplies solar, grid, battery power and battery SOC for PowerSync planning and dashboards, but it cannot execute battery modes or solar curtailment. PowerSync therefore forces monitoring mode for this connection.

### 2. Modbus TCP connection

During initial setup, select **AlphaESS** as your battery system and enter:

| Field | Description | Default |
|---|---|---|
| IP address | Local IP of the inverter | — |
| Port | Modbus TCP port | 502 |
| Slave ID | Modbus unit ID | 85 (0x55) |
| Export limit (kW) | Safety cap on grid export | Unlimited |
| Enable DC curtailment | Turn on zero-export curtailment | Off |

**Slave ID:** The AlphaESS factory default is **85 (0x55)**. This is different from most other inverters (which default to 1). Only change it if you have altered the inverter's Modbus configuration.

**Export limit:** Optional hard cap in kW. PowerSync will never request more than this amount of export regardless of price. Leave blank for no cap.

### 3. DC curtailment toggle

The **Enable DC curtailment** toggle activates zero-export curtailment. When enabled, PowerSync monitors feed-in prices and writes 0% to the AlphaESS export-limit register (0x0800) when export is uneconomical (feed-in price below 1 c/kWh). It restores normal export when prices recover.

**This toggle has no effect unless Modbus curtailment is enabled in the inverter firmware first** (see [Prerequisites](#prerequisites)).

### 4. Cloud API

The AlphaESS Cloud API provides either fallback telemetry for a Modbus setup or the only telemetry source in cloud-only mode. It does not provide any control capability — all force charge, force discharge, and curtailment commands require Modbus.

To use it:
1. Log in at [open.alphaess.com](https://open.alphaess.com)
2. Go to **API Management** and create an application to get an **App ID** and **App Secret**
3. Enter these in the Cloud API step of PowerSync setup
4. Enter your inverter's serial number if prompted

Cloud credentials are required in cloud-only mode. For a Modbus setup, leave them blank to skip the fallback.

---

## How curtailment works

When curtailment is triggered, PowerSync:

1. Releases any active force-charge or force-discharge dispatch (register 0x0880 = 0) — active dispatch overrides the export-limit register on Smile firmware and must be cleared first
2. Writes 0% to the export-limit register (0x0800)
3. The inverter firmware enforces zero grid export — solar continues to power the home and charge the battery, only grid feed-in is blocked

When curtailment is lifted, PowerSync restores 0x0800 to the previously stored value (or 100% if none was stored).

### Limitations

- Curtailment is DC-coupled PV behind the AlphaESS MPPT. PowerSync has no direct PV power setpoint — it relies on the AlphaESS firmware to enforce the export limit. If the firmware ignores the register, PV output is unaffected.
- Some firmware versions acknowledge the Modbus write but do not enforce the value. If you write 0% and export continues unchanged, the most likely cause is that Modbus curtailment is not enabled in the firmware settings.
- Mode 7 (Maximise Consumption) is documented to physically disable PV output on some hardware, but it also forces grid charging and is not currently used by PowerSync as it changes battery behavior in ways that conflict with LP optimizer decisions.

---

## Force charge / force discharge

The LP optimizer and manual force-mode services control the battery via the AlphaESS dispatch block (registers 0x0880–0x0888), using **Mode 2 (State of Charge Control)**:

- **Force charge:** charges from grid at the requested power rate until the target SoC is reached or the duration expires
- **Force discharge:** discharges to grid at the requested power rate until the floor SoC is reached or the duration expires

The inverter auto-stops when the dispatch timer elapses, so a lost connection will not leave the battery permanently locked in a forced mode.

---

## Troubleshooting

### Modbus connection fails

- Confirm Modbus TCP is enabled in the AlphaESS app
- Confirm the IP address is correct and reachable from Home Assistant
- Try slave ID 85 (0x55) — this is the AlphaESS default and differs from most other inverters
- Check that nothing else on the network is connected to port 502 on the inverter at the same time (AlphaESS typically allows only one Modbus TCP client)

### Curtailment enabled but export continues

1. **Check firmware setting first** — Modbus curtailment must be enabled in the AlphaESS app under Settings → Grid → Export Limit. This is the most common cause.
2. Check the PowerSync logs for `AlphaESS export limit set to 0%` — if this does not appear, the write is not being attempted
3. Check for `AlphaESS curtail: releasing active dispatch` in logs — if dispatch release is failing, the export register may not be evaluated
4. Read back register 0x0800 using a Modbus tool to confirm the write sticks
5. If the register sticks at 0% but export continues, the inverter firmware is not enforcing it — contact AlphaESS support about enabling Modbus export control for your firmware version

### Battery not responding to force charge / force discharge

- Confirm the battery SOC is not already at the target level
- Check logs for `AlphaESS dispatch CHARGE/DISCHARGE` — confirms the full dispatch block was written
- Verify the inverter is not in a fault or protection state (check the AlphaESS app)
