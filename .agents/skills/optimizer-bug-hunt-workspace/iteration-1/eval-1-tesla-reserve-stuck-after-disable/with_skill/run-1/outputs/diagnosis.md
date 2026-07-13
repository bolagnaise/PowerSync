# Diagnosis: Tesla Powerwall backup reserve stuck at SOC (74%) after disabling Smart Optimization mid EV-preserve window

**Status: confirmed open bug — matches optimizer-bug-hunt registry entry OB-3** (`.claude/skills/optimizer-bug-hunt/references/bug-registry.md`), verified line-by-line at HEAD (v2.12.785, manifest current). This ticket is the first field confirmation. All file paths below are under `custom_components/power_sync/`.

## Root cause (one paragraph)

Tesla has no native "no-discharge" primitive, so the scheduled-EV preserve feature falls back to the IDLE-hold mechanism, which **raises the Powerwall's hardware backup reserve to `min(SOC, 80)`** (74% here) and stashes the user's real reserve (20%) in the in-memory field `_pre_idle_backup_reserve`. When the user disables Smart Optimization mid-preserve, `disable()` only restores that reserve when the last executed action was `"idle"` — but during EV preserve it is `"no_discharge"`, so the restore is skipped. The EV-release path that *does* run restores the operation **mode only** (Tesla `restore_normal` has no saved reserve for this path), every retry mechanism (polling safety net, per-cycle self-consumption reserve reset) dies with `disable()`, and the startup stale-reserve self-heal explicitly excludes Tesla. The reserve therefore stays at 74% indefinitely and shows in the Tesla app.

## Exact code path

### Set side — how the reserve got to 74%

1. EV charging starts inside a scheduled preserve window → `scheduled_ev_preserve_state.active` is set in `hass.data`; the optimizer reads it via `_scheduled_ev_preserve_active()` (`optimization/coordinator.py:483`).
2. `_execute_optimizer_action`: `preserve_active` overrides the LP action → `effective_action = "no_discharge"` (`optimization/coordinator.py:5166-5179`).
3. The `no_discharge` branch (`optimization/coordinator.py:5346-5350`) calls `_set_scheduled_ev_no_discharge_mode(battery, ...)` (`:492`).
4. `TeslaEnergyCoordinator` (`coordinator.py:1664`) has **no** `set_no_discharge_mode` (verified: none of `set_no_discharge_mode` / `restore_no_discharge_mode` / `restore_work_mode_from_idle` / `set_backup_mode` exist on it — those live only on Sigenergy/Sungrow/DualSungrow/FoxESS/AlphaESS coordinators), so the fallback runs: `_set_idle_hold_mode(battery, preserve_charge=True)` (`optimization/coordinator.py:504`).
5. Inside `_set_idle_hold_mode` (`:575`):
   - `_pre_idle_backup_reserve = _startup_backup_reserve` (`:600-602`) — the user's configured 20%, resolved from config at enable time (`_configured_startup_backup_reserve`, `:1715`; applied in `_deferred_enable_restore`, `:2821-2828`). **The correct restore target IS captured in memory.**
   - Tesla takes the `set_backup_reserve` + `set_self_consumption_mode` branch: `reserve = min(max(soc_pct, 0), 80)` (`:650-652`) → with SOC 74% → **`set_backup_reserve(74)`** (`:661`). This is exactly why the stuck value equals the SOC at that moment.
6. `_last_executed_action = "no_discharge"` (`:5675`) and `_scheduled_ev_no_discharge_active = True` (`:516`).

### Restore side — why disable() never puts it back

`disable()` (`optimization/coordinator.py:2930`):

1. **The gate that misses (primary bug):** `if not monitoring_mode and self._last_executed_action == "idle":` (`:2940`) guards the only call to `_restore_pre_idle_backup_reserve()` in the shutdown path. The marker is `"no_discharge"`, not `"idle"` → the reserve restore (and the `restore_work_mode_from_idle` cleanup) is silently skipped. `_pre_idle_backup_reserve` (20) remains set in memory but is never used again.
2. **The release that runs but can't help:** `_release_scheduled_ev_no_discharge_mode("optimizer disabled")` (`:2961-2967`, body at `:523-573`). For Tesla it falls through its branch chain (no `restore_no_discharge_mode`, no `restore_work_mode_from_idle`) to `self._executor.battery_controller.restore_normal()` (`:548-552`) → `BatteryControllerWrapper.restore_normal` (`optimization/battery_controller.py:111`) → `power_sync.restore_normal` service → `handle_restore_normal` (`__init__.py:27190`). That handler restores a backup reserve **only** from `force_charge/discharge_state["saved_backup_reserve"]` or, for hold-SoC, from `entry.options["_user_backup_reserve"]` (`__init__.py` ~27895-28110). EV preserve created neither a force state nor a hold state (the reserve was written via `set_backup_reserve` with `_idle_reserve_adjustment=True`, which deliberately suppresses persistence) → **mode/tariff restored, reserve untouched**. Note this function never touches `_pre_idle_backup_reserve` and never calls `_restore_pre_idle_backup_reserve` — the release restores mode only, by design asymmetry.
3. `_last_executed_action = None` (`:2968`), `_enabled = False` (`:2972`), **polling task cancelled** (`:2974-2976`) → the polling-loop safety net `_should_restore_pre_idle_backup_reserve_from_polling` (`:475-481`, invoked at `:3569` inside `_schedule_polling_loop`) can never fire again. (Even before disable it was intentionally suppressed by its `not self._scheduled_ev_no_discharge_active` condition.)
4. `_executor.stop(restore_normal=True)` (`:3001`) → same `restore_normal` service → mode only, again.

