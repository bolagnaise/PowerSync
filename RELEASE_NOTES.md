<!-- release: v2.12.1167 -->

## Running two Tesla integrations: a duplicate car, and the Powerwall posing as one

**Update if you run more than one Tesla integration at once** — Tesla Fleet API alongside Teslemetry, Tessie, or the Tesla Custom Integration. If your vehicle list has an extra car in it that you do not own, this is why.

Both faults below came out of one household whose log read `Discovered 3 Tesla vehicle(s)` for two cars.

### The same car was counted once per integration

Every Tesla integration registers its own device for each vehicle, and each of those devices carries the same VIN. PowerSync walked the device list rather than the VIN list, so a car visible to two integrations was discovered twice.

The duplicate is not harmless. It becomes a phantom Smart Schedule loadpoint: a car that shows up in your vehicle list, is offered settings, and asks the optimizer to reserve import headroom for energy no real car will ever draw.

Vehicles are now identified by VIN — one car, one entry, however many integrations can see it.

**Why this needed more than deduplication.** A car's telemetry is split across whichever integrations registered it: battery level may come from one, plug state from another. Simply keeping the first device and discarding the rest would have traded a phantom car for missing sensors. Every merged device is now remembered, and the scan that reads a vehicle's live state reads all of them.

### The home battery's charge could be read as a car's

Your Powerwall and your Wall Connectors register under the same integrations as your cars, but with identifiers that are not VINs.

Those devices were being added to the vehicle lookup with an *empty* VIN. The check that asks "is this device the car I asked about?" treated an empty VIN as matching anything, so a lookup for a specific vehicle could return `sensor.<your site>_battery_charged` — the state of charge of your **home battery** — as that car's state of charge.

Whether it actually did came down to the order Home Assistant happened to iterate its entity registry, which is exactly the kind of fault that looks intermittent and unreproducible. A car reported at the Powerwall's SoC is a car whose remaining energy is wrong, and therefore a charging plan sized against a number that has nothing to do with the vehicle.

Only a 17-character VIN identifies a vehicle now, and an empty VIN no longer matches everything.
