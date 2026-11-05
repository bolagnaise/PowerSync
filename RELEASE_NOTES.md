<!-- release: v2.12.1631 -->

## Powerwall readings during cloud outages

Tesla sites with a paired local Powerwall gateway now keep fresh local energy readings visible when cloud requests fail, a Fleet token is temporarily unavailable, or the cloud response lacks a valid battery-power reading. Previously, these failures could hide local readings that were still available.

The gateway must be reachable and its power readings complete and no older than 30 seconds. This restores local telemetry fallback; it does not add offline scheduling or replace internet-dependent prices, forecasts, or controls. Revoked credentials still require reauthentication.

Update through HACS and restart Home Assistant.
