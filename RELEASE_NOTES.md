## What's Changed

**Tesla Battery Health: Direct Fleet API Fetch (No App Required)**
Battery Health data for Powerwall systems is now fetched directly from Tesla's Fleet API using the paired RSA key, without requiring the mobile app to periodically submit readings. If your Powerwall is paired in Local Control, the Battery Health endpoint will query Tesla's cloud for per-pack capacity and SOH data automatically, with a 1-hour cache. Systems that aren't paired yet will see a clear "complete pairing in Local Control" message instead of a blank screen.

**EV Charger: Remove Vehicles from PowerSync**
A `generic_ev` or other accidentally-added vehicle can now be removed from the Per-Vehicle Settings section in the app's EVs & Chargers screen. Tapping the trash icon and confirming removes it from PowerSync's config without affecting the vehicle itself.

**Opportunistic Charging Log Clarity**
The log line `Opportunistic charging: current Xc <= best planned 30.0c` was confusing users who hadn't configured price-level charging — the 30c looked like a setting they'd missed. The message now reads `cheapest scheduled window Xc` when a real schedule exists, or `default threshold 30c (no schedule set)` when it's the fallback.

Update available via HACS
