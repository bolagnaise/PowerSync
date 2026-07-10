# Verified bug registry

From the 2026-07 multi-agent review (7 reviewers + 3 adversarial verifiers) at v2.12.783.
Three sections: **confirmed open bugs** (match symptoms here first — check `git log` for a
`fix:` commit before re-diagnosing; remove entries once fixed), **refuted claims** (do NOT
rediscover these), and **historical fix clusters**. Line anchors drift; trust function names.

## Confirmed open bugs (as of v2.12.783)

### OB-1 — Greedy fallback ignores priority-export bonus windows  [FIXED in 90f9e7f5, 2026-07-07]
Regression test: `tests/test_greedy_priority_export.py` (LP/greedy parity). Original entry
kept for pattern reference:
`battery_optimizer.py::_solve_greedy` (~L2768). 557cf69a added `not priority_export_slot`
to the LP's self-consumption cap (~L2060) but not to the structurally identical greedy cap.
ZeroHero (0c base + capped bonus < acquisition cost) → greedy caps discharge to home load →
`self_consumption` all window. Verified at HEAD: same scenario, LP exports 36/36 slots,
greedy 0/36. Triggers whenever `highspy` is unavailable (platform wheels) or a single LP
solve raises. **Fix**: mirror the LP predicate in the greedy cap. Test: run the same input
through both paths and assert action parity in priority windows.

### OB-2 — EV no-discharge release: flag cleared before await, never retried  [FIXED in 1a5dc753 + 1d245f79, 2026-07-07]
Flag now clears only after confirmed success (retry contract); Sigenergy
set_self_consumption also routes through restore_normal. Regression test:
`tests/test_optimizer_restore_retry.py`. Original entry:
`optimization/coordinator.py::_release_scheduled_ev_no_discharge_mode` sets
`_scheduled_ev_no_discharge_active = False` BEFORE awaiting
`restore_no_discharge_mode()`; on exception/False the early-return at the top makes every
later call a no-op — no retry. Sungrow/FoxESS self-heal via their `restore_normal` paths;
**Sigenergy does not**: its `set_self_consumption` writes EMS mode only and never resets
`REG_ESS_MAX_DISCHARGE_LIMIT`, and it has no drift check → discharge limit stuck at 0
(charges but never discharges). **Fix**: clear the flag only after confirmed success
(mirror `_restore_pre_idle_backup_reserve`); consider routing Sigenergy
set_self_consumption through `restore_normal`.

### OB-3 — `disable()` strands Tesla's EV-preserve elevated reserve  [MAJOR — Tesla]
> **FIXED in 44ce3620 (2026-07-10)** — disable() restores pre-idle reserve ungated from `== "idle"`, adoption path skips when pending; test `tests/test_disable_ev_preserve_reserve.py`.
Tesla lacks `set_no_discharge_mode`, so EV preserve uses
`_set_idle_hold_mode(preserve_charge=True)` → reserve raised to ~SOC (≤80). In `disable()`
the idle-cleanup is gated on `_last_executed_action == "idle"` (it is `"no_discharge"`),
the EV release restores mode only, and Tesla's `restore_normal` handler has no saved
reserve to restore → reserve stuck at ~SOC indefinitely (startup self-heal excludes Tesla).
**Corollary (running case)**: the self-consumption self-heal ADOPTS the elevated reserve
into `_startup_backup_reserve` when `current_reserve <= soc`, silently overwriting the
user's real reserve in the optimizer's own model. **Fix**: in `disable()`, call
`_restore_pre_idle_backup_reserve` whenever `_pre_idle_backup_reserve is not None`
(ungate from `== "idle"`); make the adoption path skip when `_pre_idle_backup_reserve` set.

### OB-4 — Failed restore-to-self-consumption recorded as success  [FIXED in 1a5dc753, 2026-07-07]
Marker now advances only on success. Regression test:
`tests/test_optimizer_restore_retry.py`. Original entry:
`optimization/coordinator.py` self_consumption branch: return of
`battery.set_self_consumption_mode()` discarded; `_last_executed_action` set
unconditionally → next cycle's change-detection skips the command. Base
`BatteryController` returns False (never raises), so the swallowed-False path is the
guaranteed masking route. Tesla/GoodWe/Sungrow have drift checks; Solax, SAJ, Neovolt,
SolarEdge, Fronius, ESY, Anker, AlphaESS, Sigenergy have none → inverter stays in prior
forced mode while PowerSync believes self_consumption. **Fix**: only advance
`_last_executed_action` on success so the next cycle retries.

### OB-5 — Hold SoC not persisted; restart/reload mid-hold freezes the battery  [MAJOR, optimizer-disabled cohort]
> **FIXED in 0ae52626 (2026-07-08)** — persist hold_soc with absolute expiry + restore-or-exit; test `tests/test_hold_soc_persistence.py`.
`__init__.py`: `persist_force_mode_state` serializes only force charge/discharge — with
only Hold active it writes `None` (also clobbering any prior persisted force state, and
falsely implying durability). Hold puts hardware in backup/standby (Sigenergy STANDBY /
FoxESS Backup / Sungrow cap-0 …); after restart/reload the in-memory `hold_soc_state` +
expiry timer are gone; no startup path detects a stale standby work mode
(`_restore_disabled_optimizer_reserve_if_stale` is reserve-keyed and excludes
Tesla/Sig/GoodWe). Optimizer enabled → next cycle overwrites (mitigated); optimizer
disabled → battery frozen indefinitely, nothing in the UI. **Fix**: persist
`hold_soc_state` (with absolute expiry) and restore-or-exit on setup; or add a
non-reserve-keyed standby cleanup for the disabled-optimizer path.

### OB-6 — `_spread_export_schedule` rewrites LP `charge` slots to `export`  [MAJOR, spread-export opt-in]
> **FIXED in a8eb45bf (2026-07-08)** — spread_positions excludes charge slots; test `tests/test_spread_export_charge_preservation.py`.
`optimization/coordinator.py::_spread_export_schedule`: `spread_positions` takes every slot
in a contiguous export-*allowed* run filtered only by SOC>floor — never by original action.
Default allowed-mask (`_positive_price_export_slots`) marks every positive-export-price
slot, so a run can span cheap overnight charge slots and evening export slots; the charge
slot is rewritten to export (buy-low plan inverted). The two fallback branches DO filter to
export/discharge — the main loop is the asymmetry. Gated by `spread_export_enabled` +
target-export-power brand support. **Fix**: exclude non-export/discharge original actions
from `spread_positions` (preserve `charge` slots).

### OB-7 — `async_unload_entry` cancels no force/hold expiry timers  [MEDIUM]
> **FIXED in 0ae52626 (2026-07-08)** — unload cancels force/hold expiry timers (co-designed with OB-5).
Orphaned pre-reload timer closures survive with a frozen `_command_generation` and the old
state dicts (whose `active` flags unload never resets), and fire against the new setup.
Normal reload → redundant restore at the same absolute expiry (benign); force window
*extended after reload* → the orphaned timer fires at the OLD expiry and prematurely
restores. Also a leak (one timer set per reload). **Fix**: cancel
`cancel_expiry_timer`/`cancel_hardware_refresh_timer` on force_charge/force_discharge/hold
states in `async_unload_entry`.

### OB-8 — Monitoring-enable cleanup misses the pre-idle elevated reserve  [MEDIUM, narrow]
> **FIXED in 44ce3620 (2026-07-10)** — monitoring-enable cleanup now includes `_restore_pre_idle_backup_reserve` with a bypass flag; test `tests/test_disable_ev_preserve_reserve.py`.
Enabling monitoring fires a `force_restore` cleanup that releases force modes and native
control (this REFUTES the broad "monitoring strands everything" claim), but nothing
restores an IDLE/EV-elevated backup reserve, and afterwards the restore-side monitoring
gates block the optimizer's own retries. Sequence: monitoring off → IDLE hold raises
reserve → monitoring enabled → reserve stuck elevated. **Fix**: include
`_restore_pre_idle_backup_reserve` (with a bypass flag) in the monitoring-enable cleanup.

