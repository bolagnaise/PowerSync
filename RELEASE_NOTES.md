## What's Changed

**Follower PW3 Query Returns 403 — Log Level Corrected**
The RSA key registered during Powerwall local pairing is bound to the leader unit's DIN. Attempting to send a signed `DeviceControllerQuery` directly to a follower DIN returns 403 Forbidden from Tesla's backend — this is expected and not actionable. The error was previously logged as a WARNING on every battery health refresh; it is now logged at DEBUG level. The follower unit continues to display as a separate entry in the battery health screen (with no individual capacity data available via this path).

**Blocking I/O Warning in Update Entity**
Reading `manifest.json` to determine the installed version was happening synchronously on the HA event loop thread during integration setup. The read is now dispatched to an executor thread, eliminating the `Detected blocking call to read_text` warning in the HA log.

Update available via HACS
