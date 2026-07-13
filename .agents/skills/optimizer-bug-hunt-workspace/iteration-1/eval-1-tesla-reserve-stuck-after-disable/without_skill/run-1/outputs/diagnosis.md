# Diagnosis: Tesla Powerwall backup reserve stuck at 74% after disabling Smart Optimization during a scheduled EV preserve window

## Symptom

Tesla Powerwall user disabled Smart Optimization while their EV was charging inside a
scheduled EV "preserve home battery" window. The Powerwall backup reserve stayed at 74%
(the battery SOC at that moment) instead of returning to the configured 20%. The Tesla
app shows the elevated reserve and it never recovers.

## Root cause (summary)

The scheduled-EV preserve feature raises the Tesla backup reserve to the current SOC as
its "no-discharge" mechanism (Tesla has no native no-discharge primitive, so the code
falls back to the IDLE-hold path). On `OptimizationCoordinator.disable()`, the code that
restores the saved pre-hold reserve is gated on `_last_executed_action == "idle"` — but
during an active preserve window the last executed action is `"no_discharge"`, so the
restore is skipped. The release path that *does* run
(`_release_scheduled_ev_no_discharge_mode`) never touches the backup reserve for Tesla:
it degrades to the `power_sync.restore_normal` service, which is a complete no-op when no
force mode is active ("nothing to restore"). Every safety net that would otherwise fix it
(polling retry, setup-time stale-reserve self-heal) is either cancelled by `disable()` or
explicitly excludes Tesla. The user's real reserve (20%), held only in the in-memory
`_pre_idle_backup_reserve`, is discarded when the coordinator is torn down.

This is an instance of the repo's most recurring bug class: stale force/reserve state not
restored on a mode transition.

## Exact code path that raises the reserve

All paths in `custom_components/power_sync/`.

1. **EV planner publishes preserve intent** —
   `automations/ev_charging_planner.py`
   - `_set_active_charging_preserve_intent()` (line ~4931) /
     `_write_smart_schedule_preserve_state()` (line ~4905) set
     `hass.data[DOMAIN][entry_id]["scheduled_ev_preserve_state"] = {"active": True, "mode": "no_discharge_charge_allowed", ...}`.

2. **Optimizer executes it** — `optimization/coordinator.py`, `_execute_current_action`:
   - `_scheduled_ev_preserve_active()` (line 483) reads that state.
   - Lines 5166–5179: while preserve is active, any of
     `discharge/export/consume/self_consumption/idle` is overridden to
     `effective_action = "no_discharge"`.
   - Line 5346–5350: `no_discharge` → `_set_scheduled_ev_no_discharge_mode(battery, ...)`.

3. **Tesla fallback = IDLE-hold reserve raise** — `_set_scheduled_ev_no_discharge_mode`
   (line 492):
   - Line 498–504: `TeslaEnergyCoordinator` (`coordinator.py:1664`) implements **no**
     `set_no_discharge_mode` (only Sigenergy 3888, Sungrow 4685, FoxESS 5663 do), so the
     code falls back to `_set_idle_hold_mode(battery, preserve_charge=True)`.
   - `_set_idle_hold_mode` (line 575):
     - Lines 600–628: saves the user's real reserve into
       `self._pre_idle_backup_reserve` (e.g. 20%, from `_startup_backup_reserve`).
     - Tesla branch, lines 650–670 (the `battery` object is
       `BatteryControllerWrapper`, which always exposes `set_backup_reserve` +
       `set_self_consumption_mode`; TeslaEnergyCoordinator has no `set_backup_mode`):
       `reserve = min(max(soc_pct, 0), 80)` → **74** for SOC 74% → calls
       `set_self_consumption_mode()` then `set_backup_reserve(74)`.
   - `optimization/battery_controller.py:273` → `power_sync.set_backup_reserve` service
     with `source="optimizer"`; `__init__.py:29349–29356` correctly skips persisting 74%
     as `_user_backup_reserve` (so the *persisted* user value stays 20% — only the
     hardware is wrong).
   - Line 516: `_scheduled_ev_no_discharge_active = True`;
     line 5675: `_last_executed_action = "no_discharge"`.

