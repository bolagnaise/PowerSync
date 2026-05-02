## What's Changed

**Battery specs editable from config flow**
The LP optimizer used to rely entirely on auto-detected battery capacity and power limits from Tesla's API, with no way to fix it when auto-detection picked the wrong values (common with non-Tesla batteries or fresh installs before site_info populates). The Smart Optimization settings page now exposes three fields — battery capacity (kWh), maximum charge power (kW) and maximum discharge power (kW) — in both initial setup and the options flow. Defaults are tailored per battery system (Tesla, Sigenergy, Sungrow, FoxESS) so most users don't need to touch them.

**Demand-charge protection now covers EV auto-charging**
The "Allow grid charging during demand windows" setting already blocked Tesla Powerwall grid charging during peak windows, but had no effect on EV charging — the auto-schedule planner and price-level executor would happily pull EV grid power inside a demand period and push your billed monthly peak above the cap you'd configured the option to protect against. Both EV automatic charging paths now honor the same setting. Manual charging via switch presses or HA service calls is unaffected, so you can always override when you actually need the car to charge.

**Lifetime totals now persist across restarts**
Tesla's lifetime energy counters (solar generated, grid imported/exported, battery charged/discharged, home consumption) are now cached to disk and restored on startup. The Tesla API occasionally returns values lower than its previous reading for these counters, which would cause Home Assistant's `total_increasing` sensors to log warnings or appear to reset. PowerSync now clamps each counter to the highest value ever seen, keeping long-term energy statistics stable across integration restarts and Tesla API hiccups.

Update available via HACS
