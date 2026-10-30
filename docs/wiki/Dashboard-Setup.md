# Dashboard Setup

PowerSync includes an **auto-generated Lovelace dashboard** that adapts to your configuration. The dashboard checks which entities exist and only builds cards for features you have set up.

## HACS Frontend Cards

PowerSync bundles the dashboard strategy, price charts, and animated energy-flow card. Install these via HACS > Frontend only where noted:

- `button-card` — Needed only by the Solar curtailment, AC inverter and Amber battery control cards. Every other card is built into PowerSync.
- `power-flow-card-plus` — Optional fallback/additional power-flow card for custom dashboards

### Optional: Tesla-Style Energy Flow (v2.6.5+)

This used to require the external `tesla-style-solar-power-card` HACS card. Current PowerSync releases no longer use that external card for the auto-generated dashboard.

PowerSync now registers its own built-in `custom:power-sync-energy-flow` card and uses it automatically for the dashboard power-flow scene, with full entity mapping including:
- Core sensors (solar, grid, battery, home load)
- EV charging power (when EV is configured)
- Individual PV arrays (when available)
- Weather entity for dynamic backgrounds
- Sun entity for day/night theming

You do not need to install `tesla-style-solar-power-card` for the PowerSync dashboard. `power-flow-card-plus` remains optional for custom dashboards and as a legacy fallback path.

## Installation

The dashboard is auto-created when PowerSync loads. It appears in your sidebar as **PowerSync**.

**No manual YAML configuration is required.** All cards are built dynamically based on your configured entities.

If you don't see it:
1. Restart Home Assistant
2. Refresh your browser
3. Check Settings > Dashboards — "PowerSync" should appear

## Built-In PowerSync Panel

The integration also registers a built-in PowerSync panel served from the integration frontend. It provides PowerSync-native views for live status, controls, optimizer settings, tariff/price charts, EV charging, and the animated energy-flow scene. The panel uses the same entities and HTTP endpoints as the mobile app, so sections appear only when the matching feature is configured.

For the energy-flow scene, optional weather and PV string entities are auto-detected where possible. Tesla local pairing adds off-grid controls and Powerwall block health details when available.

## Dashboard Layout

The dashboard uses the same look as the PowerSync cloud app: dark navy cards, DM Sans text and mono numbers, and the same colours for solar (yellow), battery (green), grid import (cyan), export (red) and home (violet). It follows your Home Assistant light or dark theme.

Two full-width cards sit at the top:

- **Right now** says in a sentence what the battery is doing and why, with charge level, export floor, backup time, and live solar, home, battery and grid power.
- **Optimizer plan** shows the last 12 hours as measured and the next 24 hours as planned: import and export prices, the battery's charge level, what the battery will do each interval, and planned car charging. Hover over the chart, or focus it and use the arrow keys, to read any interval. Below the chart are the upcoming charge and export windows and the reserve details. The refresh button fetches the latest plan.

The other cards sit in one to three columns, depending on screen width.

### Cards

Cards appear only when the matching feature or entities exist.

| Card | When it appears |
|------|-----------------|
| Right now | Core power sensors |
| Optimizer plan | Smart Optimization enabled |
| Battery controls | Battery system configured |
| Energy flow | Core power sensors |
| Plan explanation | Smart Optimization available |
| Tariff today | TOU tariff synced |
| Prices today | Price sensors exist |
| Today so far | Daily energy and cost sensors |
| Car charging | EV smart charging configured |
| Powerwall settings | Tesla Powerwall |
| Powerwalls | Powerwall local pairing configured |
| Optimizer settings | Smart Optimization available |
| Energy since midnight | Core power sensors |
| Load forecast | Load forecast or Smart Optimization available |
| Planned battery power | Smart Optimization active |
| Solar and load forecast | Forecast sensors exist |
| Battery health | Health sensor available |
| Powerwall status | Tesla Powerwall |
| Lifetime energy | Lifetime energy sensors |
| Powerwall local control | Powerwall local pairing configured |
| Solar curtailment | Curtailment configured |
| AC inverter | AC inverter configured |
| Solar strings | PV string sensors exist |
| Demand charge | Demand tracking enabled |
| AEMO spike monitor | Spike detection configured |
| Flow Power pricing | Flow Power configured |
| GloBird | GloBird account connected |
| CovaU | CovaU account connected |
| Grid export limit | Flexible export limits configured |
| System details | Your battery integration reports extra sensors (one card per group) |
| Missing frontend cards | A frontend card that some cards need is not installed |

