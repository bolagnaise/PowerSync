# GoodWe EHA-G20 load-following feasibility and implementation plan

Reviewed 30 October 2026 for [ticket #29](https://discord.com/channels/1443943629130567874/1543802186587185245).

Follow-up: the owner subsequently authorized a supervised beta implementation.
See [the native BuyPower trial plan and operating guide](goodwe-load-following-trial.md).
This research describes the earlier evidence boundary; the trial tests an
additional documented EMS mechanism without claiming verified hardware behavior.

## Decision

Native on-grid load following is feasible for the ESA/EHA-G20 product family,
subject to the inverter receiving the correct site measurement and supporting
the requested control on its installed firmware. This installation is not yet
verified. Investigation outcome: **evidence-gated**.

The request is to retain solar supply to household loads and battery charging
while avoiding uneconomic grid exports. PowerSync's existing direct GoodWe
hybrid controller already requests the documented native zero-export function.
There is no demonstrated additional local actuator to implement yet. No new
hardware-control behavior or release accompanies this plan.

## Evidence and baseline

- Current published baseline verified through GitHub: **v2.12.1559**, main commit
  `de08996fe9763619e2e26039a618907197514050`. Local `origin/main` initially held
  v2.12.1558; the GitHub comparison confirms that the intervening release changes
  dashboard files, documentation and version metadata, not GoodWe control,
  orchestration or these regression tests.
- System reported in the ticket: GW9.999K-EHA-G20, firmware previously reported
  as 010101, built-in Meter 1, no Meter 2, three backed-up circuits. The current
  installed integration and firmware versions remain unverified.
- The latest screenshot was downloaded, signature/size checked and visually
  inspected. It shows Meter 1 Online. That does not establish whole-site meter
  coverage, polarity, or physical export containment.
- On 30 October, the reporter quoted Amber's expectation that the setup supports
  load following, with configuration confirmation after enrolment. The reporter
  plans the already-agreed read-only before/during/after SmartShift capture.
  There is no new event log or SmartShift result in the ticket to validate yet.
- Older support findings in the ticket report acknowledged and read-back 0 W
  settings with material export continuing at full battery. These are historical
  findings, not a reproduction on v2.12.1559. They do not identify the missing
  firmware, metering or command precondition.

## Primary sources

1. [Amber GoodWe enrolment guide](https://help.amber.com.au/hc/en-us/articles/47019389117581-Enrolling-your-GoodWe-system-with-Amber)
   lists GW9.999K-EHA-G20, makes load following dependent on suitable GoodWe
   metering, and requires Off-grid mode to be disabled. It documents SEMS remote
   control permission; it does not publish Amber's local Modbus sequence.
2. [GoodWe ESA user manual](https://admin.goodwe.com/Api/downloadFile?id=3105&mid=60&type=2)
   describes metering and Power Limit. The currently served manual includes the
   built-in-meter exception for whole-home backup/STS arrangements. This does
   not independently establish coverage for the reporter's three-circuit layout.
3. GoodWe `Third-Party EMS Protocol MODBUS V1.1.1`, supplied in ticket message
   [1544977317644734504](https://discord.com/channels/1443943629130567874/1543802186587185245/1544977317644734504),
   pages 17–20: 42003 enables export limiting; 42004–42005 are a signed 32-bit
   limit discussed for parallel systems or units above 30 kW. Registers 47509
   and 47510 enable and set the export limit for the documented lower-power
   path. These are export limits, not PV-generation targets. The document does
   not establish that switching this single 9.999 kW system to 42003/42004 fixes
   its physical response.
4. [Upstream GoodWe ET-family implementation](https://github.com/marcelblijleven/goodwe/blob/master/goodwe/et.py)
   maps `grid_export`/`grid_export_limit` to 47509/47510. Its
   [model definitions](https://github.com/marcelblijleven/goodwe/blob/master/goodwe/model.py)
   include the EHA-G20/NAH platform. Family recognition alone is not physical
   curtailment validation on this firmware.

## Existing implementation and proof boundary

`custom_components/power_sync/inverters/goodwe_battery.py` is the hybrid/DC
controller. `curtail()` saves the original limiter settings, enables
`grid_export`, requests `grid_export_limit=0`, and verifies the settings.
`restore()` restores the saved settings and verifies them.

`handle_goodwe_curtailment()` in `__init__.py` serializes automatic callbacks,
evaluates the tariff and control ownership, persists restoration state and
throttles retries. Entity-only profiles without a controller remain unsupported.
The GoodWe status helper in `sensor.py` requires fresh physical export evidence;
accepted settings alone do not establish Active curtailment.

| Stage | What is established |
| --- | --- |
| Capability | Family documentation and Amber expectation support feasibility. |
| Eligibility and routing | Current source selects direct hybrid zero-export control for the applicable profile and tariff. Current installation eligibility is unverified. |
| Command and readback | Existing source/tests cover enable, 0 W, verification and restoration. Earlier ticket findings report actual readback. |
| Physical effect | First unresolved stage for this installation: reliable on-grid export containment while PV supplies demand. |
| UI | Current source keeps unconfirmed/stale/material-export cases Pending. A status is not independent hardware proof. |

Two live explanations remain: the inverter does not have the measurement or
firmware/control precondition required to enforce the limit, or another/native
controller owns or changes the effective behavior. A distinct control register
bank is another possibility, not an established fix.

Do not route the separate `inverters/goodwe.py` AC controller into this hybrid
system. Do not pass household demand as its grid-export limit: that permits
additional export rather than limiting PV to household demand. Do not use
Off-grid mode, EMS Stop, repeated speculative register writes, or automatic
meter/grid-code configuration changes as a fallback.

## Implementation sequence

1. Review the capture already planned in the ticket. Keep Amber as sole writer
   and PowerSync in Monitoring Mode with automatic curtailment disabled. Use the
   existing [pinned read-only capture script and instructions](https://github.com/bolagnaise/PowerSync/blob/5793baf075f54301fbee80ce427c0a7c3ec97139/docs/goodwe-curtailment-capture.md);
   they are available at that commit, not on current main. Do not infer an Amber
   command from an unchanged sample or from its cloud status alone.
2. Correlate the active and released transitions with both control banks,
   operating mode, PV, load, grid and battery power, SOC, metering source and
   firmware. Verify sustained near-zero export with continued PV supply under
   surplus conditions and recovery after release. Battery charging, clouds and
   load changes can otherwise imitate successful curtailment.
3. If the existing 47509/47510 path works after onboarding, determine which
   supported setup/firmware change enabled it. Document that prerequisite and
   retain the existing native controller; no HA load-following loop is needed.
4. If a different local sequence is established, add a narrowly selected,
   model/firmware-specific hybrid controller capability. Prefer native export
   limiting. Do not automatically probe alternate banks with writes. Persist
   original settings before modification; preserve ownership, Monitoring Mode,
   retry limits, cancellation, unload and restart restoration.
5. Before shipping that capability, add focused tests for the observed variant,
   failed/partial writes, mismatched and unavailable readback, restore failures,
   concurrent callbacks, reload recovery, force dispatch, external ownership,
   entity-only profiles and stale/non-finite telemetry. Keep command acceptance
   distinct from physical convergence and retain Pending until fresh proof.
6. Complete focused and adjacent checks, user documentation, and the HACS release
   process if an implementation is justified. Installation and an authorized
   physical test remain separate from publication.

The smallest next artifact is the already-planned SmartShift capture with event
times and installed versions. Alternatively, model/firmware-specific written
GoodWe guidance that states the complete on-grid sequence can resolve the local
control question. Existing logs and screenshots need not be resubmitted.

## Verification performed

Used the Python 3.12 project environment against an isolated archive of
v2.12.1558 source, whose relevant controller, orchestration and test files are
unchanged in the verified v2.12.1559 comparison:

```sh
python3.12 -m pytest -q tests/test_goodwe_battery_controller.py tests/test_goodwe_curtailment_entity_profile.py
# 26 passed
python3.12 -m pytest -q tests/test_solar_curtailment_status.py -k goodwe
# 2 passed, 19 deselected
```

These checks establish existing software behavior only. No customer device
commands, installation, firmware changes or physical validation were performed.
