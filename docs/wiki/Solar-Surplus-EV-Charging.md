# Solar Surplus EV Charging

Use **Solar Surplus EV Charging** on the PowerSync **EV Charging** device to
control the same setting as the app's Solar Surplus toggle. Its suggested entity
ID is `switch.power_sync_solar_surplus_ev_charging`; Home Assistant may assign a
suffix if that ID is already in use. The switch supports `switch.turn_on`,
`switch.turn_off`, and `switch.toggle` in dashboards, scripts, and automations.

Turning it on enables automatic Solar Surplus evaluation using your existing
vehicle, surplus, battery SOC, and export-price settings. Charging starts when
those conditions are met. Turning it off saves the disabled setting and
immediately requests a stop for Solar Surplus sessions; other charging modes
keep their own controls. A failed charger stop is logged, so an off switch is
not confirmation that the physical charger stopped.

The setting survives restarts and stays synchronized with app changes. It is
off by default on installations without a saved Solar Surplus setting and is
unavailable while automation storage is not ready. Configure your EVs and
chargers before enabling it.

```yaml
action: switch.turn_on
target:
  entity_id: switch.power_sync_solar_surplus_ev_charging
```
