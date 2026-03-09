#!/usr/bin/env bash
# 99Cents Bot — Deploy script for Ubuntu 22.04 VPS
# Usage: sudo ./deploy/deploy.sh
#
# Steps: pull code → install deps → health check → restart service → confirm

set -euo pipefail

APP_DIR="/opt/99cents"
SERVICE_NAME="99cents"
VENV_DIR="$APP_DIR/.venv"
REPO_URL=""  # Set this to your git repo URL if using git pull

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log() { echo -e "${GREEN}[deploy]${NC} $1"; }
warn() { echo -e "${YELLOW}[deploy]${NC} $1"; }
fail() { echo -e "${RED}[deploy]${NC} $1"; exit 1; }

# -------------------------------------------------------------------
# 1. Check prerequisites
# -------------------------------------------------------------------
log "Checking prerequisites..."

if [ "$(id -u)" -ne 0 ]; then
    fail "Must run as root (sudo ./deploy/deploy.sh)"
fi

if ! command -v python3 &>/dev/null; then
    fail "python3 not found. Install with: apt install python3 python3-venv"
fi

# -------------------------------------------------------------------
# 2. Create app directory and user if needed
# -------------------------------------------------------------------
if ! id -u botuser &>/dev/null; then
    log "Creating botuser system account..."
    useradd --system --home-dir "$APP_DIR" --shell /usr/sbin/nologin botuser
fi

mkdir -p "$APP_DIR"

# -------------------------------------------------------------------
# 3. Pull latest code
# -------------------------------------------------------------------
if [ -d "$APP_DIR/.git" ]; then
    log "Pulling latest code..."
    cd "$APP_DIR"
    git pull --ff-only
elif [ -n "$REPO_URL" ]; then
    log "Cloning repository..."
    git clone "$REPO_URL" "$APP_DIR"
    cd "$APP_DIR"
else
    # Sync from current directory (local deploy)
    log "Syncing code from local directory..."
    SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
    rsync -av --exclude='.venv' --exclude='.env' --exclude='__pycache__' \
        --exclude='*.pyc' --exclude='bot.log*' --exclude='data/blacklist.json' \
        "$SCRIPT_DIR/" "$APP_DIR/"
fi

cd "$APP_DIR"

# -------------------------------------------------------------------
# 4. Check .env exists
# -------------------------------------------------------------------
if [ ! -f "$APP_DIR/.env" ]; then
    if [ -f "$APP_DIR/.env.example" ]; then
        warn ".env not found. Creating from .env.example — edit it before going live!"
        cp "$APP_DIR/.env.example" "$APP_DIR/.env"
        chmod 600 "$APP_DIR/.env"
    else
        fail ".env file not found at $APP_DIR/.env"
    fi
fi

# -------------------------------------------------------------------
# 5. Create/update virtual environment and install dependencies
# -------------------------------------------------------------------
log "Setting up Python virtual environment..."
if [ ! -d "$VENV_DIR" ]; then
    python3 -m venv "$VENV_DIR"
fi

log "Installing dependencies..."
"$VENV_DIR/bin/pip" install --quiet --upgrade pip
"$VENV_DIR/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"

# -------------------------------------------------------------------
# 6. Create runtime directories
# -------------------------------------------------------------------
mkdir -p "$APP_DIR/data" "$APP_DIR/report"

# -------------------------------------------------------------------
# 7. Fix ownership
# -------------------------------------------------------------------
log "Setting file ownership..."
chown -R botuser:botuser "$APP_DIR"
chmod 600 "$APP_DIR/.env"

# -------------------------------------------------------------------
# 8. Install systemd service
# -------------------------------------------------------------------
log "Installing systemd service..."
cp "$APP_DIR/deploy/99cents.service" /etc/systemd/system/99cents.service
systemctl daemon-reload
systemctl enable "$SERVICE_NAME"

# -------------------------------------------------------------------
# 9. Install logrotate config
# -------------------------------------------------------------------
if [ -f "$APP_DIR/deploy/logrotate.conf" ]; then
    log "Installing logrotate config..."
    cp "$APP_DIR/deploy/logrotate.conf" /etc/logrotate.d/99cents
fi

# -------------------------------------------------------------------
# 10. Run pre-flight health check
# -------------------------------------------------------------------
log "Running pre-flight health check..."
if sudo -u botuser "$VENV_DIR/bin/python" -m bot.preflight_check "$APP_DIR/config.yaml"; then
    log "Pre-flight checks passed"
else
    fail "Pre-flight checks failed — not starting service"
fi

# -------------------------------------------------------------------
# 11. Restart service
# -------------------------------------------------------------------
log "Restarting service..."
systemctl restart "$SERVICE_NAME"
sleep 3

# -------------------------------------------------------------------
# 12. Confirm running
# -------------------------------------------------------------------
if systemctl is-active --quiet "$SERVICE_NAME"; then
    log "Service is running"
    systemctl status "$SERVICE_NAME" --no-pager -l
    echo ""
    log "Deploy complete. Monitor with:"
    log "  journalctl -u $SERVICE_NAME -f"
    log "  tail -f $APP_DIR/bot.log"
else
    fail "Service failed to start. Check: journalctl -u $SERVICE_NAME -e"
fi
