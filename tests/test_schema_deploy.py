# tests/test_schema_deploy.py
"""
Deployment (database/connection.py, scripts/deploy.sh): routine processes only check the
schema and stop on a mismatch - a migration file in a checkout never changes a database by
itself; only the explicit deployment step migrates, after fast-forwarding production's main
to a tested revision.

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


def test_deploy_fast_forwards_main_and_migrates_from_a_committed_revision(fresh, tmp_path):
    """scripts/deploy.sh on a production checkout (here a clone standing in for ~/trading_pipeline): main is
    fast-forwarded to the revision, its migrations applied, the schema checked and the deployment logged; it refuses
    a checkout with local changes, one not on main, and a revision that is not a fast-forward."""
    head = subprocess.check_output(["git", "-C", ROOT, "rev-parse", "HEAD"], text=True).strip()
    prod = tmp_path / "prod"
    subprocess.run(["git", "clone", "--quiet", ROOT, str(prod)], check=True)
    git = lambda *a: subprocess.run(["git", "-C", str(prod), *a], check=True, capture_output=True,  # noqa: E731
                                    text=True).stdout.strip()
    git("checkout", "--quiet", "-B", "main", f"{head}~1")
    (prod / ".env").write_text(f"DATABASE_URL={DSN}\n")
    os.symlink(os.path.join(ROOT, ".venv"), prod / ".venv")
    env = dict(os.environ, DATABASE_URL=DSN, PROD_DIR=str(prod))                    # never the default store
    deploy = lambda rev: subprocess.run([os.path.join(ROOT, "scripts", "deploy.sh"), rev], env=env,  # noqa: E731
                                        capture_output=True, text=True, timeout=300)
    run = deploy(head)
    assert run.returncode == 0, run.stdout + run.stderr
    assert git("rev-parse", "HEAD") == head and git("branch", "--show-current") == "main"
    log = (prod / "logs" / "deployments.log").read_text()
    assert f"deployed {head} ({head}) to {prod} (main), database " in log and "artifacts verified" in log
    assert "has not been restarted" in log                                    # it never claims a restart
    assert sorted(p.name for p in prod.rglob("*") if p.is_symlink()) == [".venv"]   # nothing linked
    (prod / "README.md").write_text("changed\n")                             # production runs committed revisions
    again = deploy(head)
    assert again.returncode != 0 and "local changes" in again.stderr
    git("checkout", "--quiet", "--", "README.md")
    older = deploy(f"{head}~1")                                               # moving main backwards: refused
    assert older.returncode != 0 and "not a fast-forward" in older.stderr and git("rev-parse", "HEAD") == head
    git("checkout", "--quiet", "--detach")
    detached = deploy(head)
    assert detached.returncode != 0 and "not main" in detached.stderr


def _prod_at_head(tmp_path):
    """A clone standing in for ~/trading_pipeline, on main at this repository's HEAD; ``(prod, head, deploy, git)``."""
    head = subprocess.check_output(["git", "-C", ROOT, "rev-parse", "HEAD"], text=True).strip()
    prod = tmp_path / "prod"
    subprocess.run(["git", "clone", "--quiet", ROOT, str(prod)], check=True)
    git = lambda *a: subprocess.run(["git", "-C", str(prod), *a], check=True, capture_output=True,  # noqa: E731
                                    text=True).stdout.strip()
    git("checkout", "--quiet", "-B", "main", head)
    (prod / ".env").write_text(f"DATABASE_URL={DSN}\n")
    os.symlink(os.path.join(ROOT, ".venv"), prod / ".venv")
    env = dict(os.environ, DATABASE_URL=DSN, PROD_DIR=str(prod))
    deploy = lambda rev: subprocess.run([os.path.join(ROOT, "scripts", "deploy.sh"), rev], env=env,  # noqa: E731
                                        capture_output=True, text=True, timeout=300)
    return prod, head, deploy, git


def _schema_version():
    with psycopg.connect(DSN) as c:
        return c.execute("SELECT max(version) FROM schema_migrations").fetchone()[0]


def test_deploy_refuses_while_auto_or_a_dashboard_runs_from_the_checkout(fresh, tmp_path):
    """Nothing may write the database during a deployment: Auto mode's lock held, or a dashboard process started from
    the production checkout, is refused before anything changes."""
    import fcntl
    import sys
    prod, head, deploy, git = _prod_at_head(tmp_path)
    (prod / "data").mkdir(exist_ok=True)
    lock = open(prod / "data" / "auto_mode.lock", "a+")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    lock.write("pid 4242, port 8080")
    lock.flush()
    try:
        run = deploy(head)
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()
    assert run.returncode != 0 and "Auto mode is on (pid 4242, port 8080)" in run.stdout
    assert "Nothing changed" in run.stderr
    dashboard = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)", "dashboard.app"], cwd=prod)
    try:
        run = deploy(head)
    finally:
        dashboard.kill()
        dashboard.wait()
    assert run.returncode != 0 and f"pid {dashboard.pid} runs from {prod}" in run.stdout
    assert not (prod / "logs" / "deployments.log").exists()                   # nothing deployed, nothing logged
    assert deploy(head).returncode == 0                                        # once stopped, it goes ahead


def test_deploy_checks_the_artifacts_before_it_migrates(fresh, tmp_path):
    """An installed ML artifact that does not hash to its manifest is refused before the schema changes; the
    database stays at the version it had."""
    import hashlib
    import json
    from database.connection import reset_database
    from database.migrations import latest_version
    prod, head, deploy, git = _prod_at_head(tmp_path)
    reset_database(DSN, upto=latest_version() - 1)
    try:
        d = prod / "data" / "models" / "nq_ml" / "nq_ml_nq_p1_v1"
        d.mkdir(parents=True)
        (d / "model.joblib").write_bytes(b"not the artifact the manifest describes")
        (d / "manifest.json").write_text(json.dumps({"version": "nq_ml_nq_p1_v1", "sha256": "0" * 64}))
        run = deploy(head)
        assert run.returncode != 0 and "hashes to" in run.stdout and "nothing changed" in run.stderr
        assert _schema_version() == latest_version() - 1                       # not migrated
        sha = hashlib.sha256((d / "model.joblib").read_bytes()).hexdigest()
        (d / "manifest.json").write_text(json.dumps({"version": "nq_ml_nq_p1_v1", "sha256": sha}))
        run = deploy(head)
        assert run.returncode == 0, run.stdout + run.stderr
        assert _schema_version() == latest_version() and "artifacts: verified" in run.stdout
    finally:
        reset_database(DSN)
