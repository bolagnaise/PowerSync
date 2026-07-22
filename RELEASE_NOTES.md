<!-- release: v2.12.913 -->

## What's Changed

**Powerwall v1r gateway diagnostics**
Powerwall Local Control now reads Tesla's signed v1r system, firmware, network-route, and internet-reachability endpoints. New Home Assistant sensors expose the useful diagnostic state directly in the Powerwall Local Control dashboard card.

**Credential-safe local networking details**
The bundled Tesla protobuf schema has been updated from the latest community protocol findings while deliberately excluding Wi-Fi configuration and credential fields. Diagnostics include interface health, IPv4 details, signal strength, and Tesla connectivity without exposing SSIDs or passwords.

**Non-blocking local polling**
The slower diagnostic reads run in a cached background refresh, keeping the existing low-latency Powerwall energy poll responsive even when a gateway does not support the newer Common API messages.

Update available via HACS
