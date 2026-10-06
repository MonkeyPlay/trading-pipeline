#!/bin/bash
# scripts/fan_forward.sh
# The forward record of the intermarket fan experiment (docs/fan_experiment.md, chunk 8): collects the session in
# progress for the instruments the frozen model reads (NQ, VXN) on its own IB client id (IB_CLIENT_ID + 2, so it never
# meets the dashboard's collector or the live capture), then issues the forecast for the current mark - every 15
# minutes from the 18:00 ET open, and at 09:29 ET. The feed is delayed (NQ about 11 minutes, VXN about 16), so each
# run issues every mark of the last 30 minutes whose origin bar is now stored - from the bars closed by that mark
# only - and says once when a mark's bar is still missing. Outside a session it does nothing. Logs to
# logs/fan_forward.log.
#
#   */15 * * * *  /path/to/trading-pipeline/scripts/fan_forward.sh

set -uo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_FILE="$PROJECT_DIR/logs/fan_forward.log"
mkdir -p "$PROJECT_DIR/logs"
cd "$PROJECT_DIR"
# shellcheck disable=SC1091
source .venv/bin/activate

log() { echo "[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] $*" >> "$LOG_FILE"; }

sleep 10                                       # the bar that closed at the mark reaches IB first
read -r HOST PORT CLIENT < <(python3 -c "from config import Config; print(Config.IB_HOST, Config.IB_PORT, Config.IB_CLIENT_ID + 2)")
log "Collecting NQ, VXN for the forward record..."
python3 -m collector.ib_collector --days 0 --symbol NQ,VXN --host "$HOST" --port "$PORT" --client-id "$CLIENT" \
    >> "$LOG_FILE" 2>&1 || log "collection failed"
python3 scripts/fan.py forward issue >> "$LOG_FILE" 2>&1 || log "not issued (see above)"
