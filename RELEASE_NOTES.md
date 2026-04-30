## What's Changed

**Powerwall local control no longer needs the customer password**
Local LAN access to Powerwall (live polling and fast gateway writes) used to require both the gateway IP **and** the customer password (last 5 of the serial). PowerSync now uses the RSA private key established during Tesla pairing for all reads and writes — both PW2 and PW3 route through the signed `/tedapi/v1r` transport. Setup just asks for the gateway IP, and the password field is gone from initial config and the Tesla options flow. If you previously had the password saved, it's simply ignored. This matches how third-party local clients have been authenticating since Tesla closed off Bearer login.

**Fix: integration failed to load when OCPP charger tracking was enabled**
PowerSync 2.12.247 introduced an `UnboundLocalError: cannot access local variable 'async_track_time_interval'` at startup for any user who had OCPP charger session tracking enabled but didn't also have Zaptec enabled. The OCPP poll setup relied on an import statement that only ran inside the Zaptec branch, so Python treated the helper as an unbound local in the OCPP branch. Setup now imports it locally where it's used. Affected users were Flow Power / Sungrow customers and anyone else with OCPP-only EV charging.

**Fix: SAJ H2 "Hold SoC" / idle mode now actually engages the inverter**
The Hold SoC dispatcher iterates a hardcoded coordinator list to find the active battery brand and `saj_h2_coordinator` was missing from it — so SAJ requests fell through to a "no battery coordinator available" log line and `set_idle()` on the controller was never called. SAJ force-charge and force-discharge already worked because they use a different per-brand dispatch path that did include SAJ. With this fix, idle / Hold SoC drops the inverter into passive charge mode at zero power as designed, holding the battery at its current SOC.

Update available via HACS
