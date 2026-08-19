<!-- release: v2.12.1157 -->

## Daily Cost Tracking showed Unknown for the whole day

**Update if Estimated Import Cost Today, Export Earnings Today, Avg Cost per kWh (Today) or Avg Cost per kWh (Month) read "Unknown" while the rest of your energy figures look normal.**

PowerSync prices your grid energy as it integrates it: every few seconds it takes the power reading, works out how many kWh flowed since the last sample, and multiplies that by the price in force at that moment. Alongside the kWh total it also keeps a second counter — how many of those kWh actually carried a price. If the two disagree, the cost is incomplete and PowerSync refuses to publish it, because a cost total that silently omits part of the day is worse than no total at all. That guard was added after an earlier report where a full day of export sat next to an exact $0.00.

The guard was right. Its threshold was not.

### One second of missing price blanked the whole day

Priced kWh and metered kWh are counted in the same place, one immediately after the other, so the priced figure can never run *ahead* of the metered one. That made the 1 Wh allowance an exact-equality test in disguise: the two numbers had to agree to within a thousandth of a kWh, or the sensors went blank.

A thousandth of a kWh is nothing. When Home Assistant restarts, your inverter's coordinator comes back and starts integrating energy a few seconds before your tariff schedule finishes loading — so one interval gets counted as energy but not as cost. On a site exporting 5 kW that is about 23 Wh unpriced out of a 35 kWh day: a gap of two tenths of one percent. Under the exact test, those 23 Wh blanked Export Earnings Today and both average sensors until local midnight, and Avg Cost per kWh (Month) until the first of the next month.

A proportional allowance for exactly this — 0.05 kWh, or 2% of the day's energy, whichever is larger — already existed. It was being applied one step too late, at a point where it could only ever *hide* a value, never bring one back, so it had no way to help. It now decides the question directly, and the same rule drives both the sensor value and the `coverage` attribute, so the two can no longer disagree.

Coverage that is *materially* short still fails closed. The earlier report's numbers — 0.12 kWh priced against 11.8 kWh metered — remain far outside the allowance and still blank the sensor, as they should.

### Upgrading mid-day blanked the day you upgraded

The priced-kWh counters are recent. If your stored energy file for today was written by a version from before they existed, it came back with the day's cost and the day's energy intact but the priced counter at zero — indistinguishable, to the guard, from a day where nothing at all had been priced. Every cost sensor went blank for the remainder of that day, and the month-to-date average for the remainder of the month.

There was a recovery path, but it required the cost figures to match the optimiser's own separate ledger to within a hundredth of a cent. Those are two independent meters running at different intervals; on the reported site they read 0.20 kWh against 0.14 kWh, so recovery never triggered.

PowerSync now recognises a file written before the counters existed and adopts the day's energy as its priced energy — which is correct, because the version that wrote that file also only added to cost when it had a price to add. This runs once, the first time you load the new version, and restores exactly the figures the older version was showing you.

**What you will notice:** if your cost sensors are currently Unknown, they should show values again within a minute or two of updating and restarting. If they were blank because of a mid-day upgrade, today's totals come back with the day's history intact rather than restarting from zero.

This release changes only what PowerSync is willing to display about cost. No pricing, optimisation, battery command or inverter behaviour is affected.