### Why it "never recovered" (all compensating mechanisms verified dead)

- **Per-cycle Tesla reserve reset**: while the optimizer is *enabled*, the next `self_consumption` execution resets Tesla's hardware reserve to `_startup_backup_reserve` (`optimization/coordinator.py:5628-5647`). Disabled → no cycles → dead.
- **Startup self-heal**: `_restore_disabled_optimizer_reserve_if_stale` (`__init__.py:117`) — the only generic stale-reserve detector for the disabled-optimizer cohort — **explicitly returns False for Tesla** (`__init__.py:129`: `if battery_system in {"tesla", "sigenergy", "goodwe", ...}`). An HA restart does not repair it.
- **Corollary (registry OB-3 corollary, verified at `:5466-5484`)**: if the user later re-enables and the elevated reserve is encountered in a steady-state self-consumption cycle with `current_reserve > target and current_reserve <= soc`, the drift check **adopts** the elevated value: `self._startup_backup_reserve = current_reserve` + `update_hardware_reserve(current_reserve/100)` — silently overwriting the user's real 20% inside the optimizer's own model instead of repairing hardware.
- The only true recoveries today: the user manually lowers the reserve in the Tesla app, or re-enables Smart Optimization and a fresh `self_consumption` execution happens to run first (marker `None` → `apply_self_consumption=True` → skips the adoption branch → `set_backup_reserve(20)` at `:5628-5647`).

## Affected brands / conditions

- **Trigger conditions**: Smart Optimization enabled → scheduled EV preserve window active with EV charging → user disables Smart Optimization (or anything else that calls `coordinator.disable()`: HA shutdown/reload of the optimizer toggle) **before** the preserve window releases. Monitoring mode off (monitoring on is strictly worse — even the mode release is skipped, `:2962-2965`; see also registry OB-8).
- **Tesla Powerwall — worst case**: reserve raised to `min(SOC, 80)`; no release-side reserve restore; excluded from the startup self-heal → stuck **indefinitely**.
- **Other fallback brands** (any whose energy coordinator lacks `set_no_discharge_mode` and whose controller exposes `set_backup_reserve` — e.g. AlphaESS, Solax, SAJ, Fronius Reserva, Neovolt, ESY, SolarEdge, Anker): same `disable()` gate miss strands the elevated reserve, but they are *not* excluded from `_restore_disabled_optimizer_reserve_if_stale`, so an HA restart can eventually repair them (only under its narrow SOC-near-reserve/grid-importing conditions). Sigenergy/Sungrow/FoxESS use a real no-discharge primitive (discharge-limit register, reserve never raised) → not affected by this reserve variant.
- The `== "idle"` gate miss in `disable()` is brand-agnostic; the *permanent* harm is Tesla-specific.

## Fix outline (per registry OB-3 + restore-side symmetry rules in the skill)

1. **`disable()` (`optimization/coordinator.py:2940`)**: ungate the reserve cleanup from `_last_executed_action == "idle"`. Run `_restore_pre_idle_backup_reserve(battery, "optimizer disable")` whenever `_pre_idle_backup_reserve is not None` (the helper is already a no-op when it's None and already retry-safe/monitoring-gated). Extend the `restore_work_mode_from_idle` cleanup to also cover `_last_executed_action == "no_discharge"` for coordinator brands that used the idle-hold fallback.
2. **Restore-side symmetry in `_release_scheduled_ev_no_discharge_mode` (`:523`)**: when the set side used the `_set_idle_hold_mode` fallback (Tesla and other no-primitive brands), also await `_restore_pre_idle_backup_reserve` after the mode restore succeeds — keeping the OB-2 retry contract (only clear `_scheduled_ev_no_discharge_active` after both succeed, mirroring `_restore_pre_idle_backup_reserve`, not the old pre-10ba7704 pattern).
3. **Guard the adoption path (`:5466-5484`)**: skip `_startup_backup_reserve = current_reserve` adoption when `_pre_idle_backup_reserve is not None` — an optimizer-elevated reserve must never be adopted as the user's reserve.
4. **Runtime guard for already-stuck installs** (validation-only fixes don't protect existing users — this reporter is already stuck): add a Tesla-capable variant of the startup stale-reserve detection (compare live reserve vs the configured/persisted user reserve when the optimizer is disabled and no force/hold state is active), or at minimum have `enable()`'s `_deferred_enable_restore` reapply `_startup_backup_reserve` to hardware for Tesla when the live reserve is elevated above it.
5. **Regression test**: extend `tests/test_optimizer_restore_retry.py` (or a sibling) — fake Tesla battery (`set_backup_reserve` + `set_self_consumption_mode`, coordinator without `set_no_discharge_mode`): run `_set_scheduled_ev_no_discharge_mode` → assert reserve raised and `_pre_idle_backup_reserve` snapshotted → call `disable()` → assert `set_backup_reserve(<user reserve>)` was issued and `_pre_idle_backup_reserve` cleared. Add the release-while-running variant (window ends, optimizer stays enabled) to lock in the polling/self-consumption repair. Run with `python3.12 -m pytest` (3.9 fails conftest); CI does not run pytest.

## Immediate user remediation (for the ticket reply)

Set the backup reserve back to 20% in the Tesla app (PowerSync will not fight it while Smart Optimization is disabled), or re-enable Smart Optimization — its first self-consumption cycle resets Tesla's reserve to the configured value. Only advise updating after a fix release is actually published.
