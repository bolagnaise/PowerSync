# Optimizer performance and diagnostics

The optimizer keeps its 30-second allowance per inner solve. A cycle can contain
several command-projection passes and Cost Neutral budget reconciliations, so
that allowance is not a 30-second limit on the complete cycle. A feasible
time-limited incumbent can still be used after the normal schedule checks.

## Implemented changes

- Cost Neutral with EV charging allocates the continuous solar-surplus columns
  and each of the four selector families separately. Previously those families
  shared columns and overwrote coefficients. Regression cases exercise the
  surplus, charging-power and remaining-capacity branches with real HiGHS solves.
- The native model is loaded in one sparse `HighsLp` operation. Column order,
  equality/inequality order, empty rows, bounds and variable types are preserved.
  Bindings without the bulk API retain the incremental path. A rejected native
  operation cannot expose a previous model's result.
- EV start indicators remain bounded and retain all three transition rows.
  Binary on/off states already force their values to be exactly zero or one,
  so the additional integer declaration is removed. EV source, floor and
  minimum-start selectors remain integer.
- Only the same-model free-charge preference solve receives the complete raw
  primary vector. It is checked against the original constraints, including
  the appended economic limit, using effective solver feasibility tolerances.
  Unsupported or invalid starts use a cold solve; a rejected start discards the
  native instance before retrying. A failed or invalid secondary result keeps
  the primary plan. Economic termination settings are unchanged.

No cross-cycle solution/basis cache, larger economic gap, shorter horizon or
coarser public schedule is introduced.

## Cycle diagnostics

`OptimizerResult.lp_stats["cycle"]` records all reconciliation chains, projection
passes and primary/preference subsolves, including attempts whose plans were
discarded. `selected_pass` identifies the returned LP attempt; `selected_solver`
and `fallback_reason` describe the returned result. An outer LP exception records
its type and message on the fallback result.

The trace includes total cycle time, formulation time, sparse packing/loading
time, native runtime, schedule rendering/reconciliation stages, native statuses,
objective, bound, gap, nodes and unique/fixed integer counts. Timings are nested:
do not sum every trace field as though they were disjoint. Non-finite native
statistics become JSON `null`. Negative objective bounds remain valid values.
Trace state is local to the calling context and is reset even after an error.

## Reproducing measurements

Use Python 3.12 and the pinned `requirements-test.txt` environment. These are
synthetic offline calls without hardware commands. Run one heavy benchmark at
a time; do not run it alongside the full regression suite.

```sh
python scripts/benchmark_lp_optimizer.py --list-cases
python scripts/benchmark_lp_optimizer.py --case ev-48h --runs 5 --seeds 0,1,2 --jsonl /tmp/ev.jsonl
python scripts/benchmark_lp_optimizer.py --case all --jsonl /tmp/all.jsonl
python scripts/benchmark_lp_optimizer.py --case sunny-cost-neutral --incumbents --jsonl /tmp/incumbents.jsonl
```

`--component-root` selects a frozen comparison component directory. Compare
against the corrected Cost Neutral/EV allocation, rather than the malformed
combined model. `--model-dir` optionally saves the exact sparse synthetic models
and primal vectors. JSONL records contain source/harness hashes, HiGHS/Python
versions, seed, actual input coverage, full schedules, EV source allocations and
independent original-row/bounds/integrality audits of successful native points.
Timeouts, infeasible attempts and fallback outcomes remain in the records.

The saved sunny capture contains **250 five-minute slots (20h50m)**. Its configured
maximum horizon is 48 hours. Separate generated Cost Neutral, combined EV,
final-solar ledger, multi-EV deadline, permission/floor and battery-deadline
cases contain all **576 slots**. `--case all` can take several minutes.

## Measured limits and rejected experiments

Local paired screens on Tower with HiGHS 1.14.0 found substantially less model
loading time and approximately one-third lower median cycle time for the simple
48-hour EV case. The small free-charge case also improved. Flat-tariff cycle
timings varied between rounds, so they do not establish a reliable improvement.
These are instrumented local screens, not a tail-latency or Home Assistant
hardware guarantee. The implementation artifact directory records every sample,
including failures, native objectives and source hashes.

The difficult sunny and full-horizon Cost Neutral cases still spend most of
their time in native search. Tighter native-absorption constraints and
period-specific import/discharge bounds were tried separately; their speed
benefit did not repeat across seeds, so those formulation changes were removed.

A synthetic combined Cost Neutral/EV case reaches the existing final EV-source
guard and selects the greedy fallback on both the corrected baseline and the
candidate. Raw model feasibility does not establish that the rendered schedule
can physically supply EV charging; this guard remains in place.

Retaining a predecessor after a failed outer reconciliation is deferred as a
separate resilience change. Reconciliation caps are not necessarily monotonic:
merging a raw cap map can lower one day's allowance while another day's imports
trigger a retry. Safe retention needs explicit budget/provenance, deadline,
permission, reserve and source validation. Arbitrary exceptions do not establish
that the preceding plan is eligible.

Publication and installation require the normal regression/release procedures;
local benchmark or test artifacts are not protected-main release evidence.
