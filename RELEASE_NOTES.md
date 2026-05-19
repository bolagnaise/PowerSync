<!-- release: v2.12.436 -->

## What's Changed

**Fixed Sungrow SG inverter Modbus transaction collisions**
Sungrow SG string inverter polling and curtailment commands now share a per-endpoint Modbus request lock. This prevents status polling, restore, and load-following curtailment writes from overlapping on the same WiNet-S connection, avoiding transaction ID mismatch errors during optimizer control changes.

**Hardened Sungrow inverter disconnect handling**
Disconnects now wait for any in-flight Sungrow SG Modbus request to finish before closing the client, reducing the chance that a cleanup path can interrupt an active read or write.

Update available via HACS
