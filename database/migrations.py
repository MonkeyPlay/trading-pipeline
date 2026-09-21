# database/migrations.py
"""
Forward-only schema migrations tracked by ``PRAGMA user_version``.

Each file in ``database/migrations/`` is named ``NNNN_description.sql`` (NNNN is
a zero-padded integer, contiguous from 0001). Files are applied in order, each
inside its own transaction; after a file succeeds, ``user_version`` is bumped to
NNNN. Files whose number is <= the current ``user_version`` are skipped.

A migration may also need work that SQL cannot express (timezone-aware
backfills, derived aggregates). ``PRE_HOOKS`` / ``POST_HOOKS`` map a version
number to a callable run immediately before / after that version's SQL file, in
its own transaction. A failing pre-hook aborts the migration; a failing
post-hook leaves the schema migrated but the derived data stale, and logs how to
recompute it.

Init and upgrade are therefore the same code path, and an existing database is
never dropped or regenerated — new versions just add migrations.

CLI:
    python -m database.migrations                 # show status
    python -m database.migrations --db path.db    # apply pending migrations
    python -m database.migrations --snapshot      # rewrite database/schema.sql
"""

import os
import re
import sqlite3
import logging

logger = logging.getLogger(__name__)

_HERE = os.path.dirname(os.path.abspath(__file__))
MIGRATIONS_DIR = os.path.join(_HERE, "migrations")
SCHEMA_SNAPSHOT_PATH = os.path.join(_HERE, "schema.sql")
_NAME_RE = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")


# --------------------------------------------------------------------------
# Python hooks
# --------------------------------------------------------------------------

def _pre_0003_require_trading_day(conn):
    """
    0003 makes ``bars.trading_day`` NOT NULL and part of the primary key. Rows
    written before 0002 may still have it NULL; fill them here (the NY session
    date needs real timezone rules, so it cannot be done in the .sql file).
    """
    missing = conn.execute("SELECT COUNT(*) FROM bars WHERE trading_day IS NULL;").fetchone()[0]
    if not missing:
        return

    from database.backfill import backfill_trading_day

    logger.info(f"Backfilling trading_day for {missing} bar(s) before migration 0003...")
    backfill_trading_day(conn)

    left = conn.execute("SELECT COUNT(*) FROM bars WHERE trading_day IS NULL;").fetchone()[0]
    if left:
        raise RuntimeError(
            f"{left} bar(s) still have no trading_day; migration 0003 would drop them. "
            f"Fix or delete those rows, then re-run."
        )


def _post_0003_rebuild_ledger(conn):
    """Derive each session_days row's status from real expected-bar counts."""
    from database.queries import rebuild_session_days

    n = rebuild_session_days(conn)
    logger.info(f"Rebuilt the session_days ledger: {n} day(s) indexed.")


PRE_HOOKS = {3: _pre_0003_require_trading_day}
POST_HOOKS = {3: _post_0003_rebuild_ledger}


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
    return int(conn.execute("PRAGMA user_version;").fetchone()[0])


def latest_version():
    disc = discover_migrations()
    return disc[-1][0] if disc else 0


def apply_migrations(conn):
    """Applies every migration newer than the connection's user_version. Returns new version."""
    current = get_user_version(conn)
    applied = 0

    for num, path in discover_migrations():
        if num <= current:
            continue
        with open(path, "r") as f:
            body = f.read().strip()

        logger.info(f"Applying migration {num:04d}: {os.path.basename(path)}")

        pre = PRE_HOOKS.get(num)
        if pre is not None:
            pre(conn)

        script = f"BEGIN;\n{body}\nPRAGMA user_version = {num};\nCOMMIT;"
        try:
            conn.executescript(script)
        except sqlite3.Error as e:
            try:
                conn.executescript("ROLLBACK;")
            except sqlite3.Error:
                pass
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
    Materialises the fully-migrated schema into a single readable .sql file.
    This file is generated output — never edit it, add a migration instead.
    """
    mem = sqlite3.connect(":memory:")
    try:
        version = apply_migrations(mem)
        objects = mem.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' "
            "ORDER BY CASE type WHEN 'table' THEN 0 WHEN 'index' THEN 1 ELSE 2 END, name;"
        ).fetchall()
    finally:
        mem.close()

    header = (
        "-- database/schema.sql\n"
        "-- GENERATED FILE — do not edit. Regenerate with:\n"
        "--     python -m database.migrations --snapshot\n"
        "-- The authoritative schema is the ordered set of files in database/migrations/.\n"
        f"-- Snapshot of schema version {version:04d}.\n\n"
    )
    with open(path, "w") as f:
        f.write(header)
        for (sql,) in objects:
            f.write(sql.strip().rstrip(";") + ";\n\n")
    logger.info(f"Wrote schema snapshot (v{version:04d}) to {path}")


if __name__ == "__main__":
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    parser = argparse.ArgumentParser(description="Schema migration runner")
    parser.add_argument("--db", help="Apply pending migrations to this database file")
    parser.add_argument("--snapshot", action="store_true", help="Rewrite database/schema.sql")
    args = parser.parse_args()

    print(f"Migrations on disk: v{latest_version():04d} ({len(discover_migrations())} file(s))")

    if args.snapshot:
        write_schema_snapshot()

    if args.db:
        c = sqlite3.connect(args.db)
        try:
            print(f"{args.db}: currently v{get_user_version(c):04d}")
            apply_migrations(c)
            print(f"{args.db}: now v{get_user_version(c):04d}")
        finally:
            c.close()
