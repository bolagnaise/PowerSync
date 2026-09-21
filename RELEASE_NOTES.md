<!-- release: v2.12.1324 -->

## What's Changed

**SolarEdge: a curtailment could become impossible to release**
When export prices turn negative, PowerSync sets the SolarEdge active power
limit to 0%. Releasing it again is decided fresh every five minutes from the
current price, so a release should always follow the next positive feed-in
price. Two separate faults could stop that release from ever happening, leaving
the inverter producing nothing while the house imported at peak prices — which
looks from the outside like the inverter locking up after a mode change.

The first was a containment fault. If a limit write failed, PowerSync recorded
the result as uncertain and stopped all further inverter writes until a
supervised reconciliation cleared it. But the commonest failure is a refused
Modbus connection, where nothing was transmitted at all and there is nothing to
reconcile — and it was treated identically to a genuinely uncertain write. Worse,
the supervised recovery service refused to clear exactly this kind of record, and
the block was written into the control journal and replayed on every restart, so
the only way out was deleting the journal file by hand. Because the block sits
ahead of the decision to curtail or release, it held the *release* just as firmly
as it held the application.

A limit write that provably changed nothing — one that never reached the
inverter, or that the inverter answered with an explicit error — is now recorded
as a plain rejection and the next five-minute check retries the release. A
genuinely uncertain write still contains control as before, but
`power_sync.reconcile_solaredge_control` can now clear it once the storage
registers read back clean, because the active power limit is a single absolute
value that the next check rewrites from the current prices anyway.

The second was a restart fault. PowerSync tracked "am I currently curtailing?"
in memory only. Restarting Home Assistant, reloading the integration or updating
it while a curtailment was active reset that to "not curtailing" while the
inverter kept the 0% limit in hardware. The next check then saw nothing to
release and said so. PowerSync now records that it owns the limit, and after a
restart starts in a pending state so the next check re-evaluates the limit
against live prices instead of assuming the work is done. The curtailment card
reports "Pending — Export not confirmed" for that window rather than claiming a
state it has not verified.

**Solar Surplus: EV charge rate no longer chases its own charging**
On sites using the grid-based surplus method with a Tesla Powerwall, the charge
rate could swing hard — ramping to maximum, collapsing, and ramping again within
a couple of minutes.

The surplus calculation adds the car's current draw back to the grid reading,
because the meter has already netted it off. That is only correct when both
numbers describe the same moment. Tesla's cloud telemetry caches the site
figures (solar, grid, battery, home load) for around a minute, while the wall
connector reading in the very same response updates on every poll. So each watt
the car ramped up was added to a grid reading taken before the car ramped —
the calculated surplus rose by exactly the amount the car itself had increased,
PowerSync raised the amps, the car drew more, and the loop repeated until the
cached site figures finally refreshed and the surplus collapsed in one step. In
the worst samples this reported more available solar surplus than the site was
producing at all with the battery idle.

PowerSync now records the car's draw as it stood when the site figures were last
actually measured, and uses that to keep both halves of the calculation on the
same instant. Sites whose telemetry does not have this split are unaffected, and
no thresholds, buffers or reserve behaviour changed.

**Amber: unpriced forecast intervals no longer pad the price window**
Amber's forecast carries advanced prices out to roughly a day ahead; intervals
past that horizon arrive without one. PowerSync filled those gaps by repeating
the last known price — correct for the optimizer, which needs a full horizon —
but it also counted them as genuine forecast length. The repeated values then
appeared on the app's price chart as a flat tail the chart is meant to clip, and
were included in the price window used to value battery energy of unknown
origin.

Only intervals Amber actually priced now count as forecast. For a battery
holding energy carried over from a previous day, that window sets the assumed
acquisition cost and therefore the minimum feed-in price at which exporting is
worthwhile, so the flat tail could bias that floor upward. The valuation rule
itself is unchanged: energy of unknown origin is still valued conservatively
from import prices.