### OB-9 — Modbus brand restore branches lack `_restore_superseded` re-checks  [MEDIUM, structural]
> **FIXED in abec9e82 (2026-07-08)** — all 12 brand branches guarded (registry undercounted at 8; +Solax/SAJ/Fronius/Neovolt); structural AST test auto-discovers branches. Test `tests/test_restore_superseded_parity.py`.
`__init__.py::handle_restore_normal`: Tesla re-checks `_restore_superseded` after every
await; Sungrow/Sigenergy/FoxESS/GoodWe/AlphaESS/ESY/SolarEdge/Anker branches
`await coord.restore_normal()` then unconditionally clear `force_*_state["active"]` — a
force command interleaving during that await gets clobbered (its expiry timer then no-ops).
Persistent harm needs optimizer-disabled + no hardware force timeout + precise timing, so
verified as structural-inconsistency rather than a demonstrated field failure. **Fix**: add
the same superseded re-check before clearing state in each brand branch (cheap parity fix).

### OB-10 — Price-triggered solve survives `disable()` and re-commands the battery  [MAJOR]
> **FIXED in 039d796a (2026-07-08)** — price task tracked+cancelled in disable(); _enabled guard; test `tests/test_disable_and_double_command.py`.
`optimization/coordinator.py::_on_price_update` spawns an untracked background task;
`disable()` cancels four other tasks but cannot cancel this one, doesn't take the
optimization lock, and doesn't null `_executor`; `_run_optimization` checks `_enabled`
only at entry and `_execute_optimizer_action` has no `_enabled` guard. Since the LP solve
is the long pole, "disable restores normal → stray solve re-commands" is the likely
ordering when they coincide. **Fix**: store + cancel the price task in `disable()`, AND
add an `_enabled` re-check at `_execute_optimizer_action` entry (also fixes half of OB-11).

### OB-11 — Double hardware command at slot boundaries  [MEDIUM]
> **FIXED in 039d796a (2026-07-08)** — _execute_lock serializes cached + in-cycle execution.
The polling loop and the DataUpdateCoordinator refresh both call
`_execute_cached_current_action_if_changed`; dedup reads `_last_executed_action`, written
only AFTER awaited hardware I/O, and no reentrancy guard covers the cached path → at an
action transition both cadences can issue the command (double force-timer extension, double
Tesla TOU upload, Modbus contention). End state idempotent; harm = redundant writes +
rate-limit pressure. **Fix**: an in-flight guard/lock around `_execute_optimizer_action`
(shared root cause with OB-10).

### OB-12 — AlphaESS curtailment drops an active force/optimizer dispatch  [MEDIUM]
> **FIXED in 924c5bae (2026-07-08)** — _alphaess_force_dispatch_active guard; test `tests/test_alphaess_curtailment_force_guard.py`.
`__init__.py::handle_alphaess_curtailment` (~22086) has no active-force guard (FoxESS/
GoodWe/SolarEdge all have one), and `inverters/alphaess.py::curtail` (~438) releases the
active dispatch (REG_DISPATCH_START=0) before writing the export limit. Negative-price
window + optimizer CHARGE dispatch → curtailment kills the paid-to-charge dispatch at the
transition; optimizer re-issue recovers within ~1 interval (why MEDIUM). **Fix**: add an
`_alphaess_force_dispatch_active` guard mirroring the FoxESS pattern.

### OB-13 — `grid_charge_soc_cap` is inert without solar (its primary use case)  [MEDIUM-HIGH, empirically verified]
> **FIXED in 18659cbb (2026-07-08)** — grid-charge fill now advances the cap projection; regression test `tests/test_grid_charge_soc_cap.py`.
`optimization/coordinator.py::_grid_charge_allowed_slots` (~9962): the projected SOC only
advances on *solar surplus*, never on the grid charging being permitted — so with low/no
solar the cap never trips and the LP grid-charges past it (repro: cap 50%, SOC 20%, no
solar → all slots allowed). The existing test only covers SOC 1% below cap. **Fix**: model
the grid-charge contribution (assume `max_charge_kw` fill in allowed slots) in the
projection; add a no-solar regression test.

### OB-14 — Auto-reserve scalar floor over-applied outside its export run  [MEDIUM, CONFIRMED]
> **FIXED in c2aaa1b1 (2026-07-08)** — per-slot reserve floor now wins over the cross-run scalar via the matched_per_slot gate in coordinator.py::_force_discharge_reserve_floor (~1160-1169): when the per-slot lookup matches the action slot, the scalar best_meta branch is skipped (guarded by `if not matched_per_slot and self.auto_apply_reserve_enabled`). Regression test tests/test_reserve_floor_scoping.py. (Fix predates range 3303bb7f..HEAD but is unmarked at HEAD; matched_per_slot verified present.)
`optimization/coordinator.py::_force_discharge_reserve_floor` (~1130-1168): with Auto-Apply
Reserve on, the scalar `best_meta` floor is `max()`ed onto every same-day action (gate at
~1162 only drops it when the max run starts on a different DATE). Verified at the producer
(~1372-1396): `best_meta` tracks only the single highest-floor run, while the per-slot
`floors` array correctly scopes each run — so a morning window planned by the LP to export
down to its own 30% floor is blocked at the evening run's 60% at execution time
(plan-vs-execution divergence; conservative direction, over-holds SOC). **Fix**: skip the
scalar when the per-slot lookup matched the action's slot, or scope the scalar to
`best_meta`'s own run window.

