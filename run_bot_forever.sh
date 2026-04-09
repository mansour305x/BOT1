#!/usr/bin/env bash
set -u

cd "$(dirname "$0")"

LOG_FILE="bot.log"
RESTART_DELAY_SECONDS=1

echo "[$(date '+%Y-%m-%d %H:%M:%S')] supervisor started" >> "$LOG_FILE"

while true; do
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] starting bot process" >> "$LOG_FILE"
  python bot.py >> "$LOG_FILE" 2>&1
  exit_code=$?
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] bot stopped (exit_code=${exit_code}), restarting in ${RESTART_DELAY_SECONDS}s" >> "$LOG_FILE"
  sleep "$RESTART_DELAY_SECONDS"
done
