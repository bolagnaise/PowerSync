# Diagnosis: Sigenergy battery frozen after HA restart mid-Hold-SoC (optimizer disabled)

**Verdict: CONFIRMED bug — registry entry OB-5 ("Hold SoC not persisted; restart/reload mid-hold freezes the battery"), still open at HEAD v2.12.785.** This ticket is a textbook field occurrence: Sigenergy + Smart Optimization OFF is exactly the worst-case cohort. Verified against actual code at HEAD, not just the registry.

## Root cause

`Hold Battery SOC` puts the Sigenergy inverter into a **persistent hardware state** (Remote EMS STANDBY — blocks all charge *and* discharge) but tracks the hold **only in per-setup in-memory state** (`hold_soc_state` dict + an asyncio expiry timer). Engaging Hold then calls `persist_force_mode_state()`, which serializes **only** force charge/discharge — with only Hold active it writes `force_mode_state = None` to storage (also clobbering any previously persisted force state). After an HA restart, no code path knows a hold ever existed, no startup detector can see a stale STANDBY work mode, Sigenergy EMS has no hardware auto-revert timeout, and with the optimizer disabled there is no 5-minute cycle to overwrite it. The battery stays frozen indefinitely while the UI (rebuilt from the fresh `hold_soc_state`) shows nothing active.

## Exact code path (all paths under `custom_components/power_sync/`)

