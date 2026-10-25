# Native Sungrow iHomeManager battery control

The **iHomeManager battery control + inverter telemetry** connection pairs the
existing Sungrow SH inverter readings with native iHomeManager battery commands.
It supports price-driven battery charging and discharging, manual force modes,
and return to self-consumption. Available from **v2.12.1508**. Software validation has passed; physical battery
behaviour has not yet been verified on the reporter's hardware.

Existing **iHomeManager forwarding — monitoring only** connections keep their
current read-only behaviour. Updating does not migrate or enable control on an
existing site. Native AC22E-01 charger control is outside this feature.

## Setup

Select **iHomeManager battery control + inverter telemetry** in the Sungrow
connection form or battery connection profile. Configure both endpoints:

| Field | Use |
| --- | --- |
| Inverter IP address, Modbus port and slave ID | Existing SH inverter / WiNet-S telemetry, normally port 502 and unit 1 |
| iHomeManager control IP address | The separate native iHomeManager endpoint |
| iHomeManager plaintext control port | Configured non-SSL port 503 or 504; native unit is fixed at 247 |

Both hosts must be explicit. The same host is allowed with different TCP ports;
the same host/port pair is rejected. Setup checks both connections using reads
only, including the native device identity and protocol revision. It does not
send a control test. A failed check leaves the old settings intact. Changing
an active route first restores its existing control before saving the change.

A newly selected native connection starts in **Monitoring Mode**. Disable
Sungrow AI/VPP or other battery controllers before turning Monitoring Mode off.
Native control requires protocol **V1.0.2**; the firmware string alone does not
identify the protocol revision. Unsupported identities remain blocked.

No reporter-provided capture is required to implement or select this connection:
the software verifies the documented native identity itself and uses the
established inverter telemetry map. A separate capture remains useful for
troubleshooting and physical validation.

## Supported behaviour and limits

- Charge and discharge targets are battery watts, rounded down to 100 W units
  and bounded by live native minimum/maximum limits and configured SOC guards.
- A zero target in the existing force-mode API uses the fresh native maximum;
  normal explicit optimiser watt targets remain supported.
- Requests last for their specified duration, up to 24 hours, and depend on
  continued heartbeat renewal. Returning to normal releases the native session.
- Existing hardware reserves and site export limits are retained. A battery
  discharge request does not guarantee a particular amount of grid export.
- Native protocol V1.0.2 does not document the inverter's zero-discharge-cap
  primitive. **No Idle** is therefore effective for this connection: Idle plan
  slots become self-consumption. Native reserve writes, rate-limit writes,
  spread export and battery solar curtailment return unsupported rather than
  issuing SH inverter register writes. The SH telemetry client also blocks
  writes independently at its transport boundary.

Solar, grid, battery, load and SOC for planning use the existing SH telemetry
path. All required live power readings must be present and finite, including
actual PV telemetry; missing, stale or failed readings block control. Native
signed power readings are kept raw and are not used to guess power direction or
label aggregate inverter AC output as PV generation.

## Dispatch and recovery

The client is read-only without its live permission callback. Native dispatch
requires a current entry, matching routes/endpoints, Monitoring Mode off, no
monitoring handoff, fresh complete inverter/native snapshots and valid battery
SOC/limits. Optimiser leases also require the optimiser to remain enabled and
its licence generation to remain valid. Manual control remains separate from
optimiser enablement.

Starting a session requires self-consumption mode. The driver verifies a
20-second heartbeat timeout, then writes VPP mode, direction and power in one
FC16 block. All command words must read back before success is returned.
Heartbeat renewal occurs every 10 seconds and repeats the permission, ownership,
SOC and power-limit checks. The driver stops renewal and attempts cleanup on
expiry, failed checks, connection loss, cancellation or shutdown.

Sungrow V1.0.2 documents automatic return to self-consumption when the heartbeat
expires. Orderly cleanup restores self-consumption and the prior heartbeat
setting. Failed cleanup remains retryable; another session cannot discard it.
After an abrupt process restart, no old session is renewed or taken over: the
existing VPP session must expire back to self-consumption before a new dispatch
can start. The shortened heartbeat setting can remain after such an abrupt
restart because its pre-crash baseline was held in memory.

The protocol exposes no owner token. Changed command settings are treated as
external ownership and left alone, including another controller's changed
self-consumption command. Another writer using an identical target cannot be
distinguished, so the connection must have exclusive battery-control ownership.
Acknowledgement and readback are software proof, not proof of physical export.

## Read-only compatibility capture

With Python 3.12 and `pymodbus` installed, run from this checkout:

```sh
python3.12 scripts/probe_ihomemanager.py --host LOCAL_IHOMEMANAGER_IP --port 503
```

The probe prints identity, protocol revision, battery limits, SOC and raw powers.
It does not print the host address or enable control. Only one client can use an
iHomeManager TCP port at a time. For physical validation, compare a timestamped
capture with contemporaneous inverter battery/grid directions and PV readings;
any actual control test needs separate authorization.

## Protocol source

Written independently from the manufacturer's
[iHM Communication Protocol V1.0.2](https://www.photovoltaikforum.com/core/file-download/563585/).
No third-party integration code or UI is included. Protocol addresses are
one-based; the driver subtracts one for Modbus. U32/S32 values use low-word-first
order. V1.0.2's wire version bytes `02 00 01 00` decode to `0x01000200`.

| Protocol registers | Function |
| --- | --- |
| 8000–8004 | Device type, protocol number and version (FC04) |
| 8024–8027 | EMS mode, direction and U32 battery power (FC03/FC16) |
| 8033 | VPP heartbeat timeout in seconds (FC03/FC16) |
| 8151–8163 | Battery limits, raw system powers and SOC (FC04) |
