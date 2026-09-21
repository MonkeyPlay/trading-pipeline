#!/bin/bash
# scripts/run_pipeline.sh
# Automates the scheduled daily execution of the NQ Opening Forecast System.
# Intended to run before the 09:30 AM NY equity open (e.g. 09:15 AM NY / 13:15 UTC).

set -euo pipefail

PROJECT_DIR="/home/monkeyplay/trading_pipeline"
LOG_DIR="$PROJECT_DIR/logs"
LOG_FILE="$LOG_DIR/pipeline_run.log"
EXPIRY="${NQ_EXPIRY:-202609}"
DB_PATH="data/trading_pipeline.db"

mkdir -p "$LOG_DIR"
cd "$PROJECT_DIR"

# shellcheck disable=SC1091
source .venv/bin/activate

log() { echo "[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] $*" >> "$LOG_FILE"; }

log "Starting NQ opening forecast automation (expiry $EXPIRY)..."

# 1. Collect recent 1-minute bars from IB Gateway/TWS.
log "Fetching recent candles from IB Gateway..."
python3 -m collector.ib_collector --days 5 --expiry "$EXPIRY" --db "$DB_PATH" >> "$LOG_FILE" 2>&1

# 2. Compute features, run the forecast, evaluate prior outcomes, and persist everything.
log "Running feature + forecast + evaluation pipeline..."
python3 scripts/daily_forecast.py --expiry "$EXPIRY" --db "$DB_PATH" >> "$LOG_FILE" 2>&1

log "Opening forecast pipeline execution completed successfully!"
