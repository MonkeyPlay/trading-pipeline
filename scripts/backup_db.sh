#!/bin/bash
# scripts/backup_db.sh
# Safely backs up the local SQLite database file to the backups folder.
# Can be scheduled via a simple daily cron job.

set -e

PROJECT_DIR="/home/monkeyplay/trading_pipeline"
DB_FILE="$PROJECT_DIR/data/trading_pipeline.db"
BACKUP_DIR="$PROJECT_DIR/data/backups"
TIMESTAMP=$(date +"%Y%m%d_%H%M%S")
BACKUP_FILE="$BACKUP_DIR/trading_pipeline_backup_$TIMESTAMP.db"

mkdir -p "$BACKUP_DIR"

if [ -f "$DB_FILE" ]; then
    echo "Creating compressed SQLite database backup..."
    sqlite3 "$DB_FILE" ".backup '$BACKUP_FILE'"
    
    # Compress the backup file to save space
    gzip "$BACKUP_FILE"
    
    echo "Backup completed successfully! Saved as ${BACKUP_FILE}.gz"
    
    # Optional: Keep only the last 30 days of backups to prevent storage bloat
    find "$BACKUP_DIR" -name "trading_pipeline_backup_*.db.gz" -mtime +30 -exec rm {} \;
else
    echo "ERROR: Active database file not found at $DB_FILE. Backup skipped."
    exit 1
fi