### Customizing the Layout

Select **Customize layout** at the top of the dashboard to rearrange it.

- **Move a card**: drag it by its handle, or use the arrow buttons to move it up, down or to the next column. The two full-width cards can be reordered or hidden but stay full width.
- **Hide a card**: select **Hide**. At least one card always stays visible.
- **Bring a card back**: select **Show hidden** to show hidden cards dimmed, then **Unhide** the ones you want.
- **Start again**: select **Reset layout** to restore the default order and show every card.

Select **Done** when you're finished. Your layout is saved in your browser, separately for one, two and three columns, so a phone and a desktop can each have their own order.

## Battery Controls

Choose how long to run for, then select **Charge**, **Discharge**, **Power the home** or **Hold charge**. Each button asks you to tap it a second time to confirm, so a stray tap never changes the battery. **Restore normal** appears while a manual mode is running. **Power limit** sets the charge or discharge rate (Max uses the fastest rate allowed), and **Custom discharge time** overrides the duration for discharges when it is set.

The duration selectors these controls use are created automatically by the integration. No manual helper setup is required.

## Battery Health

The Battery Health sensor (`sensor.power_sync_battery_health`) shows your battery's state of health as a percentage. How it gets populated depends on your battery system:

- **Sungrow, Sigenergy, GoodWe, AlphaESS, ESY Sunhome, FoxESS H3 Smart, Neovolt/Bytewatt when exposed by the upstream integration** — State of health is read automatically. No setup required.
- **Tesla** — Complete Powerwall Local Control pairing for the TEDAPI/BMS health path, or use the capacity sensor blueprint below as a fallback.
- **FoxESS (H1/H3/H3-Pro/KH), Solax Hybrid, SAJ H2/HS2** — Not available; the sensor shows "Unknown".

See [Battery System Setup](Battery-System-Setup#powerwall-local-control-pairing) for Powerwall pairing and [Sensors](Sensors#battery-health) for full sensor details.

## Blueprints

### Battery Health from Capacity Sensor (Tesla)

If your Tesla Powerwall exposes a capacity sensor, you can use this blueprint to sync battery health data into PowerSync. It reads the current capacity, calculates degradation against the manufacturer spec, and calls `power_sync.sync_battery_health` every 6 hours.

> **Note:** Sungrow, Sigenergy, GoodWe, AlphaESS, ESY Sunhome, and FoxESS H3 Smart users don't need this blueprint — state of health is read directly from the inverter.

**Import:**

[![Import Blueprint](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https://github.com/bolagnaise/PowerSync/blob/main/blueprints/battery_health_from_powerwall_capacity_sensor.yaml)

Or manually: **Settings > Automations & Scenes > Blueprints > Import Blueprint** and paste:

```
https://github.com/bolagnaise/PowerSync/blob/main/blueprints/battery_health_from_powerwall_capacity_sensor.yaml
```

**Configuration:**

| Setting | Description | Example |
|---------|-------------|---------|
| **Capacity sensor** | Sensor reporting current battery capacity | `sensor.powerwall_capacity` |
| **Capacity sensor unit** | Whether the sensor reports in kWh or Wh | `kwh` |
| **Original capacity (Wh)** | Manufacturer spec total capacity | `13500` (1x Powerwall), `27000` (2x) |
| **Battery count** | Number of batteries (reporting only) | `1` |
