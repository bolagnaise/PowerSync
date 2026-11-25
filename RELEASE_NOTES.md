<!-- release: v2.12.1834 -->

## Stored-energy origin no longer drifts to "unknown" while the battery discharges

PowerSync tracks how much of the energy in your battery came from solar, from the grid, or from an unknown source, and uses that to value stored energy when planning. When the battery's state of charge updated in delayed steps during self-consumption, each step moved some solar and grid energy into "unknown", so the unknown share kept growing through a discharge. Unknown energy is valued at the median import price, so this could make stored energy look more expensive than it was. Discharge now draws every source in proportion, however the state of charge updates.

## EV charging no longer gets stuck behind its own safety checks

Several safety checks could leave EV charging stuck, or stop it from starting at all:

- A Tesla command sent over Bluetooth that was never confirmed could block every later command to that car, shown as `Tesla command route is blocked (ble_recovery)`. PowerSync now clears the block once a fresh Tesla cloud report, at least 15 minutes later, shows the command took effect. That check is one cloud read with no wake-up, at most every 15 minutes per car. Anything it can't prove stays blocked, and the log says why.
- After an external charge goes quiet, Solar Surplus resumes instead of staying suspended. A single missed reading no longer ends a long external charge that is still running.
- Price-Level and Scheduled modes can start a car again after it was away, or after a Stop that was never confirmed.
- A running Smart Schedule solar session keeps going through a brief dip in surplus.
- The Smart Schedule export-rule override is released when the car leaves or is unplugged.
- The short hold after a stop ends on time, and your own commands get through while it runs.
- Solar Surplus status and the price recommendation now include the car's own charging power.
- An awake, stopped Tesla is recognised from Bluetooth power readings.

When PowerSync refuses to start EV charging, it now logs the reason even at the default log level, with VINs masked. Smart Schedule also logs each change in its per-vehicle decision.

Update through HACS and restart Home Assistant.
