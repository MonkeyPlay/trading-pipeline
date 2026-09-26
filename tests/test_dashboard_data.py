# tests/test_dashboard_data.py
"""
Data behind the Session Explorer: which contract it opens on, the forecast it
finds for a day, and the matching historical day's regular session.

Needs a disposable PostgreSQL + TimescaleDB database whose name contains
"test", which it RESETS (see tests/test_forecast_store.py).
"""

import os
from datetime import timedelta

import pandas as pd
import psycopg
import pytest

from database.connection import get_db_connection, reset_database
from database.queries import (
    get_day_prediction,
    get_latest_contract,
    save_analogue_matches,
    save_bars_by_day,
    save_feature_snapshot,
    save_prediction,
    set_active_contracts,
    upsert_contract,
)
from features import calendar as cal
from features.session_windows import enrich_candle_timezones
from tests.synthetic import ES_CID, NQ_CID, make_market

DSN = os.getenv("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not DSN or "test" not in (psycopg.conninfo.conninfo_to_dict(DSN).get("dbname") or ""),
    reason="set TEST_DATABASE_URL to a disposable database whose name contains 'test'",
)

NQ_DEC = 201            # the next NQ contract: only its warm-up days are stored
NQ_JUN = 202            # the expiring June contract, still holding the first sessions
LAST_DAY = "2026-06-12"


def _store(conn, df, cid):
    df = df.assign(contract_id=cid)
    df = enrich_candle_timezones(df.assign(timestamp_utc=df["bar_start_at"].dt.strftime("%Y-%m-%d %H:%M:%S")))
    save_bars_by_day(conn, df[["contract_id", "timestamp_utc", "trading_day", "session_scope", "open", "high",
                               "low", "close", "volume"]].to_dict("records"))


@pytest.fixture(scope="module")
def market():
    reset_database(DSN)
    conn = get_db_connection(DSN)
    md, sessions = make_market(last_day=LAST_DAY, n_sessions=8)
    upsert_contract(conn, NQ_CID, "NQ", "20260918", "CME")
    upsert_contract(conn, NQ_DEC, "NQ", "20261218", "CME")
    upsert_contract(conn, NQ_JUN, "NQ", "20260619", "CME")
    upsert_contract(conn, ES_CID, "ES", "20260918", "CME")
    nq = md._bars[(NQ_CID, "TRADES")]
    _store(conn, nq, NQ_CID)
    warmup = [s for s in sessions[-3:]]
    start = warmup[0].overnight_start_at
    _store(conn, nq[nq["bar_start_at"] >= start].assign(close=lambda d: d["close"] + 50.0), NQ_DEC)
    _store(conn, nq[nq["bar_start_at"] < sessions[2].overnight_start_at].assign(open=lambda d: d["open"] - 30.0),
           NQ_JUN)
    _store(conn, md._bars[(ES_CID, "TRADES")], ES_CID)
    days = [s.session_date.isoformat() for s in sessions]
    set_active_contracts(conn, "NQ", {d: NQ_CID for d in days}, "test")
    yield conn, md, sessions
    conn.close()


def test_latest_contract_is_the_active_one_not_the_latest_expiry(market):
    conn, _, sessions = market
    # The December contract holds the same latest day (warm-up), but September is active.
    assert get_latest_contract(conn, "NQ")["contract_id"] == NQ_CID
    set_active_contracts(conn, "NQ", {LAST_DAY: NQ_DEC}, "test")      # the roll
    assert get_latest_contract(conn, "NQ")["contract_id"] == NQ_DEC
    set_active_contracts(conn, "NQ", {LAST_DAY: NQ_CID}, "test")
    # Without roll assignments: the contract with the most recent stored day.
    assert get_latest_contract(conn, "ES")["contract_id"] == ES_CID
    assert get_latest_contract(conn, "RTY") is None


def _prediction(conn, cid, day, raw="{}"):
    cutoff = cal.session(day).rth_open_at.isoformat()
    sid = save_feature_snapshot(conn, cid, cutoff, None, None, None, None, None, None, 0.0, "FLAT", None, None,
                                {}, "v1.0")
    return save_prediction(conn, cid, cutoff, sid, "analogue_baseline_v1", "none", "BULLISH",
                           {"primary_scenario": {"name": "Gap and go"}}, {"bullish_continuation_pct": 60.0},
                           raw, pd.Timestamp.now(tz="UTC").isoformat())


def test_day_prediction_prefers_the_selected_contract(market):
    conn, _, sessions = market
    day = sessions[-2].session_date.isoformat()
    assert get_day_prediction(conn, "NQ", day) is None
    sep = _prediction(conn, NQ_CID, day)
    assert get_day_prediction(conn, "NQ", day, NQ_DEC)["prediction_id"] == sep     # same symbol, other contract
    dec = _prediction(conn, NQ_DEC, day)
    assert get_day_prediction(conn, "NQ", day, NQ_CID)["prediction_id"] == sep
    assert get_day_prediction(conn, "NQ", day, NQ_DEC)["prediction_id"] == dec
    row = get_day_prediction(conn, "NQ", day, NQ_DEC)
    assert row["symbol"] == "NQ" and row["expiry"] == "20261218"
    assert get_day_prediction(conn, "NQ", sessions[-3].session_date.isoformat()) is None
    assert get_day_prediction(conn, "ES", day) is None


