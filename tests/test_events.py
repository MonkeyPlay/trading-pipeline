# tests/test_events.py
"""The economic calendar: the ISM rule, release times, coverage (no calendar is
never "no event"), the loader against the database, and how the nowcast and its
card show a day's releases."""

import os
from datetime import date, datetime, timezone

import psycopg
import pytest

from database import events as ev
from forecaster import range_nowcast as rn

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


def test_releases_count_only_on_covered_days(tmp_path):
    rows = ev.calendar_rows(_csv(tmp_path, "cal.csv", CALENDAR))
    cov = ev.coverage_rows(_csv(tmp_path, "cov.csv", COVERAGE))
    events = rn.events_from_rows(rows + ev.ism_rows(date(2026, 6, 1), date(2026, 6, 30)), cov)
    assert events.of("2026-06-17") == [(270, "high", "FOMC rate decision", "14:00")]   # minutes from 09:30
    assert events.of("2026-06-10") == [(-60, "high", "Consumer Price Index", "08:30")]
    assert [r[2] for r in events.of("2026-06-01")] == ["ISM Manufacturing PMI"]
    assert events.of("2026-06-18") == []              # covered, nothing scheduled: no release
    assert events.of("2025-06-11") is None            # no coverage that day: the calendar is missing


def test_card_lists_the_releases_and_warns_before_an_fomc_decision():
    from dashboard.views.candles import describe_releases
    fomc = [{"time": "08:30", "name": "Consumer Price Index", "tier": "high", "minute": -60},
            {"time": "14:00", "name": "FOMC rate decision", "tier": "high", "minute": 270}]
    text, warn = describe_releases(fomc, 30)
    assert text.startswith("Scheduled today: 08:30 Consumer Price Index (high) · 14:00 FOMC rate decision (high)")
    assert warn and "FOMC afternoons" in text
    assert describe_releases(fomc, 300)[1] is False           # the decision is out: nothing ahead to warn of
    assert describe_releases([], 0)[0].startswith("No scheduled release today")
    assert describe_releases(None, 0)[0].startswith("No economic calendar covers this day")


@needs_db
def test_loader_is_idempotent_and_feeds_the_snapshot_and_the_nowcast(tmp_path):
    from database.connection import get_db_connection, reset_database
    from features.market_data import DbMarketData
    reset_database(DSN)
    conn = get_db_connection(DSN)
    try:
        cal_path, cov_path = _csv(tmp_path, "cal.csv", CALENDAR), _csv(tmp_path, "cov.csv", COVERAGE)
        first = ev.load(conn, cal_path, cov_path)
        assert first == {"events": 3 + 2, "moved": 0, "coverage": 3}      # 3 rows + ISM for June 2026
        assert ev.load(conn, cal_path, cov_path) == {"events": 0, "moved": 0, "coverage": 0}
        moved = CALENDAR.replace("2026-06-17,14:00", "2026-06-18,14:00")
        assert ev.load(conn, _csv(tmp_path, "cal2.csv", moved), cov_path) == {"events": 1, "moved": 1, "coverage": 0}
        # the v2 snapshot's reader: a covered day, its FOMC decision now on the 18th
        start = datetime(2026, 6, 18, 13, 29, tzinfo=timezone.utc)
        coverage, found = DbMarketData(conn).event_calendar("2026-06-18", start,
                                                            datetime(2026, 6, 18, 20, 0, tzinfo=timezone.utc), None)
        assert coverage is not None and [e["name"] for e in found] == ["FOMC rate decision"]
        events = rn.load_events(conn)
        assert events.of("2026-06-17") == [] and events.of("2026-06-18")[0][2] == "FOMC rate decision"
    finally:
        conn.close()
