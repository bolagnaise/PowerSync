# Native Sungrow iHomeManager battery control

The native iHomeManager battery driver is an implementation for evaluation,
not a released PowerSync connection option. Existing iHomeManager forwarding
connections remain telemetry-only and keep Monitoring Mode enforced. The driver
is not registered as a service, constructed during setup, or connected to the
optimizer. Adding this module does not enable hardware writes on existing sites.

## Protocol and scope

The driver implements the manufacturer's native system dispatch protocol at unit
247 on an explicitly configured plaintext port 503 or 504. It does not send SH
inverter commands through a forwarding connection. It was written independently
from Sungrow's protocol; no third-party integration code or UI is included.

Primary source: [Sungrow iHM Communication Protocol V1.0.2](https://www.photovoltaikforum.com/core/file-download/563585/).
The older [V1.0.1 protocol](https://www.photovoltaikforum.com/core/file-download/527824/)
describes the same dispatch block, but this driver permits control only when the
native device reports the documented V1.0.2 identity. Firmware version alone does
not establish the protocol revision.

Protocol addresses below are one-based; the driver subtracts one for Modbus:

| Register | Function |
| --- | --- |
| 8000–8004 | Device type, protocol number and protocol version (FC04) |
| 8024–8027 | EMS mode, charge/discharge command and low-word-first U32 power (FC16) |
| 8033 | VPP heartbeat timeout in seconds (FC03/FC16) |
| 8151–8163 | Battery limits, raw signed system powers and SOC (FC04) |

V1.0.2's version example contains wire bytes `02 00 01 00`, decoded as
low-word-first U32 `0x01000200`. Unknown versions remain readable but cannot
start dispatch. Battery power targets use 100 W units. Charge/discharge
requests round down, clamp to the live maximum, and reject targets below the
live minimum. Configured SOC boundaries are checked initially and on renewal.
Persistent battery reserves, export limits, charger settings and inverter
power-on/off settings are outside this driver's write surface.

## Dispatch lifecycle

The client is read-only unless its caller supplies a live `control_allowed`
callback. A future integration must evaluate Monitoring Mode, current entry
lifetime, authorization and external ownership in this callback. An exception or
anything other than `True` denies control. A callback alone is not a substitute
for planning with fresh, normalized telemetry.

Starting a session requires self-consumption mode, valid native identity, fresh
SOC/limits, and confirmed 20-second heartbeat readback. One FC16 request writes
VPP mode, direction and power together; all four words must read back before the
method reports success. The target is **battery power**, not a guaranteed grid
export rate. Existing site export limits remain active.

The client renews the heartbeat every 10 seconds, for at most the requested
finite duration (maximum 30 minutes). Each renewal checks authorization,
mode/command/target, timeout, fresh limits and SOC. Expiry, failed checks,
shutdown or lost communication stops renewal and attempts to restore
self-consumption. V1.0.2 documents return to self-consumption if the heartbeat
expires. A failed restore remains retryable and blocks session replacement.
The prior heartbeat timeout is restored only after the dispatch is safe.

The protocol has no owner token. Changed settings are treated as external
ownership and left alone; another writer using an identical target cannot be
distinguished. The eventual connection must be explicitly exclusive with native
Sungrow AI/VPP/other controllers. This limitation cannot be solved by readback.
No claim of physical battery export follows from acknowledgement or readback.

## Read-only compatibility capture

With Python 3.12 and `pymodbus` installed, run from this checkout:

```sh
python3.12 scripts/probe_ihomemanager.py --host LOCAL_IHOMEMANAGER_IP --port 503
```

The script constructs the default read-only client and prints identity, native
protocol version, live battery limits, SOC and raw system power. It does not
print the host address or enable control. Keep other clients off that TCP port
while capturing; iHomeManager allows one connection per port.

Capture the output alongside the inverter's contemporaneous battery direction,
grid direction and PV reading. No setting changes or device control tests are
needed for this capture.

## Remaining integration gate

Sungrow does not define positive/negative direction for the native signed power
registers. Aggregate inverter AC output is not a documented PV generation
reading. The driver deliberately returns raw powers and never labels inverter
AC output as solar generation. Before adding an optimizer/config-flow route:

- Confirm the target firmware's native protocol identity and battery limits.
- Reconcile native battery/grid directions with simultaneous inverter readings.
- Establish a reliable independent inverter PV telemetry source and ownership.
- Implement and validate the supported optimizer actions, unsupported idle and
  reserve behaviour, Monitoring Mode handoff and shutdown/restart recovery.
- Arrange any physical control test separately; this implementation has not
  issued commands to the reporter's system or established physical export.

Native AC22E-01 charger control and Tesla vehicle-side charging are separate
features. They are not implemented by this battery driver.
