#!/bin/bash
# scripts/backup_db.sh
# Backs up the PostgreSQL / TimescaleDB database to the backups folder with pg_dump.
# Can be scheduled via a simple daily cron job.
#
# pg_dump takes a consistent snapshot, so it is safe while the pipeline is writing.
# Restore into an empty database that has the timescaledb extension available (without client tools on the host,
# run psql / pg_restore through docker exec -i <container> ...):
#
#   psql "$DATABASE_URL" -c "CREATE EXTENSION IF NOT EXISTS timescaledb;" \
#                        -c "SELECT timescaledb_pre_restore();"
#   pg_restore -d "$DATABASE_URL" --no-owner data/backups/trading_pipeline_backup_<ts>.dump
#   psql "$DATABASE_URL" -c "SELECT timescaledb_post_restore();"

set -e

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKUP_DIR="$PROJECT_DIR/data/backups"
TIMESTAMP=$(date +"%Y%m%d_%H%M%S")
BACKUP_FILE="$BACKUP_DIR/trading_pipeline_backup_$TIMESTAMP.dump"

# DATABASE_URL may live in the project's .env rather than the environment.
if [ -z "${DATABASE_URL:-}" ] && [ -f "$PROJECT_DIR/.env" ]; then
    DATABASE_URL="$(grep -E '^DATABASE_URL=' "$PROJECT_DIR/.env" | tail -n1 | cut -d= -f2- | tr -d "\"'")"
fi
DATABASE_URL="${DATABASE_URL:-postgresql://trading:trading@localhost:5432/trading_pipeline}"

# pg_dump on the host, else the database container's own (docker compose's timescaledb service): the URL's
# localhost:5432 is the server inside the container too, and the dump streams out to the file here.
CONTAINER=""
if ! command -v pg_dump >/dev/null 2>&1; then
    if command -v docker >/dev/null 2>&1; then
        CONTAINER="$(docker ps --format '{{.Names}}' --filter "name=${DB_CONTAINER:-timescaledb}" | head -n1)"
    fi
    if [ -z "$CONTAINER" ]; then
        echo "ERROR: pg_dump not found and no running database container. Install the PostgreSQL client tools." >&2
        exit 1
    fi
fi

mkdir -p "$BACKUP_DIR"

echo "Creating PostgreSQL backup..."
# Custom format is already compressed and restores selectively with pg_restore.
# TimescaleDB's catalog triggers a few harmless circular-FK warnings; they are expected.
if [ -z "$CONTAINER" ]; then
    pg_dump --format=custom --no-owner --file="$BACKUP_FILE" "$DATABASE_URL"
else
    echo "(no pg_dump on the host: using the one in container $CONTAINER)"
    docker exec "$CONTAINER" pg_dump --format=custom --no-owner "$DATABASE_URL" > "$BACKUP_FILE"
fi

echo "Backup completed successfully! Saved as $BACKUP_FILE"

# Keep only the last 30 days of backups to prevent storage bloat
find "$BACKUP_DIR" -name "trading_pipeline_backup_*.dump" -mtime +30 -exec rm {} \;
