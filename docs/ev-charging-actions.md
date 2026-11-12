# EV charging actions in Home Assistant

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
