<div align="center">
  <img src="https://raw.githubusercontent.com/bolagnaise/PowerSync/main/logo.png" alt="PowerSync Logo" width="200"/>

  # PowerSync

  A Home Assistant integration for intelligent battery energy management in Australia. Supports **Tesla Powerwall** and **Sigenergy** battery systems. Automatically sync with Amber Electric or Flow Power (AEMO wholesale) dynamic pricing, and capitalize on AEMO wholesale price spikes to maximize your battery's earning potential.

  <a href="https://paypal.me/benboller" target="_blank"><img src="https://www.buymeacoffee.com/assets/img/custom_images/orange_img.png" alt="Buy Me A Coffee" style="height: 41px !important;width: 174px !important;box-shadow: 0px 3px 2px 0px rgba(190, 190, 190, 0.5) !important;-webkit-box-shadow: 0px 3px 2px 0px rgba(190, 190, 190, 0.5) !important;" ></a>

  [![Discord](https://img.shields.io/badge/Discord-Join%20Community-5865F2?logo=discord&logoColor=white)](https://discord.gg/eaWDWxEWE3)
  [![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/custom-components/hacs)

  ### Mobile App (Beta)

  <a href="https://testflight.apple.com/join/FhnUtSFy"><img src="https://img.shields.io/badge/iOS-TestFlight-blue?logo=apple&logoColor=white" alt="iOS TestFlight"></a>
  <a href="https://play.google.com/apps/internaltest/4701190520579978532-"><img src="https://img.shields.io/badge/Android-Beta-3DDC84?logo=android&logoColor=white" alt="Android Beta"></a>

  Monitor your battery, view live pricing, control EV charging, and create powerful automations from your phone.

</div>

## Disclaimer

This is an unofficial integration and is not affiliated with or endorsed by Tesla, Inc., Sigenergy, or Amber Electric. Use at your own risk. The developers are not responsible for any damages or issues that may arise from the use of this software.

## Features

### Supported Battery Systems

| Feature | Tesla Powerwall | Sigenergy |
|---------|:---------------:|:---------:|
| Automatic TOU Tariff Sync | ✅ | ✅ |
| Spike Protection | ✅ | ✅ |
| Export Price Boost | ✅ | ✅ |
| Chip Mode | ✅ | ✅ |
| DC Solar Curtailment | ✅ Export rules | ✅ Modbus TCP |
| AC-Coupled Inverter Curtailment | ✅ | ✅ |
| AEMO Spike Detection | ✅ | ➖ Native via Globird |
| Force Mode Toggle | ✅ | ➖ N/A |
| **Automations** | ✅ | ✅ |
| **EV Charging Controls** | ✅ Tesla Fleet | ➖ N/A |

**Connection Methods:**
- **Tesla Powerwall** - Fleet API or Teslemetry proxy
- **Sigenergy** - Sigenergy Cloud API for tariff sync + Modbus TCP for real-time energy data

### Core Functionality
- **Automatic TOU Tariff Sync** - Updates your battery system with Amber Electric pricing every 5 minutes
- **Real-time Pricing** - Monitor current and historical electricity prices with live updates via WebSocket
- **Near Real-Time Energy Monitoring** - Energy usage updates every 30 seconds
- **Timezone Support** - Auto-detects timezone from Amber data for accurate time display across all Australian states

### Advanced Features
- **AEMO Spike Detection** - Monitors wholesale prices and switches to spike tariff during extreme price events
- **Solar Curtailment** - Prevents solar export during negative pricing periods
- **Spike Protection** - Prevents battery from charging from grid during Amber price spikes
- **Export Price Boost** - Artificially increase export prices to trigger battery exports at lower price points
- **Chip Mode** - Suppress battery exports during configured hours unless price exceeds a threshold
- **Flow Power + AEMO Support** - Full support for wholesale retailers using direct AEMO NEM pricing
- **Demand Charge Tracking** - Monitor peak demand for capacity-based electricity plans

---

## Installation

### Prerequisites

- Home Assistant installed and running
- HACS (Home Assistant Community Store) installed
- **For Amber users:** Amber Electric API token ([get one here](https://app.amber.com.au/developers))
- **For Flow Power users:** Uses AEMO wholesale pricing (or Amber API if you have one)
- **For Globird/AEMO VPP users:** No API token required (uses AEMO spike detection)
- Tesla or Sigenergy battery system with API access (see [Tesla API Options](#tesla-api-options) below)

### Installation Steps

1. **Install via HACS**
   - Open HACS in Home Assistant
   - Click the three dots in the top right
   - Select "Custom repositories"
   - Add repository URL: `https://github.com/bolagnaise/PowerSync`
   - Category: `Integration`
   - Click "Add"
   - Click "Download" on the PowerSync integration
   - Restart Home Assistant

2. **Add Integration**
   - Go to Settings → Devices & Services
   - Click "+ Add Integration"
   - Search for "PowerSync"
   - Click to add

3. **Configure**
   - Select your **electricity provider** (Amber, Flow Power, Globird, AEMO VPP)
   - Enter Amber API token if using Amber (or optionally for Flow Power)
   - Select your **battery system** (Tesla Powerwall or Sigenergy)
   - Enter battery API credentials (Teslemetry key, Tesla Fleet, or Sigenergy Cloud)
   - Configure additional options as needed

4. **Verify Setup**
   - Check that new sensors appear:
     - `sensor.current_electricity_price`
     - `sensor.solar_power`
     - `sensor.grid_power`
     - `sensor.battery_power`
     - `sensor.home_load`
     - `sensor.battery_level`
   - Check that the switch appears:
     - `switch.auto_sync_tou_schedule`

---

## Tesla API Options

PowerSync supports two methods for accessing your Tesla Powerwall. **Choose one** - you don't need both.

### Option 1: Teslemetry (Recommended - ~$4/month)

The easiest setup option. Teslemetry is a third-party proxy service for Tesla API.

| Pros | |
|------|---|
| ✅ Simple API key authentication | No OAuth complexity |
| ✅ Works with localhost | No public domain needed |
| ✅ 2-minute setup | Just copy/paste API key |
| ✅ Reliable service | Well-maintained proxy |

**Setup:**
1. Sign up at https://teslemetry.com
2. Connect your Tesla account
3. Copy your API key
4. Paste into PowerSync settings

### Option 2: Tesla Fleet API (Free)

Direct OAuth access to Tesla's Fleet API. Completely free but requires more setup.

| Pros | Cons |
|------|------|
| ✅ Completely free | ⚠️ Requires Tesla Fleet integration in HA |
| ✅ Direct API access | ⚠️ More setup steps |
| ✅ Automatic token refresh | |

**Setup:**
1. Install the official **Tesla Fleet** integration in Home Assistant
   - Settings → Devices & Services → Add Integration → "Tesla Fleet"
   - Follow the OAuth login flow
2. PowerSync automatically detects your Tesla Fleet credentials
3. Leave the Teslemetry field empty during PowerSync setup

---

## Key Features Explained

### AEMO Spike Detection (Tesla only)

This option is primarily intended for Tesla Powerwall users with VPPs that offer AEMO Spike exports (Globird, AGL, Engie) but don't natively support Tesla batteries. Sigenergy users on Globird don't need this feature as Globird natively supports Sigenergy for spike exports.

When prices exceed your configured threshold (e.g., $300/MWh), the system:
- Saves your current tariff configuration
- Saves your current Powerwall operation mode
- Switches to autonomous (TOU) mode
- Uploads a spike tariff with very high sell rates to encourage battery export
- Restores your original operation mode when spike ends
- Restores your normal tariff when prices return to normal

**Monitoring Frequency:** Checks AEMO prices every 1 minute.

### Solar Curtailment

Prevents paying to export solar during negative pricing periods (≤0c/kWh).

| Battery System | Method | Behavior |
|----------------|--------|----------|
| **Tesla** | Export rules API | Sets export to "never", restores to "battery_ok" when positive |
| **Sigenergy** | Modbus TCP | Sets export limit to 0kW (load-following mode) |

**Sigenergy Load-Following Mode:**
- ✅ Solar continues powering the house
- ✅ Battery still charges from solar
- ✅ Only grid export is blocked during negative prices

### Spike Protection (Amber Only)

Prevents your battery from charging from the grid during Amber price spikes. When wholesale prices spike, your battery may see an arbitrage opportunity and charge from grid - this feature stops that behavior.

**How It Works:**
When Amber reports `spikeStatus: 'potential'` or `'spike'` for a period, buy prices are overridden to ensure charging from grid is always unprofitable during spikes.

### Export Price Boost

Artificially increases export prices sent to your battery system to trigger exports at lower price points. Useful when Amber export prices are in the 20-25c range where the battery's algorithm may not trigger exports.

**Configuration Options:**
| Setting | Description | Default |
|---------|-------------|---------|
| Enable Export Price Boost | Toggle the feature on/off | Off |
| Price Offset (c/kWh) | Fixed amount added to all export prices | 0 |
| Minimum Price (c/kWh) | Floor for export prices | 0 |
| Activation Threshold (c/kWh) | Boost only applies if actual price is at or above this value | 0 |
| Boost Start Time | When to start applying boost | 17:00 |
| Boost End Time | When to stop applying boost | 21:00 |

### Chip Mode

Suppress battery exports during configured hours (typically overnight) unless the price exceeds a threshold.

**Configuration Options:**
| Setting | Description | Default |
|---------|-------------|---------|
| Enable Chip Mode | Toggle the feature on/off | Off |
| Start Time | When to start suppressing exports | 22:00 |
| End Time | When to stop suppressing exports | 06:00 |
| Price Threshold (c/kWh) | Allow exports only above this price | 30 |

---

## Sigenergy Battery System Support

Full support for Sigenergy DC-coupled battery systems as an alternative to Tesla Powerwall.

**Features:**
- **Tariff Sync via Cloud API** - Uploads Amber pricing to Sigenergy Cloud using the same 30-minute TOU format
- **Real-Time Energy Data via Modbus** - Reads solar, battery, grid power and SOC from your inverter
- **DC Solar Curtailment** - Controls DC solar via Modbus TCP during negative prices (load-following mode)

**Connection Requirements:**
| Connection | Purpose | Required |
|------------|---------|----------|
| **Cloud API** | Tariff sync to Sigenergy | ✅ Yes |
| **Modbus TCP** | Real-time energy data + DC curtailment | ✅ Yes |

### Getting Sigenergy Cloud API Credentials

**What You Need:**
| Credential | Description | Where to Find |
|------------|-------------|---------------|
| **Email** | Your Sigenergy account email | Your login email |
| **Password** | Your Sigenergy account password | Just use your normal password! |
| **Device ID** | 13-digit numeric identifier | Browser dev tools (see below) |
| **Station ID** | Your Sigenergy station identifier | SigenAI or browser dev tools |

**Getting Device ID:**

1. **Open the Sigenergy Web Portal**
   - Go to https://app-aus.sigencloud.com/ in your browser
   - Don't log in yet!

2. **Open Browser Developer Tools**
   - Press `F12` or right-click → "Inspect"
   - Go to the **Network** tab
   - Check "Preserve log" checkbox

3. **Log In Normally**
   - Enter your email and password
   - Click Login

4. **Find the Auth Request**
   - In the Network tab, look for a request to `oauth/token`
   - Click on it to see the details
   - Go to the **Payload** tab
   - **userDeviceId**: This 13-digit number is your `Device ID`

**Getting Station ID:**
- **Easiest**: Ask SigenAI in the app: "Tell me my StationID"
- **Alternative**: In dev tools, look for requests containing `stationId` in the response

### Sigenergy Configuration

1. Install PowerSync via HACS
2. Add the integration: Settings → Devices & Services → Add Integration → PowerSync
3. Select **Sigenergy** as your battery system
4. Enter your Sigenergy Cloud credentials
5. Select your Sigenergy station from the list
6. Enter your Sigenergy inverter's **Modbus IP address**
7. Optionally enable DC solar curtailment

---

## AC-Coupled Inverter Curtailment

Control AC-coupled solar inverters directly during negative pricing periods. This feature works with **any battery system** (Tesla, Sigenergy, or others).

**Supported Inverter Brands:**
| Brand | Connection | Models |
|-------|------------|--------|
| **Sungrow** | Modbus TCP | SG series (string), SH series (hybrid) |
| **Fronius** | Modbus TCP | Primo, Symo, Gen24, Tauro, Eco |
| **GoodWe** | Modbus TCP | ET, EH, BT, BH, ES, EM series (hybrid) |
| **Huawei** | Modbus TCP | SUN2000 L1, M0, M1, M2 series |
| **Enphase** | HTTPS API | IQ Gateway, Envoy-S (microinverters) |

---

## Available Services

```yaml
# Manually sync TOU schedule
service: power_sync.sync_tou_schedule

# Refresh data from Amber and Tesla
service: power_sync.sync_now

# Force charge for specified duration
service: power_sync.force_charge
data:
  duration_minutes: 60

# Force discharge for specified duration
service: power_sync.force_discharge
data:
  duration_minutes: 60

# Restore normal operation
service: power_sync.restore_normal
```

---

## Automatic TOU Syncing

**The integration automatically syncs your TOU schedule every 5 minutes** when the auto-sync switch is enabled.

**How it works:**
1. Enable the `switch.auto_sync_tou_schedule` switch (enabled by default)
2. The integration runs a background timer that checks every 5 minutes
3. If auto-sync is enabled, it automatically:
   - Fetches the latest Amber pricing forecast
   - Converts it to Tesla TOU format
   - Sends it to your Powerwall via the configured API
4. If auto-sync is disabled, the timer skips syncing

**No automation required!** Just leave the switch on and the integration handles everything automatically.

---

## Example Automations (Optional)

**Force immediate sync on price spike:**
```yaml
automation:
  - alias: "Force TOU Sync on Price Spike"
    trigger:
      - platform: state
        entity_id: sensor.current_electricity_price
    condition:
      - condition: numeric_state
        entity_id: sensor.current_electricity_price
        above: 0.30
    action:
      - service: power_sync.sync_tou_schedule
```

---

## Pre-built Dashboard (Optional)

A pre-built Lovelace dashboard is included for visualizing all PowerSync data.

**Required HACS Frontend Cards:**
- `mushroom` - Compact chips for controls
- `card-mod` - Custom card styling
- `power-flow-card-plus` - Real-time energy flow visualization
- `apexcharts-card` - Advanced charting for price/energy history

**Installation:**
1. Install the required HACS cards (HACS → Frontend → search for each card)
2. Copy the dashboard YAML from `HA Dashboard/power_sync_dashboard.yaml`
3. In Home Assistant: Settings → Dashboards → Add Dashboard
4. Edit the new dashboard → 3 dots menu → "Raw configuration editor"
5. Paste the YAML content and save

**Required Helper Entities:**

The Force Charge and Force Discharge controls require `input_select` helpers:

1. Go to **Settings → Devices & Services → Helpers**
2. Click **+ Create Helper → Dropdown**
3. Create `force_charge_duration` with options: `15`, `30`, `45`, `60`, `90`, `120`
4. Create `force_discharge_duration` with options: `15`, `30`, `45`, `60`, `90`, `120`

---

## Troubleshooting

- **No sensors appearing**: Check that the integration is enabled in Settings → Devices & Services
- **Invalid API token**: Verify tokens at Amber and Teslemetry/Tesla Fleet
- **No Tesla sites found**:
  - If using Tesla Fleet: Ensure the Tesla Fleet integration is loaded and working
  - If using Teslemetry: Ensure your Tesla account is linked in Teslemetry
- **TOU sync failing**: Check Home Assistant logs for detailed error messages

**Enable Debug Logging:**
```yaml
logger:
  logs:
    custom_components.power_sync: debug
```

---

## Support

- GitHub Issues: https://github.com/bolagnaise/PowerSync/issues
- Discord: https://discord.gg/eaWDWxEWE3

## License

MIT License
