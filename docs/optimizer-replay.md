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
