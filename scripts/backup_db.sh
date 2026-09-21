#!/bin/bash
# scripts/backup_db.sh
# Safely backs up the local SQLite database file to the backups folder.
# Can be scheduled via a simple daily cron job.

set -e

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DB_FILE="${DB_PATH:-data/trading_pipeline.db}"
# DB_PATH is relative to the project root by convention (see config.py).
case "$DB_FILE" in /*) ;; *) DB_FILE="$PROJECT_DIR/$DB_FILE" ;; esac
BACKUP_DIR="$PROJECT_DIR/data/backups"
TIMESTAMP=$(date +"%Y%m%d_%H%M%S")
BACKUP_FILE="$BACKUP_DIR/trading_pipeline_backup_$TIMESTAMP.db"

# Prefer the project venv; fall back to whatever python3 is on PATH.
PYTHON="$PROJECT_DIR/.venv/bin/python"
[ -x "$PYTHON" ] || PYTHON="$(command -v python3 || true)"
if [ -z "$PYTHON" ]; then
    echo "ERROR: no python3 found to run the backup." >&2
    exit 1
fi

mkdir -p "$BACKUP_DIR"

if [ -f "$DB_FILE" ]; then
    echo "Creating compressed SQLite database backup..."

    # sqlite3's online backup API - consistent even while the pipeline is writing.
    # Uses the stdlib module rather than the sqlite3 CLI, which is often not installed.
    "$PYTHON" - "$DB_FILE" "$BACKUP_FILE" <<'PY'
import sqlite3, sys

src, dst = sys.argv[1], sys.argv[2]
source = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
try:
    dest = sqlite3.connect(dst)
    try:
        with dest:
            source.backup(dest)
    finally:
        dest.close()
finally:
    source.close()
PY

    # Compress the backup file to save space
    gzip "$BACKUP_FILE"

    echo "Backup completed successfully! Saved as ${BACKUP_FILE}.gz"

    # Optional: Keep only the last 30 days of backups to prevent storage bloat
    find "$BACKUP_DIR" -name "trading_pipeline_backup_*.db.gz" -mtime +30 -exec rm {} \;
else
    echo "ERROR: Active database file not found at $DB_FILE. Backup skipped."
    exit 1
fi
