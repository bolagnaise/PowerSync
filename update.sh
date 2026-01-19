#!/bin/bash
# Power Sync Update Script
# Run this script to pull the latest changes and apply database migrations
#
# Usage:
#   ./update.sh          # Pull and update
#   ./update.sh --force  # Force pull (discard local changes)
#
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "========================================"
echo "  Power Sync Update Script"
echo "========================================"
echo ""

# Check for --force flag
FORCE=false
if [ "$1" == "--force" ]; then
    FORCE=true
    echo "⚠️  Force mode enabled - local changes will be discarded"
fi

# Activate virtual environment if it exists
if [ -d "venv" ]; then
    echo "📦 Activating virtual environment..."
    source venv/bin/activate
fi

# Check for uncommitted changes
if [ "$FORCE" == "false" ]; then
    if ! git diff-index --quiet HEAD -- 2>/dev/null; then
        echo "⚠️  You have uncommitted changes. Commit or stash them first."
        echo "   Or use: ./update.sh --force"
        exit 1
    fi
fi

# Pull latest changes
echo ""
echo "📥 Pulling latest changes from git..."
if [ "$FORCE" == "true" ]; then
    git fetch origin
    git reset --hard origin/main
else
    git pull origin main
fi

# Install/update dependencies
echo ""
echo "📦 Updating Python dependencies..."
pip install -r requirements.txt --quiet

# Run database migrations
echo ""
echo "🗄️  Running database migrations..."
export FLASK_APP=run.py
flask db upgrade

# Restart services if they're running
echo ""
echo "🔄 Restarting services..."

# Check if Flask service exists and is running
if systemctl is-active --quiet powersync 2>/dev/null; then
    echo "   Restarting powersync service..."
    sudo systemctl restart powersync
    echo "   ✓ powersync restarted"
elif systemctl is-active --quiet power-sync 2>/dev/null; then
    echo "   Restarting power-sync service..."
    sudo systemctl restart power-sync
    echo "   ✓ power-sync restarted"
fi

# Check if OCPP service exists and is running
if systemctl is-active --quiet powersync-ocpp 2>/dev/null; then
    echo "   Restarting powersync-ocpp service..."
    sudo systemctl restart powersync-ocpp
    echo "   ✓ powersync-ocpp restarted"
fi

echo ""
echo "========================================"
echo "  ✅ Update complete!"
echo "========================================"
echo ""
echo "Check the logs with:"
echo "  journalctl -u powersync -f"
echo "  journalctl -u powersync-ocpp -f"
echo ""
