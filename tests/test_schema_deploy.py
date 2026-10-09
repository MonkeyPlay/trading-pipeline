# tests/test_schema_deploy.py
"""
Deployment (database/connection.py, scripts/deploy.sh): routine processes only check the
schema and stop on a mismatch - a migration file in a checkout never changes a database by
itself; only the explicit deployment step migrates, from a fixed revision.

Needs a disposable PostgreSQL + TimescaleDB database whose name contains "test", which it
RESETS (TEST_DATABASE_URL, see tests/test_dashboard_data.py).
"""

import os
import subprocess

import psycopg
import pytest

DSN = os.getenv("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not DSN or "test" not in (psycopg.conninfo.conninfo_to_dict(DSN).get("dbname") or ""),
    reason="set TEST_DATABASE_URL to a disposable database whose name contains 'test'")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(scope="module")
def fresh():
    from database.connection import reset_database
    reset_database(DSN)                                      # an explicit apply: the test database
    yield DSN


def test_routine_processes_check_the_schema_and_never_migrate(fresh, monkeypatch):
    from database import connection
    from database.migrations import get_user_version, latest_version
    assert connection.init_database(DSN) == latest_version()
    # a migration file the database does not have yet: refused, and nothing is applied
    monkeypatch.setattr(connection, "latest_version", lambda: latest_version() + 1)
    with pytest.raises(connection.SchemaMismatch, match="Deploy the migrations explicitly"):
        connection.init_database(DSN)
    conn = connection.get_db_connection(DSN)
    try:
        assert get_user_version(conn) == latest_version()     # unchanged
    finally:
        conn.close()
    # a checkout older than the database: refused too
    monkeypatch.setattr(connection, "latest_version", lambda: latest_version() - 1)
    with pytest.raises(connection.SchemaMismatch, match="older than the database"):
        connection.init_database(DSN)


def test_the_cli_refuses_a_database_it_does_not_match(fresh, monkeypatch):
    from database import connection
    from database.migrations import latest_version
    from scripts.nq_journal import main
    monkeypatch.setattr(connection, "latest_version", lambda: latest_version() + 1)
    with pytest.raises(connection.SchemaMismatch):
        main(["--db", DSN, "rth-eval-status"])


def test_deploy_runs_a_fixed_revision_with_the_shared_state(fresh, tmp_path):
    """scripts/deploy.sh checks a commit out, detached, links the shared runtime state, applies that revision's
    migrations and checks the schema; it refuses a production checkout with local changes."""
    head = subprocess.check_output(["git", "-C", ROOT, "rev-parse", "HEAD"], text=True).strip()
    state, prod = tmp_path / "state", tmp_path / "prod"
    (state / "data").mkdir(parents=True)
    (state / ".env").write_text(f"DATABASE_URL={DSN}\n")
    os.symlink(os.path.join(ROOT, ".venv"), state / ".venv")
    env = dict(os.environ, DATABASE_URL=DSN, PROD_DIR=str(prod), STATE_DIR=str(state))   # never the default store
    try:
        run = subprocess.run([os.path.join(ROOT, "scripts", "deploy.sh"), head], env=env, capture_output=True,
                             text=True, timeout=300)
        assert run.returncode == 0, run.stdout + run.stderr
        assert subprocess.check_output(["git", "-C", str(prod), "rev-parse", "HEAD"], text=True).strip() == head
        for path in (".env", ".venv", "logs", "data/auto_mode.lock"):
            assert os.path.islink(prod / path) and os.path.realpath(prod / path) == os.path.realpath(state / path)
        assert f"deployed {head}" in (state / "logs" / "deployments.log").read_text()
        (prod / "README.md").write_text("changed\n")                   # production runs committed revisions only
        again = subprocess.run([os.path.join(ROOT, "scripts", "deploy.sh"), head], env=env, capture_output=True,
                               text=True, timeout=300)
        assert again.returncode != 0 and "local changes" in again.stderr
    finally:
        subprocess.run(["git", "-C", ROOT, "worktree", "remove", "--force", str(prod)], capture_output=True)
