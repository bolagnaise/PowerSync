<!-- release: v2.12.1459 -->

## SolarEdge live energy flows

PowerSync now prefers the native SolarEdge grid meter over separate import/export helper sensors. This prevents an unchanged zero helper from making live flows and battery sensors unavailable while the native readings are fresh. The two-minute freshness limit still applies to the sources being used.

## Home Assistant compatibility

Removes the Home Assistant device-ownership deprecation warnings from SolarEdge storage readback, while preserving device identity checks and compatibility with older supported Home Assistant versions and migrated shared devices.
