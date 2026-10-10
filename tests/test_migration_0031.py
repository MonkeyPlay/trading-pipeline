# tests/test_migration_0031.py
"""
Migration 0031 (the full-session RTH matcher's schema) and the tools around its deployment:
scripts/migration_dry_run.py rehearses pending migrations in a transaction that is rolled back - when they work and
when one fails - and leaves the database as it was; database/rollback/0031_rth_session_down.sql takes v31 back to
exactly v30 with the first-hour records and the frozen definitions untouched, refuses - changing nothing - whenever
a row only v31 can hold exists (whoever wrote it, whatever the date), and locks writers out between its check and
its change.

Needs a disposable database whose name contains "test" (TEST_DATABASE_URL); it RESETS it.
"""

import os
import shutil
import uuid
from datetime import datetime, timedelta, timezone

import psycopg
import pytest

DSN = os.getenv("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not DSN or "test" not in (psycopg.conninfo.conninfo_to_dict(DSN).get("dbname") or ""),
    reason="set TEST_DATABASE_URL to a disposable database whose name contains 'test'")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOWN = os.path.join(ROOT, "database", "rollback", "0031_rth_session_down.sql")
CID = 9031
UTC = timezone.utc
RTH_TABLES = ("rth_analogue_sets", "rth_analogue_members", "rth_eval_forecasts", "rth_eval_results")


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


def _rows(conn, tables=RTH_TABLES + ("definition_versions",)):
    """Every row of ``tables`` as md5 of its JSON on the columns v30 has (0031's new columns left out)."""
    new = ["session_minutes", "computation_started_at", "confirmed_by_start", "confirmed_received_at", "issue_class"]
    return {t: sorted(r[0] for r in conn.execute(
        f"SELECT md5((to_jsonb(x) - %s::text[])::text) FROM journal.{t} x", (new,)).fetchall()) for t in tables}


def _forward():
    """Applies the pending migrations (0031) the way deploy.sh does, on the project's connection."""
    from database.connection import get_db_connection
    from database.migrations import apply_migrations
    db = get_db_connection(DSN)
    try:
        return apply_migrations(db)
    finally:
        db.close()


def _down(conn):
    """Runs the reverse script; on a refusal, ends the aborted transaction (as psql's ON_ERROR_STOP would)."""
    try:
        conn.execute(open(DOWN, encoding="utf-8").read())
    except psycopg.Error:
        conn.execute("ROLLBACK")
        raise


# --------------------------------------------------------------------------
# A v30 database holding first-hour records, as production did before 0031
# --------------------------------------------------------------------------

def _seed_v30():
    """Registers the definitions and stores a snapshot, a first-hour (v2) set with a member, and an
    rth_continuation_v2 forecast on it - with v30's columns. Returns the snapshot id."""
    from contracts import nq_prompt_v2 as defs
    from contracts import nq_rth as rth
    from contracts import rth_eval, rth_operational, rth_session
    from database import journal_store as store
    from database.connection import get_db_connection
    from database.queries import upsert_contract
    db = get_db_connection(DSN)
    try:
        upsert_contract(db, CID, "NQ", "20261218", "CME", local_symbol="NQZ6")
        for rec in defs.all_records() + [rth.matcher_record(), rth.session_record(), rth_eval.record(),
                                         rth_operational.record(), rth_session.record()]:
            store.register_version(db, rec)
    finally:
        db.close()
    sid, set_id = str(uuid.uuid4()), str(uuid.uuid4())
    cutoff = datetime(2026, 10, 9, 14, 0, tzinfo=UTC)
    with psycopg.connect(DSN, autocommit=True) as c:
        c.execute("INSERT INTO journal.snapshots (snapshot_id, symbol, contract_id, session_date, snapshot_version, "
                  "convention_version, cutoff_at, rth_open_at, data_mode, pit_availability_status, source_payload_hash, "
                  "payload) VALUES (%s, 'NQ', %s, '2026-10-09', %s, %s, %s, %s, 'historical_reconstruction', "
                  "'unverified_historical', 'h', '{}');",
                  (sid, CID, defs.PROFILES[defs.DEFAULT_PROFILE].snapshot_version, defs.CONVENTION_VERSION,
                   cutoff - timedelta(minutes=31), cutoff - timedelta(minutes=30)))
        _set(c, set_id, sid, "nq_match_rth_v2", 30, cutoff)
        c.execute("INSERT INTO journal.rth_analogue_members (set_id, rank, session_date, contract_id, snapshot_id, "
                  "similarity, comparable_weight, components) VALUES (%s, 1, '2026-10-08', %s, %s, 80, 100, '{}');",
                  (set_id, CID, sid))
        _forecast(c, set_id, "rth_continuation_v2", 30, 15, cutoff)
    return sid, set_id


