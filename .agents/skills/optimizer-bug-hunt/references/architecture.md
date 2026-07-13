# Optimizer architecture maps

Distilled from the 2026-07 multi-agent review at v2.12.783. Line numbers are anchors that
drift — navigate by function name. All paths relative to `custom_components/power_sync/`.

## Table of contents

1. LP solver core (`optimization/battery_optimizer.py`)
2. Post-solve schedule pipeline (`optimization/coordinator.py`)
3. Coordinator mode/reserve lifecycle (`optimization/coordinator.py`)
4. Force/reserve state machine (`__init__.py`)
5. Tariff windows and special prices
6. Inputs: load estimator and solar forecasters
7. Cycle lifecycle, concurrency, execution

---

## 1. LP solver core (`battery_optimizer.py`, ~3,400 lines)

**Entry**: `optimize()` (~L432). Aligns inputs via `_align_forecasts` →
`n_steps = min(max(len of each non-empty array), horizon_hours*60/interval_minutes)`,
pads each to `n_steps` with `_pad_array`. In production all arrays arrive pre-equalized
(576 slots for 48 h @ 5 min) — see invariants.md before assuming a length mismatch.

**Period coarsening**: the public schedule stays at `interval_minutes`, but the LP coarsens:
1-slot periods for the first 6 h (`LP_NEAR_HORIZON_HOURS`), 30-min to 24 h, 60-min beyond
(`_build_lp_periods`). `_split_lp_period_end` splits a coarse period whenever a
correctness-sensitive input changes (flag flip, price spread > `LP_PRICE_SPLIT_THRESHOLD`
= 2c, net-load sign, surplus). Per-period prices/solar/load are averages; flags take the
first slot's value (safe only because splitting makes them uniform within a period).

**Variable layout** (`_solve_lp_inner`), all kW ≥ 0 except energy:
`grid_import | grid_export | battery_charge | battery_discharge | solar_curtail`
(p_n each), then optional `bonus_export`/`bonus_import` blocks (allocated only when
active — offsets alias harmlessly when only one is active), then energy boundaries
`E[0..p_n]` in kWh.

**Constraints**:
- Power balance (eq): `import − export − charge + discharge − curtail = load − solar`.
- Energy transition (eq): `E[t+1] − E[t] − eff·dt·charge + (dt/eff)·discharge = 0`.
  Efficiency applied once per side (round-trip = eff²) — consistent across LP, greedy,
  hold, and schedule emission.
- Intra-period discharge floor uses the *pre-export-raise* `base_reserve_floor` snapshot,
  so post-window self-consumption may go below a transient export floor (intentional).
- Export backing: `grid_export − discharge ≤ max(0, solar − load)`.
- Bonus buckets: `bonus ≤ physical flow`, battery-export fits bucket, `Σ dt·bonus ≤ cap_kwh`.
- SOC bounds: `E[0]` fixed at `soc_0·cap`; `E[t] ∈ [reserve_floor[t]·cap, ceiling·cap]`
  with a `max(lower, upper)` guard against inverted bounds.

**Objective** (minimize): `+import_price·import·dt`, `−export_price·export·dt`, `−bonus`,
`+curtail penalty`, plus a telescoping terminal valuation (`−terminal·eff·dt` on charge,
`+terminal·(dt/eff)` on discharge, summing to `−terminal·(E_final − E_0)`).
`terminal_price` = cheapest second-half recharge opportunity × `terminal_weight`
(0 when starting below reserve). `eps=1e-7` time preference breaks degeneracy;
`deadline_mode` flips the import tie-break earlier. `predicted_cost` is recomputed from
emitted flows with real prices, NOT `result.fun`.

**Solver**: `_solve_lp_highs` wraps HiGHS via a custom `_LpMatrix` sparse builder.
Only `kOptimal` results are used; a time-limit incumbent is discarded and the code falls
through to **`_solve_greedy`** — the heuristic fallback also used when `highspy` import
fails or the LP raises. Greedy must mirror every LP guard (it has diverged: see
bug-registry.md OB-1).

**Schedule emission**: `_build_schedule_from_solution` maps flow vectors to actions with
`ACTION_THRESHOLD_W = 100.0`. Post-emission branches here too: `free_import_slot`
(`import ≤ 0.001` — now correctly gated on `allow_grid_charge and grid_charge_allowed[t]
and not charge_blocked`), and the v2.12.783 idle→export conversion inside priority windows.

