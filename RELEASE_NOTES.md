<!-- release: v2.12.1517 -->

## What's Changed

**Solar Surplus EV charging switch for Home Assistant**
Control Solar Surplus EV charging from Home Assistant dashboards, scripts and automations using the new Solar Surplus EV Charging switch on the PowerSync EV Charging device. The switch uses the same saved setting as the app, stays synchronized with app changes and retains its state after a restart. Its suggested entity ID is `switch.power_sync_solar_surplus_ev_charging`.

**Consistent enable and disable behavior**
Turning the switch on lets Solar Surplus charging run when your existing vehicle, surplus, battery SOC and export-price conditions are met. Turning it off saves the disabled setting and immediately requests a stop for Solar Surplus sessions through the existing charging controls. Failed charger stops are logged, and the switch is unavailable until settings storage is ready.

Update available via HACS
