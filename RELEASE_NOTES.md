## What's Changed

**Tesla plug-in detection now agrees between status card and manual start**
Tesla vehicles whose `charging_state` reports "Stopped", "Complete",
"Starting", or "NoPower" now register as plugged in everywhere — both on
the EV loadpoint status card the mobile app reads and on the manual
start-charging path. Previously the loadpoint card could happily show your
car as plugged in based on `charge_cable` sensors while a manual Start
would refuse because the command path couldn't find a confirming signal.
A shared helper now interprets the charging-state text the same way in
both code paths, so what you see on the card matches what the Start button
can act on.

**Auto-schedule deadline charging holds full charge rate**
When auto-schedule is racing a deadline and needs grid import — solar
surplus alone won't get you there — it now passes a fixed maximum amp
target and locks the dynamic loop to that rate until the deadline is met.
Before, the surplus-based logic could throttle amps mid-charge based on
house load swings and miss the target SoC by the deadline. Solar-surplus
modes are unchanged: they still ramp dynamically to soak up free PV
without pulling from the grid.

Update available via HACS
