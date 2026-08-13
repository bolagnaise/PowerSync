<!-- release: v2.12.1094 -->

## What's Changed

**Correct SolarEdge solar telemetry on battery systems**

Fixed SolarEdge Home Battery sites reporting battery discharge as solar generation, including overnight. PowerSync now prefers complete PV-string telemetry or reconstructs PV from inverter DC and all discovered battery DC channels, rejects incomplete or non-finite telemetry instead of falling back to contaminated inverter AC power, and avoids using the inverter AC daily counter as a solar-only total. Battery-free SolarEdge AC and generic DC fallbacks remain unchanged.

Update available via HACS