## 2. Post-solve schedule pipeline (`optimization/coordinator.py`)

`_run_optimization` (~L3006) calls `_run_optimizer_once` (executor thread), then applies
overrides **in this order**, each reassigning `self._current_schedule` incrementally:

1. `_spread_import_schedule` — gated by `spread_import_enabled` + `_supports_target_charge_power`.
   Flattens LP charge energy evenly across a contiguous same-price window. Keeps the LP's
   original per-slot `soc` labels (now inconsistent with the new plan).
2. `_spread_export_schedule` — gated by `spread_export_enabled` + `_supports_target_export_power`.
   Window = contiguous run of export-*allowed* slots (default source
   `_positive_price_export_slots`: every slot with export price > 0). Main loop rewrites any
   slot with `soc > floor` — it does NOT filter by original action (bug OB-6); the two
   fallback branches DO filter to export/discharge.
3. `_bridge_short_export_gaps` — always runs, early-returns for dynamic tariffs
   (Amber/AEMO/FlowPowerKWatch). Mutates ScheduleAction objects **in place** (everything
   else builds fresh objects). Checks price match + reserve, not export permission.
4. `_disable_idle_schedule` — provider-gated + `disable_idle_enabled`.
5. `_apply_offgrid_overlay` — Tesla+Powerwall-paired only.

The whole block can run **twice**: after `_apply_auto_reserve_recommendation` / export
reserve floor computation, a second `_run_optimizer_once` re-solves with the new floors.

**Decisions log** (`custom_components.power_sync.optimization.coordinator.decisions`,
pinned INFO): one aggregate per-cycle line of action *counts* from the FINAL post-override
schedule. There is no per-slot decision line. Runtime may still convert idle →
self_consumption (disable-idle at execution, `_effective_runtime_action` inside demand
windows), so the log can legitimately say `idle=N` while hardware runs self-consumption.

**Execution selection**: `_get_current_action` picks the slot with
`timestamp <= now < next.timestamp`; past the last timestamp it returns the **final slot
forever** (matters only if solves keep failing — the schedule is normally refreshed every
cycle). `_execute_optimizer_action` (~L4718) is the ONLY live command path
(`ScheduleExecutor.execute_action` in `executor.py` is dead code).

## 3. Coordinator mode/reserve lifecycle (`optimization/coordinator.py`)

State (instance attrs, none persisted):

| Attr | Meaning |
|---|---|
| `_last_executed_action` | change-detection marker (`idle`/`self_consumption`/`no_discharge`/`charge`/`discharge`/`export`/`off_grid`/None) |
| `_pre_idle_backup_reserve` | user's real reserve %, snapshotted **only when None** (prevents stacked modes clobbering it) — the restore target |
| `_idle_hold_reserve` | reporting-only elevated reserve |
| `_scheduled_ev_no_discharge_active` | EV-preserve engaged flag |
| `_startup_backup_reserve` | authoritative reserve from **config** (deliberately not live HW, to avoid capturing a mid-IDLE elevated value) |

**IDLE hold** (`_set_idle_hold_mode`): work mode → backup/hold AND reserve raised to
`max(soc, idle_floor)` (GoodWe skips reserve — inverted DOD; Sigenergy skips
set_backup_reserve). Exit on action transition, `disable()`, or the polling safety net
(`_should_restore_pre_idle_backup_reserve_from_polling` — covers ONLY the pre-idle reserve,
runs outside the optimization lock).

**EV no-discharge preserve** (`_set_scheduled_ev_no_discharge_mode`): uses
`energy_coordinator.set_no_discharge_mode()` when available, else falls back to
`_set_idle_hold_mode(preserve_charge=True)` (Tesla path — elevates reserve!). Release
(`_release_scheduled_ev_no_discharge_mode`) clears the active flag BEFORE the hardware
await and never retries on failure — asymmetric with `_restore_pre_idle_backup_reserve`,
which keeps state on failure for retry (bugs OB-2/OB-3).

**Restore contract exemplar**: `_restore_pre_idle_backup_reserve` is the correct pattern —
clear state only after confirmed success; return False to be retried.

