---
name: optimizer-bug-hunt
description: Find, verify, and fix bugs in the PowerSync optimizer (LP solver, optimization coordinator, force/reserve state machine, tariff windows, load/solar inputs, schedule execution). Use this whenever a user reports the battery charging/discharging/exporting at the wrong time, a stuck backup reserve or discharge cap, a force mode that never restored, wrong behavior in ZeroHero/Happy Hour/free-import windows, a schedule that disagrees with prices, or any Discord ticket that routes to optimization/. Also use it before writing ANY fix in custom_components/power_sync/optimization/ or the force/reserve logic in __init__.py — it carries subsystem maps, invariants, a verified bug registry, and a verification playbook that prevent wrong fixes.
---

# PowerSync Optimizer Bug Hunt

Deep diagnostic knowledge for the optimizer subsystem, distilled from a 2026-07 multi-agent
review (7 subsystem reviewers + 3 adversarial verifiers, ~17k lines read, baseline v2.12.783).
AGENTS.md gives the general support gates (version reconciliation, log windows, monitoring
mode); this skill is the layer below it: how the optimizer actually works, where its bugs
live, and how to prove one before fixing it.

**The single most important lesson from the review: ~40% of plausible, well-argued bug
claims were refuted on adversarial verification.** Compensating mechanisms (self-heals,
watchdogs, upstream array equalization, HA coordinator semantics) rescued them. Never fix
from a first reading — run the verification playbook first.

## Reference files — read the one you need

| File | Read it when |
|---|---|
| `references/architecture.md` | You need to know how a subsystem works: LP variable layout, post-solve override pipeline, mode/reserve state machines, tariff-window flow, input forecasters, cycle lifecycle |
| `references/invariants.md` | Before asserting anything is "wrong" — units, thresholds, floor semantics, intentional behaviors, known dead code |
| `references/bug-registry.md` | FIRST for any symptom — the confirmed-open-bug list (match before re-diagnosing), plus refuted claims (do not rediscover these) and historical fix clusters |
| `references/verification-playbook.md` | Before promoting any suspicion to "bug" and before writing the fix/test |

## Workflow

1. **Gate first** (AGENTS.md): current version? log window covers the event? monitoring
   mode? classify (bug / stale / config / semantic / feature request).
2. **Match the symptom against `references/bug-registry.md`.** Several bugs are confirmed
   and awaiting fixes — the symptom may already be diagnosed. Also check
   `git log --oneline -- custom_components/power_sync/optimization/` for a fix newer than
   the user's version.
3. **Route to the subsystem** (table below), read the matching section of
   `references/architecture.md`, then read the actual code — line numbers in the references
   are anchors as of v2.12.783 and drift; trust function names.
4. **Form a failure scenario**: specific inputs/state → specific wrong output. No scenario,
   no bug.
5. **Adversarially verify** using `references/verification-playbook.md` — actively try to
   refute your own claim by hunting the compensating mechanism.
6. **Fix at the root, add a regression test for the newly observed variant**, run
   `python3.12 -m pytest` on the narrow file first (never bare `python3` — 3.9 fails
   conftest). CI does not run pytest; local verification is the only gate.

## Symptom → subsystem router

| Symptom | Subsystem (architecture.md section) | Watch out for |
|---|---|---|
| Wrong CHARGE/EXPORT/IDLE decision at some slot; plan disagrees with prices | §1 LP solver, then §2 post-solve pipeline | Action classification happens in `battery_optimizer.py::_build_schedule_from_solution`, NOT the coordinator. 100 W threshold. Greedy fallback must mirror every LP guard — it has diverged before (confirmed bug) |
| Plan is right but the *executed* action is wrong | §2 post-solve overrides, §7 execution path | Five overrides rewrite the schedule after the LP (spread import/export, bridge gaps, disable-idle, offgrid). Each must respect LP constraints; `_spread_export_schedule` overwriting charge slots is a confirmed bug |
| Stuck backup reserve, stuck 0 W discharge cap, battery frozen after mode change/restart/reload | §3 coordinator mode lifecycle, §4 force/reserve state machine in `__init__.py` | The repo's #1 recurring class. Check the restore-side contract: flag-cleared-before-await, missing retry, monitoring gate asymmetry, unpersisted state (Hold SoC!), orphaned timers across reload |
| Force mode ended early / never ended / survived when it shouldn't | §4 force/reserve state machine | Generation counter doesn't survive reload; unload cancels no force timers; only force charge/discharge persist — Hold SoC does not |
| ZeroHero / Happy Hour / free-import / export-bonus window misbehavior | §5 tariff windows | Bonus arrays, +$5/kWh in-window import penalty, bridge floors computed in TWO places, greedy-path parity, DST far-horizon labeling |
| Battery under-charged before a peak; phantom solar; load estimate off | §6 inputs | Solcast zero-fills forecast tails, Open-Meteo carries the last value forward; nowcast derate can persist overnight; check which the user runs |
| Optimizer "did nothing" / stale plan / double commands / acts after disable | §7 cycle lifecycle | Two independent 5-min cadences; exception path keeps old schedule silently; price-triggered solve is untracked and survives `disable()` (confirmed) |
| Sensor/status disagrees with behavior | §2 (decisions log semantics) + §7 (publication) | No Idle is modeled before publication so graphs and execution agree; demand windows can still rewrite runtime IDLE to self-consumption |

## Fix discipline (hard-won, specific to this subsystem)

- **Restore-side symmetry**: any code that *sets* hardware state (reserve, discharge cap,
  work mode, force) needs a restore path that (a) survives failure with retry (clear the
  guard flag only AFTER confirmed success — mirror `_restore_pre_idle_backup_reserve`, not
  `_release_scheduled_ev_no_discharge_mode`), (b) is gated identically to the set side
  (monitoring asymmetry strands state), (c) survives restart/reload (persist it or add a
  startup detector), and (d) re-checks `_restore_superseded` after every await (Tesla path
  does; Modbus brand branches don't).
- **LP/greedy parity**: `_solve_greedy` is the fallback when HiGHS is unavailable or the LP
  throws. Any guard/predicate added to the LP (e.g. priority-export exemptions) MUST be
  mirrored in the greedy path — grep for the structurally identical condition.
- **Post-solve overrides must re-check LP constraints**: filter by the slot's ORIGINAL
  action and permission masks before rewriting it. The fallback branches of
  `_spread_export_schedule` do this; the main path didn't (confirmed bug).
- **Per-brand blast radius**: for any restore/self-heal fix, enumerate which brands have
  compensating mechanisms (see verification-playbook.md inventory). A fix that "works" on
  Sungrow may be untested reality on Sigenergy/Solax/Fronius, which have NO drift checks.
- **Validation-only fixes don't protect existing installs** — bad state can survive an
  upgrade; add a runtime guard too.
- **Test patterns**: pure-logic tests with stubbed `power_sync` + injected fakes; AST
  source-extraction for `__init__.py` logic (see `tests/test_sungrow_curtailment_runtime.py`);
  `object.__new__`-built coordinators need `getattr(..., None)` defaults; SOC-cap fixtures
  need a starting SOC that actually crosses the cap. A fix for a recurring ticket must add
  a regression test for the NEW variant, not just re-cover the original.
- Release via the manifest-driven process in AGENTS.md; only tell reporters to update after
  the release is published.
