# database/migrations.py
"""
Forward-only schema migrations, tracked in the ``schema_migrations`` table.

Each file in ``database/migrations/`` is named ``NNNN_description.sql`` (NNNN is
a zero-padded integer, contiguous from 0001). Files are applied in order, each
inside its own transaction together with the row recording it in
``schema_migrations``. Files whose number is <= the current version are skipped.

A migration may also need work that SQL cannot express (timezone-aware
backfills, derived aggregates). ``PRE_HOOKS`` / ``POST_HOOKS`` map a version
number to a callable run immediately before / after that version's SQL file. A
failing pre-hook aborts the migration; a failing post-hook leaves the schema
migrated but the derived data stale, and logs how to recompute it.

Concurrent processes (the dashboard, the collector, the forecast) all migrate on
start; a transaction-scoped advisory lock makes sure only one of them applies a
given file.

Init and upgrade are therefore the same code path, and an existing database is
never dropped or regenerated — new versions just add migrations.

CLI:
    python -m database.migrations                    # show status
    python -m database.migrations --db postgresql:// # apply pending migrations
    python -m database.migrations --snapshot         # rewrite database/schema.sql
"""

import os
import re
import logging

logger = logging.getLogger(__name__)

_HERE = os.path.dirname(os.path.abspath(__file__))
MIGRATIONS_DIR = os.path.join(_HERE, "migrations")
SCHEMA_SNAPSHOT_PATH = os.path.join(_HERE, "schema.sql")
_NAME_RE = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")

# Arbitrary, fixed key for pg_advisory_xact_lock: serialises migration runs.
_MIGRATION_LOCK_KEY = 7_240_031

_LEDGER_DDL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version    INTEGER PRIMARY KEY,
    name       TEXT NOT NULL,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


# --------------------------------------------------------------------------
# Python hooks
# --------------------------------------------------------------------------

PRE_HOOKS = {}
POST_HOOKS = {}


def discover_migrations():
    """Returns an ordered list of (version:int, path:str), validated for contiguity."""
    found = []
    for fn in sorted(os.listdir(MIGRATIONS_DIR)):
        if not fn.endswith(".sql"):
            continue
        m = _NAME_RE.match(fn)
        if not m:
            raise ValueError(f"Migration '{fn}' does not match NNNN_description.sql")
        found.append((int(m.group(1)), os.path.join(MIGRATIONS_DIR, fn)))

    found.sort(key=lambda t: t[0])
    for expected, (num, path) in enumerate(found, start=1):
        if num != expected:
            raise ValueError(
                f"Migration numbering must be contiguous from 0001: "
                f"expected {expected:04d}, found {num:04d} ({os.path.basename(path)})"
            )
    return found


def get_user_version(conn):
    """The highest applied migration, or 0 for an empty database."""
    exists = conn.execute("SELECT to_regclass('schema_migrations') IS NOT NULL;").fetchone()[0]
    if not exists:
        return 0
    return int(conn.execute("SELECT COALESCE(MAX(version), 0) FROM schema_migrations;").fetchone()[0])


def latest_version():
    disc = discover_migrations()
    return disc[-1][0] if disc else 0


def apply_migrations(conn):
    """Applies every migration newer than the database's version. Returns new version."""
    applied = 0

    for num, path in discover_migrations():
        with open(path, "r") as f:
            body = f.read().strip()

        with conn:
            conn.execute("SELECT pg_advisory_xact_lock(%s);", (_MIGRATION_LOCK_KEY,))
            conn.execute(_LEDGER_DDL)
            # Re-read under the lock: another process may have just applied it.
            if num <= get_user_version(conn):
                continue

            logger.info(f"Applying migration {num:04d}: {os.path.basename(path)}")

            pre = PRE_HOOKS.get(num)
            if pre is not None:
                pre(conn)

            try:
                conn.execute(body)
                conn.execute(
                    "INSERT INTO schema_migrations (version, name) VALUES (%s, %s);",
                    (num, os.path.basename(path)),
                )
            except Exception as e:
                logger.error(f"Migration {num:04d} failed and was rolled back: {e}")
                raise

        post = POST_HOOKS.get(num)
        if post is not None:
            try:
                post(conn)
            except Exception as e:
                logger.error(
                    f"Migration {num:04d} applied, but its post-hook failed: {e}. "
                    f"Derived data may be stale — run 'python -m database.backfill'."
                )
                raise
        applied += 1

    new_version = get_user_version(conn)
    if applied:
        logger.info(f"Applied {applied} migration(s); schema now at v{new_version:04d}.")
    else:
        logger.debug(f"Schema already current (v{new_version:04d}).")
    return new_version


def write_schema_snapshot(path=SCHEMA_SNAPSHOT_PATH):
    """
    Concatenates every migration into a single readable .sql file.
    This file is generated output — never edit it, add a migration instead.
    """
    migrations = discover_migrations()
    version = migrations[-1][0] if migrations else 0

    header = (
        "-- database/schema.sql\n"
        "-- GENERATED FILE — do not edit. Regenerate with:\n"
        "--     python -m database.migrations --snapshot\n"
        "-- The authoritative schema is the ordered set of files in database/migrations/.\n"
        f"-- Snapshot of schema version {version:04d}.\n"
    )
    with open(path, "w") as out:
        out.write(header)
        for num, mig_path in migrations:
            with open(mig_path, "r") as f:
                out.write(f"\n-- ===== {os.path.basename(mig_path)} =====\n\n")
                out.write(f.read().strip() + "\n")
    logger.info(f"Wrote schema snapshot (v{version:04d}) to {path}")


if __name__ == "__main__":
    import argparse

    from database.connection import describe_dsn, get_db_connection

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    parser = argparse.ArgumentParser(description="Schema migration runner")
    parser.add_argument("--db", help="Apply pending migrations to this PostgreSQL URL")
    parser.add_argument("--snapshot", action="store_true", help="Rewrite database/schema.sql")
    args = parser.parse_args()

    print(f"Migrations on disk: v{latest_version():04d} ({len(discover_migrations())} file(s))")

    if args.snapshot:
        write_schema_snapshot()

    if args.db:
        c = get_db_connection(args.db)
        name = describe_dsn(args.db)
        try:
            print(f"{name}: currently v{get_user_version(c):04d}")
            apply_migrations(c)
            print(f"{name}: now v{get_user_version(c):04d}")
        finally:
            c.close()
