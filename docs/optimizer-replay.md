# Local optimizer replay

The first fleet-regression milestone records complete resolved solver attempts
locally and replays them offline. It does not enrol installations, upload data,
enable a remote MCP grant or change an installed Home Assistant system.

## Capture a synthetic case

Use Python 3.12 with `requirements-test.txt`. From the repository root:

```bash
python3.12 scripts/replay_optimizer.py capture --case flat-tariff --currency AUD --output /tmp/optimizer-case.json
python3.12 scripts/replay_optimizer.py validate /tmp/optimizer-case.json
python3.12 scripts/replay_optimizer.py replay /tmp/optimizer-case.json --output /tmp/optimizer-report.json
```

Capture uses the existing benchmark scenarios and the normal optimizer. The
export is explicit and created with owner-only permissions. Currency is a
required declared ISO code; it is not inferred from household location.

To compare a reviewed candidate component:

```bash
python3.12 scripts/replay_optimizer.py compare /tmp/optimizer-case.json --candidate-root /path/to/candidate/custom_components/power_sync --output /tmp/comparison.json
```

The baseline component defaults to this checkout. `--component-root` selects an
explicit baseline component directory. Same-version replay requires a matching
component, Python, platform, solver and test dependency fingerprint. A candidate may have
different source, but its dependency image must match the approved baseline.
This first tool does not build or download dependency images.

Every replay uses a fresh process. Comparison includes a separate baseline A/A
arm, and the trusted parent evaluates schedules independently of the candidate.
The report binds the input digest, component identities, runner/scorer code and
thresholds. Changed component files are detected automatically; additional
paths may be supplied with `--changed-path`.

## Meaning of a report

- **pass:** supported results match exactly or satisfy declared economic and
  terminal-energy equivalence, with a clean A/A arm.
- **fail:** a supported material behavior change or invariant violation needs
  investigation. An intentional improvement can change the result and still
  needs a reviewed regression expectation.
- **insufficient:** a required metric or changed source path is unsupported.
- **invalid:** validation, source/environment binding, worker execution or A/A
  calibration prevents using the result as passing evidence.

Solver replay feeds captured resolved inputs to the candidate optimizer.
Changes to coordinator preprocessing, retry selection, overlays, hardware
execution and unrelated component files remain outside this layer's coverage.
The checker reports these gaps rather than treating a solver match as proof
that the whole proposed update is correct.

The initial independent scorer checks schedule alignment, power and SOC energy
balance, applicable reserve/import/charge/export constraints, tariff cashflows
and terminal energy. Complex EV policy/deadline, Cost Neutral settlement, capped
bonus and solar-curtailment metrics are reported as unsupported; these cases
can be captured/reconstructed but cannot receive a passing comparison that
requires those metrics. Do not sum successive forecast horizons as daily
realised savings. `--terminal-price` supplies one declared common value per kWh
for comparing terminal inventory; terminal SOC/energy is always reported.

## Developer capture boundary

Recording is absent by default. A developer/test harness can explicitly attach
`CaptureRecorder` using `record_cycle(optimizer, recorder)`, with a source
binding and declared currency. An isolated coordinator harness can attach it
as `_regression_recorder`. There is deliberately no new HA setting or remote
activation path in this milestone.

Each `optimize` invocation records arguments and all reviewed optimizer state
before normalization. That includes temporary reserve overrides, independently
assigned capabilities, learned conversion values and retry-specific masks.
Raw solver schedules are separate from the coordinator's final committed
stage. Exceptions retain an allowlisted class, never the raw message.

The schema closes all input/state fields and admits only reviewed dataclasses.
Missing required state, an unknown dependency, changed signature, unsupported
text, non-finite values or a missing decision clock prevents a complete case.
Case-local EV/quota aliases preserve identity relationships. Capture does not
serialize the integration's generic configuration, objects or credentials.

The recorder performs bounded memory copies only. Whole cycles are admitted
within a byte/count budget; queue exhaustion or recorder faults discard the
cycle without changing the solve, retry, restoration, result or command path.
Export performs canonical serialization and hashing outside the solve path.
Capture time, estimated retained bytes, errors and dropped cycles are exposed
for local calibration. These measurements are not low-end HA hardware proof.
Nondefault benchmark solver options are explicitly rejected by capture v1;
they are not silently replayed using production defaults.

## Worker boundary

Workers have CPU, runtime and output limits (plus a 2 GiB virtual-memory cap on Linux), empty credential environment,
disabled core dumps and a network/process-launch audit tripwire installed
before component import. The parent does not include raw worker stderr in a
report. These are local diagnostic defenses, not an OS security sandbox.
Private customer cases must not be supplied to unreviewed code or public PR
jobs. The later fleet service still needs its separately reviewed private
runner, consent, retention/deletion and activation workflow.

## Verification and delivery boundary

Focused tests cover the real solver, temporary-reserve closure, exception and
retry observations, bounded queues, recorder non-interference, identity aliases,
contract validation and seeded physical/timestamp errors. Run them with:

```bash
python3.12 -m pytest tests/test_optimizer_replay.py
```

Use the repository's audited full regression runner for full verification.
Local replay evidence does not replace trusted matching protected-main test
success or authorize publication, installation or hardware operations.

HA-wide diagnostic access, ongoing enrolment, broader scoring, coordinator
sequence replay, private fleet evaluation and validated physical simulation
remain subsequent implementation milestones. They must preserve their own
scope, coverage and evidence boundaries.
