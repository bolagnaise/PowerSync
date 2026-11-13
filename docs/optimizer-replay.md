# Local optimizer replay

The developer CLI captures synthetic optimizer cases and replays them offline.
It does not install candidate code in Home Assistant or send hardware commands.

Use Python 3.12 with `requirements-test.txt`. From the repository root:

```bash
python3.12 scripts/replay_optimizer.py capture --case flat-tariff --currency AUD --output /tmp/optimizer-case.json
python3.12 scripts/replay_optimizer.py validate /tmp/optimizer-case.json
python3.12 scripts/replay_optimizer.py replay /tmp/optimizer-case.json --output /tmp/optimizer-report.json
```

To compare a reviewed candidate component, fingerprint its source and use that
fingerprint for the comparison:

```bash
python3.12 scripts/replay_optimizer.py fingerprint --component-root /path/to/candidate/custom_components/power_sync
python3.12 scripts/replay_optimizer.py compare /tmp/optimizer-case.json --candidate-root /path/to/candidate/custom_components/power_sync --expected-candidate-sha256 "$REVIEWED_COMPONENT_SHA256" --output /tmp/comparison.json
```

Read the report's coverage and unsupported-path fields before interpreting a
comparison. Missing inputs, unsupported source changes or failed reconstruction
cannot establish a passing result. A successful software replay does not prove
physical hardware behaviour or observed savings.

Use synthetic cases for shared examples and regression fixtures. Do not commit
customer captures or put them in public tickets. Optional diagnostic support
and private testing require separate approval as described in
[Diagnostics and privacy](diagnostics-privacy.md).

Replay supplements the normal tests. Publication still requires the checks in
[the regression guide](regression-release-gate.md).

## Performance diagnostics

The optimizer's 30-second allowance applies to each inner solve. A complete
cycle can include several reconciliation passes. A time-limited feasible
solution must still pass the normal schedule checks.

Cycle diagnostics include total time, formulation and native solver time,
projection attempts, selected result, fallback reason and native solver status.
Nested timings should not be added as though they were independent stages.
A fallback or unsupported comparison is a coverage limit, not a successful
hardware result.

Run synthetic offline benchmarks with the same pinned test environment:

```bash
python3.12 scripts/benchmark_lp_optimizer.py --list-cases
python3.12 scripts/benchmark_lp_optimizer.py --case ev-48h --runs 5 --seeds 0,1,2 --jsonl /tmp/ev-benchmark.jsonl
```

Run one heavy benchmark at a time, separately from the full regression suite.
Use the recorded source, runtime, input coverage and failures when comparing
results. Local timings do not guarantee performance on another HA system.
