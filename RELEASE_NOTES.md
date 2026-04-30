## What's Changed

**Tesla Powerwall — new endpoint to update the gateway IP without re-pairing**
Previously, the only way to set or change the local gateway IP was through the initial setup wizard (new installs only), the Configure → Tesla Connection options page (HA UI), or the mobile app's pairing flow (which forces a full re-pair). None of those handled the everyday case: a user is already paired and just wants to add or update an IP. The new `POST /api/power_sync/powerwall/set_gateway_ip` endpoint takes `{"gateway_ip": "192.168.x.y"}` (empty string clears), writes it to entry.data, tears down the cached local client + coordinator, and lets the next data fetch rebuild against the new host. No re-pair required, no Fleet API round-trip, no key regeneration.

The mobile app's Battery Setup → Gateway Connection screen will use this on the next build to push IP changes through to Home Assistant in real time, finally making that screen do what users expect (the existing screen only saved the IP to local app storage and never reached the integration). For Powerwall 3 sites it also fixes a hidden gating bug — the IP field was visible only on PW2, even though PW3 needs the IP just as much for direct TEDAPI v1r LAN writes (snapshot polling, automated curtailment, fast operation-mode toggles).

Update available via HACS
