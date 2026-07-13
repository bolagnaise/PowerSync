# Verification playbook

In the 2026-07 review, ~40% of well-argued bug claims died on adversarial verification.
The claims were not sloppy — they were code-accurate readings whose failure scenarios were
unreachable because something *elsewhere* compensated. Before promoting a suspicion to a
bug (and before writing any fix), run this playbook.

## The refute-first method

State the claim as: **specific inputs/state → specific wrong output**. Then actively try
to kill it, in this order:

1. **Reachability upstream**: can the trigger inputs actually occur? (RC-1/RC-2 died here:
   the "short array" could never reach the function because every producer equalizes
   lengths.) Trace the REAL call sites, not the function in isolation.
2. **Compensating mechanism downstream**: does a self-heal, watchdog, retry, next-cycle
   overwrite, or fallback rescue the bad state? (RC-6 died here; OB-2 survived only for
   the one brand with no rescue.) Use the inventory below.
3. **Framework semantics**: HA behaviors matter — `DataUpdateCoordinator` preserves `.data`
   when `_async_update_data` raises `UpdateFailed` (killed RC-4); `async_track_point_in_utc_time`
   is absolute-UTC (killed DST-expiry claims); config-entry reload re-runs setup with fresh
   local state (this *creates* bugs OB-5/OB-7 rather than fixing them).
4. **Intentional design**: check invariants.md — idle holds, curtailment skips, floor
   semantics, monitoring mode.
5. **Persistence horizon**: a transient bad state that the next 5-min cycle overwrites is
   a hardening item, not a major bug. Persistent harm usually requires: optimizer
   disabled, OR a brand with no drift check/watchdog, OR monitoring gating the retry.
6. **Empirical reproduction** when the logic is pure: the decisive verification for OB-1
   was running the same scenario through `_solve_lp` and `_solve_greedy` and diffing the
   action counts. `battery_optimizer.py` is import-safe for this; drive it with
   `python3.12` directly.

Verdict vocabulary: CONFIRMED (trigger reachable + no rescue), REFUTED (with the evidence),
UNDECIDED (name the missing evidence). Only CONFIRMED goes in a fix plan.

## Compensating-mechanism inventory (check each before confirming a stuck-state bug)

Per-cycle / runtime:
- **Next optimizer cycle overwrite** — when the optimizer is *enabled*, most work-mode
  damage is repaired within ~5 min by the next `_execute_optimizer_action`. Stuck-state
  bugs usually need the disabled-optimizer cohort.
- **Force-mode re-issue** — `_run_optimization` re-issues force charge/discharge every
  interval; change-detection-gated commands (self_consumption) do NOT re-issue (OB-4).
- **Drift/self-heal checks in the self_consumption branch** — Tesla (operation-mode
  re-check + reserve self-heal), GoodWe, Sungrow (`_restore_stale_low_discharge_limit`,
  `_discharge_appears_blocked_after_restore` telemetry reapply). NO other brand has one.
- **Polling safety net** — `_should_restore_pre_idle_backup_reserve_from_polling` covers
  ONLY `_pre_idle_backup_reserve` (not no-discharge caps, not work modes), and only when
  action ≠ idle and EV-preserve inactive.

Hardware:
- **FoxESS remote-control timeout** (~600 s) and **Sungrow force countdown** auto-revert
  forced registers. Persistent-mode brands (Sigenergy EMS, GoodWe ECO…) have no such
  timeout.
- BMS refuses out-of-range commands (e.g. charge at SOC 100) — some "wrong commands" are
  physically inert.

Startup / lifecycle:
- **`restore_force_mode_from_persistence`** — restores force charge/discharge with absolute
  expiry after restart. Covers ONLY those two (not Hold, not self-consumption toggle).
- **`_restore_disabled_optimizer_reserve_if_stale`** — the only generic stale-state
  detector; three hard gates: optimizer disabled, brand not Tesla/Sigenergy/GoodWe/custom,
  and keyed on an elevated *reserve* (blind to work modes). Many confirmed bugs exist
  precisely in its shadow.
- **Monitoring-enable cleanup** — the app path fires `restore_normal` with
  `_force_restore=True` (bypasses the monitoring block) for force modes/native control;
  does not cover pre-idle reserves (OB-8).
- **`_deferred_enable_restore`** — startup self-consumption restore; only when enabled.

## Race-claim discipline

For any concurrency claim, name: the two concrete tasks, the exact await window, the
shared state, and the guard that fails. Then check:
- `_optimization_lock` scope — solves only; cached execution and disable() run outside it.
- `_command_generation` scope — same-process only; frozen in closures across reload;
  consulted by timers but NOT by Modbus restore branches.
- `_restore_superseded` — Tesla restore path only.
- Dedup markers (`_last_executed_action`) — written after awaited I/O (TOCTOU window).
A race that only produces a redundant idempotent write is MEDIUM at most; wrong end-state
or unprotected force-timer bookkeeping is MAJOR.

## Reproduction and test patterns

- **LP/greedy differential harness** (the OB-1 proof): construct prices/flags for the
  scenario, call the optimizer once with HiGHS available and once forcing the greedy path,
  assert per-slot action parity where the invariant demands it.
- **AST source-extraction** for `__init__.py` logic: parse the file, extract the one
  function, exec it with stubbed dependencies (`tests/test_sungrow_curtailment_runtime.py`
  is the canonical example). Use for service handlers/restore branches without importing HA.
- **Lifecycle sequence tests** for restore bugs: simulate enter → failure/interruption →
  exit and assert the hardware-facing calls (fakes record calls). Cover: exception on the
  restore await, monitoring toggled between set and restore, reload between set and expiry,
  disable() mid-mode.
- **Asyncio interleaving**: drive two coroutines manually (start one, step to its await via
  `asyncio.sleep(0)`, start the second) to prove/disprove TOCTOU claims.
- Fixtures: `object.__new__` coordinators need `getattr` defaults; SOC-cap fixtures must
  cross the cap; run the narrow test file first, then adjacent files.

## Severity calibration used in the registry

- MAJOR: persistent wrong hardware state, money-losing plan the user can hit with default
  or common settings, or a confirmed remaining gap in a shipped fix.
- MEDIUM: needs an opt-in feature, a narrow timing window, or yields redundant-but-
  idempotent commands.
- Hardening (HD): real code fact, bounded/latent impact, fix opportunistically with tests.