The 74% value matching SOC (and the fact users never see 81–99%) is the fingerprint of
this branch: `min(max(soc_pct, 0), 80)`.

## Exact code path that fails to restore it on disable

1. **App toggle** — `__init__.py:33691–33703` (set_settings HTTP view): sets
   `CONF_OPTIMIZATION_ENABLED = False`, provider → native, triggers entry reload →
   `async_unload_entry` → `opt_coordinator.disable()` (`__init__.py:34188–34190`).

2. **`OptimizationCoordinator.disable()`** — `optimization/coordinator.py:2930`:
   - **Line 2940 — the primary miss:**
     `if not monitoring_mode and self._last_executed_action == "idle":` guards the only
     call to `_restore_pre_idle_backup_reserve()` in the disable path. During preserve
     the last action is `"no_discharge"` (the preserve override at 5166–5179 rewrites
     even `idle` to `no_discharge`), so this branch **can never fire** while a preserve
     window is active. The saved 20% is not written back.
   - Lines 2961–2967: `_release_scheduled_ev_no_discharge_mode("optimizer disabled")`
     runs instead (line 523):
     - Lines 538–552: Tesla has neither `restore_no_discharge_mode` nor
       `restore_work_mode_from_idle`, so it falls to
       `self._executor.battery_controller.restore_normal()` →
       `power_sync.restore_normal` service.
     - `handle_restore_normal` (`__init__.py:27190`): no force charge/discharge is
       active and no saved force state exists, so the guard at `__init__.py:27899–27901`
       returns immediately: *"Restore normal: no force mode active and no saved state —
       nothing to restore."* Even when it does run, backup reserve is only restored from
       `force_*_state["saved_backup_reserve"]` / Hold-SoC user reserve
       (`__init__.py:28095–28123`) — the preserve raise stored nothing there.
     - The service call returns True → line 568 clears
       `_scheduled_ev_no_discharge_active` — release is considered "successful" while
       the hardware reserve is still 74%. **`_release_scheduled_ev_no_discharge_mode`
       never restores `_pre_idle_backup_reserve` — the release is asymmetric with the
       enter path.**
   - Line 2974: the polling loop is cancelled, killing the safety net at
     `optimization/coordinator.py:3569–3572`
     (`_should_restore_pre_idle_backup_reserve_from_polling`, line 475 — this is the
     mechanism that recovers the reserve when a preserve window ends *while the
     optimizer stays enabled*; note it is also gated on
     `not self._scheduled_ev_no_discharge_active`, so it could not have fired before the
     release either).
   - Line 3001: `_executor.stop(restore_normal=True)` → same no-op restore_normal.
   - The coordinator instance (and the in-memory `_pre_idle_backup_reserve = 20`) is
     discarded on reload.

