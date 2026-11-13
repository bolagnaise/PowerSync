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

### Battery-powered charging near zero grid flow

Scheduled charging with no grid import holds its rate while the home battery keeps measured grid flow within the configured tolerance. Small fluctuations inside that tolerance do not reduce the rate repeatedly. Inverter capacity limits and reductions for grid imports above tolerance still apply.

### Zaptec and Price Level charging

When Price Level is enabled, **PowerSync controls charging** applies your price limit to Zaptec-authenticated starts too. **Allow hands-off external charging** leaves those sessions alone until unplugged. Pending PowerSync commands retain battery protection until fresh, valid charger readings confirm the outcome; completed charging releases that session's protection without repeated starts. See the [EV charging guide](https://github.com/bolagnaise/PowerSync/blob/main/docs/ev-charging.md#zaptec-price-level-charging).
