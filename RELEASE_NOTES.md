<!-- release: v2.12.1161 -->

## Sigenergy: the battery exported through your own zero-export curtailment

**Update if you run a Sigenergy system with DC curtailment enabled.** Nothing else is affected.

If you have ever watched your site export while the feed-in price was zero or negative — with PowerSync still showing **"CURTAILED - Export confirmed stopped"** — this is the reason.

### Two parts of PowerSync were writing the same register

On Sigenergy, the grid export limit is a single Modbus register (40038). Two independent parts of PowerSync write it:

- **DC curtailment** writes `0` when export earnings drop below 1 c/kWh, so the inverter self-curtails PV at hardware speed and only grid export is blocked.
- **The optimizer's force discharge** writes a non-zero ceiling — the export target it wants for that slot.

Neither one knew the other existed. In a reported window the sequence was exact:

- `10:11:00` — export earnings 0.56 c/kWh, below the threshold. Curtailment writes zero export.
- `10:14:43` — the optimizer plans an export slot and writes a 1.69 kW export ceiling to the same register, enables Remote EMS and selects the PV-first discharge mode.
- `10:14:47` onward — the site exports 1.6–1.7 kW at 0.56 c/kWh, and later at 0.00 c/kWh.

The optimizer's plan was internally consistent — every sell price in the forward horizon was positive, and surplus solar had nowhere else to go — but you had told PowerSync that export below 1 c/kWh is not worth making, and that instruction lost a silent race.

### PowerSync then reported the losing side

Nothing in that sequence cleared the cached curtailment state, so PowerSync still believed it was curtailed. Five minutes later, with export earnings at **−0.14 c/kWh**, the curtailment check logged *"already curtailed (zero export), no action needed"* while its own live readback from the inverter reported the opposite. The dashboard card kept claiming export was confirmed stopped, and the mobile app's Sigenergy settings kept reporting curtailment as on, because both read that same cached value.

Export only stopped when the periodic re-apply happened to sample live export above its threshold and wrote zero again — which is why the behaviour looked intermittent rather than constant.

### "Resume Auto" was lifting curtailment too

Pressing **Resume Auto** while curtailment was active restored the 5 kW export safety cap over the top of it. PowerSync already had the logic to reassert zero export during a restore, but it only applied to restores the optimizer or a force timer started — not to ones you started.

### What changed

- While Sigenergy zero-export curtailment is active, the optimizer will **not** raise the export ceiling. The export command is refused at the point where every optimizer export write goes through, it is logged plainly, and the plan falls back to self-consumption for that slot. If a force discharge was already running when curtailment started, it is handed back to self-consumption rather than left armed.
- **Every** non-native restore now reasserts the zero-export limit while curtailment is active, including one you trigger from the app. Handing the inverter back to Sigenergy's own native/VPP control still restores the normal export cap, unchanged.

### What you should see

With DC curtailment on and export earnings below 1 c/kWh, grid export stays at zero — the battery keeps charging from solar and covering the house, but nothing is sold at a price you told PowerSync to reject. Above the threshold, export planning is exactly as before.

One consequence worth knowing: curtailment state is refreshed on each price update, so when a price recovers above the threshold, an export slot can be held back until that next price update lands. That is a deliberate trade — the site never exports below your threshold, at the cost of joining a recovering window a few minutes late.

### Not changed

The optimizer still *plans* export in any slot with a positive sell price; this release makes your curtailment setting win at execution time rather than changing what the solver wants. A minimum sell price for battery export remains a separate setting we have not added.

The dashboard's curtailment card is still price-based for non-FoxESS systems. With this fix PowerSync no longer contradicts itself by lifting its own curtailment, but reconciling that card against live export telemetry for every brand is a separate change.
