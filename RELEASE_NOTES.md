<!-- release: v2.12.749 -->

## What's Changed

**Harden Smart Optimization dispatch and cost reporting**
PowerSync now keeps the LP model, displayed schedule, and predicted cost aligned across more edge cases. The optimiser better handles export reserve floors, capped ZeroHero and ZeroCharge windows, free-import charging, tiered long-horizon planning, and infeasible solves, while reported grid import/export now reflects the schedule PowerSync actually applies.

**Protect reserves during fallback export planning**
The greedy fallback used when the LP solver is unavailable now re-applies the active export reserve floor during its chronological clamp. This prevents fallback export slots from draining below the reserve that Smart Optimization calculated for the export window, even after earlier self-consumption changes the available SOC.

**Improve EV-aware load forecasting**
For non-Tesla and non-Sigenergy systems with EV integration enabled, PowerSync can subtract configured charger power history from whole-home load history before adding the planned EV charging forecast back in. This avoids recurring EV charging being double-counted in the household load forecast.

**Make optimizer settings changes apply more reliably**
User-triggered settings re-optimizations now queue behind any in-flight solve instead of being silently skipped, auto-detected battery specs are synced into the optimizer after startup detection succeeds, grid-charge price caps above $1/kWh are preserved correctly, and optimizer-owned EV amp adjustments only touch loadpoints PowerSync currently owns.

Update available via HACS
