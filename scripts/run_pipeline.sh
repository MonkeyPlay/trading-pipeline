#!/bin/bash
# scripts/run_pipeline.sh
# Automates the scheduled daily data collection.
# Intended to run before the 09:30 AM NY equity open (e.g. 09:15 AM NY / 13:15 UTC).
#
# Covers every instrument in SYMBOLS (default ES,NQ,RTY), plus CONTEXT_SYMBOLS
# (default VIX,VXN,TNX,DX,SMH,10Y,2YY) which is collected as intermarket context.
# The collector takes the whole list in one process, so its IB rate-limit pacer
# accounts for all symbols together.

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

log "Starting daily collection..."

# Collect recent 1-minute bars from IB Gateway/TWS, for the target futures and
# the context instruments alike. Symbols and roll rules come from config.py.
log "Fetching recent candles from IB Gateway..."
python3 -m collector.ib_collector --days 5 >> "$LOG_FILE" 2>&1

log "Daily collection completed."