def test_stored_forecast_reads_the_repr_without_evaluating_it(market):
    conn, _, sessions = market
    from dashboard.views.candles import stored_forecast
    day = sessions[-1].session_date.isoformat()
    pid = _prediction(conn, NQ_CID, day, raw=str({"forecast_horizon": "First 60 minutes",
                                                   "analogue_rationale": "Closest days rallied."}))
    save_analogue_matches(conn, pid, [{"match_date": sessions[-3].session_date.isoformat(),
                                       "similarity_score": 0.9, "ranking": 1}])
    f = stored_forecast(get_day_prediction(conn, "NQ", day, NQ_CID))
    assert f["opening_bias"] == "BULLISH" and f["probabilities"]["bullish_continuation_pct"] == 60.0
    assert f["scenarios"]["primary_scenario"]["name"] == "Gap and go"
    assert f["analogue_rationale"] == "Closest days rallied."
    row = dict(get_day_prediction(conn, "NQ", day, NQ_CID))
    assert "analogue_rationale" not in stored_forecast({**row, "raw_response": "__import__('os').getcwd()"})


def test_analogue_session_is_the_regular_session(market):
    conn, md, sessions = market
    from dashboard.views.candles import load_analogue_session
    s, prev = sessions[-4], sessions[-5]
    out = load_analogue_session(conn, "NQ", s.session_date.isoformat())
    assert out["contract"]["contract_id"] == NQ_CID
    bars = out["bars"]
    assert len(bars) == 390 and set(bars["session_scope"]) == {"RTH"}
    first = pd.Timestamp(bars["timestamp_utc"].iloc[0], tz="UTC")
    assert first == s.rth_open_at and bars["vwap"].notna().all()

    nq = md._bars[(NQ_CID, "TRADES")]
    prev_rth = nq[(nq["bar_start_at"] >= prev.rth_open_at) & (nq["bar_start_at"] < prev.scheduled_close_at)]
    on = nq[(nq["bar_start_at"] >= s.overnight_start_at) & (nq["bar_start_at"] < s.rth_open_at)]
    rth = nq[(nq["bar_start_at"] >= s.rth_open_at) & (nq["bar_start_at"] < s.scheduled_close_at)]
    assert out["levels"]["previous_rth_close"] == pytest.approx(prev_rth["close"].iloc[-1])
    assert out["levels"]["overnight_high"] == pytest.approx(on["high"].max())
    assert out["levels"]["overnight_low"] == pytest.approx(on["low"].min())
    assert out["stats"]["open"] == pytest.approx(rth["open"].iloc[0])
    assert out["stats"]["close"] == pytest.approx(rth["close"].iloc[-1])
    assert out["stats"]["high"] == pytest.approx(rth["high"].max())

    five = load_analogue_session(conn, "NQ", s.session_date.isoformat(), timeframe="5m")
    assert len(five["bars"]) == 78


def test_analogue_day_uses_the_closest_higher_contract(market):
    conn, _, sessions = market
    from dashboard.views.candles import load_analogue_session
    # Held by September and December (warm-up): September expires first after the day.
    recent = sessions[-1].session_date.isoformat()
    assert load_analogue_session(conn, "NQ", recent)["contract"]["contract_id"] == NQ_CID
    # Held by June, September: June is the closest contract expiring after it.
    first = sessions[0].session_date.isoformat()
    assert load_analogue_session(conn, "NQ", first)["contract"]["contract_id"] == NQ_JUN
    # Only September holds this one.
    mid = sessions[-5].session_date.isoformat()
    assert load_analogue_session(conn, "NQ", mid)["contract"]["contract_id"] == NQ_CID
    missing = load_analogue_session(conn, "NQ", (sessions[0].session_date - timedelta(days=30)).isoformat())
    assert missing["bars"] is None and missing["stats"] is None


def test_contract_order_for_a_day(monkeypatch):
    from dashboard.views import candles
    held = [{"contract_id": i, "expiry": e} for i, e in
            ((1, "20260320"), (2, "202606"), (3, "20260918"), (4, "20261218"))]   # nearest expiry first
    monkeypatch.setattr(candles, "contracts_with_day", lambda conn, symbol, day: held)
    order = lambda day: [c["contract_id"] for c in candles.analogue_contracts(None, "NQ", day)]
    assert order("2026-08-07") == [3, 4, 2, 1]      # Sep, then Dec; expired ones last, closest first
    assert order("2026-06-15") == [2, 3, 4, 1]      # a contract month counts through its month
    assert order("2026-09-18") == [3, 4, 2, 1]      # expiry day itself
    assert order("2026-12-21") == [4, 3, 2, 1]      # nothing later holds it: closest earlier first
