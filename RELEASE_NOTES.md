<!-- release: v2.12.1189 -->

## What's Changed

**Use the configured site import limit for Scheduled EV charging**
Scheduled EV charging now carries PowerSync's configured Maximum Grid Import into the live charger controller. This prevents a configured higher site limit, such as 14.9 kW, from incorrectly falling back to 12.5 kW and unnecessarily reducing available EV charging power while the home battery is charging.

PowerSync still applies any lower Tesla site or Home Power safety limit, along with existing phase, charger, battery-reservation, and live-headroom protections. This update does not change external/manual ownership or override hardware limits.

Update available via HACS
