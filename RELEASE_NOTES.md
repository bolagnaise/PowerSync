<!-- release: v2.12.1149 -->

## Fixes Price Level stopping your EV and never starting it again

When Price Level took a loadpoint over from Solar Surplus, PowerSync could stop the car, believe it had started it again, and then sit there for the rest of the window while the car stayed at zero. No retry ever fired, because nothing had reported a failure.

**Update if you use Price Level charging with a Tesla.** This affects every release from 2.12.1136 onward, including the physical-start confirmation added in 2.12.1136 for exactly this class of problem.

### What was happening

Handing a loadpoint from one mode to another tears the old session down first, and that teardown sends a real stop to the charger. Cloud EV telemetry (Tessie, Fleet, Teslemetry) only refreshes every ~30 seconds, so for the next half minute the car still reports `charging` at the amps it was drawing before the stop.

PowerSync read that reading 17 milliseconds after issuing its own stop, concluded the car was already physically charging, and took the "adopt an existing charge" shortcut — which skips sending a start command *and* skips the physical-start confirmation, marking the start confirmed from the same pre-stop sample. It then recorded a session, claimed the loadpoint, and pushed a "started" notification, all for a car that was off.

Because the start reported success, the 2.12.1136 bounded-backoff retry was never armed, and the 30-second adjustment loop could not notice either — it only restarts a Tesla when the commanded amps are at zero, and they had just been set to the target. In a real capture the car sat stopped for twelve minutes with Solar Surplus blocked every cycle, until the owner started it from the Tesla app.

### What changed

The already-charging shortcut is now disqualified when PowerSync itself just commanded that charger off. A handover falls through to the normal path: send the start, then confirm it against fresh charging state plus measured draw. If that confirmation fails, no session and no ownership lease are created, the compensating stop runs, and the existing bounded backoff retries while the price stays eligible.

Adopting a car that was genuinely already charging under external control is unchanged — PowerSync did not stop it, so its telemetry is still trustworthy. The disqualification is scoped to the vehicle that was stopped, so a second EV's recovery is unaffected.

## Fixes the Sungrow battery-sensor options page refusing to save

On Sungrow systems, **Settings → Devices & Services → PowerSync → Configure → Battery connection method & sensors** could not be submitted at all. Changing *Additional sensors* to *All supported sensors* — or changing anything else on that page — returned:

```
Entity  is neither a valid entity ID nor a valid UUID
```

The page carries an optional *battery integration anchor sensor* field. It was being given a blank default, and Home Assistant validates a form field's default even when the field is left empty, so the entity picker rejected the blank value before your changes were ever read. The initial setup wizard was never affected because its copy of the field carries no default.

The default is now attached only once a real sensor is stored. Installs that already saved an empty anchor are repaired on the next visit to the page, so no reconfiguration is needed.

## Fixes chart tooltips being cut off at the edges of the graph

Hovering near the left or right end of the **SOC & Battery Power** or **Electricity Price** chart in the 24-hour optimizer plan pushed the tooltip past the edge of the chart, where it was clipped — taking the right-hand value column with it. The same applied to the Current Price History, TOU Schedule, LP Forecast and 24-hour Energy cards.

The tooltip is centred on the hovered point and was held back from each edge by a fixed distance that assumed a narrow tooltip. Any long row label — *Conditional Price-Level window*, for example — makes it wider than that assumption, and the overflow grows with your theme's font size.

Both charts now measure the tooltip and clamp it by its actual half-width, matching the vertical flip that already measured its height. A tooltip wider than the chart itself is centred rather than pushed off one side.

## Fixes cost rows disappearing from Daily Cost Tracking

Since 2.12.1132, a monetary total whose priced coverage is incomplete reports as unknown rather than showing a misleading `$0.00`. The dashboard treated an unknown value as "this entity isn't available" and dropped the whole row, so *Export Earnings Today* — or *Avg Cost per kWh (Today)* / *(Month)* — silently vanished from the card instead of showing that it had no value.

The Daily Cost Tracking card now keeps a row whenever the sensor exists, and Home Assistant renders it as *Unknown*. A missing figure now looks like a missing figure rather than a missing feature.

If a value still reads Unknown after updating, the sensor's `coverage` and `priced_energy_kwh` attributes (Developer Tools → States) show exactly how much of the period was priced.
