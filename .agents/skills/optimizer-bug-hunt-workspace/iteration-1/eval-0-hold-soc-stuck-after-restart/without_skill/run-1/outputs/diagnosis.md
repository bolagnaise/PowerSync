# Diagnosis: Hold Battery SOC frozen after HA restart (Sigenergy, Smart Optimization OFF)

## Classification

Current bug in v2.12.785 — confirmed against source. Not stale, not a config mistake. This is a new
variant of the repo's most recurring bug class ("stale force/reserve state not restored"), except here
the state is *never persisted at all*, so the existing restart-restore machinery never engages.

## Root cause (one sentence)

`Hold Battery SOC` is armed entirely in an in-memory closure dict with an in-memory expiry timer, and
`persist_force_mode_state()` has no "hold" branch — so a HA restart destroys both the hold record and
its auto-restore timer while the inverter hardware (Sigenergy Remote EMS **STANDBY**) stays latched,
and no startup path ever releases it when Smart Optimization is off.

## Exact code path that loses the state

All paths in `/Users/benboller/Developer/power-sync/custom_components/power_sync/` unless noted.

### 1. Engaging the hold (evening, works correctly)

- `__init__.py:28382` `handle_hold_battery_soc()` — service handler for `power_sync.hold_battery_soc`.
- Picks the brand coordinator (`sigenergy_coordinator`, loop at `__init__.py:28417`) and calls
  `coord.set_backup_mode()` at `__init__.py:28488`.
- `coordinator.py:3883` `SigenergyEnergyCoordinator.set_backup_mode()` →
  `inverters/sigenergy.py:1044` `SigenergyController.set_standby_mode()`:
  writes Modbus holding register **40029 (`REG_REMOTE_EMS_ENABLE`) = 1** and
  **40031 (`REG_REMOTE_EMS_CONTROL_MODE`) = 1 (`REMOTE_EMS_MODE_STANDBY`)**.
  STANDBY blocks **both charge and discharge**, and these registers persist on the inverter
  indefinitely until something writes them again.
- Back in the handler, state is armed **only in memory**:
  - `__init__.py:28516-28518` — `hold_soc_state["active"]=True`, `expires_at=now+240min`, `locked_soc`.
    `hold_soc_state` is a plain closure-local dict created at `__init__.py:23432` inside
    `async_setup_entry`; it is *not* registered in `hass.data` and *not* backed by the Store.
  - `__init__.py:28530` — expiry timer via `async_track_point_in_utc_time(auto_restore_hold_soc, ...)`
    — an in-memory asyncio timer.
  - `__init__.py:28536` — one-shot dispatcher event `power_sync_hold_soc_state` (push only; drives the
    mobile Controls countdown).
- `__init__.py:28544` — `await persist_force_mode_state()`. **This is the core defect.**

### 2. The persistence hole

`__init__.py:23598-23637` `persist_force_mode_state()` serializes only two states:

```python
state_to_save = None
if force_charge_state["active"]:      # branch 1
    ...
elif force_discharge_state["active"]: # branch 2
    ...
stored_data["force_mode_state"] = state_to_save   # line 23632
```

There is **no hold branch**. With a hold active and neither force mode active, `state_to_save` is
`None`, so the call made by the hold handler writes `force_mode_state = None` to the Store — it not
only fails to persist the hold, it actively clears any previously persisted force record. Nothing
about the hold (active flag, expiry, brand, saved mode/reserve) ever reaches disk.

Also note: `hold_soc_state["saved_operation_mode"]` / `["saved_backup_reserve"]` (declared at
`__init__.py:23434-23435`) are never populated by the handler — even in-session restore relies on
generic `restore_normal` reconstruction.

### 3. Restart (one hour into the hold)

The HA process dies. Lost: `hold_soc_state`, the expiry timer, all dispatcher subscriptions.
Retained: the inverter's Remote EMS STANDBY registers (hardware) — the battery is now latched frozen.

### 4. Startup — nothing restores or releases the hold

- `__init__.py:18665` — `force_mode_state = stored_data.get("force_mode_state")` → `None`
  (it was nulled at hold time; hold was never saved anyway).
- `__init__.py:23396` — `persisted_force_state = ... or {}` → empty dict.
- `__init__.py:23432` — a **fresh** `hold_soc_state = {"active": False, ...}` is created.
- `__init__.py:30147` — `restore_force_mode_from_persistence()` is scheduled;
  at `__init__.py:23643` it hits `if not persisted_force_state: return`. Even if a record had
  survived, the function only understands `mode in ("charge", "discharge")` (`__init__.py:23683`,
  `23764`, `23816`, `23847`) — there is no "hold" schema anywhere in the restore path.
