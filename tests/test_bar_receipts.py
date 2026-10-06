# tests/test_bar_receipts.py
"""
When a bar reached the store (database/queries.py, migrations 0022 and 0023): a new bar gets
the database's clock for its first arrival and for its values; rewriting the day keeps both
while the values stay the same; revised values get a new version time and keep the first
arrival; an unknown first arrival stays unknown through every rewrite.
"""

import os

import psycopg
import pytest

DSN = os.getenv("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not DSN or "test" not in (psycopg.conninfo.conninfo_to_dict(DSN).get("dbname") or ""),
    reason="set TEST_DATABASE_URL to a disposable database whose name contains 'test'")
CID, DAY = 901, "2026-03-04"


def _bars(closes):
    return [{"timestamp_utc": f"2026-03-04 15:{m:02d}:00", "trading_day": DAY, "open": c, "high": c + 1, "low": c - 1,
             "close": c, "volume": 5, "session_scope": "RTH"} for m, c in closes]


def _times(conn):
    rows = conn.execute("SELECT extract(minute FROM timestamp_utc)::int, first_stored_at::text, version_stored_at::text "
                        "FROM bars WHERE contract_id = %s ORDER BY timestamp_utc;", (CID,)).fetchall()
    return {m: (f, v) for m, f, v in rows}


def test_receipt_times_survive_rewrites_and_follow_revisions():
    from database.connection import get_db_connection, reset_database
    from database.queries import save_trading_day, upsert_contract
    reset_database(DSN)
    conn = get_db_connection(DSN)
    upsert_contract(conn, CID, "NQ", "20260320", "CME", local_symbol="NQH6")
    save_trading_day(conn, CID, DAY, _bars([(0, 100.0), (1, 101.0), (2, 102.0)]))
    first = _times(conn)
    assert all(f is not None and f == v for f, v in first.values())                 # new: both the clock
    save_trading_day(conn, CID, DAY, _bars([(0, 100.0), (1, 101.0), (2, 102.0)]))
    assert _times(conn) == first                                                     # a rewrite changes nothing
    save_trading_day(conn, CID, DAY, _bars([(0, 100.0), (1, 101.5), (2, 102.0), (3, 103.0)]))
    after = _times(conn)
    assert after[0] == first[0] and after[2] == first[2]                             # unchanged: kept
    assert after[1][0] == first[1][0] and after[1][1] > first[1][1]                  # revised: new version only
    assert after[3][0] is not None and after[3][0] > first[0][0]                     # new bar: its own arrival
    with conn:
        conn.execute("UPDATE bars SET first_stored_at = NULL, version_stored_at = NULL WHERE contract_id = %s AND "
                     "extract(minute FROM timestamp_utc) = 0;", (CID,))              # held from before: unknown
    save_trading_day(conn, CID, DAY, _bars([(0, 100.0), (1, 101.5), (2, 102.0), (3, 103.0)]))
    assert _times(conn)[0] == (None, None)                                           # unknown stays unknown
    save_trading_day(conn, CID, DAY, _bars([(0, 99.0), (1, 101.5), (2, 102.0), (3, 103.0)]))
    f, v = _times(conn)[0]
    assert f is None and v is not None                                               # revised: a known version time
    conn.close()
