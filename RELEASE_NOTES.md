<!-- release: v2.12.1169 -->

## What's Changed

**Planned EV charging no longer shows impossible power levels**
The optimizer plan could show EV charging at 14.7 kW — the capability of both
chargers added together — in windows where only one car would really charge.
All vehicles were handed to the solver as a single summed block, so it could
stack two chargers' capability into one car's window and even meet one car's
departure deadline using the other car's charger. Each vehicle is now modeled
separately: capped at its own charger's rate, with its own delivery deadline.
On a two-Tesla site this drops the peak planned EV draw from 14.7 kW to
7.36 kW, keeps the departing car's energy on its own charger before its
deadline, and leaves the free-window car to charge in its own cheap window.

**Your vehicles now appear in the plan individually**
Because the combined block had no per-vehicle identity, the app could never
show which cars were in the plan — and three surfaces (the planned-EV series,
the optimization sensor attributes, and the planned-EV summary) published
only the legacy load overlay, which is deliberately blanked whenever the
solver co-optimizes the car. Those surfaces now adopt the solved plan, and a
new per-vehicle breakdown (`ev_charging_by_vehicle_w`) is published alongside
the total, so each car's planned charging is visible separately.

**EV rate control follows the right car's plan**
The optimizer's planned figure acts as a rate ceiling for each charging car.
It was the fleet total, so a single car could ramp toward the combined
capability of every charger on site. Each loadpoint now follows its own
vehicle's planned share.

Update available via HACS
