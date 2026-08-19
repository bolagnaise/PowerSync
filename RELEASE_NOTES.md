<!-- release: v2.12.1160 -->

## Smart EV charging was planned against generic prices, not your tariff

**Update if you use Smart EV Charging on a fixed or custom time-of-use tariff — anything other than Amber, Flow Power, AGL, GloBird or an AEMO VPP plan. This includes every Western Australian site, which cannot select a NEM retailer at all.**

If your car kept being scheduled just *before* your cheapest window instead of inside it, day after day, this is why.

### The planner was reading a tariff that was not yours

Smart EV Charging picks its charging windows by sorting the next 24 hours by price and filling the cheapest ones first. To do that it needs your prices.

It only ever asked for them if your electricity provider was one of five values. For every other provider — including **Other / Custom TOU**, the only setting a WA site can use — it silently substituted a generic Australian time-of-use curve written into the planner: 15 c/kWh overnight, 45 c/kWh from 07:00–09:00 and 17:00–21:00, and 25 c/kWh the rest of the day. Nothing was logged when it did this, at any log level.

On a Synergy Midday-Saver style tariff, where the real shape is roughly 24 c/kWh in the morning, **8.5 c/kWh from 09:00 to 15:00**, then a 45 c/kWh afternoon peak, the effect is exact and inverted:

- 06:00–07:00 — really 24 c/kWh, believed **15 c/kWh**
- 09:00–15:00 — really 8.5 c/kWh, believed **25 c/kWh**

So the genuinely cheapest window in your day looked like the *expensive* one, the early morning looked cheap, and the planner correctly filled the cheapest windows it believed in. The result was a full charge bought at the morning rate rather than the super off-peak rate — repeated every day, because the substitute curve is a function of the clock alone.

The same believed price was used to enforce your **maximum grid price** setting, so that guard was being applied against numbers you do not pay either.

There was a visible tell if you looked for it: on the same chart, the *battery* was correctly grid-charging inside the cheap band. The battery optimizer and the EV planner were reading different price sources.

### Why the optimizer did not catch it

PowerSync also hands your car's remaining energy to the main optimizer, which re-places it against the real forward price series and your site's import limit — the layer that should have overridden a bad EV plan.

That hand-off was failing outright for anyone with a departure or ready-by time set. The plan's target time is stored on Home Assistant's local clock without a timezone; the optimizer compared it against a timezone-aware "now", which is an error in Python. The error was caught by a broad safety net that returned "no EV demand" and logged the reason at debug level only.

The practical effect: **if you had a Smart Schedule departure time, EV co-optimization was off**, and the planner's own mispriced windows were handed to the solve as fixed household demand instead. That is also what the app plotted as your Planned EV load.

### What changed

- Smart EV Charging now uses your configured tariff schedule for **any** provider. The generic estimate remains only as a true last resort, for a site with no tariff at all, and it now logs a warning when it is used instead of substituting itself in silence.
- The departure time is read on Home Assistant's clock, so co-optimization runs for vehicles that have one. Deadlines already past are still skipped, exactly as before.
- The safety net around the EV hand-off is kept — a broken EV plan must never stop your battery from being optimized — but it now logs at warning level so a failure like this cannot go unnoticed again.
- When the optimizer co-optimizes your car, the Planned EV load chart now shows the solver's own placement. Previously that series was blank in exactly that case, because the planner overlay is deliberately zeroed to stop the car being counted twice.

### What you should see

On the next plan rebuild, EV charging should be placed inside your actual cheap window rather than ahead of it, and the prices behind that decision should be your own. On the tariff above, a 13.75 kWh charge planned into the 09:00–15:00 super off-peak costs about $1.17 instead of about $3.34 at the morning rate.

Nothing about your charge target, departure time, charger limits or maximum grid price setting has changed — only which prices those settings are measured against.