### OB-15 — EV plan regeneration picks weekday from OS-local time  [MEDIUM-HIGH for UTC-container installs, HIGH confidence]
> **FIXED in 06f277a6 (2026-07-08, co-designed with OB-31)** — _regenerate_plan → HA-local; test `tests/test_ev_plan_clock.py`.
`automations/ev_charging_planner.py::_regenerate_plan` (~4762): `now = datetime.now()`
(OS-local) drives the weekday-keyed `departure_times` lookup, the departure rollover, and
`get_effective_priority(weekday)`. The sibling `_evaluate_vehicle` was already fixed for
exactly this (derives weekday from `dt_util.now()`, comment "container UTC would
mis-classify weekday near midnight") — `_regenerate_plan` is the missed spot of that fix
wave (f9f6b982 class). Sydney + OS=UTC: Monday 06:00 local reads as Sunday → wrong day's
deadline/priority for the whole UTC-offset window, and `plan_charging`'s naive date
mismatch clamps hours_available to 1. No self-heal within the window. **Fix**: mirror the
`_evaluate_vehicle` pattern (`ha_now = dt_util.now()`; use `ha_now`-derived weekday and
candidates). Adversarial note: the related `state.current_window`/`started_at` OS-local
usage is status-only (control path `should_charge_now` is tz-correct) — do not over-fix.

### OB-16 — FoxESS force paths re-capture the temporary work mode as the restore baseline  [MAJOR, HIGH confidence]
> **FIXED in 6162a4ab (2026-07-08)** — `is None` guard mirrored in both force paths; regression test `tests/test_foxess_force_restore_baseline.py`. (Corrected from a stale/invalid truncated sha "6162ab" — all other FIXED shas in this registry are 8-char.)
`inverters/foxess.py` force_charge (~1058) / force_discharge (~1093): `_original_work_mode`
is captured **unconditionally** on every call (the sibling `set_backup_mode` ~1173 has the
`if self._original_work_mode is None` guard — self-evident in-file asymmetry). The
optimizer re-issues force every cycle to keep the ~600 s HW timeout alive, so cycle 2 reads
the temporary Feed-in (discharge) / Backup (charge) mode and overwrites the baseline;
`restore_normal` (~1130) then restores the temporary mode. H1/H3/KH affected (H3-Pro/Smart
skip the mode change). All rescues ruled out: the HW timeout reverts remote-control but NOT
work_mode (in-code comment), self_consumption is change-detection-gated (OB-4 class),
FoxESS has no drift check, the startup heal is reserve-keyed, and optimizer-sourced force
schedules no expiry timer. **Fix**: mirror the `is None` guard in both force paths (and
for `_original_min_soc`); regression test the two-cycle re-issue → restore sequence.

### OB-17 — Solax `force_time` profile snapshots force-modified entities  [MEDIUM]
> **FIXED in a1451558 (2026-07-08)** — per-key re-capture guard; regression test `tests/test_solax_force_time_snapshot.py`.
`inverters/solax_battery.py::_save_force_time_states` (~1009): unconditional overwrite, no
re-capture guard, no force-value filter (contrast Neovolt's `_stable_restore_mode` filter
and SAJ's `is None` guard). Per-cycle re-issue captures forced values (grid_export_limit,
currents, charge window, allow_grid_charge) as baseline; `restore_normal` replays them.
Harm bounded: restore also forces charger_use_mode=Self Use, so the damage is latent
period-1/export register corruption (real if the user later selects Force-Time). Cohort
narrow (third-priority profile). **Fix**: add a re-capture guard + filter force values.

### OB-18 — Non-BLE dynamic EV sessions never detect unplug: leaked lease + endless commands  [MEDIUM, HIGH confidence]
> **FIXED in 7bdb4b64 (2026-07-08)** — generic unplug detection + 2-cycle debounce (BLE exempt); test `tests/test_ev_ownership_leaks.py`.
`automations/actions.py::_clear_ble_dynamic_session_if_unplugged` (~4503) early-returns for
any vehicle whose VIN doesn't start with `ble_`, and it is the ONLY unplug detector in the
dynamic loops (~5106, ~5911). Fleet/Teslemetry/OCPP/generic loadpoints: mid-session unplug
→ session stays active, `ev_ownership` lease persists (blocking cross-family starts for
the other vehicle via `can_take_over_ev_ownership`), and the timer keeps issuing
`_set_vehicle_amps` against the unplugged car (Fleet rate-limit pressure). Restart clears
leases; nothing does in-session. **Fix**: generalize the unplug check to all providers
(plug-state per provider) and release the lease + deactivate the session on unplug.

### OB-19 — No-VIN manual stop records a `_default` hold that suppresses every vehicle  [MEDIUM, HIGH confidence, verified empirically]
> **FIXED in 7bdb4b64 (2026-07-08)** — resolve charging VIN before manual-stop hold; skip _default when ambiguous+multi-vehicle.
`automations/actions.py` (~1265-1282, ~1776) + `automations/ev_ownership.py::manual_stop_hold_reason`
(~100): a `stop_ev_charging` without vehicle_id/vin keys the 15-min restart-suppression
hold under `_default`, and every VIN's hold lookup includes `_default` as a candidate —
so stopping car A without a VIN blocks car B's scheduled/price start for 15 min (the
historical multi-Tesla suppression class, manual-stop variant). **Fix**: resolve the
actively-charging VIN before recording the hold, or scope `_default` holds to
single-vehicle installs. Regression-test with two VINs per the playbook.

### OB-20 — EPEX default export price is the retail import price, not wholesale  [MEDIUM, HIGH confidence]
> **FIXED — production in ac4608d5 (2026-07-08), regression test in 3fcb49a3 (2026-07-08).** EPEXPriceCoordinator._async_update_data (coordinator.py ~3734-3746) now defaults export_ct = 0.0 with a one-time _warned_export_rate_unset warning instead of export_ct = -total_ct (retail); configured-rate and entity overrides unchanged. Test tests/test_epex_export_price.py (AST source-extraction). (ac4608d5 lands ~1 min before range start 3303bb7f — the context assumed a concurrent-range commit; the diff shows it is ac4608d5 'fix(flow-power): fall back to AEMO when KWatch is unavailable'.)
`coordinator.py::EPEXPriceCoordinator._async_update_data` (~3614-3627): with no configured
export rate (the default), `export_ct = -total_ct` where `total` is the *final consumer
price* (wholesale + surcharge + VAT, per the class docstring) — the inline comment claims
"wholesale approximation". The LP then values feed-in at the retail rate (e.g. ~27 ct
instead of ~8 ct wholesale), so it exports midday energy it should hold for the evening
peak — money-losing on every default EPEX install. **Fix**: derive the default export
from the wholesale component (or 0 with a warning), keeping the configured-rate and
entity overrides as-is.

### OB-21 — Stuck `_skip_reload` flag swallows the next structural options reload  [MEDIUM, HIGH mechanism-confidence]
> **FIXED in c8f514e1 (2026-07-10)** — gate each `_skip_reload` set on actual persisted-state change so the flag is never left unconsumed; test `tests/test_battery_export_allowed_slots.py; tests/test_config_flow_weather_options.py`.
`optimization/coordinator.py::set_settings` (9 flag-set sites) + `config_flow.py` (~9343)
set `_skip_reload=True` BEFORE `async_update_entry`; HA fires no update listener on a
no-op write, so an unchanged settings push (the companion app's periodic sync, or
submitting the options flow without changes) leaves the flag stuck. The next genuine
config change (Modbus host, provider…) fires the listener, which pops the stale flag and
returns — the required reload silently never happens. **Fix**: only set the flag when the
new options differ (or clear it with a call_soon fallback / timestamp TTL).

### OB-22 — Live hardware-reserve change never reaches Sigenergy's restore target  [MEDIUM, Sigenergy-only]
> **FIXED in 5b01fc8f (2026-07-10)** — set_settings/_deferred_enable_restore now sync `controller._restore_backup_reserve_pct` on the persistent Sigenergy coordinator controller; test `tests/test_reserve_source_of_truth.py`.
`set_settings` hardware block (~10808) updates `_startup_backup_reserve` and the LP but
NOT `controller._restore_backup_reserve_pct`, which is assigned once at setup
(`__init__.py` ~32856) and written back to hardware by `sigenergy.py::restore_normal`
(~1376). User changes reserve 20→10 in the app (deliberately no reload): every post-force
restore sets hardware back to 20 until a restart. **Fix**: push the new value to the
controller in the hardware block (and on `_deferred_enable_restore`).