**Monitoring gate**: set-side checked once at `_execute_optimizer_action` entry;
restore-side gates inside each restore helper block-and-return-False. Enabling monitoring
via the app fires a `force_restore` cleanup that bypasses the block for force modes and
native control — but NOT for the pre-idle elevated reserve (bug OB-8).

**Self-heal drift checks** exist ONLY for Tesla, GoodWe, Sungrow inside the
self_consumption branch. Generic brands (Solax, SAJ, Neovolt, SolarEdge, Fronius, ESY,
Anker, AlphaESS, Sigenergy) have none — and the base `BatteryController` methods return
False instead of raising, so a failed command is silently masked by the unconditional
`_last_executed_action` update (bug OB-4).

## 4. Force/reserve state machine (`__init__.py`, inside `async_setup_entry`)

All state is **per-setup local dicts**: `force_discharge_state`, `force_charge_state`,
`hold_soc_state`, `self_consumption_state`, plus `_command_generation = [0]` (anti-race
counter). Only force charge/discharge are mirrored into `hass.data`.

**Entry points**: services `force_discharge` / `force_charge` / `hold_battery_soc` /
`restore_normal` / `set_self_consumption` / `set_backup_reserve` / curtailment; HTTP views
for the mobile app; and the optimizer's **hardware-only fast path** (`source ==
"optimizer"`) which writes hardware WITHOUT touching `force_*_state` or timers — the LP
owns its own lifecycle.

**Restore triggers**: per-window expiry timer (`async_track_point_in_utc_time`, absolute
UTC `expires_at` — DST-safe); force_charge↔force_discharge mutual-exclusion handoff
(inherits the other's saved baseline); `restore_force_mode_from_persistence` on startup;
manual `restore_normal`.

**Persistence**: `persist_force_mode_state` writes ONE blob — active force charge OR
discharge (charge wins). Hold SoC and self-consumption toggle are **never persisted**
(bug OB-5). Engaging Hold actually writes `None`, clobbering any prior persisted force
state.

**Race guards**: `_command_generation` + synchronous `_cancel_all_force_timers` (iterates
only charge/discharge — hold excluded); `_restore_superseded` per-await re-check exists
ONLY in the Tesla restore path; Modbus brand branches (Sungrow, Sigenergy, FoxESS, GoodWe,
AlphaESS, ESY, SolarEdge, Anker) clear `active=False` unconditionally after their await
(bug OB-9). `async_unload_entry` cancels ~30 timers but none of the force/hold expiry
timers → orphaned closures survive reload with a frozen generation counter (bug OB-7).

**Startup self-heal**: `_restore_disabled_optimizer_reserve_if_stale` is the ONLY generic
stale-state detector and it is narrow by design: excludes Tesla/Sigenergy/GoodWe, runs only
when the optimizer is disabled, and keys off an elevated *reserve* (cannot see a stale
backup/standby work mode). This narrowness is the shared root cause of several confirmed
stuck-state bugs.

## 5. Tariff windows and special prices

- LP prices are **$/kWh** (0.418 = 41.8c). Curtailment automations in `__init__.py` are
  **c/kWh** (`export_earnings < 1`). Units verified consistent at the boundary
  (`get_current_prices_for_curtailment` converts).
- ZeroHero (`zerohero.py`): presets JUL_2026 / CURRENT / LEGACY / CUSTOM; window matching is
  wall-clock `hour*60+minute` on local timestamps. `_apply_zerohero_optimizer_inputs`
  builds `bonus[idx] = max(0, super_export_rate − base_fit)` sized
  `min(len(import), len(export))`, sets the bonus cap, and mutates
  `import_prices[idx] += 5.0` in place ($5/kWh in-window no-import penalty) AFTER display
  prices are captured.
- Flow Power Happy Hour 17:30–19:30 and Export Boost use `_time_window_slots`, which
  recomputes its own **unfloored** `now` (up to one slot of mask misalignment vs the
  floored price grid). ZeroHero uses `_price_timestamps` and is unaffected.
- Priority-export windows: `optimize()` receives `priority_export_slots` +
  `export_bonus_prices` + cap. Since 557cf69a the LP predicates use
  `effective = base + bonus` and drop the acquisition-cost gate for priority slots
  (`not priority_export_slot` in the self-consumption cap). **The greedy path was not
  updated** (bug OB-1). Since f87a2386 the LP post-solve converts in-window idle slots to
  export down to the floor.
- Bridge reserve floors are computed in TWO places and max-merged:
  `_priority_export_reserve_floor_slots` (LP-side, groups by input window mask; blind to
  ZeroCharge import bonuses) and `_post_processed_export_reserve_floor_slots`
  (coordinator-side, groups contiguous export-action runs since b9cb2c7f; a window split
  by one sub-100 W slot still double-counts across the two runs).
- Far-horizon slot labels use naive wall-clock stepping (`now + t·interval`), so on the two
  AU DST transition days per year, slots planned past the 02:00 shift are labeled one hour
  off until `now` passes the transition (self-healing; imminent dispatch correct).

## 6. Inputs: load estimator and solar forecasters

- `LoadEstimator.get_forecast(horizon_hours)` → Watts per slot, always exactly
  `n_intervals` long. Pipeline: recorder history of the configured load entity
  (already brand-filtered — e.g. Sungrow night aliasing is removed upstream) → unit
  multiplier (default kW→W ×1000) → filter `0 < W < 100_000` → away-window exclusion →
  EV-power subtraction (non-Tesla/Sigenergy) → `(dow, hour, half-hour)` buckets in local
  time → 14-day half-life recency weights → MAD outlier clip → optional temperature scale →
  smoothing → recent-regime scale (deadband 0.15, cap 2.5, blend 0.7). Temperature scale
  and recent-regime scale can partially double-count a cold snap.
- Solar: Solcast maps 30-min windows to slots and **zero-fills** gaps/tails; Open-Meteo
  (`_parse_open_meteo_watts`) is a **carry-forward step function** — the last point's value
  persists to the end of the horizon. Benign with real Open-Meteo data (series end with
  explicit night zeros) but a latent asymmetry; prefer zero-fill if touching this code.
- `_apply_solar_nowcast_derate`: live-vs-forecast reality check, fades over 6 h of horizon.
  The derate factor is in-memory, recovers only when `forecast_now ≥ 0.5 kW` AND
  `ratio ≥ 0.9` (+0.08/cycle) — so a cloudy-afternoon derate persists overnight and
  suppresses early-morning decisions until ~40 min of sunny ratio restores it.
- Solar/load forecasts start at exact `dt_util.now()`; price/schedule timestamps are floored
  to the interval boundary — sub-slot misalignment, self-correcting each re-solve.

## 7. Cycle lifecycle, concurrency, execution

**Two independent ~5-min cadences**, deliberately split:
1. `DataUpdateCoordinator` refresh → `_async_update_data` → cached-action execution only
   (running the LP here caused duplicate Modbus writes — see its docstring).
2. The polling loop (`_schedule_polling_loop`, started by `enable()`) — sleeps to the wall
   boundary, executes the cached action, then runs the LP (`_run_optimization`).

Other solve triggers: price-update listener (`_on_price_update`, rate-limited, spawns an
**untracked** background task — survives `disable()`, bug OB-10), initial-startup task,
`force_reoptimize`, settings-driven coalesced re-solve.

**Locking**: `_optimization_lock` serializes solves (non-forced runs skip if locked; forced
queue). The cached-execution path does NOT hold it; its only guards are a
`lock.locked()` check and dedup on `_last_executed_action`, which is written at the END of
`_execute_optimizer_action` after awaited I/O → TOCTOU double-command window at action
transitions (bug OB-11). `disable()` does not take the lock, does not null `_executor`,
and `_execute_optimizer_action` has no `_enabled` guard.

**Error paths**: `_run_optimization`'s `except Exception` logs and swallows;
`_current_schedule` is reassigned incrementally through the override chain, so a mid-chain
raise leaves a partially transformed schedule executing until the next successful cycle
(HD-2). Sensors have no staleness/error field — `optimization_status` stays "active";
only `last_optimization` silently ages. Transient price loss does NOT wipe the plan:
HA coordinators raise `UpdateFailed` (previous data retained), and even empty prices fall
back to default flat rates (0.30/0.08) rather than an empty schedule.
