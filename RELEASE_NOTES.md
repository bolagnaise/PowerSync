<!-- release: v2.12.612 -->

## What's Changed

**Sigenergy EV Charging card API restored**
The EV Charging Home Assistant card now loads correctly for Sigenergy EVAC and EVDC charger setups. The widget and loadpoint status endpoints now use the stored Home Assistant reference when building Sigenergy charger capabilities, fixing the `API not available` error and the `EVLoadpointStatusView object has no attribute 'hass'` traceback reported from the latest card.

Update available via HACS