def _set(c, set_id, sid, version, minutes, cutoff, digest="d"):
    c.execute("INSERT INTO journal.rth_analogue_sets (set_id, symbol, session_date, contract_id, matcher_version, "
              "context_snapshot_id, elapsed_minutes, cutoff_at, input_digest, pool_size, pool_hash, excluded, "
              "target_features, quality, code_revision, issued_by) VALUES (%s, 'NQ', '2026-10-09', %s, %s, %s, %s, %s, "
              "%s, 1, 'p', '{}', '{}', '{}', 'test', 'backfill');",
              (set_id, CID, version, sid, minutes, cutoff, f"{digest}{version}{minutes}"))


def _forecast(c, set_id, evaluation, minutes, horizon, cutoff):
    c.execute("INSERT INTO journal.rth_eval_forecasts (forecast_id, evaluation_version, set_id, symbol, session_date, "
              "elapsed_minutes, cutoff_at, horizon_minutes, target_atr, forecasts, sources, digest, code_revision) "
              "VALUES (%s, %s, %s, 'NQ', '2026-10-09', %s, %s, %s, 300, '{}', '{}', %s, 'test');",
              (str(uuid.uuid4()), evaluation, set_id, minutes, cutoff, horizon, f"{evaluation}{minutes}{horizon}"))


# --------------------------------------------------------------------------
# The rehearsal
# --------------------------------------------------------------------------

def test_the_dry_run_rehearses_0031_and_leaves_the_database_as_it_was(capsys):
    from database.connection import reset_database
    from scripts import migration_dry_run
    reset_database(DSN, upto=30)
    _seed_v30()
    with psycopg.connect(DSN) as c:
        before, rows = _schema(c), _rows(c)
    assert migration_dry_run.main(["--db", DSN]) == 0
    out = capsys.readouterr().out
    assert "schema v30; rehearsing 0031_rth_session.sql" in out
    assert "journal.rth_issue_misses: new table (0 rows)" in out
    assert "journal.rth_analogue_sets: 1 rows - 0 removed or changed, 0 added; new columns session_minutes, " \
           "computation_started_at, confirmed_by_start, confirmed_received_at, issue_class" in out
    assert "0 existing table(s) with removed, changed or dropped rows or columns" in out
    assert "after the rollback: schema v30, tables, schema and rows as before" in out
    with psycopg.connect(DSN) as c:
        assert _schema(c) == before and _rows(c) == rows


def test_a_failing_migration_is_reported_and_rolled_back_by_the_dry_run(capsys, monkeypatch, tmp_path):
    """A pending migration that fails half way - after it created a table - is reported, and nothing of it stays."""
    from database import migrations
    from database.connection import reset_database
    from scripts import migration_dry_run
    reset_database(DSN, upto=30)
    for name in os.listdir(migrations.MIGRATIONS_DIR):
        if name.endswith(".sql") and int(name[:4]) <= 30:
            shutil.copy(os.path.join(migrations.MIGRATIONS_DIR, name), tmp_path)
    (tmp_path / "0031_broken.sql").write_text("CREATE TABLE journal.dry_run_probe (x INT);\n"
                                              "INSERT INTO journal.dry_run_probe VALUES (1);\nSELECT 1 / 0;\n")
    monkeypatch.setattr(migrations, "MIGRATIONS_DIR", str(tmp_path))
    with psycopg.connect(DSN) as c:
        before = _schema(c)
    assert migration_dry_run.main(["--db", DSN]) == 2
    out = capsys.readouterr().out
    assert "FAILED: DivisionByZero" in out and "rolled back" in out
    assert "after the rollback: schema v30, tables, schema and rows as before" in out
    with psycopg.connect(DSN) as c:
        assert _schema(c) == before
        assert c.execute("SELECT to_regclass('journal.dry_run_probe')").fetchone()[0] is None


# --------------------------------------------------------------------------
# The reversal
# --------------------------------------------------------------------------