### OB-23 — EV load profile bucketed by UTC hour, queried by local hour  [MEDIUM, HIGH mechanism-confidence]
> **FIXED in 36b0c21d (2026-07-08)** — dt_util.as_local before bucketing; test `tests/test_ev_load_profile_bucketing.py`.
`automations/ev_charging_planner.py::LoadProfileEstimator.update_from_history` (~1232):
buckets by `state.last_updated.hour`/`weekday()` — HA stores last_updated in UTC — while
`estimate_load_at_hour` consumers (~1564, ~2251, ~4807) query by HA-local hour. Non-UTC
installs get the learned load curve rotated by the UTC offset (Sydney: evening peak lands
in pre-dawn buckets), so `forecast_surplus` computes solar surplus against the wrong load
shape and SOLAR_PREFERRED EV plans gate charging on shifted windows. Default-profile
fallback only fires on empty buckets; profile cached hourly, no self-heal. Same class as
f9f6b982/OB-15, third instance. **Fix**: `local = dt_util.as_local(state.last_updated)`
then bucket by `local.hour`/`local.weekday()` (the query-window epoch math at ~1203 is
correct — don't touch it).

### OB-24 — Tesla local-Powerwall fallback feeds home_load with EV included  [MEDIUM, HIGH confidence]
> **FIXED in ef3d28e7 (2026-07-08)** — Tesla local fallback subtracts observed EV power; test `tests/test_home_load_telemetry.py`.
`coordinator.py::TeslaEnergyCoordinator._local_powerwall_energy_data` (~1809): the cloud-
outage fallback reads raw `snap.load_w` directly, bypassing `snapshot_as_api()`'s EV
subtraction (`powerwall_local/coordinator.py` ~258), and reports `ev_power: 0.0`. The main
Tesla path subtracts EV upstream and the load estimator deliberately skips EV subtraction
for Tesla — so during any cloud hiccup with a Wall Connector charging, home_load jumps by
the EV draw AND those points permanently poison the estimator's 30-day training history.
Side note: the same path passes buy/sell = 0.0, skipping cost accounting for the window.
**Fix**: use the EV-adjusted load (mirror snapshot_as_api) in the fallback.

### OB-25 — SAJ H2 load falls back to gridPower (battery charge baked into home_load)  [MEDIUM]
> **FIXED in ef3d28e7 (2026-07-08)** — SAJ balance-formula load fallback; test `tests/test_home_load_telemetry.py`.
`inverters/saj_h2.py` (~92, ~275, ~408): the load slot is `("TotalLoadPower", "gridPower")`
resolved once and cached — installs missing `TotalLoadPower` permanently source home_load
from the NET GRID leg. During grid charging, load reads house + battery-charge (the exact
training contamination class); during self-consumption it reads ≈0. No drift check, no
self-heal. **Fix**: compute a balance-formula fallback (solar+grid+signed battery) instead
of raw gridPower, matching the other brands.

### OB-26 — Same-endpoint AC-curtailment runtime guard is model-gated  [MEDIUM]
> **FIXED in c4eca1f9 (2026-07-08)** — dropped model gate; host/port/slave equality decides. Part 2 skipped (would block legit separate SH inverter). Test `tests/test_same_endpoint_curtailment_guard.py`.
`__init__.py::ac_inverter_is_same_hybrid` (~22420): returns True only for
`inverter_brand=="sungrow" and model.startswith("sh")` — but the config-flow rule rejects
same-endpoint configs for ANY model, and `get_models_for_brand` ignores its
`battery_system` parameter (the "filter out SH-series" call-site comment never takes
effect), so non-SH/empty-model same-endpoint configs exist and bypass the guard: every
curtailment cycle opens a second Modbus client against the battery's own endpoint. The
runtime guard exists precisely for pre-validation configs; it misses them. **Fix**: drop
the model gate (host/port/slave equality suffices) and fix get_models_for_brand.

### OB-27 — Brand switch leaves stale host keys; setup dispatches by host precedence  [MAJOR]
> **FIXED in 98184399 (2026-07-08)** — runtime dispatch now gated on _active_battery_system(entry, hass): explicit CONF_BATTERY_SYSTEM (options-over-data) is authoritative, host-key detection is legacy-only fallback; all is_<brand> flags in async_setup_entry derive from it and feed the coordinator dispatch chain (__init__.py ~17426+). Brand switch pops every other brand's BATTERY_SYSTEM_CONNECTION_KEYS in config_flow.py::_save_battery_system_selection. Test tests/test_solaredge_runtime.py.
`config_flow.py` (~6641-6669): changing battery system in the options flow does additive
merges — no code path pops the old brand's host/station keys. Setup picks the coordinator
by host-key PRESENCE in a fixed if/elif chain (`__init__.py` ~17346+), not by
`CONF_BATTERY_SYSTEM` — a stale higher-precedence key wins: Sungrow→GoodWe switch runs a
Sungrow coordinator against the dead endpoint while the settings/API layer reports GoodWe
(split-brain); any-brand→Tesla never engages Tesla. Aggravator: the new battery_system is
persisted at the menu step, so abandoning the dialog corrupts too. The identical class was
already fixed for tariffs (price coordinators gated on `electricity_provider == X`).
**Fix**: pop the old brand's connection keys on switch AND gate the dispatch chain on
`CONF_BATTERY_SYSTEM == brand` (runtime guard for existing corrupted entries).

### OB-28 — Unload shuts down only Sungrow's Modbus client; other brands leak sockets  [MEDIUM-HIGH]
> **FIXED in e492ac52 (2026-07-08)** — unload loops async_shutdown over all 12 brand coordinators; test `tests/test_unload_coordinator_teardown.py`.
`__init__.py::async_unload_entry` (~34214-34237): calls `async_shutdown()` only on
`sungrow_coordinator`; the 11 other brand coordinators (Sigenergy, AlphaESS, FoxESS,
GoodWe, Solax, SolarEdge, SAJ, Fronius, Neovolt, Anker, ESY) all have `async_shutdown`
methods that are never invoked — one orphaned AsyncModbusTcpClient per reload until the
inverter's connection pool exhausts ("Failed to connect" after several options changes).
AlphaESS is worse: its shutdown does `release_dispatch()` first ("no auto-revert: 0722H=1
stays locked") — skipping it on removal can strand forced dispatch. **Fix**: iterate all
brand coordinators and await async_shutdown in unload.

### OB-29 — Reload mid-off-grid strands the Powerwall islanded; orphan cleanup is a no-op  [MAJOR — physical safety]
> **FIXED in abbecbfe (2026-07-08)** — release(force=True) reconnects an orphaned islanded Powerwall; test `tests/test_offgrid_orphan_reconnect.py`.
`powerwall_local/curtailment_fallback.py` (~99-105, ~231-239) + `optimization/coordinator.py`
(~2907-2928): the off-grid-as-curtailment session flag (`_active`) is in-memory only; a
reload/restart while islanded rebuilds it False. The startup orphan cleanup detects the
islanded grid_status and calls `release("startup_orphan_cleanup")` — but `release()`
early-returns `True` when `not self._active`, which is BY DEFINITION the orphan state: it
logs "reconnecting" and never calls `reconnect_grid()`. `check_safety` and both other
release sites guard on the same dead flag; Tesla islanding has no hardware auto-revert.
The battery discharges with no SOC-floor reconnect until 0% → house blackout; only the
manual `powerwall_reconnect_grid` service (which bypasses the guard) recovers. **Fix**:
the orphan path must call `coord.client.reconnect_grid()` directly or `release(force=True)`
bypassing the `_active` guard; also persist `_active` with startup restore. Highest-priority
unfixed bug in the registry.

### OB-30 — The 2-second local Powerwall poller is never stopped on unload  [MEDIUM-HIGH]
> **FIXED in e492ac52 (2026-07-08)** — unload stops the powerwall_local poller (unsub keepalive + null update_interval + async_shutdown); test `tests/test_unload_coordinator_teardown.py`.
`__init__.py::async_unload_entry` (~34204) tears down only the (inert) signaling client;
the `powerwall_local` coordinator's keepalive listener (`powerwall_local/coordinator.py:96`)
is never unsubscribed, so the event-loop timer keeps the coordinator alive after
`hass.data.pop` — one leaked 2 s TEDAPI poller (2 signed POSTs each) per reload, forever.
After a key-rejection unpair+reload the orphan polls a rejecting gateway indefinitely.
Same class as OB-28 but a distinct object with far worse cadence (the views' own teardown
does it right — only unload is missing it). **Fix**: in unload, set the coordinator's
`update_interval = None`, call `_keepalive_unsub()`, and await `async_shutdown()`.

### OB-31 — EV plan staleness throttle compares HA-local vs OS-local clocks  [MEDIUM, numerically verified]
> **FIXED in 06f277a6 (2026-07-08, co-designed with OB-15)** — staleness read moved to HA-local in both consumers; test `tests/test_ev_plan_clock.py`.
`automations/ev_charging_planner.py::refresh_optimizer_forecast_plans` (~4996-5012) reads
plan age with `_ha_local_now_naive()` while `last_plan_update` is stamped with OS-local
`datetime.now()` (~4762/4817/4390). UTC-container installs: east of UTC the age always
reads > 5 min → full plan regeneration on EVERY LP solve (redundant recompute); west of
UTC the age is negative → this path never independently refreshes the LP's EV forecast
(rescued only while the control loop actively evaluates the vehicle). Fourth member of
the OB-15/OB-23/f9f6b982 family — the family sweep is now COMPLETE (repo-wide inventory
traced; all other candidates cleared). **Fix**: stamp and read `last_plan_update` on one
clock (note: fixing OB-15's `datetime.now()` alone would flip which consumer breaks —
change both together).

### OB-32 — Generic saving-session force_discharge truncates at 30 min  [MEDIUM-HIGH]
`__init__.py::GenericSavingSessionManager` (~3044-3060): `_enter_session_mode` issues one
`SERVICE_FORCE_DISCHARGE` with `{}` → default 30-min duration; the per-minute check has no
re-issue branch while `active and _in_session_mode`, so after the expiry restore the
battery self-consumes through the rest of the session. Bites the optimizer-disabled
non-Tesla cohort cleanly (enabled cohort gets force-vs-LP contention instead). **Fix**:
re-arm for the event window — but gated OFF when the optimizer is active (interaction #4).

> **FIXED in 1b9a9cdc (2026-07-10)** — re-arm added, gated off when optimizer is active; test `tests/test_generic_force_event_rearm.py`.

### OB-33 — Generic AEMO spike force_discharge has the same 30-min truncation  [MEDIUM-HIGH]
`__init__.py::GenericAEMOSpikeManager` (~2553-2583): identical shape — `elif is_spike and
self._in_spike_mode:` only logs "Still in spike mode". Spikes routinely outlast 30 min and
are the highest-value export events of the year; the battery stops at T+30. The Tesla
variant is immune (window-valid TOU upload, no force). Shared root cause and fix gating
with OB-32.

> **FIXED in 1b9a9cdc (2026-07-10)** — re-arm added, gated off when optimizer is active; test `tests/test_generic_force_event_rearm.py`.

### OB-34 — Tesla session/spike managers re-capture their own uploaded tariff as baseline  [MAJOR — Tesla TOU-only cohort]
`__init__.py::SavingSessionTariffManager._enter_session_mode` (~2776/2801, restore
~2931-2945; identical in `AEMOSpikeManager` ~2206/2235): `_in_session_mode`/`_saved_tariff`
are in-memory; a reload mid-event re-enters and captures the live tariff — which is now
the manager's OWN `OCTOPUS-SAVING-SESSION`/`AEMO-SPIKE` upload (buy = max(2×sell, 5)).
`_select_restorable_tesla_tariff` filters only force-tariff markers, not these codes. Exit
then "restores" the inflated tariff as normal → Tesla permanently on a £5+/kWh-buy tariff,
draining to grid nightly, until manual reconfiguration. OB-16/OB-17 class (temporary state
captured as baseline). **Fix**: `if self._saved_tariff is None` re-capture guard + extend
the restorable-tariff filter to reject the managers' own codes + persist the event state.

> **FIXED in 42f21caa (2026-07-10)** — filter rejects AEMO-SPIKE/OCTOPUS-SAVING-SESSION codes plus re-capture guard on both managers; test `tests/test_tesla_spike_session_tariff_baseline.py`.

### OB-35 — Dead `ml_optimization_enabled` guard: money-event managers double-control with the LP  [MEDIUM-HIGH]
`__init__.py` ~18054-18063: the comment says "Skip if Smart Optimization is enabled — it
handles spike detection instead" and the flag is computed — then referenced NOWHERE. All
four managers (Tesla/generic AEMO spike, Tesla/generic saving session) are created purely
on their user toggles and run concurrently with the LP's session/spike overlays: generic
force blows through the LP's reserve bridging then truncates (OB-32/33); Tesla managers
and the LP TOU-sync thrash the same tariff endpoint (2 writers, 1-min vs 5-min cadence).
Concrete dropped-guard bug extending interaction #4 to ALL manager variants. **Fix**:
apply the computed guard at all four creation sites (managers only when optimizer is off),
or route events through the LP.

> **FIXED in f3ff4b47 (2026-07-10)** — ml_optimization_enabled guard applied at all four manager-creation sites; test `tests/test_money_event_manager_optimizer_gate.py`.

### OB-36 — VPP AEMO spike variant: third 30-min truncation instance + hardcoded threshold  [MEDIUM-HIGH]
`__init__.py::check_aemo_spike_for_vpp` (~32943-33043): a separate inline copy of the
OB-33 shape — `force_discharge {"duration": 30}` on entry, log-only "still in spike"
branch, no re-arm; AND `is_spike = region_price >= 3000` hardcoded, ignoring
`CONF_AEMO_SPIKE_THRESHOLD` that the standalone managers honor (Globird users' configured
threshold silently inert here). Same fix gating as OB-32/33 (interaction #4).

> **FIXED in 1b9a9cdc (2026-07-10)** — threshold now honors `CONF_AEMO_SPIKE_THRESHOLD`; VPP truncation half left open (gated under LP-active path, deferred pending design decision). Test `tests/test_generic_force_event_rearm.py`.

### OB-37 — Tesla money-event managers and the demand-charging toggle bypass monitoring mode  [MEDIUM — invariant violation]
`AEMOSpikeManager` (~2172-2362) and `SavingSessionTariffManager` (~2745-2985) issue tariff
uploads and operation-mode POSTs directly against the Fleet API with no monitoring check
at class, scheduling, or method level (the generic managers route via services, which
block; the optimizer's TOU sync checks `_is_monitoring_mode()`). Same class:
`auto_demand_charging_check` (~32019-32073) calls `set_grid_charging_enabled` every peak
minute with no monitoring gate (verified). External-controller users with monitoring
forced on still get tariff overwrites, mode switches, and grid-charge toggles — violating
monitoring's "no hardware command" contract on both enter AND exit. **Fix**: gate all
three surfaces on monitoring mode.

> **FIXED in d2b5add3 (2026-07-10)** — monitoring-mode gate applied to AEMOSpikeManager, SavingSessionTariffManager, and auto_demand_charging_check; test `tests/test_money_event_monitoring_gate.py`.

### OB-38 — Tesla spike/session exit clears state even when the tariff restore failed  [MEDIUM-HIGH]
`AEMOSpikeManager._exit_spike_mode` (~2357) / `SavingSessionTariffManager._exit_session_mode`
(~2968): `send_tariff_to_tesla` returns False on failure (never raises); both exits log
the failure then unconditionally clear `_in_*_mode` — no retry ever. The Powerwall stays
on the manager's own inflated tariff (buy ≥ £5/kWh, fake-high sell → nightly grid drain):
the OB-34 outcome via a transient upload failure instead of a reload. Self-heals within
one cycle only when the optimizer is enabled (its TOU sync re-uploads); the
optimizer-disabled Tesla cohort — the managers' target audience — has no rescue. **Fix**:
keep `_in_*_mode` set on failed restore so the next minute retries (the
`_restore_pre_idle_backup_reserve` contract), co-designed with OB-34's re-capture guard.

### OB-40 — Tesla managers capture `_saved_operation_mode` unconditionally  [MEDIUM — Tesla]
OB-34's fix (42f21caa) guards `_saved_tariff` capture with `is None`, but Step 2's
`_saved_operation_mode = site_info.get("default_real_mode")` (`AEMOSpikeManager` ~2391,
`SavingSessionTariffManager` mirror ~2976) is still captured unconditionally on every
enter. A reload mid-event re-enters with the manager's own event mode live, so the exit
restores "autonomous" (or whatever the event set) instead of the user's real operation
mode — the OB-34 corruption shape, on the operation-mode axis. **Fix**: mirror the
`is None` capture guard from 42f21caa on `_saved_operation_mode` in both managers.

### OB-41 — Tesla money-event state does not survive a reload  [MEDIUM-HIGH — Tesla, optimizer-disabled cohort]
Neither `AEMOSpikeManager` nor `SavingSessionTariffManager` persists
`_saved_tariff`/`_saved_operation_mode`/`_in_*_mode`; a reload mid-event forgets that a
restore is owed, leaving the Powerwall on the manager's inflated event tariff (buy ≥
£5/kWh). Optimizer-enabled installs self-heal via the next TOU sync; the
optimizer-disabled cohort — the managers' target audience post-f3ff4b47 — has no rescue
(the OB-5 persistence pattern, on the tariff axis). Related corner: exit never resets
`_saved_tariff = None`, so a user who changes their real tariff between two events in the
same process restores the stale genuine tariff. **Fix**: persist event state with the
hold_soc persistence pattern (0ae52626) and restore-or-exit on setup; clear
`_saved_tariff` after a confirmed successful restore. Co-design with OB-34/OB-38's
capture-once + retry contract (42f21caa, c3101f2c).

### OB-39 — Residual unconditional `_skip_reload` sites outside `set_settings`  [MEDIUM]
> **FIXED in a9bcd2c8 + b9d92505 (2026-07-10)** — c8f514e1's persisted_changed no-op gate applied to the `:29908` reserve site (a9bcd2c8) and the three API views at `:8051`/`:8080`/`:9053` (b9d92505); tests `tests/test_reserve_source_of_truth.py`, `tests/test_force_mode_controls.py`.
OB-21's fix (c8f514e1) covers `optimization/coordinator.py::set_settings` (9 sites) and the
options-flow save handler — the registry entry's stated scope. The identical stuck-flag
mechanism remains live at four sites in `__init__.py`, each doing
`entry_data["_skip_reload"] = True` immediately before `async_update_entry` with no no-op
guard, all reachable from the mobile app / a service call: `__init__.py:8051` (AEMO spike
enable/disable API view), `:8080` (AEMO region API view), `:9053` (tariff-provider save
API view), `:29908` (`_user_backup_reserve` service write). A no-op resubmit at any of
them strands the flag and swallows the next genuine structural reload (the OB-21 failure
on a different config surface). `select.py:175` is NOT a residual — it early-returns on
equal value before setting the flag. **Fix**: apply c8f514e1's `persisted_changed` gate
pattern to the three API views; the `:29908` `_user_backup_reserve` site is reserve-cluster
territory (PW-5/OB-8) — gate it **within** the reserve source-of-truth co-design run so the
guard and the reserve-source change land together, not as an isolated patch.

### Open question — `manual_backup_reserve` writes never reach the LP floor  [LOW confidence]
`set_settings` (~10645) persists MANUAL only and doesn't touch `_config.backup_reserve`;
it only takes effect when auto-apply is later toggled off. If the companion app binds its
reserve slider to `manual_backup_reserve` while auto-apply is OFF, the slider is inert.
**Confirm the app's payload key per auto-apply state before treating as a bug** — if the
app sends `backup_reserve` when auto-apply is off, this is intended baseline semantics.

### Open question — Flow Power KWatch naive timestamps parsed as UTC  [UNDECIDED, would be MAJOR]
`flow_power_api.py::_parse_time` (~213) stamps tz-naive strings as UTC and the regression
tests assert that behavior, but the parsed fields (SETTLEMENT_DATE, periodDateTime) are
AEMO-native and would be naive-AEST in AEMO's own format — if KWatch ever returns them
naive, every price lands ~10 h late. Production evidently works (mapping fixtures carry
+10:00 offsets), so the offset is probably explicit today; **one raw dispatch5mins/
predispatch30mins capture settles it**. If naive-AEST is possible, the naive branch should
default to NEM time (UTC+10), not UTC.

## Powerwall local-readback overlay bugs (committed in 58bedf87, reviewed 2026-07-08)

Full ranked report was filed via the code-review findings (15 items). Headlines, all
CONFIRMED by adversarial verification unless noted:

- **PW-1** Operation-mode overlay is dead code: `powerwall_local/client.py:675` parses
  `site_info.default_real_mode` but the gateway stores it top-level (the repo's own
  hardware-verified readback loop at `__init__.py::handle_set_operation_mode` proves it).
  Fix in the parser + a fixture test that goes through the parser (the current test
  injects `operation_mode` directly and masks this).
  > **FIXED in aa7e2bb9 (2026-07-08)** — parses top-level operation mode per the prescribed parser fix; fixture-through-parser test tests/test_powerwall_operation_mode_parse.py added.
- **PW-2** Offset corruption loop: post-write offset re-detection against a stale cloud
  reserve stores a bogus offset (0–20 gate) → UI revert + next write skewed up to ±20 pts
  (`powerwall_local/coordinator.py:191`).
  > **FIXED in 60ab7b06 (2026-07-09)** — post-write offset now derived from the cached write pair (powerwall_local_backup_reserve_write_local_pct / _user_pct) via detect_local_backup_reserve_offset(pending_local_write, pending_user_reserve), short-circuiting the stale-cloud re-detection at powerwall_local/coordinator.py:~197; pending keys cleared once cloud reserve catches up. Test: tests/test_powerwall_local_dcq_snapshot.py. Residual caveat: if a snapshot arrives BEFORE local_reserve reflects the write (_reserve_matches false), the code pops the pending keys and falls through to detect_local_backup_reserve_offset(local_reserve, cloud_reserve) against possibly-stale cloud — the primary corruption loop is closed, but this one narrow pre-settle-window remains.
- **PW-3** Post-write default-5 normalization: handler invalidates the cloud cache the
  detector needs → overlay shows `percent + stored_offset − 5`; lasts the whole outage if
  Fleet is down (`client.py:679`).
  > **FIXED in 13d02652 (2026-07-10)** — coordinator fallback reapplies the last-persisted `powerwall_local_low_soe_reserve_pct` offset instead of the client's default-5 basis when the cloud reserve is missing; test `tests/test_powerwall_local_dcq_snapshot.py::test_coordinator_reapplies_persisted_offset_when_cloud_reserve_missing`.
- **PW-4** Cloud-fallback masking regression: failed local write + cloud success → refresh
  re-stamps the stale snapshot fresh → entity pinned to the old value until gateway sync
  (pre-diff self-corrected ≤30 s).
  > Note: 8a84065c narrowed PW-4's downstream exposure via `resolve_restore_target` (prefers
  > provenance-clean startup/persisted reserve over a fresh-but-possibly-corrupted LIVE
  > read), but the overlay-layer corruption in `powerwall_local/coordinator.py` is untouched
  > and PW-4 stays open — mirroring reserve-cluster-design.md §4's residual carry.
  > **FIXED in 662843d9 (2026-07-10)** — Part A routes force-save last-resort branches through `resolve_restore_target()`; Part B adds a one-shot cross-module marker so a cloud-fallback write does not re-stamp `_last_success_ts` fresh on the stale snapshot; tests `tests/test_schedule_max_backup_reserve.py`, `tests/test_dispatch.py`, `tests/test_powerwall_local_dcq_snapshot.py`.
- **PW-5** Silent reader divergence: `optimization/battery_controller.py:207
  get_backup_reserve` prefers the cloud cache (opposite of `get_tesla_operation_mode`),
  so the LP plans against the stale reserve while the UI shows the local one; the
  startup-resolve path can persist the stale value over the user's reserve.
  > **FIXED in 86fb6907 (accessor) + 50e45838 (completion) (2026-07-10)** — added trust-tagged `read_backup_reserve` accessor (CLOUD_FRESH/CLOUD_STALE/ENTITY/LIVE/NONE) and gated all 4 coordinator adoption/persist sites on trust so untrusted CLOUD_STALE/ENTITY reads are never adopted or persisted; tests `tests/test_battery_controller_wrapper.py`, `tests/test_reserve_source_of_truth.py`.
- **PW-6** `schedule_max_backup` (~`__init__.py:30086`) snapshots the reserve-to-restore
  from the stale cloud cache and its user-sourced restore clobbers the persisted user
  reserve. Force save paths have the same shape behind startup-reserve fallbacks.
  > **FIXED in 563d4516 + 8a84065c (2026-07-10)** — added `resolve_restore_target()` on the coordinator and routed `handle_schedule_max_backup`'s snapshot through it instead of reading the raw (possibly stale-cloud) `backup_reserve_percent` directly; 8a84065c reordered the resolver to prefer the provenance-clean startup/persisted reserve over even a trusted live read (design §2 PW-6 / S3 — a PW-3/PW-4-corrupted local snapshot is LIVE-tagged but must not feed the `source="user"` restore); test `tests/test_schedule_max_backup_reserve.py`.
- **PW-7** `cached_export_rule` permanent pinning for curtailment-disabled users (new
  manual write + no-TTL cache preferred by the select, persisted across restarts).
- **PW-8** VPP restore-branch caches `battery_ok` while re-posting the manual `never`
  (`__init__.py:22988`, pre-existing, newly reachable; plus wrong log + spurious
  verification warning).
- **PW-9** Unguarded store I/O in `update_cached_export_rule` now runs before the
  manual-override flags — storage exception skips the override, curtailment reverts the
  user's rule.
- **PW-10/11** (PLAUSIBLE) The inserted awaits open two race windows: curtailment cycle
  reading `manual_export_override=False` mid-write, and the force re-toggle guard
  reverting a user's mode change before `last_force_toggle_time` pops. Set flags before
  awaits / fire-and-forget the refresh.
- Efficiency/reuse: awaited refresh adds ≤15 s to blocking Hold-SoC/Max-Backup chains
  (fire-and-forget it); double store write per export-rule change; helper + 30 s constant
  duplicated across number/select/sensor; wall-clock freshness gate (fix at the
  coordinator stamp with monotonic).

## Fix-conflict interactions (co-design these — do not fix independently)

1. **OB-5 ↔ OB-7**: OB-7's timer cancellation on unload without OB-5's persistence loses
   the hold (the exact OB-5 freeze); OB-5's persistence without cancellation leaves the
   orphaned pre-reload timer firing against the new setup. One change: persist + restore
   must re-create the timer that unload cancels.
2. **OB-3/OB-8 ↔ PW-5/PW-6 (reserve source-of-truth cluster)**: restoring toward
   `_startup_backup_reserve` while PW-5's stale-cloud value can still populate it restores
   the WRONG reserve. Settle the reserve source of truth (PW-5) first. OB-22's Sigenergy
   restore target is a third member that must stay in sync.
3. **OB-4 ↔ OB-11**: `_last_executed_action` is both the retry gate (OB-4, fixed) and the
   double-command dedup key. OB-4's retry amplifies OB-11's TOCTOU double-writes — land
   OB-11's in-flight guard to bound the retry behavior.
4. **OB-32/OB-33 ↔ LP session/spike overlays**: extending the generic managers' force
   duration without gating them off when the optimizer is active stacks crude force
   control on top of the LP's own session/spike dispatch (double control + restore
   contention). Gate on optimizer-enabled, or route through it.
5. **OB-15 ↔ OB-31**: same EV plan clock, two consumers on different clocks — fixing one
   side alone flips which consumer breaks; change both stamps/reads atomically.

## Hardening items (real code facts, bounded impact — fix opportunistically)

- **HD-1** Open-Meteo tail carry-forward: zero-fill past the last forecast point to match
  Solcast (`load_estimator.py::_parse_open_meteo_watts`). Currently benign (real data ends
  with night zeros).
- **HD-2** Atomic schedule swap: `_run_optimization` reassigns `_current_schedule` through
  the override chain; build into a local and swap once at the end (mid-chain raise
  currently leaves a partial schedule for ≤1 cycle).
- **HD-3** `_pad_array` should honor `default` for non-empty arrays (dead branch today,
  footgun for future callers).
- **HD-4** Surface solver failure/staleness: swallowed exceptions leave
  `optimization_status` "active" with only `last_optimization` silently aging; expired
  schedules pin their final slot. Add an error/stale field + age guard in
  `_get_current_action`.
- **HD-5** `_spread_import_schedule` keeps the LP's stale per-slot `soc` labels; downstream
  spread-export floor checks read them.
- **HD-6** b9cb2c7f remaining gap: an export window split by one sub-100 W slot becomes two
  runs and run 1's bridge floor double-counts run 2's home load (over-reservation).
- **HD-7** LP-side bridge floor (`_priority_export_reserve_floor_slots`) is blind to
  ZeroCharge import-bonus windows (cheap-recharge break uses raw import prices).
- **HD-8** `_time_window_slots` uses an unfloored `now` → Happy Hour/Export Boost masks can
  shift one slot vs the price grid.
- **HD-9** HiGHS time-limit incumbents are discarded (falls to greedy though a
  near-optimal LP point exists).
- **HD-10** `max_battery_export_w` isn't sign-normalized like the grid limits (negative
  input → inverted bound → malformed column).
- **HD-11** Solar nowcast derate persists overnight (no recovery when forecast < 0.5 kW);
  can suppress next-morning decisions ~40 min.
- **HD-12** ~~Sigenergy `set_self_consumption` service doesn't reset
  `REG_ESS_MAX_DISCHARGE_LIMIT`~~ — FIXED in 1d245f79 (routes through restore_normal).
- **HD-13** Battery Mode sensor can never show Hold SoC: `sensor.py` reads
  `entry_data.get("hold_soc_state", {})` but only the two force states are registered
  into `hass.data` in `async_setup_entry` — the hold dict is a closure-local. Fix
  alongside OB-5 (register it, or publish via dispatcher state).
- **HD-14** Sigenergy `curtail()` captures `_original_pv_limit` only when None; after a
  reload-mid-curtailment the fresh controller re-captures 0, and `restore()` treats 0 as
  falsy → restores to safety-cap/unlimited, losing an inverter-side-only DNSP export cap.
- **HD-15** No hysteresis/deadband at the 1 c/kWh curtailment boundary — a price hovering
  at ~1c flaps curtail↔restore per WebSocket tick (Modbus writes / Tesla rate-limit
  pressure).
- **HD-16** Dual EV-load overlays stack: `optimization/coordinator.py` (~3092-3102) adds
  both the external `planned_ev_load_entity` sensor and the internal AutoScheduleExecutor
  plan to the LP load forecast with no mutual exclusion — a user configuring both for the
  same vehicle double-counts EV demand (corroborated by two independent reviewers).
- **Dead code (add to invariants)**: the ML EV schedule path in `_evaluate_vehicle`
  (~4463-4526) is unreachable — `opt_coordinator._enable_ev` / `_ev_schedules` are never
  assigned, so `_get_ml_ev_schedule` always returns None.
- **HD-19** EPEX/custom price-entity override: the positional-list branch of
  `_read_epex_price_entity` (~7738) maps element i → 5-min slot i with no time info and
  takes priority over the time-aware dict/timestamp branches — an hourly list compresses
  into the first 4 h and flat-pads the rest. Confirm a real sensor shape before fixing.
- **HD-20** Latent price-path dead code: `_current_import_price_for_action` (~4554) always
  returns None for dynamic providers (`_last_price_timestamps` is only set in the static
  path — callers fall back safely); `wholesaleKWHPrice` carries $/kWh in
  `_normalize_price_records` but c/kWh in `kwatch_prices_to_amber_format` (only the c/kWh
  value is ever consumed). Name/value footguns — harden opportunistically.
- **HD-18** `release_ev_ownership`/`clear_ev_ownerships` pop only the exact key while the
  read path and `claim_ev_ownership` resolve through the `_default` overlap — a lease
  claimed under `_default` but released under a resolved VIN leaks and keeps blocking
  cross-family starts (`ev_ownership.py` ~388 vs ~347; most current call sites avoid it
  via the dynamic-state key mapping, hence hardening).
- **HD-17** SAJ H2 cross-type force transition pollutes the cached opposing bitmask:
  `force_discharge` after `force_charge` captures `_cached_charge_enable` with slot-7
  CHARGE_BIT still set (`_clear_switch_controls_for_tou` clears only passive switches);
  restore writes `original | CHARGE_BIT`. Inert under Self-Use, latent for TOU users.
- **HD-24** No hysteresis/deadband on the AEMO spike threshold (`aemo_api.py::check_price_spike`
  ~341, entry `>=` / exit `<` on one threshold): dispatch prices oscillating at the
  boundary flap enter/exit up to ~12×/hr — Tesla: 2 tariff uploads + 2 mode switches per
  flap (rate-limit pressure); generic/VPP: force/restore Modbus churn. Same class as
  HD-15. **Corollary**: a flap whose exit-restore fails while the live tariff is still
  `AEMO-SPIKE` lets the next enter capture it as `_saved_tariff` — OB-34's corruption
  without any reload.
- **HD-23** `tariff_converter.py` (~820): artificial-demand-price day filter falls back to
  OS-local `datetime.now().weekday()` when `detected_tz` is None — the exact pattern the
  inline comment warns against. ALPHA feature + rare fallback; control-influencing via the
  uploaded tariff. Fix the else-branch to HA-local.
- **Dead code (FoxESS cloud)**: `foxess_api.py` force_charge/force_discharge (~442/468)
  have no callers — the live force path is Modbus `inverters/foxess.py`.
- **Dead code (powerwall_local, add to invariants)**: `signaling.py` is never instantiated
  (`_build_client` removed it; `pw_local.get("signaling")` is always None) — its whole
  reconnect/JWT surface is unreachable; `client.curtail_via_backup_mode` /
  `restore_from_curtailment` have no callers.
- **Hardware-check flag**: `normalize_local_soc_percent` assumes DCQ
  `nominalEnergyRemainingWh/FullPackWh` is full-pack (matching cloud `percentage_charged`);
  if the DCQ already excludes the 5% reserve it under-reports ~5% — needs one hardware
  comparison to settle.
- **HD-22** Tesla calibration-recovery 30-min interval timer (`_calibration_check_unsub`,
  `__init__.py` ~21665) self-cancels only when the mode returns to autonomous and is never
  cancelled in `async_unload_entry` — a reload mid-calibration leaks a timer that keeps
  hitting the Fleet API and mutating the new entry's calibration state. Same class as
  OB-7 but a distinct timer.
- **HD-21** Tesla/Sigenergy third-party AC charger double-count: the EV-subtraction
  exemption for these brands assumes the integrated charger; a separate AC charger that
  PowerSync's EV automation plans sits in home_load (never subtracted) AND gets the
  planned-EV overlay added on top. Config/semantic edge — document or detect.
- **Brand home_load formula table** (2026-07-08 audit): all balance-formula brands
  (AlphaESS, Sungrow fallback, SolarEdge/Solax fallback, FoxESS H3-Pro, Neovolt multi,
  Sigenergy) use signed battery → charge correctly excluded; sensor-sourced brands
  (GoodWe, Anker, ESY, Fronius, FoxESS entity/cloud) clean; exceptions are OB-24 (Tesla
  local fallback) and OB-25 (SAJ gridPower fallback). Grid sign normalized to +import
  everywhere; only Tesla/Sigenergy subtract EV upstream (estimator handles the rest).
- **Brand force/restore symmetry table** (2026-07-08 audit): FoxESS H1/H3/KH — snapshot
  unguarded, restore corrupted (OB-16); Solax force_time — snapshot unguarded, harm
  bounded (OB-17); SAJ H2 — guarded for re-issue, minor cross-type pollution (HD-17);
  Neovolt — doubly guarded (force filter + preserve flag), clean; GoodWe — DOD sign
  symmetric both sides, force doesn't touch DOD, clean; Fronius Reserva / Anker / ESY —
  no snapshots, hardcoded default-mode restore (won't preserve a custom mode — design
  choice, not a bug); FoxESS H3-Pro/Smart — no mode change, clean.
- **Reload-mid-curtailment exposure by brand** (resolves a wave-1 open question):
  Tesla self-heals (reads live export rule); FoxESS self-heals ≤~600 s (HW timeout);
  **Sungrow, Sigenergy, GoodWe, SolarEdge, AlphaESS stay stuck at zero export** until the
  next negative-price cycle re-arms the state machine (persistent registers, no HW
  timeout; AlphaESS worst — no reapply loop and no live-export re-check).

## Refuted claims — do not rediscover

- **RC-1 Phantom bonus via `_pad_array` last-value padding**: unreachable — bonus arrays
  are sized to the price arrays, which are always full-horizon (see invariants.md).
- **RC-2 Short solar/load extrapolation via `_align_forecasts` max-length**: unreachable —
  every forecaster equalizes to `n_intervals` before returning.
- **RC-3 Open-Meteo phantom overnight solar**: mechanism real, impact benign — real series
  end with explicit night zeros, and far-horizon slots carry minimal decision weight.
- **RC-4 "Empty price fetch wipes the plan"**: transient provider failure raises
  `UpdateFailed` → HA preserves previous coordinator data; genuinely empty prices fall back
  to default flat rates, not an empty schedule.
- **RC-5 "Monitoring mode strands ALL restores"**: enabling monitoring via the app fires a
  `force_restore` cleanup that bypasses the block for force modes/native control. Only the
  pre-idle reserve sub-case survives (OB-8).
- **RC-6 EV-release stuck cap on Sungrow/FoxESS**: rescued — the next self-consumption
  cycle's `restore_normal` path repairs the cap (Sungrow stale-limit self-heal + telemetry
  reapply; FoxESS work-mode restore).
- **RC-7 Stacked modes clobbering the reserve snapshot**: guarded — `_pre_idle_backup_reserve`
  snapshots only when None.
- **RC-8 `free_import_slot` bypassing `allow_grid_charge`**: the historical bug is fixed
  (gated on `allow_grid_charge and grid_charge_allowed[t] and not charge_blocked`).

## Historical fix clusters (where regressions recur — from git history)

- **ZeroHero/export windows** (7+ fixes: 557cf69a, b9cb2c7f, f87a2386, 3c8ac894, fa842367,
  0772d65d, 176d99b3, 73eb7598…): every fix here has had a remaining gap; always check the
  greedy path, split windows, and window-edge slots.
- **Force-mode stability across replans** (9ea42a95 hold through LP flips, 06b577c7 hold
  during export replans, 443f2244, d9435511): replans fighting active force state.
- **Price-feed alignment** (9b51865b/4eed330b Amber retail vs spot, 8a0c6381 Flow Power
  slot alignment, 85468457 KWatch refresh, 0dfdfe1e speculative spikes).
- **Cached-schedule execution** (e0d8c573): interval-boundary execution semantics.
- **Below-reserve recovery holds** (e0d15600) and grid charge caps (f4de2fa6).
