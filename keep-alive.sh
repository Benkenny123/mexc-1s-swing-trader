#!/bin/bash
# Keep swing trader alive - cron runs every 60s
PROJECT_DIR="/root/Documents/Codex/2026-07-02/build-me-an-autonomous-agent-with/mexc-swing-trader"
LOG_FILE="/tmp/swing-trader.log"
CRON_LOG="/tmp/swing-trader-cron.log"

if pgrep -f "python3.*live_trader.py" > /dev/null 2>&1; then
    # All good
    exit 0
fi

echo "[$(date)] Bot dead, restarting..." >> "$CRON_LOG"
cd "$PROJECT_DIR" || exit 1
nohup python3 -u live_trader.py BTCUSDT ETHUSDT --min-diff 5 --start 1000 >> "$LOG_FILE" 2>&1 &
PID=$!
echo "[$(date)] Started PID $PID" >> "$CRON_LOG"
