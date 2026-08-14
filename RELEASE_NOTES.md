<!-- release: v2.12.1104 -->

## What's Changed

**Aligned vehicle status across Home Assistant and mobile**
Home Assistant EV sensors, the energy-flow dashboard, loadpoint status, and the mobile widgets now share one canonical vehicle display snapshot. The charging vehicle's configured name, connection state, charging state, power, and battery level are projected consistently, preventing one dashboard from showing an idle vehicle while another omits the vehicle that is actually charging.

**Corrected multi-charger Home Load attribution**
Every supported observed charger is now aggregated once within the same coordinated refresh before Home Load is normalized. Tesla schedule aliases are resolved to their physical VIN before deduplication, so sequential vehicle IDs and provider telemetry cannot double-count charging or leave a charger included in Home Load.

**Removed duplicate mobile detection**
The mobile app now treats PowerSync's loadpoint and widget responses as canonical instead of running a second vehicle coalescing pass that could hide or reassign vehicles.

Update available via HACS
