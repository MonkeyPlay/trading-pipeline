# tests/test_live_store.py
"""
Real-time bar storage (migration 0005, queries.save_live_bars) against a
disposable database - see tests/test_forecast_store.py for TEST_DATABASE_URL.
"""

import os
from datetime import datetime, timedelta, timezone

import psycopg
import pytest

from database.connection import get_db_connection, reset_database
from database.queries import get_bar_receipts, get_day_bars, get_session_day, save_live_bars, \
    save_trading_day, upsert_contract
from features.market_data import DbMarketData

DSN = os.getenv("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not DSN or "test" not in (psycopg.conninfo.conninfo_to_dict(DSN).get("dbname") or ""),
    reason="set TEST_DATABASE_URL to a disposable database whose name contains 'test'",
)

CID, DAY = 201, "2026-06-10"
T0 = datetime(2026, 6, 10, 13, 20, tzinfo=timezone.utc)          # 09:20 ET


def bar(i, close, received=None, how="next_bar"):
    start = T0 + timedelta(minutes=i)
    return {"timestamp_utc": start.strftime("%Y-%m-%d %H:%M:%S"), "trading_day": DAY,
            "session_scope": "ETH", "open": 100.0, "high": max(close, 100.0) + 1, "low": 99.0,
            "close": close, "volume": 10, "received_at": received or start + timedelta(seconds=62),
            "finalised_by": how}


@pytest.fixture(scope="module")
def conn():
    reset_database(DSN)
    c = get_db_connection(DSN)
    upsert_contract(c, CID, "NQ", "20260918", "CME")
    yield c
    c.close()


def closes(conn):
    return {r["timestamp_utc"][11:16]: (r["close"], r["source"])
            for r in get_day_bars(conn, CID, DAY)}


def test_live_bars_and_receipts(conn):
    counts = save_live_bars(conn, CID, [bar(i, 100.0 + i) for i in range(9)], stream_id="t")
    assert counts == {"receipts": 9, "unchanged": 0}
    day = get_session_day(conn, CID, DAY)
    assert day["status"] == "PARTIAL" and day["open_bar_count"] == 9
    assert closes(conn)["13:28"] == (108.0, "IBKR_LIVE")

    assert save_live_bars(conn, CID, [bar(8, 108.0)]) == {"receipts": 0, "unchanged": 1}
    received = T0 + timedelta(minutes=9, seconds=2)
    save_live_bars(conn, CID, [bar(8, 108.25, received, "confirm_fetch")])
    revs = get_bar_receipts(conn, CID, T0 + timedelta(minutes=8))
    assert [(r["revision"], r["close"], r["finalised_by"]) for r in revs] == \
        [(1, 108.0, "next_bar"), (2, 108.25, "confirm_fetch")]
    assert closes(conn)["13:28"][0] == 108.25

    receipt = DbMarketData(conn).bar_receipt(CID, T0 + timedelta(minutes=8), "TRADES")
    assert receipt["revision"] == 2 and receipt["received_at"] == received


def test_two_revisions_in_one_batch_keep_their_own_receive_times(conn):
    first, second = T0 + timedelta(minutes=10, seconds=1), T0 + timedelta(minutes=10, seconds=9)
    save_live_bars(conn, CID, [bar(9, 50.0, first), bar(9, 51.0, second, "late_update")])
    revs = get_bar_receipts(conn, CID, T0 + timedelta(minutes=9))
    assert [(r["revision"], r["close"], r["received_at"]) for r in revs] == \
        [(1, 50.0, "2026-06-10 13:30:01"), (2, 51.0, "2026-06-10 13:30:09")]


def test_receipts_are_append_only(conn):
    for sql in ("UPDATE bar_receipts SET close = 0", "DELETE FROM bar_receipts"):
        with pytest.raises(psycopg.Error, match="append-only"):
            conn.execute(sql)


def test_download_keeps_newer_live_bars(conn):
    # A download requested at 09:26 covers 09:20..09:25 (09:22 had no print in it).
    download = [dict(bar(i, 200.0 + i), source="IBKR") for i in range(6) if i != 2]
    save_trading_day(conn, CID, DAY, download)
    c = closes(conn)
    assert c["13:25"] == (205.0, "IBKR")            # downloaded minutes: the download wins
    assert "13:22" not in c                          # older than the download: not resurrected
    assert c["13:26"] == (106.0, "IBKR_LIVE")        # newer live minutes survive the replace
    assert c["13:28"] == (108.25, "IBKR_LIVE")       # at their latest revision
    assert get_session_day(conn, CID, DAY)["bar_count"] == 9      # 09:20-09:25 less 09:22, 09:26-09:29

    # A later download through 09:28 replaces those live minutes too (the receipts remain);
    # the 09:29 minute it does not reach stays live.
    save_trading_day(conn, CID, DAY, [dict(bar(i, 300.0 + i), source="IBKR") for i in range(9)])
    c = closes(conn)
    assert {v[1] for k, v in c.items() if k <= "13:28"} == {"IBKR"}
    assert c["13:29"] == (51.0, "IBKR_LIVE")
    assert len(get_bar_receipts(conn, CID, T0 + timedelta(minutes=8))) == 2
