# Diagnosis: Spread Export rewrites LP charge slots to export (Flow Power overnight case)

**Verdict: CONFIRMED at HEAD (v2.12.785).** Matches confirmed open bug **OB-6** in the
optimizer-bug-hunt registry; no fix commit exists (`git log -S "_spread_export_schedule"`
shows only the feature commits `5e3bc514`, `fce25242`, `143504e9`).

## Root cause

`_spread_export_schedule` (post-solve override #2 in the pipeline) flattens planned export
energy across each **contiguous run of export-allowed slots**, but its main loop selects
rewrite targets filtered **only by SOC-above-floor — never by the slot's original LP
action**. Any slot the LP planned as `charge` that falls inside the allowed run and whose
LP SOC label is above the floor is rewritten to `action="export"`. The buy-low/sell-high
plan is inverted: the battery exports during the cheap overnight window instead of
grid-charging.

The two fallback branches of the same function DO filter by original action
(`in ("export", "discharge")`) — the main loop is the asymmetry.

## Exact code path

File: `custom_components/power_sync/optimization/coordinator.py`

1. `_run_optimization` → `battery_export_allowed = self._battery_export_allowed_slots(...)`
   (L3225). For a non-GloBird provider (`_zerohero_config()` is None — Flow Power included),
   `_battery_export_allowed_slots` (L5680) inserts `_positive_price_export_slots` (L6264)
   as a mask source: **every slot with export price > 0.0 is export-allowed**. Flow Power's
   flat positive feed-in makes essentially the whole 48 h horizon one contiguous allowed run.
2. `_run_optimization` applies overrides after the LP solve: `_spread_import_schedule`,
   then — gated by `_should_spread_export_schedule()` (L5750: `spread_export_enabled`
   AND `_supports_target_export_power()`) — `_spread_export_schedule(self._current_schedule,
   battery_export_allowed)` at **L3325–3330** (first pass) and **L3401–3407** (second pass
   with `export_reserve_floor`). Both passes have the same defect.
3. Inside `_spread_export_schedule` (L5945):
   - Window = contiguous `allowed[idx]` run (L5997–6005).
   - `export_wh` sums energy only from slots originally `export`/`discharge` (L6021–6026) —
     e.g. the LP's Happy Hour 17:30–19:30 exports.
   - **L6030: `spread_positions = list(range(start, end))`** — every slot in the run.
   - L6032–6044: the only filter is `soc > floor + 0.0001`. An overnight charge slot's LP
     SOC label (rising during charging) is normally well above the reserve floor, so it
     stays in `spread_positions`.
   - **L6100–6124: for each position, `new_actions[pos] = ScheduleAction(..., action="export",
     power_w=slot_target_w, battery_charge_w=0.0, battery_discharge_w=slot_target_w)` —
     unconditionally, without checking `original.action`.** A planned `charge` slot becomes
     `export`.
   - Secondary harm: when the running `soc_cursor` depletes to the floor, the `else` branch
     (L6126–6137) rewrites remaining slots — including original `charge` slots — to
     `self_consumption`, also destroying the charge plan.
   - Contrast: the empty-`spread_positions` fallback (L6049–6064), the `target_w <= 0`
     fallback (L6080–6094), and the trailing cleanup loop (L6138–6154) all filter with
     `if getattr(original, "action", None) in ("export", "discharge")`.

**Why the decisions log over-counts exports**: the decisions logger
(`...optimization.coordinator.decisions`, pinned INFO) counts actions from the FINAL
post-override schedule, so the rewritten charge slots inflate the export count relative to
the raw LP solution — exactly the reporter's observation. Not a separate logging bug.

**No compensating mechanism** (adversarially checked per the verification playbook): the
override re-runs identically on the second solve pass and on every ~5-min cycle;
`_bridge_short_export_gaps` / `_disable_idle_schedule` don't restore charge slots;
`_execute_optimizer_action` executes whatever the final schedule says at "now"; export at
mid-range SOC is physically executable, so hardware executes the inverted plan.

## Conditions required (all must hold)

1. **Spread Export toggle on** — `spread_export_enabled` (opt-in; user confirmed).
2. **Brand supports target export power** — `_supports_target_export_power()` (L782):
   battery system in `TARGET_EXPORT_POWER_BATTERY_SYSTEMS` (`const.py` L1883): **GoodWe,
   Sigenergy, Sungrow, FoxESS, AlphaESS, Solax, SAJ H2, Fronius Reserva, Neovolt,
   Anker Solix**. Tesla/Powerwall, Enphase, etc. are NOT affected (override never runs).
3. **Default positive-price export mask in play** — provider without an active ZeroHero
   bonus config (Flow Power qualifies), so `_positive_price_export_slots` marks every
   positive-feed-in slot allowed; a flat positive feed-in makes the contiguous run span
   the cheap overnight charge slots and the evening export slots.
4. **LP planned ≥1 export/discharge slot** (>= 100 W) somewhere in the same contiguous
   allowed run, so `export_wh > 0` (e.g. Happy Hour exports).
5. **The charge slot's LP `soc` label > window floor + 0.0001** (typical while charging).

Severity: MAJOR (money-losing plan), scoped to the spread-export opt-in cohort on the ten
brands above. Applies to any tariff provider meeting condition 3, not just Flow Power.

## Fix outline

1. In `_spread_export_schedule`'s main loop, **exclude slots whose original action is
   `charge`** (or with `battery_charge_w > 0`) from `spread_positions`, and leave those
   slots' original `ScheduleAction` untouched in `new_actions` — including in the
   `slot_target_w <= 0` else-branch (L6126) which currently rewrites them to
   `self_consumption`. Spreading may still expand into `idle`/`self_consumption` slots
   (that is the feature's purpose); it must never consume planned charge slots.
2. Preferably also **break the contiguous window at charge slots** when building runs
   (treat a charge slot as `allowed=False` for run-detection), so the `soc_cursor`
   bookkeeping doesn't silently span a charge block whose energy gain it doesn't model,
   and export energy from an evening window isn't smeared backwards across an overnight
   charge block into the pre-charge night hours.
3. Regression test in `tests/test_battery_export_allowed_slots.py` (existing home of
   `test_spread_export_schedule_*`): schedule with overnight `charge` slots + evening
   `export` slots under an all-True allowed mask; assert charge slots retain action
   `charge` and their power/soc, export energy is spread only across non-charge slots,
   and total spread export energy is conserved.
4. Verify with `python3.12 -m pytest tests/test_battery_export_allowed_slots.py` first,
   then adjacent optimizer tests (CI does not run pytest; local verification is the gate).
5. On fix, update the skill's `references/bug-registry.md` OB-6 entry to FIXED with the
   commit hash and regression-test path.
