# tests/test_backup_verify.py
"""
scripts/verify_backup.py on a real archive of the test database (never production): it records the archive's
sha256, header and the source's schema and row counts, restores it into the disposable
trading_pipeline_restore_check database with TimescaleDB's procedure and compares, drops it again - and catches an
archive that changed after it was recorded.

Needs TEST_DATABASE_URL (a disposable database whose name contains "test"; it is RESET) and the running database
container (docker), whose pg_dump makes the archive.
"""

import os
import shutil
import subprocess

import psycopg
import pytest

DSN = os.getenv("TEST_DATABASE_URL")


def _container():
    if not shutil.which("docker"):
        return None
    run = subprocess.run(["docker", "ps", "--format", "{{.Names}}", "--filter", "name=timescaledb"],
                         capture_output=True, text=True)
    names = run.stdout.split()
    return names[0] if run.returncode == 0 and names else None


pytestmark = pytest.mark.skipif(
    not DSN or "test" not in (psycopg.conninfo.conninfo_to_dict(DSN).get("dbname") or "") or _container() is None,
    reason="set TEST_DATABASE_URL to a disposable database whose name contains 'test', with the database container "
           "running")


def test_a_backup_is_recorded_restored_into_a_disposable_database_and_compared(tmp_path, capsys):
    import json
    from database.connection import reset_database
    from database.migrations import latest_version
    from scripts import verify_backup
    reset_database(DSN)
    with psycopg.connect(DSN, autocommit=True) as c:
        c.execute("INSERT INTO economic_event_coverage (source, covered_from, covered_to) VALUES "
                  "('test', '2026-01-01', '2026-12-31')")
    dump = tmp_path / "trading_pipeline_backup_test.dump"
    with open(dump, "wb") as f:
        subprocess.run(["docker", "exec", _container(), "pg_dump", "--format=custom", "--no-owner", DSN], stdout=f,
                       stderr=subprocess.DEVNULL, check=True)
    assert verify_backup.main([str(dump), "--at-backup", "--restore", "--db", DSN]) == 0
    out = capsys.readouterr().out
    assert "VALID" in out and "matches the source's counts: True" in out
    rec = json.loads((tmp_path / "trading_pipeline_backup_test.dump.json").read_text())
    assert rec["archive"]["dbname"] == psycopg.conninfo.conninfo_to_dict(DSN)["dbname"]
    assert rec["source"]["schema_version"] == rec["restore"]["schema_version"] == latest_version()
    assert rec["restore"]["rows"]["public.economic_event_coverage"] == 1 and rec["restore"]["pg_restore_exit"] == 0
    assert len(rec["file"]["sha256"]) == 64 and rec["file"]["bytes"] == dump.stat().st_size
    with psycopg.connect(DSN, autocommit=True) as c:                        # the disposable database is gone again
        assert c.execute("SELECT count(*) FROM pg_database WHERE datname = %s",
                         (verify_backup.RESTORE_DB,)).fetchone()[0] == 0
    with open(dump, "ab") as f:                                               # changed after it was recorded
        f.write(b"x")
    assert verify_backup.main([str(dump), "--db", DSN]) == 1
    assert "it changed" in capsys.readouterr().out
    reset_database(DSN)
