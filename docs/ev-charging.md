# EV charging and shared chargers

- [EV charging actions in Home Assistant](#ev-charging-actions)
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

<a id="shared-charger-vehicles"></a>

## Multiple vehicles on one generic charger

The EV Charging dashboard can store multiple vehicle profiles for the configured generic charger. Each profile has its own SOC entity, usable battery capacity and Smart Schedule settings, including target SOC and weekly departure times. The charger retains one meter, one controller and one charging-session identity.

In **EV Charging → Shared charger vehicles**, add each car with its SOC sensor and usable battery capacity. Before swapping cars, stop charging, unplug the car and wait for a fresh disconnected charger reading. Choose **Connected vehicle** before plugging the next car in. Expand Smart Schedule to set the selected car's target and departure times. These settings are restored when that car is selected again.

Selection is manual. The charger meter and connection-status entities must describe the physical charger, not one particular vehicle. PowerSync does not infer which car is connected from SOC values. Missing selection or unavailable selected-car SOC blocks Smart Schedule charging. The physical meter still contributes once to the EV/Home load split.

A vehicle cannot be switched or removed while charging, a command is pending, or charger recovery still owns the loadpoint. Stop/recovery records are preserved. Deleting an inactive profile does not remove another car's saved schedule. Existing single-car configurations retain their existing behavior until the first shared-charger profile is created.
