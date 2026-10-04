# tests/test_events.py
"""The economic calendar: the ISM rule, release times, and the loader against the
database."""

import os
from datetime import date, datetime, timezone

import psycopg
import pytest

from database import events as ev

DSN = os.getenv("TEST_DATABASE_URL")
needs_db = pytest.mark.skipif(
    not DSN or "test" not in (psycopg.conninfo.conninfo_to_dict(DSN).get("dbname") or ""),
    reason="set TEST_DATABASE_URL to a disposable database whose name contains 'test'",
)


def _csv(tmp_path, name, text):
    path = tmp_path / name
    path.write_text(text)
    return str(path)


CALENDAR = ("source,event_key,date,time_et,name,tier,country\n"
            "fed,fomc-decision:2026-06,2026-06-17,14:00,FOMC rate decision,high,US\n"
            "bls,cpi:2026-05,2026-06-10,08:30,Consumer Price Index,high,US\n"
            "bls,cpi:2025-05,2025-06-11,08:30,Consumer Price Index,high,US\n")
COVERAGE = ("source,covered_from,covered_to,notes\n"
            "fed,2026-01-01,2026-12-31,decisions\n"
            "bls,2026-06-01,2026-12-31,cpi\n"
            "ism_rule,2026-06-01,2026-06-30,rule\n")


def test_ism_reports_follow_exchange_sessions():
    rows = {r["event_key"]: r["scheduled_at"] for r in ev.ism_rows(date(2025, 9, 1), date(2026, 1, 31))}
    # September 2025: Labor Day on the 1st, so the first session is the 2nd and the third the 4th
    assert rows["ism-manufacturing:2025-09"] == datetime(2025, 9, 2, 14, 0, tzinfo=timezone.utc)
    assert rows["ism-services:2025-09"] == datetime(2025, 9, 4, 14, 0, tzinfo=timezone.utc)
    # January 2026: New Year's Day closed; 10:00 ET is 15:00 UTC in winter
    assert rows["ism-manufacturing:2026-01"] == datetime(2026, 1, 2, 15, 0, tzinfo=timezone.utc)
    assert rows["ism-services:2026-01"] == datetime(2026, 1, 6, 15, 0, tzinfo=timezone.utc)
    assert len(rows) == 2 * 5


def test_calendar_rows_are_new_york_times(tmp_path):
    rows = ev.calendar_rows(_csv(tmp_path, "cal.csv", CALENDAR))
    assert rows[0]["scheduled_at"] == datetime(2026, 6, 17, 18, 0, tzinfo=timezone.utc)    # 14:00 EDT
    assert rows[1]["tier"] == "high" and rows[1]["country"] == "US"


@needs_db
def test_loader_is_idempotent_and_replaces_a_moved_release(tmp_path):
    from database.connection import get_db_connection, reset_database
    reset_database(DSN)
    conn = get_db_connection(DSN)
    try:
        cal_path, cov_path = _csv(tmp_path, "cal.csv", CALENDAR), _csv(tmp_path, "cov.csv", COVERAGE)
        first = ev.load(conn, cal_path, cov_path)
        assert first == {"events": 3 + 2, "moved": 0, "coverage": 3}      # 3 rows + ISM for June 2026
        assert ev.load(conn, cal_path, cov_path) == {"events": 0, "moved": 0, "coverage": 0}
        moved = CALENDAR.replace("2026-06-17,14:00", "2026-06-18,14:00")
        assert ev.load(conn, _csv(tmp_path, "cal2.csv", moved), cov_path) == {"events": 1, "moved": 1, "coverage": 0}
        # the FOMC decision's one row now stands on the 18th
        rows = conn.execute("SELECT scheduled_at FROM economic_events WHERE event_key = %s;",
                            ("fomc-decision:2026-06",)).fetchall()
        assert [str(r["scheduled_at"]) for r in rows] == ["2026-06-18 18:00:00"]
    finally:
        conn.close()
