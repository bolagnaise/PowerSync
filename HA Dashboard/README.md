# Home Assistant Dashboard for PowerSync

A pre-built Lovelace dashboard for visualizing your battery system and energy data.

## Preview

The dashboard includes:
- **Price Gauges** - Compact gauges for import price, export price, and battery level
- **Battery Control** - Force charge, force discharge, and restore normal buttons with duration selectors
- **Power Flow Card** - Real-time energy flow visualization
- **Price Charts** - Amber/Flow Power prices and TOU schedule sent to battery
- **Solar Curtailment Status** - DC curtailment (Tesla) and AC inverter status cards
- **AC Inverter Controls** - Load following, shutdown, and restore buttons for AC-coupled inverters
- **Battery Health** - Gauges showing overall and individual battery health (up to 4 batteries)
- **Energy Charts** - Solar, Battery, Grid, and Home load graphs

## Requirements

### Required HACS Integrations

Install these from HACS (Frontend) before setting up the dashboard:

1. **[button-card](https://github.com/custom-cards/button-card)** - For the force discharge control chips
2. **[card-mod](https://github.com/thomasloven/lovelace-card-mod)** - For compact gauge styling
3. **[power-flow-card-plus](https://github.com/flixlix/power-flow-card-plus)** - For the real-time energy flow visualization
4. **[apexcharts-card](https://github.com/RomRider/apexcharts-card)** - For all the price and energy charts

### Duration Select Entities (Auto-Created)

The PowerSync integration automatically creates two select entities for duration selection:
- `select.power_sync_force_charge_duration`
- `select.power_sync_force_discharge_duration`

**No manual setup required!** These entities are created when you install the integration and persist across restarts.

> **Upgrading from older versions?** If you previously created manual `input_select` helpers, you can delete them. The dashboard now uses the auto-created `select.power_sync_*` entities instead.

## Installation

### Method 1: Import as New Dashboard

1. In Home Assistant, go to **Settings → Dashboards**
2. Click **+ Add Dashboard**
3. Choose **New dashboard from scratch**
4. Give it a name (e.g., "PowerSync")
5. Click **Create**
6. Open the new dashboard and click the three dots menu → **Edit Dashboard**
7. Click the three dots menu again → **Raw configuration editor**
8. Delete any existing content and paste the entire contents of `power_sync_dashboard.yaml`
9. Click **Save**

### Method 2: Add as a View to Existing Dashboard

1. Open your existing dashboard
2. Click the three dots menu → **Edit Dashboard**
3. Click the three dots menu → **Raw configuration editor**
4. Add the view from `power_sync_dashboard.yaml` to your existing views array
5. Click **Save**

## Customization

### Entity Names

The dashboard uses these default entity names. If your entities have different names, use find/replace:

| Default Entity | Description |
|---------------|-------------|
| `sensor.current_import_price` | Amber import price ($/kWh) |
| `sensor.current_export_price` | Amber feed-in/export price ($/kWh) |
| `sensor.battery_level` | Powerwall battery percentage |
| `sensor.battery_power` | Powerwall charge/discharge power (kW) |
| `sensor.solar_power` | Solar generation power (kW) |
| `sensor.grid_power` | Grid import/export power (kW) |
| `sensor.home_load` | Home consumption power (kW) |
| `sensor.tariff_schedule` | TOU schedule sent to Tesla |
| `sensor.dc_solar_curtailment` | DC solar curtailment status (Tesla) |
| `sensor.inverter_status` | AC-coupled inverter status |
| `sensor.battery_health` | Battery health from mobile app TEDAPI scan |

### Chart Heights

The energy charts (Solar, Battery, Grid, Home) use `height: 150` by default. Adjust this value in each chart's `apex_config.chart.height` to make them larger or smaller.

### Price Range

The price gauges are configured for typical Australian electricity prices:
- Import: 0-60 ¢/kWh
- Feed-in: -10 to 30 ¢/kWh

Adjust the `min` and `max` values if your prices differ.

## Amber Price Models

PowerSync supports three pricing models: **Predicted** (default), **High** (conservative), and **Low** (aggressive).

See the [main README](../README.md#price-models) for full details on each model.

To change the price model:
1. Go to **Settings → Devices & Services → PowerSync**
2. Click **Configure**
3. Select your preferred **Price Model**
4. Click **Submit**

## Troubleshooting

### Cards showing "Custom element doesn't exist"

This means a required HACS card isn't installed. Install the missing integration from HACS:
- `custom:button-card` → Install button-card
- `custom:apexcharts-card` → Install apexcharts-card
- `custom:power-flow-card-plus` → Install power-flow-card-plus

### Gauges or curtailment card not styled correctly

Install the **card-mod** HACS integration for full styling support.

### Battery control buttons not working

1. Ensure the PowerSync integration is installed and configured
2. Verify the auto-created select entities exist:
   - `select.power_sync_force_charge_duration`
   - `select.power_sync_force_discharge_duration`
3. If entities are missing, restart Home Assistant
4. Check Developer Tools → States to confirm entities are available

### Charts showing no data

- Ensure the PowerSync integration is properly configured
- Check that entity names match your actual entities (see below)
- Wait for the integration to collect some data (may take 5-10 minutes)
- Trigger a sync via the "Sync Now" service or wait for automatic sync

### Finding Your Entity IDs

The dashboard uses generic entity names like `sensor.tariff_schedule`. Your actual entity IDs may differ based on your Home Assistant configuration.

To find your actual entity IDs:
1. Go to **Developer Tools → States**
2. Search for "power_sync" or "tariff" to find your entities
3. Update the dashboard YAML with your actual entity IDs

Common entity ID patterns:
- `sensor.tariff_schedule` (default)
- `sensor.power_sync_tariff_schedule` (with integration prefix)

### TOU Schedule Chart Not Updating

If the TOU Schedule chart shows old data or no data:
1. Check HA logs for "Tariff schedule stored" messages to verify sync happened
2. Verify the entity_id in the dashboard matches your actual sensor
3. Reload the dashboard page (not just refresh)
4. Check that `apexcharts-card` is installed from HACS
