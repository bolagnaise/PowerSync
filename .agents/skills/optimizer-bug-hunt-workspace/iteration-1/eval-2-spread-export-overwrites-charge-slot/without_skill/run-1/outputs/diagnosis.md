# Diagnosis: Spread Export overwrites planned overnight charge slots with export

## Symptom (ticket)

Flow Power user with the "Spread Export" toggle enabled. The LP solution contained a cheap
overnight grid-charge slot, but at that time the battery **exported** instead of charging, and the
decisions log summary (`custom_components.power_sync.optimization.coordinator.decisions`,
`"Optimization complete ... [charge=N export=M ...]"`, emitted at
`optimization/coordinator.py:3500`) counted more export slots than the LP solution produced.
Integration version is current — this is a live bug, not a stale one.

## Root cause

`OptimizationCoordinator._spread_export_schedule()`
(`custom_components/power_sync/optimization/coordinator.py:5945-6161`) is a **post-solve rewrite**
that flattens the LP's planned export energy across each contiguous "export allowed" window. Inside
each window it builds the rewrite target list as **every slot in the window**, regardless of the
slot's original action:

```python
spread_positions = list(range(start, end))          # line 6030 — ALL slots in the window
```

and then unconditionally replaces each such slot (as long as the window's flattened `target_w > 0`
and the slot's LP-predicted SOC is above the export reserve floor) with:

```python
new_actions[pos] = ScheduleAction(
    timestamp=original.timestamp,
    action="export",                                # line 6115-6122
    power_w=slot_target_w,
    ...
    battery_charge_w=0.0,                           # planned grid charge silently dropped
    battery_discharge_w=slot_target_w,
)
```

There is **no exclusion for `charge` actions**. If an LP `charge` slot falls inside a contiguous
allowed-export window that also contains at least one LP `export`/`discharge` slot
(`export_wh > 0`, computed at lines 6021-6026 over the whole window), the charge slot is rewritten
to `export`. The SOC-floor filter at lines 6032-6044 does not save it: a charging slot's
LP-predicted SOC is by construction above the reserve floor, so it stays in `spread_positions`.

This is a known-dangerous pattern in this repo (post-solve branches in the coordinator bypassing LP
constraints), and the asymmetry is telling: the mirror function `_spread_import_schedule()`
**explicitly skips** export/discharge slots when spreading charge energy
(`coordinator.py:5857`: `if blocked[idx] or getattr(actions[idx], "action", None) in ("discharge",
"export")`), but `_spread_export_schedule()` has no equivalent skip for `charge` slots.

## Why the window reaches the overnight charge slot (Flow Power specifics)

The allowed mask passed to `_spread_export_schedule()` is `battery_export_allowed` from
`_battery_export_allowed_slots()` (`coordinator.py:5680-5715`, call site 3225-3228, passed at
3325-3330 and 3401-3407). For a Flow Power user (no ZeroHero config → `zerohero_config is None`),
the slot sources **include `_positive_price_export_slots()`** (`coordinator.py:5702`, definition
6264-6289), which marks **every slot with feed-in price > 0** as export-allowed.

Flow Power feed-in is wholesale-linked and typically stays a few c/kWh **positive all night**, even
while the *import* price dips low enough for the LP to schedule a cheap grid charge. Result: one
giant contiguous `allowed == True` run stretching from the evening export period (e.g. Happy Hour
17:30-19:30 or any high-price evening export the LP planned) through the entire night. The evening
slots supply `export_wh > 0` for that window, and the flattening loop then rewrites the 2-4am
charge slots to `export` at the flattened `target_w`.

Secondary effect: the SOC cursor is advanced *downward* through the rewritten window
(`_advance_export_soc`, lines 5985-5989, applied at 6110-6124), so every downstream slot's
published SOC is wrong too — the plan shows the battery draining overnight instead of filling.

Ordering makes it worse: `_spread_import_schedule()` runs **first** (call sites 3315-3324 /
3391-3400) and may spread the grid-charge energy across *more* overnight slots — all of which are
then clobbered by `_spread_export_schedule()` immediately after. `_bridge_short_export_gaps()`
(3943+) is **not** a culprit — it only converts `SELF_USE_ACTIONS` gaps, never `charge`.

## Exact code path

1. `OptimizationCoordinator` update cycle → LP solve via `_run_optimizer_once()` (coordinator.py:3271-3310). LP output contains the overnight `charge` slot.
2. `_should_spread_import_schedule()` → optional `_spread_import_schedule()` (3315-3324).
3. `_should_spread_export_schedule()` (5750-5755) → True because `spread_export_enabled` AND `_supports_target_export_power()` (782-788, brand in `TARGET_EXPORT_POWER_BATTERY_SYSTEMS`).
4. `_spread_export_schedule(self._current_schedule, battery_export_allowed)` (3325-3330; second pass with `export_reserve_floor` at 3401-3407).
5. Inside: contiguous allowed window found (5997-6005) → `export_wh > 0` (6021-6027) → `spread_positions = range(start, end)` includes the charge slot (6030) → SOC filter keeps it (6032-6044) → rewritten to `action="export"`, `battery_charge_w=0.0` (6100-6124).
6. `_DECISION_LOGGER.info("Optimization complete ...")` (3497-3512) counts the **rewritten** schedule → more export slots than the LP produced. The executor then commands the hardware from the same rewritten schedule → battery exports during the cheap import slot.

## Conditions required to reproduce

All of:

1. **"Spread Export" toggle ON** (`spread_export_enabled`, set via `set_spread_export_enabled`, coordinator.py:798).
2. **Battery brand supports target export power** — `battery_system` in `TARGET_EXPORT_POWER_BATTERY_SYSTEMS` (`const.py:1883-1894`): **GoodWe, Sigenergy, Sungrow, FoxESS, AlphaESS, Solax, SAJ H2, Fronius Reserva, NeoVolt, Anker Solix**. (Tesla/Powerwall, Enphase, Fronius GEN24 etc. are unaffected — `_should_spread_export_schedule()` returns False.)
3. The LP's charge slot sits inside a **contiguous export-allowed run**. For Flow Power (any non-ZeroHero provider) this happens whenever **feed-in prices stay > 0 across the evening-to-overnight span** (`_positive_price_export_slots`). Export-boost / saving-session / Happy Hour profit windows can also contribute to the mask.
4. The same contiguous window contains **at least one LP export/discharge slot** (`export_wh > 0`) — e.g. evening high-price export or the 17:30-19:30 Happy Hour export.
5. Window flattened `target_w > 0` (export cap positive) and the charge slot's LP SOC > export reserve floor + 0.0001 — effectively always true for a charging slot.

Not a monitoring-mode artifact: the same rewritten schedule feeds both the decisions log and the
hardware command path, so with monitoring off the battery genuinely exports.

## Fix outline

Minimal, in `_spread_export_schedule()` (coordinator.py:5945+):

1. **Treat charge slots as window breakers**, mirroring `_spread_import_schedule()`'s skip at
   line 5857. When scanning the contiguous window (`while idx < n and allowed[idx]`), also stop/skip
   when `getattr(actions[idx], "action", None) == "charge"` or
   `float(getattr(actions[idx], "battery_charge_w", 0) or 0) > 0`. This both preserves the charge
   slot verbatim and splits the spread window so evening export energy is only flattened across
   charge-free sub-windows (spreading "through" a charge would misstate SOC continuity anyway).
2. Equivalently/minimally: exclude charge slots from `spread_positions` and from the
   `export_wh` window sum, and when the SOC cursor passes a preserved charge slot, advance it using
   the slot's original charge contribution instead of `_advance_export_soc`.
3. **Regression test** in `tests/test_battery_export_allowed_slots.py` next to
   `test_spread_export_schedule_flattens_planned_energy_across_allowed_window` (line 4512):
   build a schedule with two evening `export` slots, a middle overnight `charge` slot, and trailing
   `self_consumption` slots, all inside `allowed=[True]*n`; assert the charge slot keeps
   `action == "charge"` and its `battery_charge_w`, that no export power is assigned to it, and that
   the export energy is conserved within the charge-free sub-windows. Note the existing suite has
   `test_spread_import_schedule_preserves_export_actions` (line 5137) but **no** export-side
   counterpart preserving charge actions — this gap is exactly where the bug hid.
4. Verify with `rtk python3.12 -m pytest tests/test_battery_export_allowed_slots.py` first, then
   adjacent optimizer tests. (CI does not run pytest; local verification is the gate.)
