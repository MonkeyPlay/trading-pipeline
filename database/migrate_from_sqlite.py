# database/migrate_from_sqlite.py
"""
One-time import of an existing SQLite store into PostgreSQL / TimescaleDB.

The SQLite file must be at schema version 0003 (the day-partitioned store) — run
the pre-Postgres checkout's ``python -m database.migrations --db file.db`` first
if it is older. The target database is migrated to the latest schema and must
hold no market data yet; everything is copied in one transaction, so an
interrupted import leaves the target empty rather than half-filled.

    python -m database.migrate_from_sqlite --sqlite data/trading_pipeline.db
    python -m database.migrate_from_sqlite --sqlite data/x.db --db postgresql://...

The SQLite file is opened read-only and never modified.
"""

import argparse
import logging
import os
import sqlite3

from database.connection import default_dsn, describe_dsn, get_db_connection, init_database
from database.queries import rebuild_session_days

logger = logging.getLogger(__name__)

REQUIRED_SQLITE_VERSION = 3

# Parents before children, so every foreign key already has its target.
TABLES = (
    "contracts",
    "session_days",
    "bars",
    "collection_runs",
    "feature_snapshots",
    "predictions",
    "outcomes",
    "analogue_matches",
)

# Identity columns whose sequences must continue after the copied ids.
IDENTITY_COLUMNS = {
    "collection_runs": "run_id",
    "feature_snapshots": "snapshot_id",
    "predictions": "prediction_id",
    "outcomes": "outcome_id",
    "analogue_matches": "match_id",
}


def _target_columns(conn, table):
    rows = conn.execute(
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_schema = current_schema() AND table_name = %s ORDER BY ordinal_position;",
        (table,),
    ).fetchall()
    return {r[0]: r[1] for r in rows}


def _clean(value, data_type):
    # SQLite happily stores '' in a date/time column; Postgres needs NULL.
    if value == "" and ("date" in data_type or "time" in data_type or data_type == "jsonb"):
        return None
    return value


def copy_table(src, conn, table):
    target = _target_columns(conn, table)
    source_cols = [r[1] for r in src.execute(f"PRAGMA table_info({table});")]
    cols = [c for c in source_cols if c in target]
    dropped = sorted(set(source_cols) - set(cols))
    if dropped:
        logger.warning(f"{table}: columns not in the Postgres schema, skipped: {dropped}")

    types = [target[c] for c in cols]
    n = 0
    with conn.raw.cursor().copy(f"COPY {table} ({', '.join(cols)}) FROM STDIN") as copy:
        for row in src.execute(f"SELECT {', '.join(cols)} FROM {table};"):
            copy.write_row([_clean(v, t) for v, t in zip(row, types)])
            n += 1
    logger.info(f"{table}: copied {n:,} row(s).")
    return n


def migrate(sqlite_path, dsn=None):
    if not os.path.exists(sqlite_path):
        raise SystemExit(f"SQLite file not found: {sqlite_path}")

    src = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
    try:
        version = src.execute("PRAGMA user_version;").fetchone()[0]
        if version != REQUIRED_SQLITE_VERSION:
            raise SystemExit(
                f"{sqlite_path} is at schema v{version:04d}; this import expects "
                f"v{REQUIRED_SQLITE_VERSION:04d}. Upgrade it with the SQLite-era code first."
            )

        init_database(dsn)
        conn = get_db_connection(dsn)
        try:
            held = conn.execute("SELECT COUNT(*) FROM contracts;").fetchone()[0]
            if held:
                raise SystemExit(
                    f"{describe_dsn(dsn)} already holds {held} contract(s); refusing to import "
                    f"on top of existing data. Point --db at an empty database."
                )

            with conn:
                for table in TABLES:
                    copy_table(src, conn, table)
                for table, column in IDENTITY_COLUMNS.items():
                    conn.execute(
                        f"SELECT setval(pg_get_serial_sequence('{table}', '{column}'), "
                        f"COALESCE((SELECT MAX({column}) FROM {table}), 0) + 1, false);"
                    )

            # Status thresholds are re-derived, exactly as 'python -m database.backfill' does.
            days = rebuild_session_days(conn)
            logger.info(f"Import complete: {days} trading day(s) in the ledger at {describe_dsn(dsn)}.")
        finally:
            conn.close()
    finally:
        src.close()


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    parser = argparse.ArgumentParser(description="Copy a SQLite store into PostgreSQL / TimescaleDB")
    parser.add_argument("--sqlite", required=True, help="Path to the existing SQLite database file")
    parser.add_argument("--db", default=default_dsn(), help="Target PostgreSQL connection URL")
    args = parser.parse_args()
    migrate(args.sqlite, args.db)


if __name__ == "__main__":
    main()
