<!-- release: v2.12.1621 -->

## What's Changed

**Paired Powerwall energy accounting**
Smart Optimization now uses fresh local battery level and power readings together on Powerwalls paired to a local gateway. This prevents delayed Tesla cloud updates from turning measured solar/grid energy into unknown during charging and discharging.

Cloud fallback remains conservative when local readings are unavailable; existing unknown energy is not reassigned. Update through HACS and restart Home Assistant.

Update available via HACS
