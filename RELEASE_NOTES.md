<!-- release: v2.12.1153 -->

## Fronius no longer reports a power limit it never set, and Avg Cost per kWh stops reading Unknown

Two unrelated reporting defects, both of which made PowerSync state something that was not true.

**Update if you run a Fronius inverter with AC Inverter Curtailment, or if your Daily Cost Tracking card shows Unknown for Avg Cost per kWh.**

### Fronius: "limit confirmed at NW" when no limit was ever written

Fronius curtailment has two modes.

**Load following** calculates a power limit from your home load and writes it to the inverter's `WMaxLimPct` register. That is a real device limit.

**Simple mode** — the default — does not write a power limit at all. It writes a single register that *disables* PowerSync's own throttling, so the inverter falls back to the 0 W export limit configured in its own settings. That soft limit needs an installer password to set, and PowerSync cannot see whether it exists.

PowerSync did not distinguish the two. Whichever mode ran, it logged `Fronius limit confirmed at 1066W`, set the status sensor to `Load Following` with `target_power_w: 1066` and `device_limit_confirmed: true`, and re-issued the same command every 30 seconds with a warning saying the target "still matches" while the site exported 8.5 kW.

On a site running simple mode without that 0 W soft limit, every one of those statements was false. No wattage was ever sent, so nothing could converge, and the reapply was a no-op rewriting a register that already held the value being written.

Now:

- Simple mode reports `Curtailed` with no target power and `device_limit_confirmed: false`. The log says a simple-mode curtailment was applied and that no device power limit is set.
- Load following reports the limit that was actually written to the register.
- When simple mode is not taking effect, the warning names the two things that fix it: set the inverter's export limitation to 0 W, or enable **Fronius load following** in PowerSync's inverter options, which calculates and writes a real limit and needs no installer access.

The status sensor keeps telling you curtailment has not converged — it now reads `Curtailment Pending` with the measured residual export, rather than claiming a confirmed limit alongside it.

### Fronius: a false convergence result from stale telemetry

The convergence check compared the commanded limit against grid power that had been read *before* the command was sent, with no refresh in between. Whenever the site happened not to be exporting in the moment before a write, PowerSync logged that site export was "within the 100 W convergence threshold" and credited the command with a result that predated it.

The apply path no longer publishes a convergence verdict at all. The 30-second load-following cycle, which reads live telemetry at the start of every cycle, is now the only thing that decides whether the site has converged.

### Avg Cost per kWh (Today) read Unknown on Sungrow whenever grid import was small

Since v2.12.1132, a cost total whose priced coverage is incomplete reports `unknown` rather than a misleading $0.00. On Sungrow, PowerSync re-checks that coverage against the inverter's own daily import and export registers, and the comparison was symmetric — it flagged a gap whether the priced energy fell *short* of the register or exceeded it.

That check exists to catch an accumulator that only ran part of the day being paired with a full-day hardware counter. That is the short direction. The other direction means every metered kWh carried a price, which is complete coverage, and the difference is just ordinary disagreement between integrating live power readings and reading the inverter's own daily counter.

On a small counter that disagreement is easily larger than the 0.05 kWh floor the check falls back to. One reported site had 0.26 kWh priced against a 0.10 kWh register: 0.16 kWh apart, well inside the noise for a fraction of a kilowatt-hour, and enough to blank *Estimated Import Cost Today* and *Avg Cost per kWh (Today)* for the entire day, every day. No value of the priced counter could have rescued it.

The check is now one-sided. Both existing under-coverage protections are unchanged.

### Avg Cost per kWh (Month) could stay Unknown all month after a restart

PowerSync withholds Home Load from energy accounting when it cannot yet attribute EV charging to a loadpoint, so an unmeasured car does not silently inflate your household load. Any sample without a Home Load value set both the daily and month-to-date "incomplete accounting" flags.

The daily flag clears at midnight. The month-to-date flag only clears at month rollover — and both were being set even by samples that contribute nothing to the totals, such as the first sample after a Home Assistant restart, or any sample after a gap longer than six minutes. A single restart could therefore disable *Avg Cost per kWh (Month)* until the first of the next month, without one kilowatt-hour of load actually going unmeasured.

Only an interval that was genuinely integrated can mark accounting incomplete now.

### A note on Home Load being withheld

If your *Avg Cost per kWh* sensors are still Unknown after this update, that is the withholding above doing its job: your Home Load really is going unrecorded, and the monthly average would be wrong if it were published.

That withholding used to be completely silent. It now writes one debug line each time, naming how old the EV load snapshot is and which loadpoint could not be measured, so a debug capture will show how often it happens and why. Turn on debug logging with:

```yaml
logger:
  logs:
    custom_components.power_sync: debug
```
