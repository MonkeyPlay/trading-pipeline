# database/connection.py
"""
SQLite connection management and schema initialization for the NQ Trading Pipeline.

Schema is managed exclusively through forward-only migrations
(``database/migrations/``); see ``database/migrations.py``. An existing database
is upgraded in place and never regenerated.
"""

import os
import sqlite3
import logging

from database.migrations import apply_migrations

logger = logging.getLogger(__name__)

_DEFAULT_DB_PATH = "data/trading_pipeline.db"


def get_db_connection(db_path=_DEFAULT_DB_PATH):
    """
    Opens a connection with foreign keys on, WAL journaling, and Row results.

    ``check_same_thread=False`` is required because the dashboard shares one
    connection across NiceGUI's worker threads. All writes go through
    ``with conn:`` transactions, which serialise access.
    """
    db_dir = os.path.dirname(db_path)
    if db_dir and not os.path.exists(db_dir):
        os.makedirs(db_dir, exist_ok=True)
        logger.info(f"Created database directory: {db_dir}")

    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.row_factory = sqlite3.Row
    return conn


def init_database(db_path=_DEFAULT_DB_PATH, schema_path=None):
    """
    Ensures the database exists and is migrated to the latest schema version.
    Safe to call on every process start. ``schema_path`` is accepted for
    backwards compatibility and ignored (migrations are authoritative).
    """
    conn = get_db_connection(db_path)
    try:
        apply_migrations(conn)
    finally:
        conn.close()


def reset_database(db_path=_DEFAULT_DB_PATH):
    """
    DEV ONLY. Drops every user table, resets the schema version, and re-migrates
    from scratch. Never call this against a database holding real collected data.
    """
    conn = get_db_connection(db_path)
    try:
        with conn:
            conn.execute("PRAGMA foreign_keys = OFF;")
            names = [
                r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%';"
                )
            ]
            for name in names:
                conn.execute(f"DROP TABLE IF EXISTS {name};")
            conn.execute("PRAGMA user_version = 0;")
            conn.execute("PRAGMA foreign_keys = ON;")
        logger.info(f"Dropped {len(names)} table(s); schema version reset to 0.")
    finally:
        conn.close()
    init_database(db_path=db_path)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    test_db = "data/trading_pipeline_test.db"
    try:
        init_database(db_path=test_db)
        c = get_db_connection(test_db)
        print("schema version:", c.execute("PRAGMA user_version;").fetchone()[0])
        c.close()
        print(f"Success! Test database initialized at: {test_db}")
    finally:
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(test_db + suffix):
                os.remove(test_db + suffix)
