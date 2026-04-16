## What's Changed

**Cleaner EV vehicle names in the energy flow card**
Vehicle labels now strip raw BLE prefixes and trailing sensor suffixes for a clean display.
Names like "Tesla BLE (ble_slater)" or "Tesla BLE Slater Charge Level" now render simply
as "Slater". Multi-vehicle setups will see consistent, readable names throughout the card.

**BLE vehicles correctly disappear from the energy flow card when out of range**
BLE entities retain their last reported state when a vehicle drives away, which previously
caused absent vehicles to linger on the dashboard indefinitely. The card now treats
BLE-backed sensors as absent if they haven't reported in 15 minutes, allowing vehicles
that are actually present to render correctly. Person and device_tracker entities are
unaffected.

**Ghost EV charging sessions are now auto-ended**
Sessions that recorded no energy update for 30+ minutes (typically caused by a charger
pause or BLE drop-out) used to accumulate forever and inflate session history. The
planner now sweeps for stale sessions every 5 minutes and ends them with a clear
"stale_timeout" reason, keeping session logs clean and accurate.

Update available via HACS
