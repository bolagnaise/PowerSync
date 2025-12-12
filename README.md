<div align="center">
  <img src="assets/images/logo.png" alt="Tesla Sync Logo" width="400"/>

  # Tesla Sync

  Intelligent Tesla Powerwall energy management for Australia. Automatically sync with Amber Electric dynamic pricing, create custom TOU schedules for any provider, and capitalize on AEMO wholesale price spikes to maximize your battery's earning potential.

  <a href="https://paypal.me/benboller" target="_blank"><img src="https://www.buymeacoffee.com/assets/img/custom_images/orange_img.png" alt="Buy Me A Coffee" style="height: 41px !important;width: 174px !important;box-shadow: 0px 3px 2px 0px rgba(190, 190, 190, 0.5) !important;-webkit-box-shadow: 0px 3px 2px 0px rgba(190, 190, 190, 0.5) !important;" ></a>

  [![Discord](https://img.shields.io/badge/Discord-Join%20Community-5865F2?logo=discord&logoColor=white)](https://discord.gg/eaWDWxEWE3)

  [![Docker Hub](https://img.shields.io/docker/v/bolagnaise/tesla-sync?label=docker%20hub&logo=docker)](https://hub.docker.com/r/bolagnaise/tesla-sync)
  [![Docker Pulls](https://img.shields.io/docker/pulls/bolagnaise/tesla-sync)](https://hub.docker.com/r/bolagnaise/tesla-sync)
  [![Build Status](https://github.com/bolagnaise/tesla-sync/actions/workflows/docker-publish.yml/badge.svg)](https://github.com/bolagnaise/tesla-sync/actions)
</div>

## Disclaimer

This is an unofficial integration and is not affiliated with or endorsed by Tesla, Inc. or Amber Electric. Use at your own risk. The developers are not responsible for any damages or issues that may arise from the use of this software.

## Screenshots

<div align="center">

### Dashboard Overview
<img src="https://i.imgur.com/E500MNw.jpeg" alt="Tesla Sync Dashboard" width="80%"/>

*Complete dashboard showing real-time energy monitoring, Amber Electric pricing, Tesla Powerwall status, and 24-hour tariff schedule*

### Tesla Powerwall Status
<img src="https://i.imgur.com/I7F6C7Z.png" alt="Tesla Powerwall Status" width="80%"/>

*Real-time Tesla Powerwall monitoring showing battery level, solar generation, battery power, grid power, and firmware version*

### Electricity Tariff Schedule (24H)
<img src="https://i.imgur.com/wEsczbz.png" alt="Electricity Tariff Schedule" width="80%"/>

*Rolling 24-hour tariff schedule with 30-minute intervals, buy/sell rates, and auto-sync to Tesla Powerwall*

### Price History
<img src="https://i.imgur.com/LtylkNH.png" alt="Price History Chart" width="80%"/>

*Historical electricity price chart showing import and export prices with day selector*

### Energy Usage History
<img src="https://i.imgur.com/cOlnp2D.png" alt="Energy Usage History" width="80%"/>

*Comprehensive energy usage charts tracking solar generation, grid power, battery power, and home consumption with real-time updates*

---

**[View Full Screenshot Gallery →](https://imgur.com/a/zjyTqsE)**

</div>

## Features

### Core Functionality
- 🔋 **Automatic TOU Tariff Sync** - Updates Tesla Powerwall with Amber Electric pricing every 5 minutes
- 📊 **Real-time Pricing Dashboard** - Monitor current and historical electricity prices with live updates
- ⚡ **Near Real-Time Energy Monitoring** - Energy usage charts update every 30 seconds
- 🌏 **Timezone Support** - Auto-detects timezone from Amber data for accurate time display across all Australian states

### Advanced Features
- ⚡ **AEMO Spike Detection** - Automatically monitors Australian wholesale electricity prices and switches to spike tariff during extreme price events (configurable threshold). Includes intelligent operation mode switching - automatically saves your current Powerwall mode and switches to autonomous (TOU) mode during spikes, then restores your original mode when prices normalize
- 🌞 **Solar Curtailment** - Automatically prevents solar export during negative pricing periods (≤0c/kWh). When Amber feed-in prices go negative, the system sets Powerwall export to "never" to avoid paying to export, then restores to "battery_ok" when prices return to positive
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
- 🌏 **Australia-Wide Compatibility** - Auto-detects timezone from Amber data, works in all Australian states (QLD, NSW, VIC, SA, TAS, WA, NT)
- 📊 **Intelligent Price Averaging** - Averages 5-minute Amber intervals into 30-minute Tesla periods for maximum accuracy
- 🎯 **Period Alignment** - Correctly aligns with Amber's forecast labels (e.g., "18:00 forecast" → Tesla PERIOD_17_30)
- 🔄 **Rolling 24-Hour Window** - Always provides Tesla with 9-24 hours lookahead for optimal battery management
- 📡 **Real-Time WebSocket** - Connects to Amber's WebSocket API for instant price updates (with automatic reconnection)

## Key Features Explained

### AEMO Spike Detection
This option is disabled by default and is primarily intended for use with VPPs that offer AEMO Spike exports (GLOBIRD,AGL,ENGIE) and where Tesla Batteries are not natively supported.
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
Prevents paying to export solar during negative pricing periods common with Amber Electric. The system monitors feed-in prices every minute and:

**During Negative Prices (≤0c/kWh):**
- Sets Powerwall export rule to "never" to prevent grid export
- Implements Tesla API bug workaround: if already set to "never", toggles to "pv_only" then back to "never" to force the setting to apply
- Protects you from financial penalties during negative price events

**During Positive Prices (>0c/kWh):**
- Restores export rule to "battery_ok" to allow both solar and battery export
- Enables normal revenue generation from grid export

**Configuration:**
- Enable/disable in Amber Settings (Flask web interface)
- Enable/disable during Home Assistant integration setup
- Default: Disabled (opt-in feature)

This feature is particularly useful with Amber's wholesale pricing to avoid paying to export your solar during oversupply periods.

### Custom TOU Schedules
Not using Amber Electric? No problem! Create custom time-of-use schedules for any Australian electricity provider:
- Define multiple seasonal periods (e.g., Summer, Winter)
- Set different rates for peak, shoulder, and off-peak periods
- Configure weekday/weekend variations
- Upload directly to Tesla Powerwall via Teslemetry

### Saved TOU Profiles
Backup and restore your tariff configurations:
- Save current Tesla tariff as named profile
- Restore previous configurations anytime
- Manage multiple tariff setups
- Pre-spike backup for AEMO spike detection

## How It Works

### Intelligent Price Conversion

Tesla Sync uses sophisticated algorithms to convert Amber Electric's dynamic pricing into Tesla-compatible TOU (Time-of-Use) tariffs:

#### 1. **Smart Period Mapping**
Amber Electric labels their forecasts using **END time** convention (e.g., "18:00 forecast" = 17:30-18:00 period), while Tesla uses **START time** labels (e.g., PERIOD_17_30 = 17:30-18:00). Tesla Sync automatically aligns these conventions so prices match exactly what you see in the Amber app.

#### 2. **5-Minute Averaging**
- **Recent/Current Prices:** Amber provides 5-minute actual intervals with high precision
- **Conversion:** Tesla Sync averages six 5-minute intervals into each 30-minute Tesla period
- **Result:** More accurate pricing that captures real market volatility
- **Example:** Period 20:00-20:30 averages prices from 20:05, 20:10, 20:15, 20:20, 20:25, 20:30

#### 3. **Rolling 24-Hour Window**
Tesla requires a static 24-hour tariff structure, but Tesla Sync makes it "roll" forward:
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
- **Sydney/Melbourne (AEDT UTC+11:00):** DST in summer
- **Adelaide (ACDT UTC+10:30):** Unique 30-minute offset + DST
- **Perth (AWST UTC+8:00):** No DST
- **Darwin (ACST UTC+9:30):** No DST

The system automatically extracts timezone information from Amber's API data, ensuring correct "past vs future" period detection for all locations.

#### 5. **Precision Matching**
Prices are rounded to **4 decimal places** with trailing zeros automatically removed:
- `0.2014191` → `0.2014` (4 decimals)
- `0.1990000` → `0.199` (3 decimals, trailing zeros dropped)

### Sync Frequency

**Every 5 minutes** - Perfectly aligned with Amber Electric's forecast update schedule for maximum freshness.

## Installation Options

Tesla Sync is available in two deployment options:

1. **[Home Assistant Integration](#home-assistant-integration)** - Native HA custom integration (Recommended for HA users)
2. **[Docker Application](#docker-deployment)** - Standalone web app with dashboard

---

## Home Assistant Integration

The easiest way to use Tesla Sync if you're already running Home Assistant.

### Features

- ✅ **Native HA Integration** - Seamless integration with your Home Assistant instance
- ✅ **HACS Installation** - Install and update via HACS (Home Assistant Community Store)
- ✅ **Automatic Discovery** - Auto-discovers Tesla energy sites from Teslemetry
- ✅ **Real-time Sensors** - Amber pricing and Tesla energy data as HA sensors
- ✅ **Automatic TOU Sync** - Background syncing every 5 minutes (just like Docker version)
- ✅ **Manual Services** - On-demand sync services for advanced automation
- ✅ **No External Services** - Runs entirely within Home Assistant

### Prerequisites

- Home Assistant installed and running
- HACS (Home Assistant Community Store) installed
- Amber Electric account with API token (https://app.amber.com.au/developers)
- **Tesla API access (choose one):**
  - **Option 1 (Recommended):** Teslemetry account with API token (https://teslemetry.com) - ~$4/month
  - **Option 2 (Free alternative):** Tesla Fleet API (direct OAuth with Tesla) - Completely free

### Tesla API Options

Tesla Sync supports two methods for accessing your Tesla Powerwall. Choose whichever works best for you.

#### Option 1: Teslemetry (Recommended - ~$4/month)

**Pros:**
- ✅ Simple API key authentication
- ✅ No OAuth setup required
- ✅ Works with both Docker and Home Assistant deployments
- ✅ Configurable via web GUI (Flask) or integration setup (HA)
- ✅ Reliable and well-maintained proxy service
- ✅ Fastest setup (2 minutes)

**Setup:**
1. Sign up at https://teslemetry.com
2. Connect your Tesla account
3. Copy your API key
4. Enter it during Tesla Sync setup

**Best for:** Users who want a simple, reliable setup and don't mind a small monthly fee.

#### Option 2: Tesla Fleet API (FREE)

**Pros:**
- ✅ Completely free (no monthly costs)
- ✅ Direct Tesla API access via OAuth
- ✅ Works with both Docker and Home Assistant deployments
- ✅ Automatic token refresh
- ✅ Built-in Cloudflare Tunnel for easy setup (no port forwarding needed)

**Cons:**
- ⚠️ Requires OAuth app registration with Tesla
- ⚠️ Slightly more setup steps than Teslemetry

**Setup - Home Assistant:**
1. Install the official "Tesla Fleet" integration in Home Assistant
   - Go to Settings → Devices & Services → Add Integration
   - Search for "Tesla Fleet"
   - Follow the OAuth login flow
2. Tesla Sync will automatically detect and use your Tesla Fleet credentials
3. During Tesla Sync setup, **leave the Teslemetry token field empty**
4. The integration will display: "Tesla Fleet integration detected! You can leave this field empty to use your existing Tesla Fleet OAuth tokens (free)"

**Setup - Docker/Flask:**
1. Register an OAuth application at https://developer.tesla.com
   - Set your Redirect URI to: `http://localhost:5001/fleet-api/callback` (or your server's URL)
2. Get your Client ID and Client Secret from the Tesla Developer Portal
3. Configure everything via the Settings page in the Flask web GUI (no .env needed!):
   - Select "Tesla Fleet API (Direct, Free)" as your Tesla API Provider
   - Enter your Client ID in the API Configuration section
   - Enter your Client Secret
   - Enter your Redirect URI (e.g., `http://localhost:5001/fleet-api/callback`)
   - Click "Save Settings" - credentials are encrypted and stored securely
   - Click "Connect to Tesla Fleet API" to complete OAuth authorization
4. See [TESLA_FLEET_SETUP.md](docs/TESLA_FLEET_SETUP.md) for detailed OAuth app registration

**Note:** All Fleet API credentials are now configurable via GUI - no environment variables required!

**Best for:** Users who want to avoid recurring costs and are comfortable with OAuth setup.

### Installation Steps

1. **Install via HACS**
   - Open HACS in Home Assistant
   - Click the three dots in the top right
   - Select "Custom repositories"
   - Add repository URL: `https://github.com/bolagnaise/tesla-sync`
   - Category: `Integration`
   - Click "Add"
   - Click "Download" on the Tesla Sync integration
   - Restart Home Assistant

2. **Add Integration**
   - Go to Settings → Devices & Services
   - Click "+ Add Integration"
   - Search for "Tesla Sync"
   - Click to add

3. **Configure**
   - **Step 1: Amber Electric**
     - Enter your Amber API token
     - Get token from: https://app.amber.com.au/developers

   - **Step 2: Tesla API**
     - **Using Teslemetry (Recommended):** Enter your Teslemetry API token
       - Get token from: https://teslemetry.com
     - **Using Tesla Fleet (Optional):** Leave the Teslemetry token field empty
       - If you have Tesla Fleet configured, the integration will automatically detect it
       - You'll see: "Tesla Fleet integration detected! You can leave this field empty to use your existing Tesla Fleet OAuth tokens (free)"
       - Your existing Tesla Fleet OAuth tokens will be used automatically

   - **Step 3: Site Selection**
     - Select your Tesla energy site from the dropdown
     - Select Amber site (if you have multiple)
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
service: tesla_amber_sync.sync_tou_schedule

# Refresh data from Amber and Teslemetry
service: tesla_amber_sync.sync_now
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
      - service: tesla_amber_sync.sync_tou_schedule
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
          entity_id: switch.tesla_amber_sync_auto_sync

  - alias: "Enable Auto-Sync in Morning"
    trigger:
      - platform: time
        at: "06:00:00"
    action:
      - service: switch.turn_on
        target:
          entity_id: switch.tesla_amber_sync_auto_sync
```

### Pre-built Dashboard (Optional)

A pre-built Lovelace dashboard is included for visualizing all Tesla Amber Sync data.

**Required HACS Frontend Cards:**
- `power-flow-card-plus` - Real-time energy flow visualization
- `apexcharts-card` - Advanced charting for price/energy history

**Installation:**
1. Install the required HACS cards (HACS → Frontend → search for each card)
2. Copy the dashboard YAML from `custom_components/tesla_amber_sync/dashboard/tesla_amber_sync_dashboard.yaml`
3. In Home Assistant: Settings → Dashboards → Add Dashboard → "New dashboard from scratch"
4. Edit the new dashboard → 3 dots menu → "Raw configuration editor"
5. Paste the YAML content and save

**Dashboard Features:**
- Current price gauge with color-coded severity
- Battery level gauge
- Power flow visualization (solar → battery → grid → home)
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
curl -O https://raw.githubusercontent.com/bolagnaise/tesla-sync/main/docker/docker-compose.hub.yml
curl -O https://raw.githubusercontent.com/bolagnaise/tesla-sync/main/.env.example
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
git clone https://github.com/bolagnaise/tesla-sync.git
cd tesla-sync
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

## Tesla API Authentication

This application supports two methods for Tesla API access:

### Option 1: Teslemetry (Recommended - Easier Setup)

Teslemetry is a third-party proxy service for Tesla API.

**Pros:**
- ✅ Simple setup (2 minutes)
- ✅ Works with localhost
- ✅ Configurable via web GUI
- ✅ Reliable and well-maintained
- ✅ ~$4/month (reasonable for convenience)

**Setup:**
1. Sign up at https://teslemetry.com
2. Connect your Tesla account
3. Copy your API key
4. Paste into Settings page in dashboard

### Option 2: Tesla Fleet API (Free)

Direct OAuth access to Tesla's Fleet API with built-in tunnel support.

**Pros:**
- ✅ Completely free
- ✅ Direct API access
- ✅ Configurable via web GUI
- ✅ Built-in Cloudflare Tunnel (no port forwarding or public domain needed)
- ✅ Step-by-step guided setup in the UI

**Setup:**
1. Register OAuth app at https://developer.tesla.com
2. In Settings, click "Install" to auto-download cloudflared (or it's pre-installed in Docker)
3. Start the Cloudflare tunnel to get a public URL
4. Update Tesla Developer Portal with the tunnel URL
5. Enter Client ID and Client Secret in Settings
6. Generate keys and register domain (one-click buttons)
7. Authorize with Tesla
8. See [TESLA_FLEET_SETUP.md](docs/TESLA_FLEET_SETUP.md) for details

## Configuration

### Required Credentials

1. **Amber Electric API Token**
   - Get from: https://app.amber.com.au/developers
   - Used for: Fetching real-time electricity prices
   - Configure: Via Settings page in dashboard

2. **Tesla API Access** (choose one):
   - **Option A - Teslemetry API Key** (Recommended)
     - Get from: https://teslemetry.com
     - Used for: Tesla Powerwall communication
     - Configure: Via Settings page in dashboard

   - **Option B - Tesla Fleet API OAuth** (Free)
     - Get from: https://developer.tesla.com (register OAuth app)
     - Used for: Direct Tesla Fleet API access
     - Configure: Via Settings page in dashboard

3. **Tesla Energy Site ID**
   - Your Powerwall/Solar site ID
   - Find in: Teslemetry dashboard or Tesla app
   - Configure: Via Settings page in dashboard

### Dashboard Setup

After logging in, go to the Settings page:

1. **Configure Amber Electric**
   - Enter your Amber API token
   - Save settings

2. **Choose Tesla API Method**
   - Select either "Teslemetry" or "Tesla Fleet API" from dropdown

   **If using Teslemetry:**
   - Enter your Teslemetry API key

   **If using Tesla Fleet API:**
   - Enter your Fleet API Client ID
   - Enter your Fleet API Client Secret
   - Enter your Fleet API Redirect URI (e.g., `http://localhost:5001/fleet-api/callback`)

3. **Set Energy Site ID**
   - Enter your Tesla energy site ID
   - Save settings

4. **Verify Connection**
   - Check API status indicators turn green
   - View current prices and battery status

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

**"Teslemetry API connection failed"**
- Verify API key is correct
- Check that your Tesla account is connected to Teslemetry
- Ensure API key has proper permissions

**"No energy sites found"**
- Verify your Tesla Powerwall/Solar is connected to your Tesla account
- Check Teslemetry dashboard to confirm site is visible
- Ensure site is properly commissioned in Tesla app
- Check public key is accessible externally

### Logs

Check Flask logs for detailed errors:
```bash
tail -f flask.log
```

Enable debug mode for more details:
```bash
export FLASK_DEBUG=1
flask run
```

## Documentation

- **[UNRAID_SETUP.md](docs/UNRAID_SETUP.md)** - Complete Unraid deployment guide
- **[TESLA_FLEET_SETUP.md](docs/TESLA_FLEET_SETUP.md)** - Complete Tesla Fleet API setup guide
- **[CLAUDE.md](docs/CLAUDE.md)** - Development guide for Claude Code
- **[Docker Hub](https://hub.docker.com/r/bolagnaise/tesla-sync)** - Pre-built container images
- **[GitHub Actions](https://github.com/bolagnaise/tesla-sync/actions)** - Automated build status
- **Tesla Developer Docs:** https://developer.tesla.com/docs/fleet-api
- **Amber API Docs:** https://api.amber.com.au/docs

## License

MIT

## Support

For issues or questions:
1. Review Flask logs for error details
2. Verify Teslemetry API key is correct
3. Check Teslemetry dashboard for connection status
4. Ensure Tesla Energy Site ID is correct

## Contributing

Contributions welcome! Please:
1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Submit a pull request

---

**Made with ⚡ by combining Tesla Powerwall optimization with Amber Electric dynamic pricing**
