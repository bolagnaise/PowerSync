# EV charging and shared chargers

- [EV charging actions in Home Assistant](#ev-charging-actions)
- [Tessie Price Level handoff](#tessie-price-level-handoff)
- [Zaptec Price Level charging](#zaptec-price-level-charging)
- [EV SoC sensors with OCPP](#ev-soc-sensors-with-ocpp)
- [Multiple vehicles on one generic charger](#shared-charger-vehicles)

<a id="ev-charging-actions"></a>

## EV charging actions in Home Assistant

Use these PowerSync actions in Home Assistant automations or Developer Tools → Actions:

| Action | Setting controlled |
| --- | --- |
| `power_sync.set_solar_surplus_charging` | Solar Surplus enabled |
| `power_sync.set_scheduled_charging` | Scheduled Charging enabled |
| `power_sync.set_price_level_charging` | Price Level enabled |
| `power_sync.set_smart_schedule_charging` | Smart Schedule enabled for one configured vehicle |

Select your PowerSync integration and set **Enabled** to true or false. Smart Schedule also requires the exact configured vehicle ID. These actions set a desired state rather than toggling it, so retrying an automation does not reverse the setting. They preserve the other charging options shown in the PowerSync app.

New setups use actions rather than adding EV mode switches to the integration screen. Existing Solar Surplus EV Charging switches retain their entity IDs and visibility, so current automations keep working, including device- or area-targeted switch actions. They are not automatically hidden or removed. You can migrate switch-based automations to `power_sync.set_solar_surplus_charging` at your convenience. Only hide an old entity after updating indirect automation targets too. Saved charging settings and the app's charging controls are preserved.

For example, an automation can disable Scheduled Charging when the home battery reaches your chosen minimum:

```yaml
action: power_sync.set_scheduled_charging
data:
  entry_id: YOUR_POWERSYNC_ENTRY_ID
  enabled: false
```

Re-enable it with the same action and `enabled: true`. Check battery charge both when your morning window begins and when it crosses the minimum; a numeric-state crossing alone misses a battery already below the threshold. Include an appropriate check after Home Assistant restarts. Use a margin above the battery's absolute minimum because telemetry and charging evaluation are periodic.

These actions change the mode's saved eligibility, not the physical charger directly. Scheduled Charging, Price Level and Smart Schedule apply changes on the normal EV evaluation cycle. Solar Surplus also uses its existing disable cleanup. Enabling a mode does not force a charging start, and disabling one mode does not disable other eligible charging modes. A successful action confirms the setting was saved, not that the charger physically stopped. Monitoring Mode and existing ownership and provider safeguards still apply.

## Using EV charging with or without Smart Optimization

Scheduled Charging and Price Level can use the current retail rate from EPEX, Octopus or Localvolts with the battery optimizer switched off. A price-limited session waits when its provider has no valid rate covering the current time. Smart Schedule still needs live site power before increasing charging. Disabling Smart Schedule lets it clean up its own session if site telemetry goes offline.

When the battery optimizer includes Smart Schedule, it uses the vehicle's resolved charger limit and the price policy for its next departure. A five-minute cheap interval can supply five minutes of charging. If two cars request **Preserve Home Battery**, ending one mode keeps the other car's protection active until its request also ends.

## Site import limits during EV charging

From v2.12.1766, Dynamic EV reads Smart Optimization’s **Maximum grid import** on each charging control cycle. Changing or clearing that setting takes effect during the active session. When adjusting the rate, PowerSync rounds down to a whole amp if rounding up would exceed the site import limit. Independent site and charger limits still apply. With no configured site limit, battery-target charging uses a 12.5 kW fallback.

EV charging history records the car’s state of charge at the start and end of a managed session when that reading is available.

## EV SoC sensors with OCPP

In the integration's **EV Charging** options, set **EV battery SoC sensor** and, optionally, **Fallback EV battery SoC sensor** to percentage sensors for the same car. These existing fields also work with a single OCPP charger; leave **Enable generic charger** off when OCPP controls it. Previously saved sensor choices are reused.

PowerSync prefers a valid SoC reported by that OCPP connector, then tries your primary sensor and finally the fallback. The resolved value feeds the EV display, Smart Schedule and target-SoC checks. Invalid or unavailable readings remain unknown; Smart Schedule waits for a usable SoC and requests a stop if its charging session loses that reading.

The global sensor pair is not assigned automatically when multiple OCPP chargers or a separate generic charger are configured. Native per-connector SoC still works. For multiple cars sharing a generic charger, use the selected vehicle profiles below. See [EV SoC sources](ev-soc-sources.md) for source precedence and limitations.

## Tessie Price Level handoff

When Price Level takes over from Solar Surplus, PowerSync can keep your Tesla charging while it changes modes. It needs a fresh Tessie report for that car and checks that both the measured and requested current fit the incoming plan. If the plan prohibits grid imports, the vehicle report is unavailable, or transport recovery is pending, the normal start checks apply.

If the Tessie report becomes unavailable or no longer fits the Price Level plan during the handoff, Solar Surplus keeps control and Price Level can try again at a later evaluation.

## Zaptec Price Level charging

With Price Level enabled and **PowerSync controls charging** selected, PowerSync applies your price limit even when charging begins after authentication in Zaptec. **Allow hands-off external charging** leaves that session under external control until a fresh charger reading confirms it was unplugged. Manual and Boost ownership remain separate.

An accepted current setting or resume request leaves the session waiting until a charger poll detects power or phase current. A Zaptec session can report charging while the car is still drawing 0 W. PowerSync keeps the Price Level home-battery protection while its command is unresolved. Invalid readings do not confirm a stop. A completed session releases its battery protection without repeatedly restarting the car at cheap prices.

The same pending-command handling applies when Zaptec uses an associated Tesla vehicle for its charging decision. Ambiguous vehicle associations cannot independently start the same charger. Unchanged current allocations can be reused only after a fresh installation readback confirms them; requests for a different allocation still respect Zaptec's update interval.

PowerSync applies current limits to the installation linked to the selected charger in Zaptec. If current control is refused after updating, reconnect Zaptec in PowerSync's Home Assistant integration options to refresh the charger and installation pairing.

An active battery force-charge or force-discharge timer pauses automatic EV evaluations. Quick EV Charge is a manual session and does not inherit Price Level's **Preserve Home Battery** setting.

<a id="shared-charger-vehicles"></a>

## Multiple vehicles on one generic charger

The EV Charging dashboard can store multiple vehicle profiles for the configured generic charger. Each profile has its own SOC entity, usable battery capacity and Smart Schedule settings, including target SOC and weekly departure times. The charger retains one meter, one controller and one charging-session identity.

In **EV Charging → Shared charger vehicles**, add each car with its SOC sensor and usable battery capacity. Before swapping cars, stop charging, unplug the car and wait for a fresh disconnected charger reading. Choose **Connected vehicle** before plugging the next car in. Expand Smart Schedule to set the selected car's target and departure times. These settings are restored when that car is selected again.

Selection is manual. The charger meter and connection-status entities must describe the physical charger, not one particular vehicle. PowerSync does not infer which car is connected from SOC values. Missing selection or unavailable selected-car SOC blocks Smart Schedule charging. The physical meter still contributes once to the EV/Home load split.

A vehicle cannot be switched or removed while charging, a command is pending, or charger recovery still owns the loadpoint. Stop/recovery records are preserved. Deleting an inactive profile does not remove another car's saved schedule. Existing single-car configurations retain their existing behavior until the first shared-charger profile is created.
