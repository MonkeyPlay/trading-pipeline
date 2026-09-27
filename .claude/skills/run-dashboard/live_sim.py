"""
A simulated live session in the disposable tp_test database (reset by the test
suite anyway): 139 complete synthetic NQ sessions, spot VIX, and 'today'
(2026-06-12) stored as the real-time streamer stores it, up to a given minute.

    python live_sim.py setup 23      # reset tp_test; today's bars before 09:30 + 23 min
    python live_sim.py add 25        # today's bars before 09:30 + 25 min (the new ones only)
"""
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", ".."))

from database.connection import get_db_connection, reset_database
from database.queries import save_bars_by_day, save_live_bars, set_active_contracts, upsert_contract
from features.session_windows import enrich_candle_timezones
from tests.synthetic import NQ_CID, VIX_CID, make_market

DSN = "postgresql://trading:trading@localhost:5432/tp_test"
TODAY = "2026-06-12"
COLS = ["contract_id", "timestamp_utc", "trading_day", "session_scope", "open", "high", "low", "close", "volume"]


def records(df, cid):
    df = df.assign(contract_id=cid)
    df = enrich_candle_timezones(df.assign(timestamp_utc=df["bar_start_at"].dt.strftime("%Y-%m-%d %H:%M:%S")))
    return df[COLS].to_dict("records")


def main(action, minutes):
    md, sessions = make_market(last_day=TODAY, n_sessions=140)      # deterministic (seeded)
    nq = md._bars[(NQ_CID, "TRADES")]
    today = sessions[-1]
    if action == "setup":
        reset_database(DSN)
    conn = get_db_connection(DSN)
    if action == "setup":
        upsert_contract(conn, NQ_CID, "NQ", "20260918", "CME")
        upsert_contract(conn, VIX_CID, "VIX", None, "CBOE", sec_type="IND")
        save_bars_by_day(conn, records(nq[nq["bar_start_at"] < today.overnight_start_at], NQ_CID))
        save_bars_by_day(conn, records(md._bars[(VIX_CID, "TRADES")], VIX_CID))
        set_active_contracts(conn, "NQ", {s.session_date.isoformat(): NQ_CID for s in sessions}, "sim")
        start = today.overnight_start_at
    else:
        row = conn.execute("SELECT max(timestamp_utc) FROM bars WHERE contract_id = %s AND trading_day = %s",
                           (NQ_CID, TODAY)).fetchone()
        start = datetime.fromisoformat(str(row[0])).replace(tzinfo=timezone.utc) + timedelta(minutes=1)
    cut = today.rth_open_at + timedelta(minutes=minutes)
    new = nq[(nq["bar_start_at"] >= start) & (nq["bar_start_at"] < cut)]
    now = datetime.now(timezone.utc)
    bars = [{**r, "received_at": now, "finalised_by": "next_bar"} for r in records(new, NQ_CID)]
    print(action, save_live_bars(conn, NQ_CID, bars), f"{len(bars)} bar(s) up to", cut.isoformat())
    conn.close()


if __name__ == "__main__":
    main(sys.argv[1], int(sys.argv[2]))
