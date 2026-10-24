<!-- release: v2.12.1480 -->
## Powerwall pairing with Teslemetry

Powerwall local pairing now uses Teslemetry's registration and verification endpoints and checks the key generated for the current attempt. This fixes an API mismatch that could prevent pairing from completing.

Update PowerSync, restart Home Assistant and retry pairing. You can keep your existing Teslemetry connection; a separate Fleet API token or PowerSync broker connection is not required.