**Engage (yesterday evening):**
1. `__init__.py::handle_hold_battery_soc` (~L28382) — brand loop picks `sigenergy_coordinator`, calls `coord.set_backup_mode()` (L28488).
2. `coordinator.py::SigenergyEnergyCoordinator.set_backup_mode` (~L3883) → `controller.set_standby_mode()`.
3. `inverters/sigenergy.py::set_standby_mode` (~L1044) — writes `REG_REMOTE_EMS_ENABLE = 1` (reg 40029) and `REG_REMOTE_EMS_CONTROL_MODE = 1` (STANDBY, reg 40031). This is a **persistent** Modbus mode: no countdown, no firmware timeout (unlike FoxESS ~600 s remote-control timeout or Sungrow's force countdown).
4. Back in the handler: `hold_soc_state["active"] = True`, `expires_at = utcnow + 240 min` (L28516–28518); expiry timer registered via `async_track_point_in_utc_time` (L28530) — **both in-memory only**.
5. L28544: `await persist_force_mode_state()` → in `persist_force_mode_state` (~L23598) `state_to_save` stays `None` because neither `force_charge_state["active"]` nor `force_discharge_state["active"]` is set (Hold is checked nowhere), so `stored_data["force_mode_state"] = None` is saved. Persistence now affirmatively says "nothing active".

**Restart (~1 h into the hold):**
6. The asyncio expiry timer and `hold_soc_state` die with the process. `async_setup_entry` re-creates `hold_soc_state` fresh with `active: False` (~L23432).
7. `restore_force_mode_from_persistence` (~L23640) loads the blob → `None` → early return at L23643. Even with a blob, it only handles `mode in {"charge","discharge"}`.
8. Disabled-optimizer startup cleanup (~L33082–33115) iterates `disabled_optimizer_cleanup_targets` — **Sigenergy is not in the target list at all** (only sungrow/foxess/esy_sunhome/solax/saj_h2/fronius_reserva/neovolt/solaredge/anker_solix), and the function itself, `_restore_disabled_optimizer_reserve_if_stale` (L117), hard-excludes `{"tesla", "sigenergy", "goodwe", CUSTOM}` at L129 **and** is reserve-keyed (`live_reserve > target + 5`, L151) — Sigenergy STANDBY deliberately never touches backup_reserve (`set_standby_mode` docstring), so a stale standby work mode is invisible to it even structurally.

**Morning state:** hardware still in Remote EMS STANDBY (frozen both directions); software state clean; the mobile UI's `power_sync_hold_soc_state` dispatcher signal was never re-fired, so no hold/force is shown. Exit requires a manual `restore_normal` (routes Sigenergy through `restore_from_standby()` → `set_self_consumption_mode()`, or native-control release via `disable_remote_ems()`).

**Compensating mechanisms checked and absent** (verification playbook inventory): next-cycle optimizer overwrite (optimizer OFF; polling loop starts only in `enable()`), self-consumption drift checks (Tesla/GoodWe/Sungrow only — Sigenergy has none), polling safety net (covers only `_pre_idle_backup_reserve`), hardware timeout (Sigenergy EMS is persistent), startup persistence restore (charge/discharge only), startup stale-reserve heal (excludes Sigenergy, reserve-keyed), monitoring-enable cleanup (N/A).

## Affected brands / conditions

Requires: Hold SoC active at restart/reload + **Smart Optimization disabled** (enabled installs self-repair within ~5 min via the next `_execute_optimizer_action`).

- **Worst — fully frozen, zero coverage:** **Sigenergy** (Remote EMS STANDBY, excluded from startup heal), **AlphaESS** (standby via `set_backup_mode`; not even in the startup cleanup target list), **GoodWe** (ECO/backup mode; explicitly excluded from the heal).
- **Tesla — discharge-frozen:** Hold = `backup_reserve := SOC (≤80)` + self-consumption via services; after restart the elevated reserve persists as a discharge floor (solar can still charge). Excluded from the startup heal. (Commit 61be5240 fixed the in-session restore path only, not restart.)
- **Sungrow — partially exposed:** hold hardware state is a discharge cap; the startup heal runs for Sungrow but is reserve-keyed, so a cap-only hold is invisible to it. (Sungrow's `_restore_stale_low_discharge_limit` self-heals only inside the optimizer's self-consumption branch — needs the optimizer enabled.)
- **FoxESS / Neovolt / SolarEdge — likely rescued at startup:** their hold raises min-SOC/reserve, they are in the cleanup target list, and the frozen-at-reserve morning state (SOC ≈ reserve, grid importing, battery idle) matches the heal's trigger conditions — but only when those telemetry conditions line up.
- Reload (vs full restart) additionally hits OB-7: `async_unload_entry` cancels no hold/force expiry timers, so an orphaned pre-reload timer can fire against the new setup.

## Fix outline

1. **Persist the hold** — extend `persist_force_mode_state` to serialize `hold_soc_state` when active (`mode: "hold_soc"`, absolute UTC `expires_at`, `locked_soc`, brand, and any saved restore targets such as Tesla's pre-hold reserve). The blob must represent hold as a first-class mode instead of writing `None` (which also fixes the clobbering side-effect).
2. **Restore-or-exit on startup** — extend `restore_force_mode_from_persistence` to handle `mode == "hold_soc"`: expiry in the future → re-mark `hold_soc_state` active, re-arm the expiry timer, re-fire the `{DOMAIN}_hold_soc_state` dispatcher event (UI countdown returns; hardware is already in standby, no re-command needed); expired → run the hold-exit restore (`restore_work_mode_from_idle` / `restore_normal`) using the retry contract from `_restore_pre_idle_backup_reserve` — clear state only after confirmed success — and mirror the existing monitoring-mode / Sigenergy native-control handling in the force-restore branch.
3. **Runtime guard for existing installs** (persistence only protects *future* holds; per AGENTS.md a bad state can survive an upgrade): add a **non-reserve-keyed** stale-standby detector for the disabled-optimizer startup path — e.g. read Sigenergy `REG_REMOTE_EMS_ENABLE`/`REG_REMOTE_EMS_CONTROL_MODE` (readable work-mode telemetry) and restore when EMS=enabled+STANDBY with no active PowerSync mode; equivalent work-mode checks for GoodWe (EMS mode entity) and AlphaESS. Add these brands to the startup cleanup coverage they are currently excluded from.
4. **OB-7 adjacency** — cancel `hold_soc_state["cancel_expiry_timer"]` (and the force timers) in `async_unload_entry` so the reload variant is covered too.
5. **Regression tests** (patterns from the verification playbook): AST source-extraction of `persist_force_mode_state` / `restore_force_mode_from_persistence` asserting (a) the blob includes an active hold and no longer clobbers force state, (b) future-expiry hold re-arms timer + dispatcher, (c) past-expiry hold triggers the brand restore call with retry-on-failure; plus a lifecycle test: engage hold → simulate restart (fresh state dicts) → assert Sigenergy `restore_from_standby` is invoked. Run with `python3.12 -m pytest` on the narrow file first.

**Ticket remediation now:** the user can un-freeze immediately by calling `power_sync.restore_normal` (or the app's Restore Normal / Self-Use control), which releases Remote EMS back to self-consumption or native control. Evidence worth collecting to confirm: debug log lines `Sigenergy Remote EMS set to STANDBY (mode 1) for IDLE hold` (yesterday evening) and the absence of any restore line after the restart.
