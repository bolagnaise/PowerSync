# Home Assistant Dashboard for PowerSync

A dynamic Lovelace dashboard that automatically shows only the cards relevant to your setup. Uses a **dashboard strategy** — a JS module that checks which PowerSync entities exist and builds the dashboard accordingly.

## Features

The strategy dynamically includes sections based on your configuration:
- **Price Gauges** - Import price, export price, and battery level (if price sensors exist)
- **Battery Control** - Force charge, force discharge, and restore buttons with duration selectors
- **Optimizer Status** - Current action + next action with color-coded state and power display
- **Power Flow Card** - Real-time energy flow visualization
- **Price Charts** - Amber/Octopus prices and TOU schedule sent to battery
- **LP Forecast Charts** - 48-hour solar, load, and price forecasts from the built-in optimizer
- **Solar Curtailment Status** - DC curtailment (Tesla) and AC inverter status cards
- **AC Inverter Controls** - Load following, shutdown, and restore buttons
- **FoxESS Sensors** - PV1/PV2, CT2 power, work mode, min SOC, daily energy
- **Battery Health** - Overall and individual battery health gauges (up to 4 batteries)
- **Energy Charts** - Solar, Battery, Grid, and Home load graphs
- **Demand Charge** - Period status, peak demand, and cost tracking
- **AEMO Spike Monitor** - AEMO price and spike detection status
- **Flow Power** - Import/export prices, TWAP average, and network tariff

Only sections with existing entities appear — no more unavailable cards!

## Requirements

### Required HACS Integrations

Install these from HACS (Frontend) before setting up the dashboard:

1. **[button-card](https://github.com/custom-cards/button-card)** - For control chips and status cards
2. **[card-mod](https://github.com/thomasloven/lovelace-card-mod)** - For compact gauge styling
3. **[power-flow-card-plus](https://github.com/flixlix/power-flow-card-plus)** - For real-time energy flow visualization
4. **[apexcharts-card](https://github.com/RomRider/apexcharts-card)** - For all price and energy charts

## Installation

### Step 1: Install the Integration

The dashboard strategy JS is bundled with the PowerSync integration and auto-registers on startup. No manual Lovelace resource setup needed.

### Step 2: Create the Dashboard

1. In Home Assistant, go to **Settings > Dashboards**
2. Click **+ Add Dashboard**
3. Choose **New dashboard from scratch**
4. Give it a name (e.g., "PowerSync")
5. Click **Create**
6. Open the new dashboard and click the three dots menu > **Edit Dashboard**
7. Click the three dots menu again > **Raw configuration editor**
8. Delete any existing content and paste:

```yaml
strategy:
  type: custom:power-sync-strategy
views: []
```

9. Click **Save**

That's it! The strategy will automatically generate the right cards for your setup.

### YAML Mode Dashboards

If your Lovelace is configured in YAML mode (not storage mode), you need to manually register the strategy resource in your `configuration.yaml`:

```yaml
lovelace:
  resources:
    - url: /power_sync/frontend/power-sync-strategy.js
      type: module
```

## Configuration Options

The strategy accepts optional configuration:

```yaml
strategy:
  type: custom:power-sync-strategy
  entity_prefix: "power_sync"  # Override entity prefix detection
views: []
```

### Entity Prefix

The strategy auto-detects whether your entities use the `power_sync_` prefix (modern installs) or bare names (legacy). Override with `entity_prefix` if needed:
- `"power_sync_"` — entities like `sensor.power_sync_current_import_price`
- `""` — entities like `sensor.current_import_price`

## Troubleshooting

### Cards showing "Custom element doesn't exist"

A required HACS card isn't installed. Install the missing integration from HACS:
- `custom:button-card` > Install button-card
- `custom:apexcharts-card` > Install apexcharts-card
- `custom:power-flow-card-plus` > Install power-flow-card-plus

### Dashboard is blank

1. Check **Developer Tools > Lovelace Resources** for `power-sync-strategy.js`
2. If missing, restart Home Assistant to trigger auto-registration
3. For YAML mode, add the resource manually (see above)

### Strategy not found error

Clear browser cache and hard refresh (Ctrl+Shift+R / Cmd+Shift+R).

### Battery control buttons not working

1. Ensure the PowerSync integration is installed and configured
2. Verify select entities exist: `select.power_sync_force_charge_duration` and `select.power_sync_force_discharge_duration`
3. If missing, restart Home Assistant

### Charts showing no data

- Ensure the PowerSync integration is properly configured
- Wait for the integration to collect some data (may take 5-10 minutes)
- Trigger a sync via the "Sync Now" service or wait for automatic sync

### Seeing wrong sections or missing sections

The strategy checks `hass.states` at render time. If entities are temporarily unavailable during startup, refresh the dashboard once HA is fully loaded.

## Amber Price Models

PowerSync supports three pricing models: **Predicted** (default), **High** (conservative), and **Low** (aggressive).

See the [main README](../README.md#price-models) for full details on each model.

To change the price model:
1. Go to **Settings > Devices & Services > PowerSync**
2. Click **Configure**
3. Select your preferred **Price Model**
4. Click **Submit**
