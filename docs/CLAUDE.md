# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Tesla Sync is a Flask web application and Home Assistant integration that synchronizes Tesla Powerwall energy management with dynamic electricity pricing (Amber Electric). It automatically updates Tesla's Time-of-Use tariffs based on real-time prices, enables AEMO spike detection for VPP participation, and provides solar curtailment during negative pricing periods.

## Architecture

### Application Factory Pattern
The app uses Flask's application factory pattern in `app/__init__.py`:
- `create_app()` initializes Flask extensions (SQLAlchemy, Flask-Migrate, Flask-Login)
- Database, migrations, and login manager are initialized separately and bound to the app instance
- Blueprints registered: `main` (routes), `custom_tou` (custom TOU schedules)

### Database & Models
- **ORM**: SQLAlchemy with Flask-SQLAlchemy
- **Database**: SQLite (`data/app.db`)
- **Migrations**: Flask-Migrate (Alembic) in `migrations/` directory
- **Key Models** (`app/models.py`):
  - `User` - User accounts with encrypted API credentials
  - `PriceRecord` - Historical price data
  - `CustomTOUSchedule`, `TOUSeason`, `TOUPeriod` - Custom TOU schedules

### Authentication & Security
- Flask-Login handles user sessions
- Passwords hashed using Werkzeug
- API tokens encrypted at rest using Fernet symmetric encryption
- Encryption key auto-generated on first run, stored in `data/.fernet_key`
- Encryption/decryption utilities in `app/utils.py`

### Tesla API Integration
The app supports two Tesla API methods (`app/api_clients.py`):

1. **Teslemetry** (Recommended)
   - Simple API key authentication
   - ~$4/month proxy service
   - `TeslemetryAPIClient` class

2. **Tesla Fleet API** (Free)
   - Direct OAuth with Tesla
   - Requires developer app registration
   - `FleetAPIClient` class

**Client Selection:**
- `get_tesla_client()` returns appropriate client based on user config
- Tries Fleet API first (if configured), falls back to Teslemetry

### Key Components
```
app/
├── __init__.py          # App factory, extensions
├── models.py            # User, PriceRecord, CustomTOU models
├── routes.py            # Main blueprint routes
├── custom_tou_routes.py # Custom TOU blueprint
├── api_clients.py       # Amber, Tesla (Fleet + Teslemetry) clients
├── utils.py             # Encryption, key generation
├── scheduler.py         # Background TOU sync (APScheduler)
├── tariff_converter.py  # Amber → Tesla format conversion
├── tasks.py             # Background tasks (sync, curtailment, spikes)
└── templates/           # Jinja2 templates

custom_components/tesla_sync/  # Home Assistant integration
├── __init__.py          # HA setup, services
├── coordinator.py       # Data coordinator
├── sensor.py            # HA sensor entities
├── switch.py            # Auto-sync switch
├── websocket_client.py  # Amber WebSocket client
└── manifest.json        # HA integration manifest
```

## Development Commands

### Setup
```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Edit .env with SECRET_KEY (encryption key auto-generated)
```

### Database
```bash
flask db migrate -m "Description"  # Create migration
flask db upgrade                   # Apply migrations
flask db downgrade                 # Rollback
```

### Running
```bash
# Development
flask run

# Production
gunicorn -w 4 -b 0.0.0.0:5001 run:app
```

### Flask Shell
```bash
flask shell
# Available: db, User, PriceRecord
```

## Environment Variables

**Required:**
- `SECRET_KEY` - Flask session secret

**Auto-generated:**
- `FERNET_ENCRYPTION_KEY` - Created on first run, saved to `data/.fernet_key`

**Optional (can configure via web UI instead):**
- `TESLA_CLIENT_ID` - Tesla Fleet OAuth client ID
- `TESLA_CLIENT_SECRET` - Tesla Fleet OAuth client secret
- `TESLA_REDIRECT_URI` - Tesla OAuth callback URL
- `APP_DOMAIN` - Application domain for OAuth

## Key Features

### TOU Sync
- Fetches Amber prices every 5 minutes
- Converts to Tesla TOU format via `tariff_converter.py`
- Uploads to Powerwall via Tesla API
- Smart deduplication prevents duplicate uploads

### AEMO Spike Detection
- Monitors AEMO wholesale prices for configured region
- When price exceeds threshold, uploads spike tariff to encourage export
- Saves/restores original tariff and operation mode
- 1-minute check interval

### Solar Curtailment
- Monitors feed-in prices every minute
- Sets export rule to "never" during negative prices (≤0c/kWh)
- Restores to "battery_ok" when prices return positive
- Includes workaround for Tesla API bug (toggle to force apply)

## Home Assistant Integration

Located in `custom_components/tesla_sync/`:
- Uses Amber WebSocket for real-time price updates
- Supports both Teslemetry and Tesla Fleet API
- Creates sensors for prices, energy flow, battery status
- Auto-sync switch for TOU schedule updates

## Important Notes

- Database and encryption key stored in `data/` directory
- Always backup both `data/app.db` AND `data/.fernet_key` together
- Tesla API credentials can be configured via web UI or environment variables
- Docker images: `bolagnaise/tesla-sync` on Docker Hub