def test_the_rollback_restores_v30_exactly_and_keeps_the_first_hour_records():
    from database.connection import reset_database
    reset_database(DSN, upto=30)
    _seed_v30()
    with psycopg.connect(DSN) as c:
        v30, rows = _schema(c), _rows(c)
    assert _forward() == 31
    with psycopg.connect(DSN, autocommit=True) as c:
        assert _rows(c) == rows                              # 0031 changes no first-hour row
        _down(c)
        assert _schema(c) == v30                             # exactly v30's tables, constraints, indexes, function
        assert _rows(c) == rows                              # the first-hour set, its member, its forecast, every
    reset_database(DSN)                                      # definition (frozen experiments included) unchanged


def _in_use(kind, c, sid, set_id):
    """Writes one row only v31 can hold."""
    cutoff = datetime(2026, 10, 9, 16, 0, tzinfo=UTC)
    if kind == "full-session set":
        _set(c, str(uuid.uuid4()), sid, "nq_match_rth_v3", 150, cutoff)
    elif kind == "first-hour set stored since 0031":               # its trigger stamps issue_class on every new row
        _set(c, str(uuid.uuid4()), sid, "nq_match_rth_v2", 45, cutoff, digest="later")
    elif kind == "miss":
        c.execute("INSERT INTO journal.rth_issue_misses (symbol, session_date, matcher_version, first_minutes, "
                  "last_minutes, reason, detail, issued_by) VALUES ('NQ', '2026-10-09', 'nq_match_rth_v3', 1, 2, "
                  "'expired', 'test', 'auto')")
    elif kind == "rth_session_v1 forecast":
        _forecast(c, set_id, "rth_session_v1", 30, 60, cutoff)
    elif kind == "second horizon of a first-hour key":
        _forecast(c, set_id, "rth_continuation_v2", 30, 5, cutoff)
    elif kind == "result of a new evaluation":
        c.execute("INSERT INTO journal.rth_eval_results (evaluation_version, results, code_revision) VALUES "
                  "('rth_session_v1', '{}', 'test')")


@pytest.mark.parametrize("kind", ["full-session set", "first-hour set stored since 0031", "miss",
                                  "rth_session_v1 forecast", "second horizon of a first-hour key",
                                  "result of a new evaluation"])
def test_the_rollback_refuses_whenever_a_row_needs_v31_and_changes_nothing(kind):
    """Refused on the data, not the date: whoever wrote the row - here a test - and whenever."""
    from database.connection import reset_database
    reset_database(DSN, upto=30)
    sid, set_id = _seed_v30()
    assert _forward() == 31
    with psycopg.connect(DSN, autocommit=True) as c:
        try:
            _in_use(kind, c, sid, set_id)
        except psycopg.Error as e:                           # a kind the v31 schema itself rejects needs no guard
            pytest.skip(f"v31 rejects a {kind} itself: {e}")
        schema, rows = _schema(c), _rows(c, RTH_TABLES + ("definition_versions", "rth_issue_misses"))
        with pytest.raises(psycopg.errors.RaiseException, match="0031 is in use"):
            _down(c)
        assert _schema(c) == schema                          # no partial reversal
        assert _rows(c, RTH_TABLES + ("definition_versions", "rth_issue_misses")) == rows
    reset_database(DSN)


def test_the_rollback_locks_writers_out_between_its_check_and_its_change():
    """The script's opening statements lock every table it reads or alters: a writer waits (here: times out) until
    the reversal commits or rolls back."""
    from database.connection import reset_database
    reset_database(DSN)
    head = open(DOWN, encoding="utf-8").read().split("DO $$")[0]
    assert "LOCK TABLE" in head and "rth_issue_misses" in head and "rth_analogue_sets" in head
    with psycopg.connect(DSN, autocommit=True) as a, psycopg.connect(DSN, autocommit=True) as b:
        a.execute(head)                                      # BEGIN; lock_timeout; LOCK TABLE ...
        b.execute("SET lock_timeout = '500ms'")
        with pytest.raises(psycopg.errors.LockNotAvailable):
            b.execute("INSERT INTO journal.rth_issue_misses (symbol, session_date, matcher_version, first_minutes, "
                      "last_minutes, reason, detail, issued_by) VALUES ('NQ', '2026-10-09', 'nq_match_rth_v3', 1, 2, "
                      "'expired', 'test', 'auto')")
        a.execute("ROLLBACK")
    reset_database(DSN)
