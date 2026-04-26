## What's Changed

**Generic EV Charger: Fix manual Start command returning "Vehicle is not plugged in"**
The app sends `vehicle_id=generic_ev` for generic chargers, but the VIN resolver only recognised Tesla VINs and BLE identifiers — `generic_ev` fell through to the Tesla precondition path, which immediately returned "not plugged in". `generic_ev` (and `zaptec_standalone`) are now passed through as-is, so the correct generic charger code path is reached.

Update available via HACS
