<!-- release: v2.12.1183 -->

## What's Changed

**Prevent duplicate Tesla Wall Connector loadpoints**
PowerSync now recognises when a configured generic charger and Home Assistant's native Tesla Wall Connector discovery are reading the same registered Home Assistant device. That charger contributes once to EV power, Home Load, solar-surplus calculations, and the loadpoint-status endpoint instead of being counted twice. Separate chargers remain independent, even when their power or connection states happen to match.

This changes observation and display aggregation only. It does not start, stop, or otherwise take ownership of an externally controlled Wall Connector.

Update available via HACS
