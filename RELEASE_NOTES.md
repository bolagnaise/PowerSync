<!-- release: v2.12.1329 -->

## Fixes

### Sigenergy: a planned export could still buy power from the grid

During an optimizer export, PowerSync set the inverter's ESS Max Discharging
Limit (register 40034) from the plan's battery figure, raised to whatever the
house happened to be drawing at the moment the command went out. Register
40034 is a capability ceiling, not an export target — it can only ever stop
the battery from discharging — so any load above that figure was served from
the grid instead. In a high-price evening window an "export" action spent the
window importing at full retail price.

Sampling live load once per dispatch did not fix it. The optimizer writes the
ceiling once and holds it for the whole window, so an oven or air-conditioner
switched on afterwards stayed uncovered until the next run, minutes later. And
when live demand already exceeded the plan, that sample replaced the export
target rather than adding to it, so the planned export delivered nothing.

The ESS ceiling now keeps the full rated headroom — or your configured
discharge cap, when you have set one — for the whole export window. The
grid-point export limit (register 40038) still bounds export on its own, and
the EV-charging discharge throttle is unaffected. The visible difference is
that the battery, not the grid, now covers house load during an export window,
so state of charge falls a little faster in windows with heavy load.
