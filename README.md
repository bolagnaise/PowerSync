<div align="center">
  <img src="https://raw.githubusercontent.com/bolagnaise/PowerSync/main/app/static/logo.png" alt="PowerSync Logo" width="200"/>

  # PowerSync

  Intelligent battery energy management for Australia. Supports **Tesla Powerwall** and **Sigenergy** battery systems. Automatically sync with Amber Electric or Flow Power (AEMO wholesale) dynamic pricing, create custom TOU schedules for any provider, and capitalize on AEMO wholesale price spikes to maximize your battery's earning potential.

  <a href="https://paypal.me/benboller" target="_blank"><img src="https://www.buymeacoffee.com/assets/img/custom_images/orange_img.png" alt="Buy Me A Coffee" style="height: 41px !important;width: 174px !important;box-shadow: 0px 3px 2px 0px rgba(190, 190, 190, 0.5) !important;-webkit-box-shadow: 0px 3px 2px 0px rgba(190, 190, 190, 0.5) !important;" ></a>

  [![Discord](https://img.shields.io/badge/Discord-Join%20Community-5865F2?logo=discord&logoColor=white)](https://discord.gg/eaWDWxEWE3)

  [![Docker Hub](https://img.shields.io/docker/v/bolagnaise/power-sync?label=docker%20hub&logo=docker)](https://hub.docker.com/r/bolagnaise/power-sync)
  [![Docker Pulls](https://img.shields.io/docker/pulls/bolagnaise/power-sync)](https://hub.docker.com/r/bolagnaise/power-sync)
  [![Build Status](https://github.com/bolagnaise/PowerSync/actions/workflows/docker-publish.yml/badge.svg)](https://github.com/bolagnaise/PowerSync/actions)

  ### 📱 Mobile App (Beta)

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
| Custom TOU Schedules | ✅ | ➖ N/A |
| **Automations** | ✅ | ✅ |
| **EV Charging Controls** | ✅ Tesla Fleet | ➖ N/A |

**Connection Methods:**
- 🔋 **Tesla Powerwall** - Fleet API or Teslemetry proxy
- ⚡ **Sigenergy** - Sigenergy Cloud API for tariff sync + Modbus TCP for real-time energy data

### Core Functionality
- 🔋 **Automatic TOU Tariff Sync** - Updates your battery system with Amber Electric pricing every 5 minutes
- 📊 **Real-time Pricing Dashboard** - Monitor current and historical electricity prices with live updates
- ⚡ **Near Real-Time Energy Monitoring** - Energy usage charts update every 30 seconds
- 🌏 **Timezone Support** - Auto-detects timezone from Amber data for accurate time display across all Australian states

### Advanced Features
- ⚡ **AEMO Spike Detection** - Automatically monitors Australian wholesale electricity prices and switches to spike tariff during extreme price events (configurable threshold). Includes intelligent operation mode switching - automatically saves your current Powerwall mode and switches to autonomous (TOU) mode during spikes, then restores your original mode when prices normalize
- 🌞 **Solar Curtailment** - Automatically prevents solar export during negative pricing periods (≤0c/kWh). When Amber feed-in prices go negative, the system blocks grid export to avoid paying to export, then restores normal operation when prices return to positive
- 🛡️ **Spike Protection** - Prevents your battery from charging from grid during Amber price spikes. Overrides buy prices when Amber detects spike status to eliminate arbitrage opportunities
- 📤 **Export Price Boost** - Artificially increase export prices to trigger battery exports at lower price points. Useful when prices are in the 20-25c range where the battery's algorithm may not trigger exports
- 🔒 **Chip Mode** - Suppress battery exports during configured hours unless price exceeds a threshold. Keeps your battery stable overnight while still capturing unexpected price spikes (inverse of Export Boost)
- 🔌 **Flow Power + AEMO Support** - Full support for Flow Power and other wholesale electricity retailers using direct AEMO NEM pricing with configurable network tariffs
- 🎯 **Custom TOU Schedules** - Create and manage custom time-of-use schedules for any electricity provider (not just Amber)
- 💾 **Saved TOU Profiles** - Backup, restore, and manage multiple tariff configurations
- 📈 **Demand Charge Tracking** - Monitor and track peak demand for electricity plans with capacity-based fees

### Technical Features
- 🔐 **Teslemetry Integration** - Secure Tesla API access via Teslemetry proxy service (no public domain required)
- 🔒 **Secure Credential Storage** - All API tokens encrypted at rest using Fernet encryption
- ⏱️ **Background Scheduler** - Automatic syncing every 5 minutes (aligned with Amber's forecast updates)
- 🔄 **Smart Tariff Deduplication** - Only syncs to Tesla when tariff actually changes, preventing duplicate rate plan entries in Tesla dashboard
- 🐳 **Docker Ready** - Pre-built multi-architecture (amd64/arm64) images for easy deployment
- 🏠 **Home Assistant Integration** - Native HACS integration for seamless HA deployment
- 🌏 **Australia-Wide Compatibility** - Auto-detects timezone from Amber data, works in all Australian states and territories (QLD, NSW, ACT, VIC, SA, TAS, WA, NT)
- 📊 **Intelligent Price Averaging** - Averages 5-minute Amber intervals into 30-minute Tesla periods for maximum accuracy
- 🎯 **Period Alignment** - Correctly aligns with Amber's forecast labels (e.g., "18:00 forecast" → Tesla PERIOD_17_30)
- 🔄 **Rolling 24-Hour Window** - Always provides Tesla with 9-24 hours lookahead for optimal battery management
- 📡 **Real-Time WebSocket** - Connects to Amber's WebSocket API for instant price updates (with automatic reconnection)

## Key Features Explained

### AEMO Spike Detection 🔋 *Tesla only*
This option is disabled by default and is primarily intended for Tesla Powerwall users with VPPs that offer AEMO Spike exports (Globird, AGL, Engie) but don't natively support Tesla batteries. Sigenergy users on Globird don't need this feature as Globird natively supports Sigenergy for spike exports.
Automatically monitors AEMO NEM wholesale electricity prices for your region (NSW1, QLD1, VIC1, SA1, TAS1). When prices exceed your configured threshold (e.g., $300/MWh), the system:
- Saves your current tariff configuration
- **Saves your current Powerwall operation mode** (self_consumption, autonomous, or backup)
- **Automatically switches to autonomous (TOU) mode** (required for TOU tariffs to work)
- Uploads a spike tariff with very high sell rates to encourage battery export
- Tesla Powerwall responds by exporting to grid during the spike
- **Automatically restores your original operation mode** when spike ends
- Restores your normal tariff when prices return to normal

Perfect for maximizing revenue during extreme price events! Works seamlessly regardless of your normal Powerwall mode.

**Monitoring Frequency:** Checks AEMO prices every 1 minute for responsive spike detection.

### Solar Curtailment
Prevents paying to export solar during negative pricing periods (≤0c/kWh). Works with both battery systems:

| Battery System | Method | Behavior |
|----------------|--------|----------|
| **Tesla** | Export rules API | Sets export to "never", restores to "battery_ok" when positive |
| **Sigenergy** | Modbus TCP | Sets export limit to 0kW (load-following mode) |

**Sigenergy Load-Following Mode:**
Unlike full solar shutdown, Sigenergy uses zero-export mode:
- ✅ Solar continues powering the house
- ✅ Battery still charges from solar
- ✅ Only grid export is blocked during negative prices

**Configuration:**
- Enable/disable in Amber Settings (Flask web interface)
- Enable/disable during Home Assistant integration setup
- Default: Disabled (opt-in feature)

### Spike Protection (Amber Only)

Prevents your battery from charging from the grid during Amber price spikes. When wholesale prices spike, your battery may see an arbitrage opportunity and charge from grid - this feature stops that behavior.

**Supported Battery Systems:** Tesla Powerwall ✅ | Sigenergy ✅

**The Problem:**
During price spikes, your battery receives 30-minute averaged forecast prices (~$0.85/kWh) rather than the real-time spike prices ($10-$20/kWh). It doesn't "see" the spike and may decide to charge from grid for later arbitrage.

**How It Works:**
When Amber reports `spikeStatus: 'potential'` or `'spike'` for a period, buy prices are overridden:

```
override_buy = max(all_sell_prices) + $1.00
```

This ensures charging from grid is always unprofitable during spikes - eliminating any arbitrage opportunity.

**Example:**
```
During a spike event:
  Actual import price: $16.48/kWh
  Actual export price: $14.69/kWh
  Max export in forecast: $21.29/kWh

Without spike protection:
  Battery sees forecast: ~$0.85/kWh buy
  Battery thinks: "Charge now, sell later!"
  Result: Grid charging during $16/kWh spike

With spike protection:
  Override buy: $21.29 + $1.00 = $22.29/kWh
  Battery calculates: $22.29 buy - $21.29 sell = $1.00 LOSS
  Result: No grid charging
```

**Key Features:**
- **Per-period protection** - Only affects periods Amber flags as spikes, not your whole day
- **Uses Amber's detection** - Relies on Amber's `spikeStatus` field, not arbitrary thresholds
- **Works with Export Boost** - They complement each other (spike protection = buy prices, export boost = sell prices)

**Note for 30-minute billing customers:**
If you're on 30-minute billing (not 5-minute), spike prices get averaged out in your bill. This feature is most critical for 5-minute billed customers who pay the full spike price.

**Configuration:**
- Enable/disable in Amber Settings (Flask web interface)
- Enable/disable during Home Assistant integration options flow
- Default: Disabled (opt-in feature)

### Export Price Boost

Artificially increases export prices sent to your battery system to trigger exports at lower price points. This is useful when Amber export prices are in the 20-25c range where the battery's algorithm may not trigger exports due to internal hysteresis.

**Supported Battery Systems:** Tesla Powerwall ✅ | Sigenergy ✅

**How It Works:**
The feature adds a configurable offset to export prices and/or sets a minimum floor price, but only during a specified time window (default 5pm-9pm evening peak).

**Configuration Options:**
| Setting | Description | Default |
|---------|-------------|---------|
| Enable Export Price Boost | Toggle the feature on/off | Off |
| Price Offset (c/kWh) | Fixed amount added to all export prices | 0 |
| Minimum Price (c/kWh) | Floor for export prices | 0 |
| Activation Threshold (c/kWh) | Boost only applies if actual price is at or above this value (0 = always apply) | 0 |
| Boost Start Time | When to start applying boost | 17:00 |
| Boost End Time | When to stop applying boost | 21:00 |

**Example Calculation:**
```
With offset=5c, min=20c, and threshold=10c:

Amber export price: 18c/kWh
→ 18 >= 10 (threshold), so boost applies
→ 18 + 5 = 23c (above min, battery sees 23c)

Amber export price: 12c/kWh
→ 12 >= 10 (threshold), so boost applies
→ 12 + 5 = 17c (below min, battery sees 20c floor)

Amber export price: 5c/kWh
→ 5 < 10 (below threshold), boost skipped
→ Battery sees 5c (unchanged)

Amber export price: -3c/kWh
→ -3 < 10 (below threshold), boost skipped
→ Battery sees -3c (unchanged)
```

**Use Cases:**
- Force battery to export during evening peak when prices are moderate (20-25c)
- Overcome the battery's internal decision-making that may not export at certain price points
- Maximize battery revenue during predictable high-demand periods
- Use activation threshold to skip boosting very low or negative prices where exporting doesn't make financial sense

**Note:** The boosted price is only what your battery sees for decision-making - you still get paid the actual Amber export rate. This feature tricks the battery into exporting when it otherwise wouldn't.

**Configuration:**
- Enable/disable in Amber Settings (Flask web interface)
- Enable/disable during Home Assistant integration options flow
- Default: Disabled (opt-in feature)

### Chip Mode

Suppress battery exports during configured hours (typically overnight) unless the price exceeds a threshold. This is the inverse of Export Price Boost - instead of encouraging exports, it prevents them while still allowing you to capture unexpected price spikes.

**Supported Battery Systems:** Tesla Powerwall ✅ | Sigenergy ✅

**The Problem:**
Your battery's algorithm may decide to export overnight based on price signals you don't want to respond to. This drains your battery when you'd rather keep it charged for morning use or to capture genuine price spikes.

**How It Works:**
During the configured time window (default 10pm-6am), the system sets export prices to $0:
- Battery sees $0 export rate → Won't export (no profit incentive)
- **Exception:** If the actual price exceeds your threshold (e.g., 30c), the original price is preserved
- This allows capturing unexpected overnight spikes while keeping the battery stable otherwise

**Configuration Options:**
| Setting | Description | Default |
|---------|-------------|---------|
| Enable Chip Mode | Toggle the feature on/off | Off |
| Start Time | When to start suppressing exports | 22:00 |
| End Time | When to stop suppressing exports | 06:00 |
| Price Threshold (c/kWh) | Allow exports only above this price | 30 |

**Example:**
```
With threshold=30c between 22:00-06:00:

Amber export price: 8c/kWh
→ 8 < 30 (below threshold)
→ Battery sees $0 (export suppressed)

Amber export price: 35c/kWh
→ 35 >= 30 (above threshold)
→ Battery sees 35c (export allowed)
```

**Use Cases:**
- Keep battery charged overnight for morning peak usage
- Prevent the battery's algorithm from exporting during low-value periods
- Still capture genuine price spikes (e.g., unexpected demand events)
- Works with Amber SmartShift to let Amber manage overnight charging while preventing unwanted exports

**Note:** Like Export Price Boost, the modified price is only what your battery sees for decision-making - your actual earnings are based on real Amber rates.

**Configuration:**
- Enable/disable in Amber Settings (Flask web interface)
- Enable/disable during Home Assistant integration options flow
- Default: Disabled (opt-in feature)

### Sigenergy Battery System Support

Full support for Sigenergy DC-coupled battery systems as an alternative to Tesla Powerwall.

**Features:**
- **Tariff Sync via Cloud API** - Uploads Amber pricing to Sigenergy Cloud using the same 30-minute TOU format
- **Real-Time Energy Data via Modbus** - Reads solar, battery, grid power and SOC from your inverter
- **DC Solar Curtailment** - Controls DC solar via Modbus TCP during negative prices (load-following mode)
- **Load-Following Curtailment** - Sets export limit to 0 instead of full PV shutdown, so solar continues to power house and charge battery

**Connection Requirements:**
| Connection | Purpose | Required |
|------------|---------|----------|
| **Cloud API** | Tariff sync to Sigenergy | ✅ Yes |
| **Modbus TCP** | Real-time energy data + DC curtailment | ✅ Yes |

#### Getting Sigenergy Cloud API Credentials

To sync Amber prices to your Sigenergy system, you need your Sigenergy account credentials.

**What You Need:**
| Credential | Description | Where to Find |
|------------|-------------|---------------|
| **Email** | Your Sigenergy account email | Your login email |
| **Password** | Your Sigenergy account password | Just use your normal password! |
| **Device ID** | 13-digit numeric identifier | Browser dev tools (see below) |
| **Station ID** | Your Sigenergy station identifier | SigenAI or browser dev tools |

**Password Authentication:**

PowerSync automatically encrypts your password using Sigenergy's AES-128-CBC encoding before sending it to the API. Just enter your plain password - no need to capture anything from browser dev tools!

> **Advanced users:** If you already have an encrypted password (from browser dev tools or a previous setup), you can still use it directly in the "Encrypted Password" field. This takes priority over the plain password field.

**Getting Device ID:**

The Device ID is a 13-digit identifier you need to capture once from browser dev tools:

1. **Open the Sigenergy Web Portal**
   - Go to https://app-aus.sigencloud.com/ (Australia) in your browser
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
   - Go to the **Payload** tab (or "Request" in some browsers)

5. **Copy the Device ID**
   - **userDeviceId**: This 13-digit number is your `Device ID`

**Getting Station ID:**

- **Easiest**: Ask SigenAI in the app: "Tell me my StationID"
- **Alternative**: In dev tools, look for requests containing `stationId` in the response
- **Alternative**: Save a tariff manually and look for `stationId` in the network request

**Example Credentials:**
```
Email: your.email@example.com
Password: MySecurePassword123!
Device ID: 1756353655250
Station ID: 102025092300219
```

> **Reference:** Based on [amber2sigen](https://github.com/Talie5in/amber2sigen) by [@Talie5in](https://github.com/Talie5in)

#### How DC Curtailment Works

Unlike full solar shutdown, Sigenergy uses **load-following mode** (zero export):

| Mode | What Happens |
|------|--------------|
| **Full Shutdown** | PV limit = 0 → Solar stops completely |
| **Load Following** (used) | Export limit = 0 → Solar powers house + charges battery, no grid export |

This is smarter because:
- ✅ Solar continues powering the house
- ✅ Battery still charges from solar
- ✅ Only grid export is blocked during negative prices

**Modbus Registers Used:**
- `plant_grid_point_maximum_export_limitation` (40038) - Sets export limit to 0kW for curtailment
- Reference: [TypQxQ/Sigenergy-Local-Modbus](https://github.com/TypQxQ/Sigenergy-Local-Modbus)

#### Configuration (Home Assistant)

**Initial Setup:**
1. Install PowerSync via HACS
2. Add the integration: Settings → Devices & Services → Add Integration → PowerSync
3. Select **Sigenergy** as your battery system
4. Enter your Sigenergy Cloud credentials (see above)
5. Select your Sigenergy station from the list
6. Enter your Sigenergy inverter's **Modbus IP address**
7. Optionally enable DC solar curtailment

**Adding Cloud Credentials Later:**
If you set up PowerSync before entering Cloud credentials, you can add them via Options:
1. Go to Settings → Devices & Services → PowerSync → Configure
2. Scroll down to the Sigenergy Cloud API section
3. Enter your credentials (Email, Encrypted Password, Device ID, Station ID)
4. Save and restart Home Assistant

#### Configuration (Flask Web App)

1. Go to Settings → Battery System → Select "Sigenergy"
2. Enter your Cloud credentials (captured from browser dev tools)
3. Validate credentials and select your station
4. Go to Amber Settings → Enter Sigenergy Modbus IP address, port, and slave ID

**Note:** Sigenergy is a **DC-coupled** battery system. If you have an AC-coupled solar inverter from another brand (Sungrow, Fronius, etc.) connected to your Sigenergy system, you can configure AC-coupled inverter curtailment separately (see below).

### AC-Coupled Inverter Curtailment

Control AC-coupled solar inverters directly during negative pricing periods. This feature works with **any battery system** (Tesla, Sigenergy, or others) and is useful when you have a separate solar inverter that bypasses the battery.

**Supported Inverter Brands:**
| Brand | Connection | Models |
|-------|------------|--------|
| **Sungrow** | Modbus TCP | SG series (string), SH series (hybrid) |
| **Fronius** | Modbus TCP | Primo, Symo, Gen24, Tauro, Eco |
| **GoodWe** | Modbus TCP | ET, EH, BT, BH, ES, EM series (hybrid) |
| **Huawei** | Modbus TCP | SUN2000 L1, M0, M1, M2 series |
| **Enphase** | HTTPS API | IQ Gateway, Envoy-S (microinverters) |

**How It Works:**
When Amber feed-in price drops below 1c/kWh (configurable), PowerSync sends a curtailment command to your inverter:
- **Sungrow/Fronius/GoodWe/Huawei**: Sets export limit to 0W via Modbus registers
- **Enphase**: Uses IQ Gateway API to limit production

When prices rise above the threshold, normal operation is restored.

**Why Use This:**
AC-coupled solar inverters export directly to the grid, bypassing your battery. During negative prices, this costs you money. Curtailment stops the export while still powering your home.

**Configuration (Home Assistant):**
1. Go to PowerSync options
2. Enable "AC-Coupled Inverter Curtailment"
3. Select your inverter brand and model
4. Enter the inverter's IP address
5. Configure Modbus port (502 default) and slave ID

**Configuration (Flask Web App):**
1. Go to Settings → Amber Settings
2. Enable "Inverter Curtailment"
3. Configure inverter connection details

**Note:** This is separate from Sigenergy DC curtailment. You can use both if you have:
- Sigenergy battery with DC solar → Use Sigenergy DC curtailment
- AC-coupled inverter (any brand) → Use AC-coupled inverter curtailment

### Force Mode Toggle (Alpha) 🔋 *Tesla only*

An experimental feature that forces the Powerwall to immediately recalculate its behavior after receiving new tariff prices.

**The Problem:**
After PowerSync uploads a new tariff, the Powerwall may take several minutes to recognize the price changes and adjust its charging/discharging behavior accordingly.

**How It Works:**
After a successful tariff sync, this feature briefly switches the Powerwall to self-consumption mode, waits 5 seconds, then switches back to Time-Based Control (autonomous mode). This mode toggle forces the Powerwall to immediately recalculate its optimal behavior based on the new prices.

**Smart Timing:**
The toggle only triggers when **settled/actual prices** are synced (at :35/:60 seconds), not on the initial forecast sync (at :00 seconds). This ensures:
- Only one toggle per 5-minute interval (not two)
- Powerwall recalculates using actual prices, not potentially different forecast prices
- Reduced unnecessary mode switches

**Trade-offs:**
- May cause a brief interruption (~5 seconds) to battery behavior during the mode switch
- Some users report faster Powerwall response; others see no difference
- Experimental feature - results may vary

**Configuration:**
- Enable/disable in Amber Settings → Alpha Options (Flask web interface)
- Enable/disable during Home Assistant integration options flow
- Default: Disabled (opt-in feature)

### Flow Power + AEMO Wholesale Pricing

Full support for **Flow Power** and other wholesale electricity retailers that pass through AEMO NEM spot prices.

**How It Works:**
AEMO wholesale prices only include the energy cost - they don't include network (DNSP) charges, environmental fees, or GST. PowerSync automatically calculates your total retail price using the [aemo_to_tariff](https://github.com/powston/aemo_to_tariff) library.

**Configuration:**
1. Select **Flow Power** as your electricity provider
2. Choose your **NEM Region** (QLD1, NSW1, VIC1, SA1)
3. Select **AEMO NEM Wholesale** as the price source
4. Configure your **Network Tariff**:

   **Option A: Automatic (Recommended)**
   - Select your **Network Distributor** (DNSP) from the dropdown:
     - Energex, Ergon, Ausgrid, Endeavour, Essential, SA Power Networks
     - Powercor, CitiPower, AusNet, Jemena, United Energy
     - TasNetworks, Evoenergy
   - Enter your **Tariff Code** from your electricity bill (e.g., NTC6900, EA025)
   - The system automatically calculates network fees, market charges, and GST

   **Option B: Manual Rates**
   - Check "Use manual rates instead of automatic lookup"
   - Enter your rates manually:
     - **Flat Rate**: Single rate all day (e.g., 8c/kWh)
     - **Time of Use**: Peak/Shoulder/Off-Peak rates with time windows
     - **Other Fees**: Environmental levies, market charges (~3-4c/kWh typical)
     - **GST**: Automatically adds 10%

**Finding Your Tariff Code:**
Look on your electricity bill for the network tariff code. Common examples:
| Distributor | Example Tariffs |
|-------------|-----------------|
| Energex (QLD) | NTC6900, NTC8400, NTC8500 |
| Ausgrid (NSW) | EA025, EA050, EA116 |
| SA Power Networks | RTOU, RELE |

**Flow Power Happy Hour:**
Export rates during Happy Hour (5:30pm - 7:30pm daily):
- NSW, QLD, SA: 45c/kWh
- VIC: 35c/kWh
- Outside Happy Hour: 0c/kWh

**Total Price Calculation (Automatic):**
The aemo_to_tariff library handles all calculations:
```
Total = AEMO Wholesale + Network Charges + Market Fees + Environmental Levies + GST
```

**Note:** When using manual rates, enter all values in **cents/kWh**. If your tariff shows $0.19367/kWh, enter `19.367`.

### Custom TOU Schedules 🔋 *Tesla only*
Not using Amber Electric? No problem! Create custom time-of-use schedules for any Australian electricity provider:
- Define multiple seasonal periods (e.g., Summer, Winter)
- Set different rates for peak, shoulder, and off-peak periods
- Configure weekday/weekend variations
- Upload directly to Tesla Powerwall via Teslemetry

### Automations

Create powerful automations that respond to your energy system, EV charging, weather, and electricity prices. Automations can send notifications or execute actions on your battery system.

**Trigger Types:**
| Trigger | Description | Example Use Case |
|---------|-------------|------------------|
| **Time** | Fire at specific times | Start battery charging at 2am |
| **Battery** | Battery SOC thresholds | Notify when battery reaches 100% |
| **Power Flow** | Solar/grid/load thresholds | Alert when solar exceeds 5kW |
| **Price** | Import/export price thresholds | Export when price rises above 30c |
| **Grid** | Grid status changes | Alert when going off-grid |
| **Weather** | Weather condition changes | Notify when it becomes sunny |
| **Solar Forecast** | Solcast forecast thresholds | Charge battery if tomorrow < 10kWh |
| **EV** | EV charging state (Tesla Fleet) | Notify when EV starts charging |
| **OCPP** | OCPP charger state | Alert when charger becomes available |

**Available Actions:**
| Action | Description |
|--------|-------------|
| **Set Operation Mode** | Switch to self-consumption, autonomous, or backup mode |
| **Set Backup Reserve** | Adjust backup reserve percentage |
| **Force Charge** | Force battery to charge from grid |
| **Force Discharge** | Force battery to discharge to grid |

**Features:**
- **Time Windows**: Restrict triggers to specific hours (e.g., only during peak 5pm-9pm)
- **Repeat Days**: Limit to specific days of the week
- **Priority System**: Higher priority automations take precedence
- **Notification Only Mode**: Get alerts without executing actions
- **Edge Detection**: Triggers fire on state changes, not continuously

**Configuration:**
- Create automations via the PowerSync mobile app
- Automations sync to your Home Assistant instance
- Manage via Settings → Devices & Services → PowerSync → Automations

### EV Charging Controls 🚗 *Tesla Fleet only*

Control your Tesla vehicle's charging directly from the PowerSync mobile app.

**Available Commands:**
| Command | Description |
|---------|-------------|
| **Wake Up** | Wake the vehicle from sleep |
| **Start Charging** | Begin charging session |
| **Stop Charging** | End charging session |
| **Set Charge Limit** | Adjust charge limit percentage |
| **Set Charging Amps** | Adjust charging amperage |

**Requirements:**
- Tesla Fleet integration configured in Home Assistant
- Vehicle entities available (sensor.*_charging_state, binary_sensor.*_charger, etc.)

### Saved TOU Profiles
Backup and restore your tariff configurations:
- Save current Tesla tariff as named profile
- Restore previous configurations anytime
- Manage multiple tariff setups
- Pre-spike backup for AEMO spike detection

## How It Works

### Intelligent Price Conversion

PowerSync uses sophisticated algorithms to convert Amber Electric's dynamic pricing into Tesla-compatible TOU (Time-of-Use) tariffs:

#### 1. **Smart Period Mapping**
Amber Electric labels their forecasts using **END time** convention (e.g., "18:00 forecast" = 17:30-18:00 period), while Tesla uses **START time** labels (e.g., PERIOD_17_30 = 17:30-18:00). PowerSync automatically aligns these conventions so prices match exactly what you see in the Amber app.

#### 2. **5-Minute Averaging**
- **Recent/Current Prices:** Amber provides 5-minute actual intervals with high precision
- **Conversion:** PowerSync averages six 5-minute intervals into each 30-minute Tesla period
- **Result:** More accurate pricing that captures real market volatility
- **Example:** Period 20:00-20:30 averages prices from 20:05, 20:10, 20:15, 20:20, 20:25, 20:30

#### 3. **Rolling 24-Hour Window**
Tesla requires a static 24-hour tariff structure, but PowerSync makes it "roll" forward:
- **Future periods** (not yet reached today): Use today's forecast prices
- **Past periods** (already passed today): Use tomorrow's forecast prices
- **Benefit:** Tesla always has 9-24 hours of lookahead for every period, enabling optimal battery decisions

**Example at 2:15 PM:**
```
PERIOD_00_00 → Tomorrow's 00:00 forecast (+9h 45m lookahead)
PERIOD_14_00 → Today's 14:30 forecast    (+15m lookahead - current)
PERIOD_23_30 → Tomorrow's 00:00 forecast (+9h 45m lookahead)
```

#### 4. **Timezone Auto-Detection**
Works anywhere in Australia without configuration:
- **Brisbane (AEST UTC+10:00):** No DST
- **Sydney/Melbourne/Canberra (AEDT UTC+11:00):** DST in summer
- **Adelaide (ACDT UTC+10:30):** Unique 30-minute offset + DST
- **Perth (AWST UTC+8:00):** No DST
- **Darwin (ACST UTC+9:30):** No DST

The system automatically extracts timezone information from Amber's API data, ensuring correct "past vs future" period detection for all locations.

### Price Models

PowerSync supports three different Amber pricing models for TOU schedule generation:

| Model | Description | Best For |
|-------|-------------|----------|
| **Predicted** (Default) | Uses Amber's forecast price - their best estimate | Most users, balanced approach |
| **High** (Conservative) | Uses Amber's high estimate - upper bound of confidence interval | Risk-averse users who want to avoid unexpected high prices |
| **Low** (Aggressive) | Uses Amber's low estimate - lower bound of confidence interval | Users comfortable with volatility who want to maximize savings |

**Predicted** is recommended for most users. Use **High** if you want to ensure your battery is always prepared for worst-case pricing. Use **Low** if you're comfortable with price volatility and want to maximize potential savings.

Configure the price model in the integration options (HA) or Amber Settings (Flask).

#### 5. **Precision Matching**
Prices are rounded to **4 decimal places** with trailing zeros automatically removed:
- `0.2014191` → `0.2014` (4 decimals)
- `0.1990000` → `0.199` (3 decimals, trailing zeros dropped)

### Sync Frequency

**Every 5 minutes** - Perfectly aligned with Amber Electric's forecast update schedule for maximum freshness.

## Installation Options

PowerSync is available in two deployment options:

1. **[Home Assistant Integration](#home-assistant-integration)** - Native HA custom integration (Recommended for HA users)
2. **[Docker Application](#docker-deployment)** - Standalone web app with dashboard

Both options require **Tesla API access** - see [Tesla API Options](#tesla-api-options) below for setup.

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
| ✅ Completely free | ⚠️ Requires OAuth app registration |
| ✅ Direct API access | ⚠️ More setup steps |
| ✅ Built-in Cloudflare Tunnel | |
| ✅ Automatic token refresh | |

**Setup for Home Assistant:**
1. Install the official **Tesla Fleet** integration in Home Assistant
   - Settings → Devices & Services → Add Integration → "Tesla Fleet"
   - Follow the OAuth login flow
2. PowerSync automatically detects your Tesla Fleet credentials
3. Leave the Teslemetry field empty during PowerSync setup

**Setup for Docker:**
1. Register an OAuth app at https://developer.tesla.com
2. In PowerSync Settings, select "Tesla Fleet API (Direct, Free)"
3. Enter Client ID, Client Secret, and Redirect URI
4. Click "Connect to Tesla Fleet API" to authorize
5. See [TESLA_FLEET_SETUP.md](docs/TESLA_FLEET_SETUP.md) for detailed instructions

---

## Home Assistant Integration

The easiest way to use PowerSync if you're already running Home Assistant.

### Features

- ✅ **Native HA Integration** - Seamless integration with your Home Assistant instance
- ✅ **HACS Installation** - Install and update via HACS (Home Assistant Community Store)
- ✅ **Automatic Discovery** - Auto-discovers Tesla energy sites
- ✅ **Real-time Sensors** - Amber pricing and Tesla energy data as HA sensors
- ✅ **Automatic TOU Sync** - Background syncing every 5 minutes
- ✅ **Manual Services** - On-demand sync services for advanced automation
- ✅ **Automations Engine** - Create automations triggered by battery, price, weather, EV, and more
- ✅ **EV Charging Controls** - Control Tesla vehicle charging via Tesla Fleet integration
- ✅ **Push Notifications** - Receive alerts on your phone when automations trigger

### Prerequisites

- Home Assistant installed and running
- HACS (Home Assistant Community Store) installed
- Amber Electric API token ([get one here](https://app.amber.com.au/developers))
- Tesla API access ([choose an option above](#tesla-api-options))

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
   - Enter your **Amber API token** ([get one here](https://app.amber.com.au/developers))
   - Enter your **Tesla API credentials** (Teslemetry key or leave empty for Tesla Fleet)
   - Select your Tesla energy site and Amber site
   - Enable automatic TOU schedule syncing (recommended)

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

### Automatic TOU Syncing

**The integration automatically syncs your TOU schedule every 5 minutes** (aligned with Amber Electric's forecast updates) when the auto-sync switch is enabled.

**How it works:**
1. Enable the `switch.auto_sync_tou_schedule` switch (enabled by default during setup)
2. The integration runs a background timer that checks every 5 minutes
3. If auto-sync is enabled, it automatically:
   - Fetches the latest Amber pricing forecast
   - Converts it to Tesla TOU format
   - Sends it to your Powerwall via Teslemetry API
4. If auto-sync is disabled, the timer skips syncing

**No automation required!** Just leave the switch on and the integration handles everything automatically, just like the Docker version.

You can disable automatic syncing by turning off the switch, and re-enable it anytime.

### Available Services (Advanced)

```yaml
# Manually sync TOU schedule
service: power_sync.sync_tou_schedule

# Refresh data from Amber and Teslemetry
service: power_sync.sync_now
```

### Example Automations (Optional)

These are optional automations for advanced users. **Auto-sync is automatic and doesn't require any automations.**

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

**Disable auto-sync during off-peak hours:**
```yaml
automation:
  - alias: "Disable Auto-Sync at Night"
    trigger:
      - platform: time
        at: "23:00:00"
    action:
      - service: switch.turn_off
        target:
          entity_id: switch.power_sync_auto_sync

  - alias: "Enable Auto-Sync in Morning"
    trigger:
      - platform: time
        at: "06:00:00"
    action:
      - service: switch.turn_on
        target:
          entity_id: switch.power_sync_auto_sync
```

### Pre-built Dashboard (Optional)

A pre-built Lovelace dashboard is included for visualizing all PowerSync data.

**Required HACS Frontend Cards:**
- `mushroom` - Compact chips for controls
- `card-mod` - Custom card styling
- `power-flow-card-plus` - Real-time energy flow visualization
- `apexcharts-card` - Advanced charting for price/energy history

**Installation:**
1. Install the required HACS cards (HACS → Frontend → search for each card)
2. Copy the dashboard YAML from `custom_components/power_sync/dashboard/power_sync_dashboard.yaml`
3. In Home Assistant: Settings → Dashboards → Add Dashboard → "New dashboard from scratch"
4. Edit the new dashboard → 3 dots menu → "Raw configuration editor"
5. Paste the YAML content and save

**Required Helper Entities:**

The Force Charge and Force Discharge controls require `input_select` helpers for duration selection:

1. Go to **Settings → Devices & Services → Helpers**
2. Click **+ Create Helper → Dropdown**
3. Create the first helper:
   - Name: `force_charge_duration` (creates entity `input_select.force_charge_duration`)
   - Options: `15`, `30`, `45`, `60`, `90`, `120`
4. Click **Create**
5. Click **+ Create Helper → Dropdown** again
6. Create the second helper:
   - Name: `force_discharge_duration` (creates entity `input_select.force_discharge_duration`)
   - Options: `15`, `30`, `45`, `60`, `90`, `120`
7. Click **Create**

**Dashboard Features:**
- Current price gauge with color-coded severity
- Battery level gauge
- Power flow visualization (solar → battery → grid → home)
- Force discharge controls with duration dropdown and restore button
- 24-hour price history chart
- Energy usage charts (solar, grid, battery, home load)
- Demand charge monitoring section
- AEMO wholesale price monitoring section

**Customization:**
If Home Assistant renamed your entities (e.g., added `_2` suffix due to conflicts), adjust the entity IDs in the YAML accordingly.

### Troubleshooting

- **No sensors appearing**: Check that the integration is enabled in Settings → Devices & Services
- **Invalid API token**: Verify tokens at Amber and Teslemetry/Tesla Fleet
- **No Tesla sites found**:
  - If using Tesla Fleet: Ensure the Tesla Fleet integration is loaded and working
  - If using Teslemetry: Ensure your Tesla account is linked in Teslemetry
- **TOU sync failing**: Check Home Assistant logs for detailed error messages
- **Tesla Fleet not detected**:
  - Verify Tesla Fleet integration is installed and loaded (green status)
  - Restart Home Assistant after installing Tesla Fleet
  - Check Settings → Devices & Services to ensure Tesla Fleet shows "Loaded"

---

## Docker Deployment

Standalone web application with dashboard for users who prefer Docker or don't use Home Assistant.

## Quick Start

### Method 1: Docker Hub (Recommended)

The easiest way to deploy is using the official pre-built image from Docker Hub.

**Option A: Using docker-compose (Recommended)**

```bash
# Download the docker-compose file
curl -O https://raw.githubusercontent.com/bolagnaise/PowerSync/main/docker/docker-compose.hub.yml
curl -O https://raw.githubusercontent.com/bolagnaise/PowerSync/main/.env.example
mv .env.example .env

# Edit .env with your Tesla credentials (encryption key auto-generated on first run)
nano .env

# Create data directory for persistence
mkdir -p ./data

# Start the container (note: file is in docker/ folder in repo, but we downloaded it to current dir)
docker-compose -f docker-compose.hub.yml up -d

# Access the app
open http://localhost:5001
```

**Option B: Using docker run**

```bash
# Create data directory first
mkdir -p $(pwd)/data

docker run -d \
  --name tesla-sync \
  -p 5001:5001 \
  -v $(pwd)/data:/app/data \
  -e SECRET_KEY=your-secret-key-here \
  --restart unless-stopped \
  bolagnaise/tesla-sync:latest

# Note: Encryption key is auto-generated and saved to ./data/.fernet_key
# Tesla OAuth credentials can be configured via the Environment Settings page in the web UI
```

**Environment Variables:**

```bash
# Required
SECRET_KEY=your-random-secret-key-here

# Optional - Auto-generated if not provided
# FERNET_ENCRYPTION_KEY=your-custom-key-here
```

**Note on Configuration:**
- **Encryption Key:** Automatically generated and saved to `./data/.fernet_key` on first run. Only set `FERNET_ENCRYPTION_KEY` if you want to use a specific key (e.g., migrating from another instance).
- **Important:** Back up `./data/.fernet_key` - without it, you cannot decrypt stored credentials

**Docker Hub Image Details:**
- **Repository:** `bolagnaise/tesla-sync`
- **Multi-Architecture:** Supports `linux/amd64` and `linux/arm64`
- **Automated Builds:** Every push to main branch
- **Production Server:** Gunicorn with 4 workers

**Note:** For Tesla Fleet API registration, `cloudflared` is bundled in the Docker image and will be auto-downloaded for local installations.

### Method 2: Build from Source

For development or customization:

1. **Clone the repository**
```bash
git clone https://github.com/bolagnaise/PowerSync.git
cd PowerSync
```

2. **Create `.env` file**
```bash
cp .env.example .env
```

3. **Edit `.env` with your credentials** (see environment variables above)
   - Encryption key will be auto-generated on first run

4. **Create data directory**
```bash
mkdir -p ./data
```

5. **Start with Docker Compose**
```bash
docker-compose -f docker/docker-compose.yml up -d
```

6. **Access the dashboard**
```
http://localhost:5001
```

### Method 3: Python Virtual Environment (Advanced)

For local development without Docker:

```bash
# Create virtual environment
python3 -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Configure environment
cp .env.example .env
# Edit .env with your credentials

# Initialize database
flask db upgrade

# Run application
flask run
```

Navigate to http://localhost:5001

---

## Docker Management

### View Logs
```bash
# Docker Compose (from source)
docker-compose -f docker/docker-compose.yml logs -f

# Docker Compose (pre-built, if using docker-compose.hub.yml)
docker-compose -f docker-compose.hub.yml logs -f

# Docker run
docker logs -f tesla-sync
```

### Update to Latest Version

**Pre-built Image:**
```bash
docker pull bolagnaise/tesla-sync:latest
docker restart tesla-sync

# Or with docker-compose
docker-compose -f docker-compose.hub.yml pull
docker-compose -f docker-compose.hub.yml up -d
```

**Built from Source:**
```bash
cd tesla-sync
git pull
docker-compose -f docker/docker-compose.yml down
docker-compose -f docker/docker-compose.yml up -d --build
```

**Local Development (Python venv):**
```bash
cd tesla-sync
git pull
source venv/bin/activate
flask db upgrade  # Apply any database migrations
# Restart flask
```

> **Note:** Database migrations are automatically applied when using Docker. For local development, you must run `flask db upgrade` after pulling updates to ensure your database schema is current.

### Data Persistence

⚠️ **IMPORTANT**: Your database is stored in the `./data` directory. **Create this directory before first run** to prevent data loss during upgrades:

```bash
mkdir -p ./data
```

**Your data:**
- Database: `./data/app.db` (user accounts, API credentials, settings)
- This directory is mounted as a Docker volume for persistence
- **If `./data` doesn't exist, a fresh database is created on each restart**

**Quick backup:**
```bash
# Backup database
cp ./data/app.db ./data/app.db.backup-$(date +%Y%m%d)

# Restore database
cp ./data/app.db.backup-YYYYMMDD ./data/app.db
docker restart tesla-sync
```

📖 **See [DATABASE.md](DATABASE.md) for:**
- Detailed backup/restore instructions
- Automated backup scripts
- Troubleshooting database issues
- Unraid-specific setup

## Configuration

After starting PowerSync, open the web interface at `http://localhost:5001` and go to **Settings**:

1. **Amber Electric** - Enter your API token ([get one here](https://app.amber.com.au/developers))
2. **Tesla API** - Choose Teslemetry or Fleet API ([see options](#tesla-api-options))
3. **Energy Site** - Enter your Tesla Energy Site ID (found in Teslemetry or Tesla app)
4. **Save & Verify** - Check that API status indicators turn green

## Usage

### Automatic Sync

The app automatically:
- Syncs TOU tariff every 5 minutes (aligned with Amber Electric's forecast updates)
- Fetches latest pricing forecasts from Amber API
- Sends optimized rates to Tesla Powerwall

**Sync Timing:**
- **Frequency:** Every 5 minutes
- **Alignment:** Matches Amber Electric's pricing forecast update schedule (updated every 5 minutes)
- **Forecast Window:** 48 half-hour periods (24 hours ahead)
- **Tesla Format:** Tesla still receives 30-minute TOU schedules, but with the latest forecast data

### Monitoring

- **Current Prices**: Real-time Amber pricing with 5-minute interval updates
- **Battery Status**: Powerwall charge level, power flow
- **Energy Usage**: Near real-time charts (30-second updates) with enhanced hover tooltips
- **Price History**: 24-hour price chart with timezone-adjusted timestamps
- **TOU Schedule**: Upcoming 24-hour tariff plan (auto-refreshes every 30 minutes)

## Architecture

### Tech Stack

- **Backend**: Flask (Python)
- **Production Server**: Gunicorn (4 workers, 120s timeout)
- **Database**: SQLite (PostgreSQL supported)
- **Auth**: Flask-Login
- **Scheduler**: APScheduler (5-minute TOU sync, 5-minute data collection)
- **Encryption**: Fernet (cryptography)
- **Timezone Support**: Python zoneinfo (IANA timezones)
- **Containerization**: Docker (multi-arch: amd64, arm64)
- **CI/CD**: GitHub Actions (automated builds)

### Key Components

```
app/
├── __init__.py          # App factory, extensions
├── models.py            # User, PriceRecord models
├── routes.py            # All endpoints
├── forms.py             # WTForms
├── api_clients.py       # Amber, Tesla, Teslemetry clients
├── utils.py             # Encryption, key generation
├── scheduler.py         # Background TOU sync
├── tariff_converter.py  # Amber → Tesla format
└── templates/           # Jinja2 templates
```

### Authentication Flow

**Tesla Fleet API:**
1. Generate EC key pair (prime256v1)
2. Host public key at `/.well-known/appspecific/com.tesla.3p.public-key.pem`
3. OAuth2 flow with Tesla
4. Register public key with Partner Account API
5. Pair vehicle via Tesla mobile app

**Teslemetry:**
1. User enters API key
2. Key encrypted and stored
3. Proxied API calls via Teslemetry

**Client Priority:**
- Tries Fleet API first (if configured)
- Falls back to Teslemetry (if configured)
- Returns None if neither available

## Development

### Database Migrations

```bash
# Create migration
flask db migrate -m "Description"

# Apply migration
flask db upgrade

# Rollback
flask db downgrade
```

### Flask Shell

```bash
flask shell
# Available: db, User, PriceRecord
```

### Debug Mode

```bash
export FLASK_DEBUG=1
flask run
```

## Production Deployment

### Requirements

- HTTPS domain with valid SSL certificate
- PostgreSQL database (recommended)
- Reverse proxy (nginx/Apache)
- Process manager (systemd/supervisor)

### Example Nginx Config

```nginx
server {
    listen 443 ssl http2;
    server_name yourdomain.com;

    ssl_certificate /path/to/cert.pem;
    ssl_certificate_key /path/to/key.pem;

    location / {
        proxy_pass http://localhost:5001;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }

    location /.well-known/appspecific/com.tesla.3p.public-key.pem {
        proxy_pass http://localhost:5001/.well-known/appspecific/com.tesla.3p.public-key.pem;
        add_header Content-Type application/x-pem-file;
    }
}
```

### Environment Variables (Production)

```bash
# Required
SECRET_KEY=strong-random-secret

# Optional
FERNET_ENCRYPTION_KEY=your-fernet-key
DATABASE_URL=postgresql://user:pass@localhost/dbname

# Tesla OAuth Credentials (Optional - can be configured via web UI)
# TESLA_CLIENT_ID=your-client-id
# TESLA_CLIENT_SECRET=your-client-secret
# TESLA_REDIRECT_URI=https://yourdomain.com/tesla-fleet/callback
# APP_DOMAIN=https://yourdomain.com
```

**Note:** Tesla OAuth credentials can now be configured via the Environment Settings page in the web UI. This is recommended for easier management and eliminates the need to restart the container when updating credentials.

### Run with Gunicorn

```bash
gunicorn -w 4 -b 0.0.0.0:5001 run:app
```

## Security

- ✅ All API tokens encrypted with Fernet
- ✅ Passwords hashed with Werkzeug
- ✅ CSRF protection via Flask-WTF
- ✅ Private keys never exposed publicly
- ✅ OAuth2 state parameter validation

**Best Practices:**
- Never commit `.env` file
- Rotate credentials periodically
- Use HTTPS in production
- Enable Tesla two-factor authentication
- Review active virtual keys regularly

## Troubleshooting

### Common Issues

| Problem | Solution |
|---------|----------|
| **Teslemetry connection failed** | Verify API key is correct and Tesla account is linked in Teslemetry |
| **Tesla Fleet API failed** | Check OAuth tokens, try re-authorizing via Settings |
| **No energy sites found** | Ensure Powerwall is commissioned and visible in Tesla app |
| **TOU sync not working** | Check logs, verify Energy Site ID is correct |

### Logs

```bash
# Docker
docker logs -f tesla-sync

# Local development
tail -f flask.log
```

## Documentation

| Guide | Description |
|-------|-------------|
| [TESLA_FLEET_SETUP.md](docs/TESLA_FLEET_SETUP.md) | Tesla Fleet API setup guide |
| [UNRAID_SETUP.md](docs/UNRAID_SETUP.md) | Unraid deployment guide |
| [Docker Hub](https://hub.docker.com/r/bolagnaise/power-sync) | Pre-built images |

**External Docs:** [Tesla Fleet API](https://developer.tesla.com/docs/fleet-api) · [Amber API](https://api.amber.com.au/docs)

## Support

For issues: Check logs first, then [open a GitHub issue](https://github.com/bolagnaise/PowerSync/issues) or join [Discord](https://discord.gg/eaWDWxEWE3).

## Acknowledgments

PowerSync builds on the work of these excellent open-source projects:

**Sigenergy Support:**
| Project | Author | Used For |
|---------|--------|----------|
| [amber2sigen](https://github.com/Talie5in/amber2sigen) | [@Talie5in](https://github.com/Talie5in) | Sigenergy Cloud API authentication and tariff sync |
| [Sigenergy-Local-Modbus](https://github.com/TypQxQ/Sigenergy-Local-Modbus) | [@TypQxQ](https://github.com/TypQxQ) | Sigenergy Modbus register documentation for DC curtailment |

**AC-Coupled Inverter Support:**
| Project | Author | Used For |
|---------|--------|----------|
| [Sungrow-SHx-Inverter-Modbus-Home-Assistant](https://github.com/mkaiser/Sungrow-SHx-Inverter-Modbus-Home-Assistant) | [@mkaiser](https://github.com/mkaiser) | Sungrow SH series Modbus registers |
| [sungrow-sg5-price-curtailment](https://github.com/Artic0din/sungrow-sg5-price-curtailment) | [@Artic0din](https://github.com/Artic0din) | Sungrow SG series curtailment logic |
| [goodwe](https://github.com/marcelblijleven/goodwe) | [@marcelblijleven](https://github.com/marcelblijleven) | GoodWe inverter Modbus protocol |
| [huawei-solar-lib](https://github.com/wlcrs/huawei-solar-lib) | [@wlcrs](https://github.com/wlcrs) | Huawei SUN2000 Modbus registers |
| [pyenphase](https://github.com/pyenphase/pyenphase) | [@pyenphase](https://github.com/pyenphase) | Enphase IQ Gateway API |
| [Enphase-API](https://github.com/Matthew1471/Enphase-API) | [@Matthew1471](https://github.com/Matthew1471) | Enphase API documentation |
| [Fronius Curtailment Guide](https://www.smartmotion.life/2023/09/12/amber-electric-curtailment-with-home-assistant/) | smartmotion.life | Fronius SunSpec Modbus curtailment |

**Pricing & Tariffs:**
| Project | Author | Used For |
|---------|--------|----------|
| [aemo_to_tariff](https://github.com/powston/aemo_to_tariff) | [@powston](https://github.com/powston) | AEMO wholesale to retail price conversion with network tariffs |

Thank you to all contributors and the Australian energy automation community!

## License

MIT

## Contributing

Contributions welcome! Please:
1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Submit a pull request

---

**Made with ⚡ for Australian battery owners - Tesla Powerwall & Sigenergy**
