## What's Changed

**AEMO: optimizer no longer runs continuously while searching for new dispatch data**
The AEMO price coordinator switches to 1-second polling in the 15 seconds after each 5-minute boundary while it waits for a new dispatch file to appear on NEMWEB. Home Assistant fires the price-update callback on every poll — even when the file hasn't changed — which was causing the LP optimizer to run 10+ times in a 20-second window, each run overwriting the previous battery control command. The optimizer now tracks which dispatch file triggered its last run and skips re-optimization when the underlying price data hasn't actually changed. LP runs exactly once per new dispatch file.

**Dashboard: battery health now shows live RSA/BMS data instead of stale scan data**
The main status endpoint (used by the dashboard battery health card) was reading from the mobile-app WiFi-scan POST cache, ignoring the live RSA/TEDAPI BMS data fetched by the dedicated battery health endpoint. For Tesla systems, the status endpoint now checks the live BMS cache first (populated on demand, 1-hour TTL) and falls back to stored scan data only when no fresh BMS result is available. Battery health metrics — capacity, degradation percentage, battery count — will now reflect the most recent RSA scan rather than the last time a phone was on the local network.

*Update available via HACS*
