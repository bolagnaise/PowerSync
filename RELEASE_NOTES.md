<!-- release: v2.12.511 -->

## What's Changed

**Powerwall local pairing uses the complete Fleet API authorization envelope**
Powerwall local pairing and pairing verification now include the Fleet API authorization category and command name metadata when registering or listing authorized clients. This matches Tesla's expected command envelope and helps pairing requests reach the gateway authorization flow reliably.

Update available via HACS
