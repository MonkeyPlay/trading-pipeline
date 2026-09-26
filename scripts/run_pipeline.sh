#!/bin/bash
# scripts/run_pipeline.sh
# Automates the scheduled daily execution of the Opening Forecast System.
# Intended to run before the 09:30 AM NY equity open (e.g. 09:15 AM NY / 13:15 UTC); it must
# start early enough for the live v2 forecast (step 3) to train before 09:29 ET.
#
# Covers every instrument in SYMBOLS (default ES,NQ,RTY), plus CONTEXT_SYMBOLS
# (default VIX,VXN,TNX,DX,SMH,10Y,2YY) which is collected as intermarket context
# but never forecast. Both steps take the whole list in one process: the collector
# so its IB rate-limit pacer accounts for all symbols together, the forecast so
# one instrument's missing data does not suppress the others.

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_DIR="$PROJECT_DIR/logs"
LOG_FILE="$LOG_DIR/pipeline_run.log"

mkdir -p "$LOG_DIR"
cd "$PROJECT_DIR"

if [ ! -f .venv/bin/activate ]; then
    echo "ERROR: no virtualenv at $PROJECT_DIR/.venv - see README first-time setup." >&2
    exit 1
fi
# shellcheck disable=SC1091
source .venv/bin/activate

log() { echo "[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] $*" >> "$LOG_FILE"; }

log "Starting opening forecast automation..."

# 0. The v2 forecast freezes its snapshot at 09:29 ET from the 09:28 bars, which only the
# real-time streamer stores in time. Start it (until 09:31 ET) unless one already runs,
# e.g. from its own cron line; it uses its own IB client id beside the collector.
if ! pgrep -f "collector.live_stream" >/dev/null; then
    log "Starting the real-time bar streamer..."
    python3 -m collector.live_stream >> "$LOG_DIR/live_stream.log" 2>&1 &
fi

# 1. Collect recent 1-minute bars from IB Gateway/TWS, for the forecast targets and
# the context instruments alike. Symbols and roll rules come from config.py.
log "Fetching recent candles from IB Gateway..."
python3 -m collector.ib_collector --days 5 >> "$LOG_FILE" 2>&1

# 2. v1: features, analogue forecast, outcomes of prior sessions (SYMBOLS only).
log "Running the analogue forecast pipeline..."
python3 scripts/daily_forecast.py >> "$LOG_FILE" 2>&1 || log "WARNING: analogue forecast failed."

# 3. v2: label the sessions that have closed since the last run, so the model trains on
# them, then the live NQ forecast: it trains first, waits for T = 09:29 ET, freezes the
# snapshot and stores the forecast before 09:30.
RECENT="$(python3 -c 'import datetime as d; print(d.date.today() - d.timedelta(days=10))')"
TODAY="$(python3 -c 'import datetime as d; print(d.date.today())')"
log "Recording v2 outcomes since $RECENT..."
python3 scripts/nq_forecast_v2.py outcomes --start "$RECENT" --end "$TODAY" >> "$LOG_FILE" 2>&1 \
    || log "WARNING: recording v2 outcomes failed."
log "Running the live v2 model forecast..."
python3 scripts/nq_forecast_v2.py live >> "$LOG_FILE" 2>&1 || log "WARNING: live v2 forecast failed."

log "Opening forecast pipeline execution completed."
