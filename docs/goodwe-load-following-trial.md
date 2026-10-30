# GoodWe EHA-G20 supervised load-following trial

Version: **0.1.0-beta.1**. Experimental test tool, not automatic Home Assistant control.

## Goal and scope

Test native on-grid load following on **GW9.999K-EHA-G20** by requesting
GoodWe's regional EMS **BuyPower** mode with a **0 W grid-import target**.
The inverter should balance against its site meter, retain solar supply to loads
and battery charging, and limit excess PV. This is the behavior to test, not a
promise that this model/firmware implements the protocol as described.

The owner authorized this beta implementation on 30 October 2026 in the linked
[ticket #29](https://discord.com/channels/1443943629130567874/1543802186587185245)
chat follow-up. It changes no installed HA configuration and is not part of the
automatic optimizer. The first version is a supervised, standalone LAN tool.
An HA control option comes after a successful command/physical/recovery test.

## Why this experiment

The existing PowerSync hybrid controller enables 47509 and requests a 0 W export
limit at 47510. Earlier ticket findings report correct readback but continued
export. Repeating that sequence with a different label does not test a new
mechanism.

GoodWe's `Third-Party EMS Protocol MODBUS V1.1.1`, Table 1-4, documents BuyPower
mode **9**. With working meter communication it controls purchased grid power
to the requested target, limits excess PV and permits battery balancing. The
protocol lists ESA support for EMS mode **47511** and the setpoint at **47512**.
It also lists the alternative 42000 bank, but its fallback description refers
specifically to 47511, so this trial uses 47511/47512 only. All writes use the
protocol's documented function 0x10. This gives an experimental control surface distinct
from the existing export limiter. It does not establish support on firmware
010101 or prove that the installed meter measures the whole site.

Source: [protocol attachment](https://discord.com/channels/1443943629130567874/1543802186587185245/1544977317644734504),
pages 20–21 and 26–27. The [earlier research plan](goodwe-eha-load-following-plan.md)
records the existing zero-export evidence and Amber comparison.

Do not use import/export EMS modes 4/5 for this trial: their documented behavior
excludes grid-side load. Do not change Off-grid mode, fallback mode, grid code,
meter settings, export limits or battery protection settings.

## Implementation contract

1. **Read-only by default.** Inspection cannot write to the inverter. A live
   trial requires explicit execution, sole-control and whole-site-meter flags.
2. **Identify and validate.** Read the actual model and serial; permit only the
   exact supported model. Require on-grid work mode and self-use EMS mode before
   takeover, and a valid battery SOC at or above the trial floor (95% by default).
   Read original EMS mode/target and fallback before any write. Reject
   shutdown fallback rather than changing it. Missing or rejected reads stop
   the test.
3. **Make recovery durable.** Save the endpoint, hashed device identity, original
   mode/target and pending recovery state to a journal before the first write.
   Lock the endpoint against overlapping local runs. An unresolved journal must
   be recovered rather than replaced by a new trial.
4. **Apply a fixed native target.** Write target 0, then mode 9. Read back the
   actual values. Refresh the owned mode on the protocol's one-second cadence
   during a bounded 30–300 second trial. No adjustable target, arbitrary register
   writes or automatic alternate-bank probing is exposed.
5. **Observe rather than declare success.** Capture timestamped baseline,
   active and recovery telemetry. Stop on communication or ownership failure,
   or SOC falling below the trial floor.
   Record raw meter power separately from any assumed import/export sign.
6. **Restore and verify.** Return to saved self-use mode first, then restore the
   saved target. Attempt recovery after normal completion, interrupt, partial
   command failure and readback failure. Refuse to overwrite an unexpected
   foreign mode or target. Failed recovery leaves a durable recovery-required
   state and a nonzero result; it must not be reported as a successful trial.

The journal cannot automatically undo a setting after computer power loss,
SIGKILL or loss of the inverter connection. In particular, the existing
fallback may retain the last EMS mode. Keep the supervising computer awake and
connected. The recovery command and the installer's normal recovery method must
be available before a live trial. A one-second software loop is not a guaranteed
hardware watchdog.

## Running the test version

Use Python **3.9 or later on Linux or macOS**, on a computer on the inverter's
LAN. The script uses only Python's standard library. Keep the script, journal
and capture files together in a permanent local directory. Replace
`INVERTER_IP` with the existing working address; use the established port and
unit ID if different from TCP 502 and unit 247.

First run the read-only inspection:

```sh
python3 scripts/goodwe_load_following_trial.py --host INVERTER_IP inspect
```

The command also defaults to inspection when no subcommand is given. Review
the model, current modes, fallback and telemetry. Missing/unsupported reads are
an inspection failure; do not substitute a different register map or enable a
write flag to bypass them. A ready inspection does not itself verify the meter
layout, sole control or physical capability.

After the test conditions below are satisfied, the first write trial is:

```sh
python3 scripts/goodwe_load_following_trial.py --host INVERTER_IP \
  --journal goodwe-trial-01.journal.json run --duration 30 \
  --log goodwe-trial-01.jsonl --execute --exclusive-control \
  --whole-site-meter-confirmed
```

The default SOC floor is 95%. Stop with Ctrl+C if needed and allow restoration
to finish. The journal must end in `restored`; save both files for review.
A completed run deliberately reports `physical_success_claimed: false`.

If restoration was interrupted or is unconfirmed, keep other controllers
paused, retain the journal and run supervised recovery on the same endpoint:

```sh
python3 scripts/goodwe_load_following_trial.py --host INVERTER_IP \
  --journal goodwe-trial-01.journal.json recover
```

Recovery validates the saved device identity before restoring settings. If it
refuses because the device is now in an unexpected mode or target, inspect the
inverter and use the agreed installer recovery procedure; do not edit the
journal or retry through a different mode. A new trial requires a new journal
filename. Do not delete an unresolved journal to start another run.

Only after reviewing the first trace and confirmed recovery, repeat with
`--duration 120` and a new journal/log pair to observe an ordinary household
load change. Do not add continuous scheduling at this stage.

## Test conditions and interpretation

Before a live run, pause SmartShift and every other writer, place PowerSync in
Monitoring Mode, disable its automatic curtailment, and pause other GoodWe write
automations. Keep telemetry enabled. The explicit sole-control flag records
the operator's confirmation; it cannot discover or disable a cloud controller.
Modbus has no compare-and-set or external ownership lock: a change between a
read and a write can race this tool, and another writer choosing the same
mode/target cannot be detected. Pausing the other writers is a required test
condition, not a guarantee supplied by the local process lock.
Recovery restores only the EMS fields this tool changes. Work mode, fallback
and export-limit readings are observations/guards; the tool will not overwrite
changes another controller makes to those settings.

Confirm with the installer/Amber that this meter arrangement measures whole-site
grid exchange. An Online Meter 1 tile alone does not prove that. Use steady
sunlight, a naturally full or nearly full battery, and clear surplus generation.
Record any separate whole-site meter's sign convention and the installed
inverter firmware. Do not begin the write trial until inspection succeeds and
the settings and recovery instructions have been reviewed.

A useful result has all of the following:

- New device readback confirms mode 9 and target 0.
- Across multiple fresh samples, site export falls near zero while PV remains
  above zero and supplies demand. Inspect battery power so battery charging
  cannot masquerade as curtailment.
- During an ordinary safe household load change, PV follows demand without
  material sustained export. This distinguishes tracking from a one-time drop.
- Original EMS settings read back after restoration, and PV/export recover while
  sunlight remains available.

Readback alone proves only retained settings. Low export alone can also result
from clouds or increased demand. Continued export with correct readback is a
negative/inconclusive capability result; it must not trigger retries through
undocumented modes or register banks.

## Validation and next iteration

Use fake device state and a local simulated Modbus TCP server for protocol,
failure and recovery regressions. Cover wrong device, missing baseline,
unsupported modes, rejected writes, ambiguous acknowledgements, partial writes,
interrupts, changed ownership, recovery failures, tampered journals and duplicate
runs. Run the new narrow suite, then the existing GoodWe controller and status
tests. No real device command is part of software validation.

After the supervised trial, compare its JSONL capture and journal with the
physical meter trace. If it works and recovery is verified, add an explicitly
opt-in HA capability using the same verified sequence, shared ownership fences,
persisted restore state, tariff eligibility and honest Pending/Active status.
An HA release must follow the current full-suite regression release gate;
publication, installation and physical validation remain separate steps.
