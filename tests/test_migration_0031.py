# tests/test_migration_0031.py
"""
Migration 0031 (the full-session RTH matcher's schema) and the tools around its deployment:
scripts/migration_dry_run.py rehearses it in a transaction that is rolled back and leaves the
database as it was; database/rollback/0031_rth_session_down.sql takes v31 back to exactly v30
while nothing uses 0031, and refuses - changing nothing - once something does.

Needs a disposable database whose name contains "test" (TEST_DATABASE_URL); it RESETS it.
"""

import os

import psycopg
import pytest

DSN = os.getenv("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not DSN or "test" not in (psycopg.conninfo.conninfo_to_dict(DSN).get("dbname") or ""),
    reason="set TEST_DATABASE_URL to a disposable database whose name contains 'test'")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOWN = os.path.join(ROOT, "database", "rollback", "0031_rth_session_down.sql")


def _schema(conn):
    """The RTH tables' columns, constraints and indexes, the stamp function's body, the schema version."""
    cols = conn.execute("SELECT table_name, column_name, data_type, is_nullable FROM information_schema.columns "
                        "WHERE table_schema = 'journal' AND table_name LIKE 'rth%' ORDER BY 1, 2").fetchall()
    cons = conn.execute("SELECT conrelid::regclass::text, conname, pg_get_constraintdef(oid) FROM pg_constraint "
                        "WHERE connamespace = 'journal'::regnamespace AND conrelid::regclass::text LIKE "
                        "'journal.rth%' ORDER BY 1, 2").fetchall()
    idx = conn.execute("SELECT tablename, indexname, indexdef FROM pg_indexes WHERE schemaname = 'journal' AND "
                       "tablename LIKE 'rth%' ORDER BY 1, 2").fetchall()
    fn = conn.execute("SELECT prosrc FROM pg_proc WHERE proname = 'stamp_rth_analogue_set'").fetchone()
    version = conn.execute("SELECT max(version) FROM schema_migrations").fetchone()
    return cols, cons, idx, fn, version


def test_the_dry_run_rehearses_0031_and_leaves_the_database_as_it_was(capsys):
    from database.connection import reset_database
    from scripts import migration_dry_run
    reset_database(DSN, upto=30)
    with psycopg.connect(DSN) as c:
        before = _schema(c)
    assert migration_dry_run.main(["--db", DSN]) == 0
    out = capsys.readouterr().out
    assert "schema v30; rehearsing 0031_rth_session.sql" in out
    assert "journal.rth_issue_misses: new table (0 rows)" in out
    assert "new columns session_minutes, computation_started_at, confirmed_by_start, confirmed_received_at, " \
           "issue_class" in out
    assert "0 existing table(s) with removed, changed or dropped rows or columns" in out
    assert "after the rollback: schema v30, tables as before" in out
    with psycopg.connect(DSN) as c:
        assert _schema(c) == before


def _forward():
    """Applies the pending migrations (0031) the way deploy.sh does, on the project's connection."""
    from database.connection import get_db_connection
    from database.migrations import apply_migrations
    db = get_db_connection(DSN)
    try:
        return apply_migrations(db)
    finally:
        db.close()


def test_the_rollback_restores_v30_exactly_and_refuses_once_0031_is_in_use():
    from database.connection import reset_database
    reset_database(DSN, upto=30)
    with psycopg.connect(DSN) as c:
        v30 = _schema(c)
    assert _forward() == 31
    with psycopg.connect(DSN, autocommit=True) as c:
        c.execute(open(DOWN, encoding="utf-8").read())
        assert _schema(c) == v30                           # exactly v30's tables, constraints, indexes, function
    assert _forward() == 31                                 # and forward again
    with psycopg.connect(DSN, autocommit=True) as c:
        c.execute("INSERT INTO journal.definition_versions (version, kind, definition, definition_hash) VALUES "
                  "('nq_match_rth_v3', 'rth_matcher', '{}'::jsonb, 'x')")
        c.execute("INSERT INTO journal.rth_issue_misses (symbol, session_date, matcher_version, first_minutes, "
                  "last_minutes, reason, detail, issued_by) VALUES ('NQ', '2026-10-12', 'nq_match_rth_v3', 1, 2, "
                  "'expired', 'test', 'auto')")
        in_use = _schema(c)
        with pytest.raises(psycopg.errors.RaiseException, match="0031 is in use"):
            c.execute(open(DOWN, encoding="utf-8").read())
        c.execute("ROLLBACK")
        assert _schema(c) == in_use and c.execute("SELECT count(*) FROM journal.rth_issue_misses").fetchone()[0] == 1
    reset_database(DSN)
