# Tesla Sync - Home Assistant Integration

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/custom-components/hacs)

A Home Assistant custom integration that synchronizes Amber Electric pricing with Tesla Powerwall Time-of-Use (TOU) schedules.

## Features

### Core Functionality
- **Automatic TOU Sync**: Syncs Amber pricing to Tesla Powerwall every 5 minutes
- **Real-time Prices**: WebSocket connection for instant price updates
- **Energy Monitoring**: Solar, grid, battery power, and home consumption sensors
- **AEMO Spike Detection**: Monitors wholesale prices for VPP participation
- **Solar Curtailment**: Prevents export during negative pricing periods

### Technical Features
- Australia-wide timezone support
- 5-minute price averaging into 30-minute Tesla periods
- Rolling 24-hour window with 9-24 hours lookahead
- Automatic reconnection for WebSocket

## Prerequisites

1. **Home Assistant** (version 2024.8.0 or newer)
2. **Amber Electric Account** with API token from [Amber Developer Portal](https://app.amber.com.au/developers)
3. **Tesla API Access** (choose one):
   - **Teslemetry** (~$4/month) - Simple API key, recommended
   - **Tesla Fleet** (free) - Requires Tesla Fleet integration configured in HA

## Installation

### HACS (Recommended)

1. Open HACS in Home Assistant
2. Click "Integrations" → three dots → "Custom repositories"
3. Add: `https://github.com/bolagnaise/tesla-sync` (Category: Integration)
4. Find "Tesla Sync" and click "Install"
5. Restart Home Assistant

### Manual

1. Copy `custom_components/tesla_sync/` to your HA config directory
2. Restart Home Assistant

## Configuration

1. Go to **Settings** → **Devices & Services**
2. Click **Add Integration** → search "Tesla Sync"
3. Follow the setup wizard:
   - Enter Amber Electric API token
   - Enter Teslemetry token OR leave blank to use Tesla Fleet
   - Select your Tesla energy site
   - Enable auto-sync (recommended)

### Tesla API Options

| Option | Setup | Cost |
|--------|-------|------|
| **Teslemetry** | Enter API key during setup | ~$4/month |
| **Tesla Fleet** | Install Tesla Fleet integration first, leave Teslemetry blank | Free |

## Entities

### Sensors
- `sensor.current_electricity_price` - Current $/kWh with spike status
- `sensor.solar_power` - Solar generation (W)
- `sensor.grid_power` - Grid import/export (W)
- `sensor.battery_power` - Battery charge/discharge (W)
- `sensor.home_load` - Home consumption (W)
- `sensor.battery_level` - Battery percentage (%)

### Switches
- `switch.auto_sync_tou_schedule` - Enable/disable automatic TOU sync

## Services

```yaml
# Manually sync TOU schedule
service: tesla_sync.sync_tou_schedule

# Refresh data immediately
service: tesla_sync.sync_now
```

## Example Automation

```yaml
automation:
  - alias: "Notify on Price Spike"
    trigger:
      - platform: state
        entity_id: sensor.current_electricity_price
    condition:
      - condition: template
        value_template: "{{ state_attr('sensor.current_electricity_price', 'price_spike') == 'spike' }}"
    action:
      - service: notify.mobile_app
        data:
          message: "Electricity price spike detected!"
```

## Troubleshooting

### Integration Not Showing
- Restart Home Assistant after installation
- Check logs for errors

### No Price Data
- Verify Amber API token is correct
- Check internet connectivity

### TOU Not Syncing
- Verify Tesla API credentials
- Check that auto-sync switch is enabled
- Enable debug logging:

```yaml
logger:
  logs:
    custom_components.tesla_sync: debug
```

## Support

- GitHub Issues: https://github.com/bolagnaise/tesla-sync/issues
- Discord: https://discord.gg/eaWDWxEWE3

## License

MIT License
