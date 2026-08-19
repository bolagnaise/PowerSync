<!-- release: v2.12.1143 -->

## EV charging no longer steals the home battery's charge window

If your EV was charging while the optimizer was grid-charging the home battery, the car could quietly take the import headroom the battery's plan needed. The battery then got throttled to keep the site under its meter limit and missed its charge target — most visible on sites with a tight import limit, where a planned 14 kW battery charge would settle several kW lower for as long as the car was plugged in.

PowerSync now reserves the battery's **planned** charge power for the current optimization interval before offering anything to the EV.

### What changed

- **The reserve is the plan, not the hardware maximum.** The EV controller previously used the value captured when the charging session started — the battery's maximum charge rate, or nothing at all if the session began outside a cheap-price window. It now reads what the optimizer actually intends to charge in this interval, so the car yields exactly what the battery needs and keeps the rest.
- **The battery's own grid draw is no longer mistaken for spare solar.** One decision path read the battery's charging power as surplus available to the car and ramped the EV up because of it. Every path is now bounded by the real remaining import headroom.
- **Battery-aware control applies to all charging sources.** It previously engaged only for sessions that started in a grid price window. Any session that shares the grid import limit now respects the battery's reserve. Solar Surplus sessions are unaffected, and deadline "charge at maximum rate" sessions still run at full rate by design.

Genuine battery taper is still detected and released to the car: if the battery cannot accept its planned rate and there is real spare headroom, the EV gets it. The change only stops the car from taking headroom the battery is actively being starved of.

This applies to single-vehicle sessions, multi-vehicle households sharing one site meter, and the initial charge rate chosen when a Smart Schedule session starts.

### Diagnosing it

The dynamic EV debug line now reports the resolved reserve alongside the live figures, so a single log line shows whether the reserve was applied:

```
Dynamic EV: battery=-14.7kW (target=-14.7kW), grid=16.1kW (max=16.1kW), battery_reserve=14.7kW, ...
```
