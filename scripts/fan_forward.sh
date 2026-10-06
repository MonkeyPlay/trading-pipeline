#!/bin/bash
# scripts/fan_forward.sh
# The forward record of the intermarket fan experiment (docs/fan_experiment.md, chunk 8): collects the session in
# progress for the instruments the frozen model reads (NQ, VXN) on its own IB client id (IB_CLIENT_ID + 2, so it never
# meets the dashboard's collector or the live capture), then issues the forecast - every 15 minutes from the 18:00 ET
# open, and at 09:29 ET. Outside a session it does nothing. Logs to logs/fan_forward.log. Two modes, two cron lines:
#
#   0,15,29,30,45 * * * *  /path/to/trading-pipeline/scripts/fan_forward.sh mark
#   */2 * * * *            /path/to/trading-pipeline/scripts/fan_forward.sh
#
# mark      the live attempt, at each mark (minute 29 is for 09:29 ET; at any other :29 it does nothing): collects at
#           once, again every few seconds until the mark's origin bar is stored or the 60 s live deadline nears, then
#           issues that mark alone (scripts/fan.py forward mark). On a delayed feed it finds the bar missing and
#           records the mark stale - the evidence that the data, not the schedule, was late.
# catch-up  every 2 minutes (the default): issues every mark of the last 30 minutes whose origin bar is now stored,
#           from the bars closed by that mark only. On this feed (NQ about 11 minutes late, VXN about 16) that is the
#           delayed-feed evaluation: a horizon is a delayed-origin forecast only when 75 % of it remains at issuance,
#           so a 60-minute forecast must be out within 15 minutes of its mark.
#
# The two never collect at once: they share a lock (logs/fan_forward.lock) - a mark run waits up to 45 s for it, a
# catch-up run that finds it held is skipped. Every run records what triggered it and when (fan_forward.TRIGGERS), so
# `scripts/fan.py forward report` measures each against the deadline.

set -uo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_FILE="$PROJECT_DIR/logs/fan_forward.log"
LOCK_FILE="$PROJECT_DIR/logs/fan_forward.lock"
mkdir -p "$PROJECT_DIR/logs"
cd "$PROJECT_DIR"
# shellcheck disable=SC1091
source .venv/bin/activate

log() { echo "[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] $*" >> "$LOG_FILE"; }
utc_now() { date -u '+%Y-%m-%dT%H:%M:%S.%6N+00:00'; }

STARTED="$(utc_now)"
MODE="${1:-catchup}"
exec 9>"$LOCK_FILE"

if [ "$MODE" = "mark" ]; then
    flock -w 45 9 || { log "mark trigger: the lock stayed held for 45 s"; exit 0; }
    python3 scripts/fan.py forward mark --triggered-at "$STARTED" >> "$LOG_FILE" 2>&1 || log "mark trigger: not issued (see above)"
    exit 0
fi

sleep 10                                       # the bar that closed at the mark reaches IB first
flock -n 9 || { log "catch-up skipped: another forward run holds the lock"; exit 0; }
read -r HOST PORT CLIENT < <(python3 -c "from config import Config; print(Config.IB_HOST, Config.IB_PORT, Config.IB_CLIENT_ID + 2)")
log "Collecting NQ, VXN for the forward record..."
python3 -m collector.ib_collector --days 0 --no-trailing-refresh --workers 2 --symbol NQ,VXN --host "$HOST" \
    --port "$PORT" --client-id "$CLIENT" >> "$LOG_FILE" 2>&1 || log "collection failed"
python3 scripts/fan.py forward issue --trigger catchup --triggered-at "$STARTED" --collected-at "$(utc_now)" \
    >> "$LOG_FILE" 2>&1 || log "not issued (see above)"
