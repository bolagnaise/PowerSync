<!-- release: v2.12.1093 -->

## What's Changed

**Restore legacy GoodWe hybrid connections**

Fixed an upgrade regression that could leave existing GoodWe systems without an energy coordinator when they used Home Assistant entities for EMS commands but relied on PowerSync's direct IP connection for telemetry. Legacy installations now keep their direct telemetry fallback and existing EMS entity relay, while explicitly selected Home Assistant-only GoodWe profiles continue to fail closed when required telemetry is unavailable.

Update available via HACS
