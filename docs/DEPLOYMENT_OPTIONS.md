# Tesla Sync - Deployment Options

## Overview

Tesla Sync is available in two deployment options:

1. **Flask Web App** (Docker/Unraid) - Standalone web application with dashboard
2. **Home Assistant Integration** (HACS) - Native HA integration

You can use **either or both** depending on your needs.

---

## Option 1: Flask Web App (Docker)

### Architecture
- **Standalone Flask web server** with SQLite database
- **Background scheduler** for automatic TOU syncing
- **Energy charts** with Chart.js visualization
- **Web dashboard** accessible via browser

### Deployment

#### Docker Compose (Recommended)
```yaml
services:
  tesla-sync:
    image: bolagnaise/tesla-sync:latest
    container_name: tesla-sync
    ports:
      - "5001:5001"
    volumes:
      - ./data:/app/data
    environment:
      - SECRET_KEY=your-secret-key
    restart: unless-stopped
```

> **Note:** Encryption key is auto-generated on first run. No need to set `FERNET_ENCRYPTION_KEY`.

#### Unraid
See [UNRAID_SETUP.md](UNRAID_SETUP.md) for detailed instructions.

### Features
- Web UI with login/authentication
- Real-time energy flow charts
- Historical price and energy tracking
- 24-hour energy usage graphs
- Auto-sync toggle and manual sync button
- AEMO spike detection
- Solar curtailment
- Custom TOU schedules

### When to Use
- You want detailed energy visualization
- You don't use Home Assistant
- You prefer a dedicated web interface
- You're comfortable with Docker/Unraid

### Access
- Web UI: `http://your-server:5001`
- Login with registered email/password

---

## Option 2: Home Assistant Integration

### Architecture
- **Custom integration** inside Home Assistant
- **Data coordinators** for Amber and Tesla data
- **Native HA entities** (sensors, switches)
- **No web server** - uses HA's UI

### Deployment

#### HACS Installation (Recommended)
1. Add custom repository to HACS: `https://github.com/bolagnaise/tesla-sync`
2. Install "Tesla Sync"
3. Restart Home Assistant
4. Configure via UI

#### Manual Installation
1. Copy `custom_components/tesla_sync/` to HA config
2. Restart Home Assistant
3. Add integration via Settings → Devices & Services

### Features
- Native HA sensor entities
- Auto-sync switch
- Real-time price monitoring via WebSocket
- Energy flow sensors
- HA automations support
- Energy Dashboard integration
- AEMO spike detection
- Solar curtailment

### When to Use
- You use Home Assistant for automation
- You want native HA entities
- You want to trigger automations based on prices
- You want to avoid running a separate service

### Access
- Settings → Devices & Services → Tesla Sync
- View entities in HA UI
- Use in Lovelace dashboards

---

## Comparison Table

| Feature | Flask Web App | HA Integration |
|---------|--------------|----------------|
| **Deployment** | Docker/Unraid | Inside Home Assistant |
| **Web Interface** | Dedicated web UI | Home Assistant UI |
| **Energy Charts** | Built-in Chart.js | Requires Lovelace card |
| **Authentication** | Separate login | Home Assistant login |
| **Database** | SQLite | Uses HA's storage |
| **Auto-Sync** | Yes | Yes |
| **Manual Sync** | Button | Service call |
| **AEMO Spike Detection** | Yes | Yes |
| **Solar Curtailment** | Yes | Yes |
| **Automations** | No | Native HA automations |
| **Dependencies** | Docker only | Home Assistant |
| **Updates** | Docker pull | HACS |

---

## Running Both (Hybrid Setup)

You can run both deployments simultaneously for maximum flexibility.

### Important Configuration
**To avoid conflicts:**

1. **Disable auto-sync on one deployment**
   - Either Flask app OR HA integration should have auto-sync enabled
   - Not both simultaneously
   - This prevents duplicate TOU updates

2. **Use different update intervals** (if both have auto-sync)
   - Flask: Every 5 minutes
   - HA: Every 10 minutes (stagger them)

### Recommended Hybrid Configuration

**Flask App (Docker)**:
- Enable auto-sync: **YES**
- Use for: Charts, monitoring, historical data

**HA Integration**:
- Enable auto-sync: **NO** (to avoid duplicates)
- Use for: Automations, dashboards, real-time sensors

---

## Decision Guide

### Choose Flask Web App If:
- You don't use Home Assistant
- You want detailed energy charts
- You prefer a dedicated web interface

### Choose HA Integration If:
- You use Home Assistant
- You want native HA entities
- You want HA automations

### Choose Both If:
- You want the best of both worlds
- You can manage disabling auto-sync on one

---

## System Requirements

### Flask Web App
- **CPU**: 1 core
- **RAM**: 512 MB
- **Storage**: 1 GB
- **Network**: Internet access
- **Platform**: Docker, Unraid, any Linux

### HA Integration
- **Home Assistant**: 2024.8.0 or newer
- **Additional RAM**: ~50 MB
- **Storage**: Minimal
- **Network**: Internet access

---

## Support & Updates

### Flask Web App
- **Docker Hub**: `bolagnaise/tesla-sync`
- **Docs**: README.md, UNRAID_SETUP.md
- **Issues**: GitHub Issues

### HA Integration
- **Updates**: HACS (automatic notifications)
- **Docs**: HA_README.md
- **Issues**: GitHub Issues
