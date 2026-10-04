# tests/test_coverage_rules.py
"""
When the collector treats a stored day as complete: the per-instrument, per-day
expectation, the regular-session requirement for forecast targets, and the
planner re-judging days stored under an older, looser rule.
"""

import os
from datetime import date

import pandas as pd
import psycopg
import pytest

from collector.coverage import day_expectation, plan_trading_days, scheduled_rth_minutes
from config import INSTRUMENTS
from database.queries import derive_day_status

NQ, RTY, VIX = INSTRUMENTS["NQ"], INSTRUMENTS["RTY"], INSTRUMENTS["VIX"]


def test_scheduled_regular_session_minutes():
    assert scheduled_rth_minutes(date(2026, 6, 10)) == 390
    assert scheduled_rth_minutes(date(2026, 11, 27)) == 210        # day after Thanksgiving
    assert scheduled_rth_minutes(date(2026, 6, 19)) == 0           # Juneteenth: no regular session
    assert scheduled_rth_minutes(date(2023, 6, 1)) is None         # outside the calendar


def test_day_expectation():
    assert day_expectation(NQ, date(2026, 6, 10)) == (1380, 390)
    assert day_expectation(NQ, date(2026, 11, 27)) == (1200, 210)
    assert day_expectation(RTY, date(2026, 6, 10)) == (1290, 390)   # thin overnight minutes allowed
    assert day_expectation(VIX, date(2026, 6, 10)) == (390, None)   # judged by its count only
    assert day_expectation(NQ, date(2023, 6, 1)) == (1380, None)


def test_day_status_needs_the_whole_regular_session():
    assert derive_day_status(1380, 1380, 0, 390, 390) == "COMPLETE"
    assert derive_day_status(1350, 1380, 0, 360, 390) == "PARTIAL"   # enough bars, RTH gap
    assert derive_day_status(1350, 1380, 0, 360, None) == "COMPLETE"  # the old, looser rule
    assert derive_day_status(1155, 1200, 0, 225, 210) == "COMPLETE"   # early close: Globex to 13:15
    assert derive_day_status(1200, 1380, 0, 390, 390) == "PARTIAL"   # < 90 % of the Globex day
    assert derive_day_status(0, 1380, 0, 0, 390) == "EMPTY"


DSN = os.getenv("TEST_DATABASE_URL")
db = pytest.mark.skipif(
    not DSN or "test" not in (psycopg.conninfo.conninfo_to_dict(DSN).get("dbname") or ""),
    reason="set TEST_DATABASE_URL to a disposable database whose name contains 'test'",
)


@db
def test_planner_refetches_days_stored_under_the_old_rule():
    from database.connection import get_db_connection, reset_database
    from database.queries import save_bars_by_day, upsert_contract
    from features import calendar as cal
    from features.session_windows import enrich_candle_timezones
    from tests.synthetic import NQ_CID, make_market

    reset_database(DSN)
    conn = get_db_connection(DSN)
    try:
        bars, sessions = make_market(last_day="2026-06-12", n_sessions=5)
        upsert_contract(conn, NQ_CID, "NQ", "20260918", "CME")
        df = bars[NQ_CID]
        gap_day = cal.session("2026-06-10")
        hole = (df["bar_start_at"] >= gap_day.rth_open_at + pd.Timedelta(minutes=120)) & \
               (df["bar_start_at"] < gap_day.rth_open_at + pd.Timedelta(minutes=150))
        df = enrich_candle_timezones(df[~hole].assign(
            timestamp_utc=lambda d: d["bar_start_at"].dt.strftime("%Y-%m-%d %H:%M:%S")))
        # Stored the old way: judged by the day count alone, 90 % of 1290.
        save_bars_by_day(conn, df[["contract_id", "timestamp_utc", "trading_day", "session_scope", "open",
                                   "high", "low", "close", "volume"]].to_dict("records"),
                         expected_bar_count=1290)
        start, end = sessions[0].session_date, sessions[-1].session_date

        old = {p.trading_day: p for p in plan_trading_days(conn, NQ_CID, start, end, expected=1290)}
        assert old["2026-06-10"].action == "ok"                   # the looser rule let the gap through

        plan = {p.trading_day: p for p in plan_trading_days(
            conn, NQ_CID, start, end, expected=NQ.expected_bars,
            expectation=lambda d: day_expectation(NQ, d))}
        assert plan["2026-06-10"].action == "refetch"
        assert plan["2026-06-10"].reason == "regular session incomplete (360/390 bars)"
        assert plan["2026-06-10"].status == "PARTIAL"
        assert all(p.action == "ok" for day, p in plan.items() if day != "2026-06-10")

        # The dashboard's coverage map judges the week the same way.
        from dashboard.components.coverage_map import coverage_weeks
        week = coverage_weeks(conn, ["NQ"], today=date(2026, 6, 15))["cells"][("NQ", date(2026, 6, 8))]
        assert (week["complete"], week["partial"], week["category"]) == (4, 1, 4)
    finally:
        conn.close()
