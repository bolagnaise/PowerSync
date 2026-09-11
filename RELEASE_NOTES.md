<!-- release: v2.12.1277 -->

## What's Changed

**Tesla partial telemetry now stays unavailable**
PowerSync no longer publishes a missing or invalid Tesla `battery_power` field as a measured 0 W value. A partial Tesla energy response now fails safely instead of feeding a synthetic idle battery reading into the energy flow and optimizer, while a genuine reported 0 W remains valid.

Update available via HACS