- Smart Optimization is OFF, so no LP cycle ever writes a new EMS mode (with the optimizer ON, the
  next 5-minute cycle would have incidentally overwritten STANDBY — that is why this bug bites the
  optimizer-off population hardest).
- The only startup hardware-hygiene guard, `_restore_disabled_optimizer_reserve_if_stale()`
  (`__init__.py:117`, invoked at `__init__.py:33102` when the optimizer is configured-but-disabled),
  is doubly useless here: line 129 explicitly returns `False` for
  `{"tesla", "sigenergy", "goodwe", CUSTOM}`, and it only fixes a stale **backup reserve** — never a
  work mode / Remote EMS mode / discharge cap.
- The expiry timer is never re-armed; `auto_restore_hold_soc` (`__init__.py:28522`) no longer exists.

Result: Sigenergy stays in Remote EMS STANDBY forever → battery neither charges nor discharges.

### 5. Why the UI shows "no hold active"

- Primary: the fresh `hold_soc_state` says `active=False` and no `power_sync_hold_soc_state`
  dispatcher event is re-fired after restart, so the mobile Controls screen shows nothing.
- Secondary latent bug found while tracing: the Battery Mode sensor
  (`sensor.py:5688`, `5725`) reads `entry_data.get("hold_soc_state", {})` from
  `hass.data[DOMAIN][entry_id]` — but only `force_charge_state`/`force_discharge_state` are ever
  registered there (`__init__.py:23482-23483`). `hold_soc_state` (and `self_consumption_state`) are
  **never** placed into `hass.data`, so `sensor.power_sync_battery_mode` can never report `hold_soc`
  or `self_consumption` even while a hold is active pre-restart. The hold's only UI surface is the
  fire-and-forget dispatcher event.

## Log signature to confirm from the user's debug log

- At engagement: `🔒 HOLD SoC: activating for 240 minutes on sigenergy (source=user)` and
  `Sigenergy Remote EMS set to STANDBY (mode 1) for IDLE hold`.
- After restart: **absence** of `🔄 Restoring force ... from persistence` and absence of any
  Sigenergy Remote EMS write lines. (`Found persisted force mode state` at `__init__.py:18667` will
  also be absent.)

## Affected brands / conditions (ranked)

Trigger conditions in all cases: hold engaged → HA restart (or integration reload — same closure
loss) before expiry → Smart Optimization OFF (or monitoring mode preventing optimizer writes), so
nothing else ever rewrites the hardware mode.

**Fully frozen (charge AND discharge blocked, persistent hardware state, no hardware TTL):**

| Brand | Hold primitive | Stuck hardware state |
|---|---|---|
| **Sigenergy** (this ticket — worst) | `coordinator.py:3883` → `inverters/sigenergy.py:1044` | Remote EMS enable=1, mode=STANDBY(1); regs 40029/40031 latched until rewritten |
| **Fronius Reserva** | `coordinator.py:7299` `set_idle()` ("no charge or discharge") | storage control locked idle |
| **Neovolt / Bytewatt** | `coordinator.py:7434` `set_idle()` | idle dispatch latched |
| **AlphaESS** | `coordinator.py:4064` → `inverters/alphaess.py:513` | SoC-Control dispatch (mode 2, power=0, cutoff=current SoC) pinned; best-effort lock persists as a dispatch |
| **SolarEdge / Anker Solix** | `coordinator.py:7073` / `7551` (delegate to controller backup/idle) | controller-dependent, same pattern |

**Partially frozen (discharge blocked; solar can still charge — presents as "battery never discharges"):**

- **Sungrow SH** — `coordinator.py:4674`: 0 W (or 10 W fallback) discharge cap; the
  `_capture_discharge_limit_for_restore()` snapshot is also in-memory, so even the prior limit is
  lost. This is the already-documented "stuck discharge cap" recurring class, now reachable via Hold.
- **FoxESS** (Modbus `coordinator.py:5658`, entity `5844`, cloud `6127`) — work mode latched to
  Backup in inverter settings.
