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

Solar Surplus also pauses while the current export value is above your
Solar Surplus export limit, because exporting is then worth more than charging
the car. The vehicle status names the export value and the limit. When there is
no solar generation, such as at night, the status says "No solar generation
right now" instead. The limit only applies to solar charging; planned grid
charging and manual charging are not affected.

For a Tesla Wall Connector with a local Home Assistant entry, PowerSync can
match the connector's serial number to the vehicle reported by Tesla Fleet or
Teslemetry. A fresh unplugged reading prevents a Solar Surplus start while
older Tesla telemetry still shows the car connected. The matching connector's
recent unplug can still be used if Tesla clears the charger’s vehicle identity
first and PowerSync observed the plug session. A newer vehicle cable-on report
lets PowerSync evaluate the next charge session.

When the car reports stopped, an open contactor and fresh readings from the
same Wall Connector let PowerSync distinguish standby draw from charging.
For a Tesla waiting to start, draw below one amp is treated as standby when
there is no charging indication or unresolved Start. Solar Surplus can start
the car when the configured conditions are met.

If you tap **Boost** while Solar Surplus is waiting, fresh stopped readings
allow PowerSync to hand over control without sending a Stop first. A handoff
that fails before Start shows the reason and clears the temporary Boost
record. A command awaiting vehicle confirmation stays in transport recovery.

While Solar Surplus is waiting for the home battery to reach its minimum, it
can request a stop if the Tesla starts charging on plug-in. This requires
PowerSync control of that vehicle and fresh, matching charging state and power.
The hands-off external charging setting continues to leave those sessions alone.

If Home Assistant has more than one location tracker for the same Tesla,
PowerSync uses the latest report for that car when deciding whether it is home.

The setting survives restarts and stays synchronized with app changes. It is
off by default on installations without a saved Solar Surplus setting and is
unavailable while automation storage is not ready. Configure your EVs and
chargers before enabling it.

In the mobile app, use **Solar Charging** in a vehicle's charging settings to
include that vehicle in automatic Solar Surplus. Turning it off ends the
vehicle's Solar Surplus session on the next control update. Check the vehicle
or charger power reading to confirm charging has stopped.

```yaml
action: switch.turn_on
target:
  entity_id: switch.power_sync_solar_surplus_ev_charging
```

### Tesla Start waiting for fresh charging data

While Solar Surplus remains eligible, PowerSync waits at least five minutes
between Tesla Start recovery attempts. A retry requires the configured sustained
surplus. PowerSync clears the pending state when fresh vehicle data confirms
charging or when the plan ends. A Tesla can come online before its next fresh
charging-state report arrives.

For a Tessie recovery already bound to the car in Home Assistant, PowerSync can
use that car's Wake button. It checks for a new charging-state report from the
same car before attempting Start.

During charging, Solar Surplus tracks the current it sent after Home
Assistant's Tesla current limit. If that limit rises and surplus still
supports a higher current, PowerSync can request the increase.

When Tesla Fleet and Teslemetry report different locations for the same car,
manual charging checks the newest report. If equally recent reports disagree,
an away report blocks the request.

### Battery-powered charging near zero grid flow

Scheduled charging with no grid import holds its rate while the home battery keeps measured grid flow within the configured tolerance. Small fluctuations inside that tolerance do not reduce the rate repeatedly. Inverter capacity limits and reductions for grid imports above tolerance still apply.

### Price Level charging on a shared Sigenergy AC charger

After PowerSync confirms the first EV was unplugged and ends its charging session, it evaluates the next EV connected to that charger against the current Price Level settings. You can leave Price Level enabled between cars.

### Zaptec and Price Level charging

When Price Level is enabled, **PowerSync controls charging** applies your price limit to Zaptec-authenticated starts too. **Allow hands-off external charging** leaves those sessions alone until unplugged. A Zaptec session reporting charging at 0 W is still waiting for the car to draw power. Pending Price Level commands keep battery protection while PowerSync checks the charger. If current control is refused after updating, reconnect Zaptec in PowerSync's Home Assistant integration options to refresh the installation linked to the charger. See the [EV charging guide](https://github.com/bolagnaise/PowerSync/blob/main/docs/ev-charging.md#zaptec-price-level-charging).

### EV SoC sensors with OCPP

A single OCPP charger can reuse **EV battery SoC sensor** and **Fallback EV battery SoC sensor** from EV Charging options with **Enable generic charger** off. Both sensors must describe the same car. Native OCPP SoC takes priority; unavailable or invalid readings fall back to those sensors. The EV display, Smart Schedule and target-SoC checks share the resolved value. Multiple OCPP chargers are not assigned the global sensor pair automatically. See [EV SoC setup and limitations](https://github.com/bolagnaise/PowerSync/blob/main/docs/ev-charging.md#ev-soc-sensors-with-ocpp).
