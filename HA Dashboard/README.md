# Home Assistant Dashboard for PowerSync

A dynamic Lovelace dashboard that automatically shows only the cards relevant to your setup. Uses a **dashboard strategy** — a JS module that checks which PowerSync entities exist and builds the dashboard accordingly.

## Features

The dashboard uses the PowerSync cloud app's look in your Home Assistant light or dark theme. Two full-width cards sit at the top:

- **Right now** - What the battery is doing and why, charge level, export floor, backup time, and live solar, home, battery and grid power
- **Optimizer plan** - The last 12 hours measured and the next 24 hours planned, with prices, charge level, battery actions, car charging, upcoming windows and reserve details

Below them, one to three columns of cards appear only for features you use: battery controls, energy flow, plan explanation, tariff, prices, today's costs and energy, car charging, Powerwall and optimizer settings, forecasts, battery health, Powerwall status, lifetime energy, curtailment, AC inverter, solar strings, demand charge, AEMO spikes, Flow Power, GloBird, CovaU, export limits and extra battery integration details.

Select **Customize layout** to move or hide cards. The layout is saved in your browser, separately for one, two and three columns. **Reset layout** restores the default.

The design it implements is kept in `preview/power-sync-ha-dashboard.html`, a standalone page with sample data.

## Requirements

### Required HACS Integrations

Install these from HACS (Frontend) before setting up the dashboard:

1. **[button-card](https://github.com/custom-cards/button-card)** - Only for the Solar curtailment, AC inverter and Amber battery control cards

Everything else, including charts and the energy flow scene, is built into PowerSync.

## Installation

### Automatic (Recommended)

The dashboard is **auto-created** when PowerSync starts. After installing the integration, a **"PowerSync"** dashboard appears in your sidebar automatically — no manual setup required.

If you previously created a dashboard manually, the auto-created "PowerSync" dashboard will appear alongside it. You can delete your old manual dashboard from **Settings > Dashboards**.

### YAML Mode Dashboards

If your Lovelace is configured in YAML mode (not storage mode), you need to manually register the strategy resource in your `configuration.yaml`:

```yaml
lovelace:
  resources:
    - url: /power_sync/frontend/power-sync-strategy.js
      type: module
```

Then create a dashboard with this config:

```yaml
strategy:
  type: custom:power-sync-strategy
views: []
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

A card that needs `button-card` is showing before it is installed. Install **button-card** from HACS Frontend, then refresh your browser. The dashboard's **Missing frontend cards** card lists anything still missing.

### Dashboard is blank

1. Check **Developer Tools > Lovelace Resources** for `power-sync-strategy.js`
2. If missing, restart Home Assistant to trigger auto-registration
3. For YAML mode, add the resource manually (see above)

### Strategy not found error

Clear browser cache and hard refresh (Ctrl+Shift+R / Cmd+Shift+R).

### Dashboard shows the old layout

The new layout loads `power-sync-dashboard.js` from the same folder as the strategy. If it cannot load, the dashboard falls back to the previous layout and logs a warning in the browser console. Hard refresh the page, and check that the file exists in `custom_components/power_sync/frontend/`.

### Battery control buttons not working

Each control needs a second tap to confirm. If nothing happens after confirming:

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
