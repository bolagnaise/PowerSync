<!-- release: v2.12.1110 -->

## What's Changed

**Completed named-zone protection for Tesla Home Load fallbacks**
The legacy Tesla status fallback now uses the same Home Assistant location rule as the canonical EV display path: only the literal `home` zone is on-site, while `not_home` and named zones are away. Remote Fleet/Teslemetry charging can no longer reappear as site EV power when Powerwall telemetry has no Wall Connector reading, so it is not subtracted from Home Load during startup or fallback polling.

**Kept paired BLE identity scoped to explicit Both mode**
An away Fleet vehicle now suppresses a paired Tesla BLE charging stream only when the configured provider is `Both`, where the bridge-to-VIN association is explicit or unambiguous. Fleet-only and BLE-only configurations retain independent charging telemetry instead of allowing a stale cross-provider identity to hide a different vehicle's load.

Update available via HACS
