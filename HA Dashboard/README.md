# Home Assistant Dashboard for Tesla Sync

A pre-built Lovelace dashboard for visualizing your Tesla Powerwall and Amber Electric data.

## Preview

The dashboard includes:
- **Price Gauges** - Import price, feed-in price, and battery level
- **Power Flow Card** - Real-time energy flow visualization
- **Price Charts** - Amber prices and TOU schedule sent to Tesla
- **Energy Charts** - Solar, Battery, Grid, and Home load graphs
- **Solar Curtailment Status** - Shows when export is blocked due to negative prices

## Requirements

### Required HACS Integrations

Install these from HACS before setting up the dashboard:

1. **[apexcharts-card](https://github.com/RomRider/apexcharts-card)** - For all the price and energy charts
2. **[power-flow-card-plus](https://github.com/flixlix/power-flow-card-plus)** - For the real-time energy flow visualization

### Optional HACS Integrations

3. **[card-mod](https://github.com/thomasloven/lovelace-card-mod)** - For enhanced styling on the curtailment card (colored borders and backgrounds)

## Installation

### Method 1: Import as New Dashboard

1. In Home Assistant, go to **Settings → Dashboards**
2. Click **+ Add Dashboard**
3. Choose **New dashboard from scratch**
4. Give it a name (e.g., "Tesla Sync")
5. Click **Create**
6. Open the new dashboard and click the three dots menu → **Edit Dashboard**
7. Click the three dots menu again → **Raw configuration editor**
8. Delete any existing content and paste the entire contents of `tesla_sync_dashboard.yaml`
9. Click **Save**

### Method 2: Add as a View to Existing Dashboard

1. Open your existing dashboard
2. Click the three dots menu → **Edit Dashboard**
3. Click the three dots menu → **Raw configuration editor**
4. Add the view from `tesla_sync_dashboard.yaml` to your existing views array
5. Click **Save**

## Customization

### Entity Names

The dashboard uses these default entity names. If your entities have different names, use find/replace:

| Default Entity | Description |
|---------------|-------------|
| `sensor.amber_general_price` | Amber import price |
| `sensor.amber_feed_in_price` | Amber feed-in price |
| `sensor.battery_level` | Powerwall battery percentage |
| `sensor.battery_power` | Powerwall charge/discharge power |
| `sensor.solar_power` | Solar generation power |
| `sensor.grid_power` | Grid import/export power |
| `sensor.home_load` | Home consumption power |
| `sensor.tariff_schedule` | TOU schedule sensor |
| `sensor.solar_curtailment` | Curtailment status sensor |

### Chart Heights

The energy charts (Solar, Battery, Grid, Home) use `height: 150` by default. Adjust this value in each chart's `apex_config.chart.height` to make them larger or smaller.

### Price Range

The price gauges are configured for typical Australian electricity prices:
- Import: 0-60 ¢/kWh
- Feed-in: -10 to 30 ¢/kWh

Adjust the `min` and `max` values if your prices differ.

## Troubleshooting

### Cards showing "Custom element doesn't exist"

This means a required HACS card isn't installed. Install the missing integration from HACS:
- `custom:apexcharts-card` → Install apexcharts-card
- `custom:power-flow-card-plus` → Install power-flow-card-plus

### Curtailment card not showing styled borders

Install the **card-mod** HACS integration for full styling support.

### Charts showing no data

- Ensure the Tesla Sync integration is properly configured
- Check that entity names match your actual entities (see below)
- Wait for the integration to collect some data (may take 5-10 minutes)
- Trigger a sync via the "Sync Now" service or wait for automatic sync

### Finding Your Entity IDs

The dashboard uses generic entity names like `sensor.tariff_schedule`. Your actual entity IDs may differ based on your Home Assistant configuration.

To find your actual entity IDs:
1. Go to **Developer Tools → States**
2. Search for "tesla" or "tariff" to find your entities
3. Look in the HA logs for: `Tariff schedule sensor registered with entity_id: sensor.xxx`
4. Update the dashboard YAML with your actual entity IDs

Common entity ID patterns:
- `sensor.tariff_schedule` (if no device prefix)
- `sensor.tesla_sync_tariff_schedule` (with integration prefix)
- `sensor.<site_name>_tariff_schedule` (with site name prefix)

### TOU Schedule Chart Not Updating

If the TOU Schedule chart shows old data or no data:
1. Check HA logs for "Tariff schedule stored" messages to verify sync happened
2. Verify the entity_id in the dashboard matches your actual sensor
3. Reload the dashboard page (not just refresh)
4. Check that `apexcharts-card` is installed from HACS
