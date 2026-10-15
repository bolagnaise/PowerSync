# GoodWe ESA curtailment capture

This standalone collector records what a GoodWe ESA exposes locally while Amber
SmartShift controls it. It needs Python 3.9 or later on a computer on the same LAN
as the inverter, with access to the existing Modbus TCP endpoint. It needs no
Python packages, Home Assistant token, SEMS password or installer login.

The script sends **only function 03, Read Holding Registers**, to a fixed list of
addresses. It cannot write settings, enable EMS, change mode or discover devices.
It does not read serial numbers. Read traffic still uses the communication module;
stop if it causes connection errors in the existing integration or SmartShift.

## Before running

Use this with the test plan in ticket #29. Ask Amber whether this installation
uses load-following or on/off curtailment. Record installed PowerSync, GoodWe
Experimental and DSP/ARM versions before onboarding; record any changes onboarding
makes. Do not change firmware or installer settings during a capture.

Before Amber takes control, put PowerSync in Monitoring Mode, disable its automatic
solar curtailment and pause other automations that write inverter settings. Verify
there are no competing mode or limit writes. Keep the inverter on-grid and leave
its commissioned meter/grid-code settings alone. Agree a pause/recovery procedure
with Amber. Keep only one controller writing throughout the test and afterward.

Save [capture_goodwe_curtailment.py](../scripts/capture_goodwe_curtailment.py) on the
LAN computer. Use the **same IP, TCP port and unit ID already working in the GoodWe
integration**. Defaults are port 502 and unit 247; do not scan for another address.

First take one sample (replace the example IP):

```sh
python3 capture_goodwe_curtailment.py 192.168.1.50 --check --output goodwe-check
```

Open `goodwe-check/capture.csv`. Check that the two control groups and the expected
power readings are present. Unsupported blocks appear in `errors` and have blank
cells, never zero values. An illegal-address response disables that block for the
rest of this run; it does not trigger a scan or alternative protocol. Partial
captures may still be useful, but a successful connection alone does not establish
that all required readings are available. If key readings are missing, send the
check files for review before the timed test. Do not enable Modbus/change access
settings just to make this script work.

For a one-hour capture at 30-second intervals:

```sh
python3 capture_goodwe_curtailment.py 192.168.1.50 --minutes 60 --output goodwe-amber-test
```

Use `--port` / `--unit` only to match the existing working configuration. Optional
`--note "DSP 07; ARM 06; ..."` adds setup information to the metadata. The output
directory must be new. Ctrl+C stops capture and preserves completed rows. Three
consecutive failed samples stop it automatically. The minimum interval is 10
seconds; the default 30 seconds avoids unnecessarily frequent polling. Each sample
uses sequential reads on one short-lived TCP connection, spaced by 250 ms.

## During the test

Choose steady daylight, a naturally full battery and surplus solar. Include at
least five minutes before, five minutes during confirmed Amber curtailment, and
five minutes after release. Start earlier or use a longer capture if necessary.
Record Amber's curtailment start/end times, feed-in price and displayed status,
including timezone. Those do **not** come from this script. Keep a matching
whole-site power history from an independent meter if available.

No manual EMS/export-limit changes are needed. A normal household load change
can help show whether PV follows load; note when it occurs. Stop for faults,
unexpected supply changes or new communication problems. Follow the agreed
recovery procedure rather than experimenting with modes. Confirm generation
recovers after Amber releases curtailment while sunlight remains. Confirm Amber
has relinquished control before re-enabling PowerSync writes.

## Files and interpretation

Share the three output files in the ticket, plus the Amber event times/status:

- `capture.csv`: decoded readings, UTC sample start/end, and errors. Every row
  contains fresh readings only. A blank is missing; it never means zero.
- `readbacks.jsonl`: each request and complete response bytes, raw register words,
  per-request UTC start/end and errors. This distinguishes actual reads from
  cached UI values and records timing within each sample.
- `metadata.json`: script hash, Python version, polling settings, register list,
  setup note and unsupported blocks. It omits the inverter IP and serial number;
  check free-text notes before sharing.

| Block | Literal register addresses | Purpose |
| --- | --- | --- |
| Legacy controls | 47509–47512 | Export limiter enable/limit, EMS mode/power |
| Extended controls | 42000–42005 | Alternate EMS mode/power and export limiter |
| EMS fallback | 47505 | Existing fallback policy, read only |
| Runtime mode | 35187–35188 | Work/operation mode codes |
| PV | 35301–35302 | Total PV power |
| Inverter/grid | 35137–35140 | Inverter power and reported AC active power |
| Loads | 35169–35172 | Backup and on-grid load readings |
| Battery | 35182–35184, 37007 | Battery power, mode and SOC |
| Meter | 36003–36004, 36025–36026 | Meter status and active power |

Addresses are sent literally, without subtracting one or 40001. Signed values
and multiword fields follow the supplied protocol, most significant word first.
For example, legacy limit raw `65535` decodes as signed `-1`; the raw word is
retained, and its firmware-specific meaning must not be guessed. Mode codes stay
numeric. The extended block being readable does not prove Amber uses it; its
export-limit applicability is model/topology dependent.

The inverter's AC/meter readings do not independently establish whole-house
coverage. GoodWe documents that AC active power can fall back to inverter-port
power if meter communication fails. Verify meter status, wiring coverage and
power signs against the site's known import/export and battery charge/discharge
states. The script does not label either direction or compute a whole-house
load from possibly incomplete measurements.

Compare **before → active → released**. Near-zero export with PV supplying and
following load supports load-following; near-zero PV with the house supplied
from grid/battery supports solar shutdown. Account for battery charging, clouds
and load changes, and require sustained fresh samples plus recovery. A changed
setting is a lead, not proof of the command path. Sequential samples can miss
transient commands and are not atomic. Unchanged registers cannot rule out cloud
commands or other controls not exposed here. No curtailment event means an
inconclusive test. Replaying any observed setting is a separate, reviewed test.

Register sources: the **Third-Party EMS Protocol MODBUS V1.1.1** supplied in ticket
#29 ([GoodWe download](https://admin.goodwe.com/Api/downloadFile?id=3105&mid=60&type=2)),
and the [GoodWe library's ET mapping](https://github.com/marcelblijleven/goodwe/blob/master/goodwe/et.py)
for meter-status/operation-mode registers. The script has been tested against a
simulated Modbus TCP server; it has not been verified on Raardvark's inverter.
