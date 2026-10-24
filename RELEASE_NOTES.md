<!-- release: v2.12.1481 -->

## What's Changed

**Powerwall pairing with PowerSync and Tesla Fleet API**

Powerwall local pairing through PowerSync and Tesla Fleet API now verifies the public key generated for the current attempt. Previously, another registered client's state could make pairing report success too early or time out even after the new key was verified. If affected, update PowerSync, restart Home Assistant and retry pairing using your existing connection.

Update available via HACS