3. **The existing self-heal explicitly excludes Tesla** — `__init__.py`:
   - `_restore_disabled_optimizer_reserve_if_stale()` (lines 117–199) was written for
     exactly this failure ("Undo a stale IDLE reserve left behind while the optimizer is
     disabled") but line 129–130 hard-excludes it:
     `if battery_system in {"tesla", "sigenergy", "goodwe", BATTERY_SYSTEM_CUSTOM}: return False`.
   - The disabled-optimizer cleanup loop at setup (`__init__.py:33082–33115`) iterates
     only Sungrow/FoxESS/ESY/Solax/SAJ/Fronius/Neovolt/SolarEdge/Anker coordinators —
     no Tesla.

Result: nothing in the running system will ever write the reserve back to 20%. The Tesla
app faithfully shows 74% until the user changes it by hand (which then persists 20% via
`_user_backup_reserve`) or re-enables the optimizer long enough for an IDLE cycle to
restore from the persisted user reserve.

### Log fingerprint at disable time

1. `Scheduled EV preserve: battery discharge blocked, charging still allowed (...)` and
   `Optimizer: IDLE — holding SOC at 74% via self_consumption (backup reserve=74%)` (at window start)
2. `🔄 RESTORE NORMAL: Restoring normal operation`
3. `Restore normal: no force mode active and no saved state — nothing to restore`
4. `Scheduled EV preserve: battery no-discharge mode released (optimizer disabled)`
5. `Optimization disabled` — with **no** `Optimizer: Restored backup reserve to 20%` line anywhere.

## Affected brands / conditions

- **Tesla Powerwall (Fleet API and local)** — primary. Requires ALL of:
  - Smart Optimization (PowerSync provider) enabled with a scheduled EV preserve intent
    active (`scheduled_ev_preserve_state.active`, i.e. Smart Schedule EV charging with
    "preserve home battery", or price-level preserve intent);
  - optimizer executed at least one `no_discharge` cycle (reserve already raised to SOC,
    capped at 80%);
  - user disables Smart Optimization (or the entry reloads/HA restarts, which also runs
    `disable()`) before the preserve window ends;
  - not in monitoring mode (monitoring skips the writes entirely).
- **GoodWe and custom-battery systems** share the same latent shape: their energy
  coordinators also lack `set_no_discharge_mode`/`restore_work_mode_from_idle`, they take
  the same `_set_idle_hold_mode(preserve_charge=True)` fallback (the GoodWe early-return
  at line 586 only applies when `preserve_charge` is False), and they are also excluded
  from `_restore_disabled_optimizer_reserve_if_stale`. GoodWe additionally has inverted
  DOD semantics, so any fix there needs its own handling.
- **Not affected:** Sigenergy/Sungrow/FoxESS (native `set/restore_no_discharge_mode`
  primitives that don't touch backup reserve), and the brands covered by the setup-time
  cleanup loop.
- The **normal** end-of-window path (optimizer stays enabled) is fine: release at
  coordinator line 5181 clears the flag and the polling safety check (3569) restores the
  pre-idle reserve on the next loop iteration. Only the disable/unload path is broken.

## Fix outline

1. **Restore the pre-hold reserve in `disable()`** (`optimization/coordinator.py`):
   after the scheduled-EV release block (lines 2961–2967), restore whenever a saved
   reserve is pending rather than only for IDLE, e.g. change the flow to:
   - release no-discharge first (existing lines 2961–2967), then
   - `if not monitoring_mode and self._pre_idle_backup_reserve is not None and self.battery_controller: await self._restore_pre_idle_backup_reserve(self.battery_controller, "optimizer disable")`.
   This single change fixes the reported case and also covers any future action that
   raises the reserve without being literally `"idle"`.

2. **Make release symmetric with enter** — in
   `_release_scheduled_ev_no_discharge_mode()` (line 523): when the enter path used the
   `_set_idle_hold_mode` fallback (i.e. energy coordinator lacks the no-discharge
   primitive), call `_restore_pre_idle_backup_reserve()` after a successful release so
   the running (non-disable) path does not depend on the polling loop.

3. **Runtime guard for already-affected installs** (per AGENTS.md: validation-only fixes
   don't protect existing installs): add Tesla handling to the disabled-optimizer
   stale-reserve cleanup. Either lift the Tesla exclusion in
   `_restore_disabled_optimizer_reserve_if_stale` with a Tesla-specific branch (target
   from `_disabled_optimizer_backup_reserve_target()` — hardware reserve config →
   `_user_backup_reserve` → optimizer floor; live value from the Tesla coordinator's
   `site_info` `backup_reserve_percent`), or add a parallel Tesla helper invoked from
   the `__init__.py:33082` cleanup block. Keep the existing safety guards: skip when
   force/hold-SoC state is active and when current SOC is below the target (mirroring the
   SOC guard in `handle_restore_normal`, `__init__.py:28125–28148`) so the restore never
   triggers grid import.

4. **Regression test**: extend `tests/test_optimizer_restore_retry.py` (it already
   builds lightweight coordinators via `object.__new__` and covers
   `_release_scheduled_ev_no_discharge_mode` /
   `_should_restore_pre_idle_backup_reserve_from_polling`) with:
   - a Tesla-shaped coordinator (energy coordinator without no-discharge primitives,
     battery stub with `set_backup_reserve`) that enters preserve via the fallback,
     then runs `disable()` and asserts `set_backup_reserve(pre_idle_value)` was called;
   - a case asserting the release path itself restores the reserve when the fallback
     raised it.
   Run with `python3.12 -m pytest tests/test_optimizer_restore_retry.py` first, then
   adjacent optimizer tests (CI does not run pytest; local verification is the gate).
