#!/bin/bash
# Keep mirror trader alive
PROJECT_DIR="/root/Documents/Codex/2026-07-02/build-me-an-autonomous-agent-with/mexc-swing-trader"
LOG_FILE="/tmp/mirror-trader.log"
CRON_LOG="/tmp/mirror-trader-cron.log"

if pgrep -f "python3.*mirror_trader.py" > /dev/null 2>&1; then
    exit 0
fi

echo "[$(date)] Mirror dead, restarting..." >> "$CRON_LOG"
cd "$PROJECT_DIR" || exit 1
nohup python3 -u mirror_trader.py >> "$LOG_FILE" 2>&1 &
echo "[$(date)] Started PID $!" >> "$CRON_LOG"
