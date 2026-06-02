#!/usr/bin/env bash
# ============================================================
# fintrack daily runner -- Linux / Raspberry Pi
# Add to crontab with: crontab -e
#   0 7 * * * /path/to/fintrack/scripts/daily.sh >> /path/to/fintrack/logs/daily.log 2>&1
# ============================================================

set -e
FINTRACK_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$FINTRACK_DIR"
mkdir -p logs

echo "[$(date -u '+%Y-%m-%d %H:%M UTC')] Starting fintrack daily run"

# 1. Pull new transactions
fintrack sync

# 2. Alerts
fintrack check

# 3. Push to Google Sheets
fintrack push

echo "[$(date -u '+%Y-%m-%d %H:%M UTC')] Daily run complete"
