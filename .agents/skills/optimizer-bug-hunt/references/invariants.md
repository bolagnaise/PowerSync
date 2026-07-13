# Invariants and intentional behaviors

Check this list BEFORE asserting something is a bug. Each entry has burned at least one
reviewer or support thread. Baseline v2.12.783.

## Units and thresholds

- **LP prices are $/kWh** (0.418 = 41.8c). Code comments sometimes say "c" loosely; the
  numerics are dollars. Curtailment logic in `__init__.py` is **c/kWh** (threshold
  `export_earnings < 1` c/kWh). The boundary converts correctly.
- **Action classification threshold is 100 W** (`ACTION_THRESHOLD_W`, battery_optimizer.py).
  Tiny LP residuals (1e-9 kW) can never become CHARGE/EXPORT.
- Battery efficiency is applied **once per side** (charge ×eff, discharge ÷eff; round trip
  = eff²) — consistent in LP, greedy, hold solver, and schedule emission. If you think it's
  doubled, re-read.
- Power is kW inside the LP, W in schedules/sensors; SOC is 0–1 internally, 0–100 in
  `battery_level`.

## Reserve semantics (three different "floors" — name the right one)

- **Optimizer `backup_reserve`** is a soft/planning floor for forced actions only. Natural
  self-consumption in the emitted plan drains to `min(soc_0, hardware_reserve)`
  (`_natural_self_consumption_floor`). A hard floor requires raising the HARDWARE reserve.
  This is documented in-code and is the #1 semantic misunderstanding in tickets.
- **Export/bridge reserve floor** is an end-of-window *boundary condition* on `E[t+1]`
  for export periods — NOT a floor later self-consumption respects (intra-period discharge
  uses the pre-raise `base_reserve_floor` snapshot). Intentional.
- **"Reserve floor" vs "planning reserve / bridge to next cheap window"** are different
  features — users often mean the latter (see AGENTS.md vocabulary note).
- `_pre_idle_backup_reserve` is snapshotted **only when it is None** — this is the guard
  that stops stacked modes (idle entered during EV-preserve etc.) from capturing a
  temporary reserve as the user's "real" one. Don't "fix" it into unconditional snapshots.
- `_startup_backup_reserve` deliberately reads from **config, not live hardware**, to avoid
  adopting a mid-IDLE elevated value.
- GoodWe reserve is **inverted** (on-grid DOD = 100 − reserve%); optimizer IDLE must not
  rewrite persistent DOD. Sungrow IDLE caps discharge to 0 (fallback 0.01 kW), never
  Forced+Stop, and must restore the prior limit.

## Array-shape invariants (why "padding bugs" keep getting refuted)

- Every production input to `optimize()` — import/export prices, solar, load, bonus
  arrays — is equalized upstream to exactly `horizon_hours*60/interval_minutes` slots
  (576 @ 48 h/5 min). Producers fill via `for _ in range(n_intervals)` loops or explicit
  padding; bonus arrays are sized `min(len(import), len(export))` which equals both.
- Therefore `_pad_array`'s last-value branch and `_align_forecasts`' max-length behavior
  are **dead in production**. They ARE latent footguns for future callers
  (`_pad_array` ignores its `default` for non-empty arrays) — harden if touched, but do not
  report as live bugs.
- Import and export prices share one slot grid; `_apply_saving_session_prices` and
  `_apply_demand_charge_penalty` preserve length symmetry.

## Timing and scheduling

- Schedule slot timestamps are floored to the interval boundary; expiry timers use
  absolute UTC (`async_track_point_in_utc_time`) — DST-safe for dispatch.
  Far-horizon *labels* (window masks across a DST transition) can be an hour off on the
  two AU transition days; self-heals as now advances. Imminent actions are correct.
- `_get_current_action` compares absolute instants — no per-slot off-by-one was found in
  the LP↔schedule↔price indexing (verified 1:1).
- Two independent ~5-min cadences (coordinator refresh + polling loop) is intentional;
  the LP runs only in the polling loop (history: LP-in-refresh caused duplicate writes).

## Logging and status semantics

- The decisions logger emits **aggregate counts of planned actions** post-override. Runtime
  converts idle → self_consumption under disable-idle or demand windows
  (`_effective_runtime_action`), so `idle=N` in the log with self-consumption on hardware
  is a documented gap, not a command-path bug.
- `[MONITORING] Optimizer would execute: ...` = no hardware command was sent (AGENTS.md
  gate 3).
- `self_consumption` in logs is not "force discharge" — describe logged actions verbatim.
- Logs auto-redact VINs/tokens/serials — a "missing" identifier is redaction.

## Intentional behaviors that look like bugs

- **Idle holds are intentional SOC preservation** unless "Disable Idle" is on (converts
  idle → self-consumption).
- **AC curtailment deliberately skips when the battery can absorb the solar**; threshold is
  export_earnings < 1 c/kWh.
- Fronius GEN24/BYD 200–370 W battery movement in Auto is inverter self-balancing, not a
  PowerSync command.
- Grid-charge-only-when-solar-poor, persistent SOC guards, hard SOC ceilings: historically
  **feature requests**, not bugs.
- The `free_import_slot` branch forcing full-rate charge display at SOC 1.0 inflates
  `grid_import_w` telemetry during free windows (BMS refuses the actual charge; cost
  unaffected) — known display quirk.
- Default flat rates (0.30/0.08 $/kWh) replacing prices during a genuine provider blackout
  is the intended degraded mode, with a logged warning.

## Known dead / vestigial code (do not diagnose bugs into it)

- `ScheduleExecutor.execute_action` and its whole command path in
  `optimization/executor.py` (`_command_charge`, `_tick`, callbacks…) are never invoked.
  The live path is `OptimizationCoordinator._execute_optimizer_action`. (Latent
  CHARGE→EXPORT-without-restore bug inside if ever wired up.)
- `_below_reserve_recovery_target` and `_relaxing` in battery_optimizer.py are read but
  never assigned — the recovery-ramp / relax-suppression branches are unreachable.
- `terminal_weight` is fixed at construction; `update_config` cannot change it.

## Test-environment invariants

- Run tests with `python3.12 -m pytest` (system python3 = 3.9 fails the conftest guard).
  Prefix shell commands with `rtk` on this machine. CI runs only HACS/hassfest — local
  pytest is the only gate.
- Test fixtures build coordinators with `object.__new__` (bypasses `__init__`): new
  coordinator attributes need `getattr(..., None)` defaults to stay test-compatible — this
  is a fixture concern, not a production bug pattern.
- SOC-cap fixtures need a starting SOC that actually crosses the cap.