- **SAJ H2** — `coordinator.py:7185` `set_idle()` (no discharge).
- **Tesla / Powerwall** — different primitive (`__init__.py:28450-28474`): backup_reserve pinned to
  the held SoC (capped 80%) + self_consumption. After restart the reserve stays wrong; the
  `_user_backup_reserve` recovery branch (`__init__.py:28104-28119`) only runs inside an in-session
  `restore_normal` with `restore_was_hold_soc=True`, which is impossible post-restart because the
  flag was reset. Presents as a stuck discharge floor, not a full freeze.

**Not affected by this failure mode:** GoodWe — its coordinator has no `set_backup_mode`, so
`handle_hold_battery_soc` errors out at `__init__.py:28481` before touching hardware.

## Immediate user remediation (before any code fix ships)

Call the `power_sync.restore_normal` service once (Developer Tools → Actions). The Sigenergy branch
(`__init__.py:27358-27401`) instantiates a `SigenergyController` and calls
`restore_normal(native_control=...)`, which writes Remote EMS back to self-consumption (mode 2) —
or releases Remote EMS entirely for native/VPP control. Briefly enabling Smart Optimization for one
cycle also unfreezes it, but `restore_normal` is the clean path.

## Fix outline

1. **Persist the hold.** Add a third branch to `persist_force_mode_state()` (`__init__.py:23598`):
   when `hold_soc_state["active"]`, save
   `{"mode": "hold", "expires_at", "duration", "locked_soc", "source", "saved_operation_mode", "saved_backup_reserve"}`
   into `stored_data["force_mode_state"]` (schema-compatible with the existing record). Populate
   `saved_operation_mode`/`saved_backup_reserve` in `handle_hold_battery_soc` when engaging (the
   fields exist at `__init__.py:23434-23435` but are never written).
2. **Restore the hold on startup.** Extend `restore_force_mode_from_persistence()`
   (`__init__.py:23640`) with a `mode == "hold"` path mirroring the charge/discharge logic:
   - *Not yet expired:* re-arm `hold_soc_state` (active, expires_at, locked_soc), re-schedule the
     expiry timer for the remaining minutes, re-fire the `power_sync_hold_soc_state` dispatcher so
     the UI countdown reappears. Optionally re-assert `set_backup_mode()` idempotently in case the
     inverter was power-cycled too.
   - *Expired during downtime (this ticket):* call `SERVICE_RESTORE_NORMAL` (source `"user"` so the
     hold-cleanup branch at `__init__.py:27240` and the Tesla `_user_backup_reserve` branch at
     `__init__.py:28104` both fire), then clear the stored record.
   - *Monitoring mode:* follow the existing pattern (`__init__.py:23695-23719`) — for Sigenergy,
     restore native/VPP control instead of replaying.
3. **Clear persistence symmetrically.** The new persist branch automatically makes
   `restore_normal` and `auto_restore_hold_soc` clear the stored hold (both already call
   `persist_force_mode_state()` after resetting state) — verify the user-sourced-only guard at
   `__init__.py:27233` doesn't leave optimizer-sourced restores holding a stale record.
4. **Register the state dicts in `hass.data`.** Add
   `hass.data[DOMAIN][entry.entry_id]["hold_soc_state"] = hold_soc_state` (and the same for
   `self_consumption_state`) next to `__init__.py:23482-23483`, so `sensor.power_sync_battery_mode`
   (`sensor.py:5688`) actually reports `hold_soc` — this makes the failure diagnosable from entity
   states in future tickets.
5. **Runtime guard for existing stuck installs** (per playbook: validation-only fixes don't protect
   installs already in the bad state). At setup, when the optimizer is disabled/off and no
   force/hold record is persisted, read Sigenergy regs 40029/40031; if Remote EMS is enabled in
   STANDBY, release it (mode 2 or disable EMS) and log a warning. Consider extending the
   `_restore_disabled_optimizer_reserve_if_stale` sweep (`__init__.py:117`) to cover work-mode/EMS
   state, not just reserve, and to stop unconditionally excluding Sigenergy.
6. **Regression tests.** Extend `/Users/benboller/Developer/power-sync/tests/test_force_mode_controls.py`
   (AST source-extraction pattern already used there for `persist_force_mode_state` /
   `restore_force_mode_from_persistence` / `handle_hold_battery_soc`): assert the persist function
   has a hold branch, the restore function handles `mode == "hold"` in both expired and unexpired
   cases, and the hold handler persists after arming the timer. Run with
   `python3.12 -m pytest tests/test_force_mode_controls.py` (CI does not run pytest).
