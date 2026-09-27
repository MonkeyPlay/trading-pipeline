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


def test_analogue_session_is_the_regular_session_with_15_minutes_either_side(market):
    conn, md, sessions = market
    from dashboard.views.candles import load_analogue_session
    s, prev = sessions[-4], sessions[-5]
    out = load_analogue_session(conn, "NQ", s.session_date.isoformat())
    assert out["contract"]["contract_id"] == NQ_CID
    bars = out["bars"]
    # 09:15 .. 16:14: 15 muted minutes, the 390 regular ones, 15 muted minutes
    assert len(bars) == 420 and bars["vwap"].notna().all()
    first = pd.Timestamp(bars["timestamp_utc"].iloc[0], tz="UTC")
    last = pd.Timestamp(bars["timestamp_utc"].iloc[-1], tz="UTC")
    assert first == s.rth_open_at - pd.Timedelta(minutes=15)
    assert last == s.scheduled_close_at + pd.Timedelta(minutes=14)
    muted = bars["muted"].tolist()
    assert muted[:15] == [True] * 15 and muted[15:405] == [False] * 390 and muted[405:] == [True] * 15
    assert set(bars.loc[~bars["muted"], "session_scope"]) == {"RTH"}
    # the matched day's own pre-open features, as the selected day's panel shows them
    assert out["snapshot"]["trading_day"] == s.session_date.isoformat()
    assert out["snapshot"]["previous_rth_close"] == pytest.approx(out["levels"]["previous_rth_close"])

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
    assert len(five["bars"]) == 84 and five["bars"]["muted"].sum() == 6


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
    from database import queries
    held = [{"contract_id": i, "expiry": e} for i, e in
            ((1, "20260320"), (2, "202606"), (3, "20260918"), (4, "20261218"))]   # nearest expiry first
    monkeypatch.setattr(queries, "contracts_with_day", lambda conn, symbol, day: held)
    order = lambda day: [c["contract_id"] for c in candles.analogue_contracts(None, "NQ", day)]
    assert order("2026-08-07") == [3, 4, 2, 1]      # Sep, then Dec; expired ones last, closest first
    assert order("2026-06-15") == [2, 3, 4, 1]      # a contract month counts through its month
    assert order("2026-09-18") == [3, 4, 2, 1]      # expiry day itself
    assert order("2026-12-21") == [4, 3, 2, 1]      # nothing later holds it: closest earlier first


def test_coverage_map_scores_weeks(market):
    conn, _, sessions = market
    from datetime import date
    from dashboard.components.coverage_map import band, chart_options, coverage_weeks
    # Stored NQ sessions: 2026-06-03 .. 06-12 (8 days); "today" is the next Monday.
    data = coverage_weeks(conn, ["NQ", "ES", "RTY"], today=date(2026, 6, 15))
    assert data["weeks"] == [date(2026, 6, 1), date(2026, 6, 8), date(2026, 6, 15)]
    cells = data["cells"]
    assert cells[("NQ", date(2026, 6, 8))]["category"] == 5                 # 5/5 complete
    first = cells[("NQ", date(2026, 6, 1))]
    assert (first["complete"], first["missing"], first["category"]) == (3, 2, 3)   # 60 %
    assert cells[("NQ", date(2026, 6, 15))]["category"] == 0                # no finished day yet
    assert cells[("RTY", date(2026, 6, 8))]["category"] == 1                # nothing stored
    assert "Week of Mon 2026-06-08" in cells[("NQ", date(2026, 6, 8))]["tip"]

    # A partial day lowers the week below "complete"; the best contract of a day counts.
    def partial(day):
        with conn:
            conn.execute("UPDATE session_days SET status = 'PARTIAL', bar_count = 690, expected_bar_count = 1380 "
                         "WHERE contract_id = %s AND trading_day = %s;", (NQ_CID, day))
        return coverage_weeks(conn, ["NQ"], today=date(2026, 6, 15))["cells"][("NQ", date(2026, 6, 8))]

    week = partial("2026-06-10")          # December's warm-up copy of the day is complete
    assert (week["complete"], week["category"]) == (5, 5)
    week = partial("2026-06-09")          # only September holds this one
    assert (week["complete"], week["partial"], week["category"]) == (4, 1, 4)   # 90 %

    assert [band(s, False) for s in (None, 0.0, 0.2, 0.5, 0.95)] == [0, 1, 2, 3, 4] and band(1.0, True) == 5
    opts = chart_options(data)
    assert len(opts["series"][0]["data"]) == 3 * 3 and opts["yAxis"]["data"] == ["NQ", "ES", "RTY"]


def test_session_window_and_early_close():
    from dashboard.views.candles import session_window, window_bars
    w = session_window("2026-11-27")                       # the day after Thanksgiving closes at 13:00
    assert (w["start"].strftime("%H:%M"), w["open"].strftime("%H:%M"),
            w["close"].strftime("%H:%M"), w["end"].strftime("%H:%M")) == ("09:15", "09:30", "13:00", "13:15")
    outside = session_window("2023-06-01")                 # outside the calendar: 09:30-16:00
    assert (outside["start"].strftime("%H:%M"), outside["end"].strftime("%H:%M")) == ("09:15", "16:15")

    idx = pd.date_range("2026-11-27 08:00", "2026-11-27 16:59", freq="1min", tz="America/New_York")
    df = pd.DataFrame({"timestamp_ny": idx, "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1})
    shown = window_bars(df, "2026-11-27")
    assert shown["timestamp_ny"].iloc[0].strftime("%H:%M") == "09:15"
    assert shown["timestamp_ny"].iloc[-1].strftime("%H:%M") == "13:14"
    assert shown["muted"].sum() == 30
    # a 30-minute bar starting 09:00 overlaps the window and is shown, muted
    half = df.set_index("timestamp_ny").resample("30min").agg("first").reset_index()
    shown30 = window_bars(half, "2026-11-27", "30m")
    assert shown30["timestamp_ny"].iloc[0].strftime("%H:%M") == "09:00" and shown30["muted"].iloc[0]


def test_day_contracts_put_the_active_contract_first(market):
    conn, _, sessions = market
    from dashboard.views.candles import analogue_contracts, day_contracts
    from database.queries import set_active_contracts
    recent = sessions[-1].session_date.isoformat()
    held = analogue_contracts(conn, "NQ", recent)
    assert len(held) >= 2
    later = held[1]["contract_id"]
    set_active_contracts(conn, "NQ", {recent: later}, "test")
    assert [c["contract_id"] for c in day_contracts(conn, "NQ", recent)][0] == later


def test_first_hour_move_from_the_stored_bars(market):
    conn, md, sessions = market
    from forecaster.analogue import first_hour_move
    from forecaster.evaluator import evaluate_session_outcomes
    s = sessions[-4]
    got = first_hour_move(conn, "NQ", s.session_date.isoformat())
    nq = md._bars[(NQ_CID, "TRADES")]
    hour = nq[(nq["bar_start_at"] >= s.rth_open_at) & (nq["bar_start_at"] < s.rth_open_at + timedelta(hours=1))]
    assert got["contract_id"] == NQ_CID
    assert got["open"] == pytest.approx(hour["open"].iloc[0]) and got["close"] == pytest.approx(hour["close"].iloc[-1])
    assert got["move"] == pytest.approx(got["close"] - got["open"])
    assert first_hour_move(conn, "NQ", "2026-01-05") is None                  # nothing stored that day

    # the post-close outcome keeps the same 10:29 close
    df = nq.assign(timestamp_utc=nq["bar_start_at"].dt.strftime("%Y-%m-%d %H:%M:%S"))
    out = evaluate_session_outcomes(df, s.session_date.isoformat())
    assert out["raw_outcomes"]["first_60_minute_close"] == pytest.approx(got["close"])


def test_opening_range_box_and_channel():
    from dashboard.components.spec import build_chart_spec
    from dashboard.views.candles import opening_range, window_bars
    idx = pd.date_range("2026-06-10 09:00", "2026-06-10 16:30", freq="1min", tz="America/New_York")
    df = pd.DataFrame({"timestamp_ny": idx, "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0,
                       "volume": 1})
    df.loc[df["timestamp_ny"] == pd.Timestamp("2026-06-10 09:37", tz="America/New_York"), "high"] = 110.0
    df.loc[df["timestamp_ny"] == pd.Timestamp("2026-06-10 09:45", tz="America/New_York"), "low"] = 80.0  # after it
    orr = opening_range(df, "2026-06-10")
    assert (orr["high"], orr["low"]) == (110.0, 99.0)
    assert orr["start"].strftime("%H:%M") == "09:30" and orr["end"].strftime("%H:%M") == "09:45"

    spec = build_chart_spec(window_bars(df, "2026-06-10"), opening_range=orr)
    box, orh = spec["series"]["or:box_high"], spec["series"]["features:orh"]
    epoch = lambda hhmm: int(pd.Timestamp(f"2026-06-10 {hhmm}").value // 10 ** 9)   # wall clock, as drawn
    assert [p["time"] for p in box["points"]] == [epoch("09:30"), epoch("09:44")]
    assert box["style"]["line_visible"] is False and spec["bands"]["or:box"]["lower"] == "or:box_low"
    assert [p["time"] for p in orh["points"]] == [epoch("09:45"), epoch("16:14")]    # to the last bar shown
    assert orh["points"][0]["value"] == 110.0 and spec["series"]["features:orl"]["points"][0]["value"] == 99.0
    assert spec["bands"]["or:range"] == {"upper": "features:orh", "lower": "features:orl",
                                         "color": spec["bands"]["or:range"]["color"]}
    assert {"ORH", "ORL"} <= {item["label"] for item in spec["legend"]}
    # no opening range, no drawing
    assert not any(k.startswith("or:") for k in build_chart_spec(window_bars(df, "2026-06-10"))["series"])
    assert opening_range(df[df["timestamp_ny"].dt.hour < 9], "2026-06-10") is None


def test_first_hours_from_one_query(market):
    conn, md, sessions = market
    from forecaster.first_hour import load_first_hours
    hours = load_first_hours(conn, "NQ")
    s = sessions[-1]
    day = s.session_date.isoformat()
    assert day in hours
    h = hours[day]
    assert h.contract_id == NQ_CID                    # September, not December's warm-up copy of the day
    nq = md._bars[(NQ_CID, "TRADES")]
    hour = nq[(nq["bar_start_at"] >= s.rth_open_at) & (nq["bar_start_at"] < s.rth_open_at + timedelta(hours=1))]
    assert h.open == pytest.approx(hour["open"].iloc[0]) and h.close == pytest.approx(hour["close"].iloc[-1])
    assert h.high == pytest.approx(hour["high"].max()) and h.low == pytest.approx(hour["low"].min())


def test_overlay_is_placed_on_the_selected_day_from_its_09_29_close():
    from dashboard.views.candles import overlay_bars
    ny = "America/New_York"
    bars = pd.DataFrame({"timestamp_ny": pd.to_datetime(["2026-01-05 09:29", "2026-01-05 09:30", "2026-01-05 10:00"])
                         .tz_localize(ny), "open": [100.0, 100.0, 110.0], "high": [101.0, 102.0, 112.0],
                         "low": [99.0, 98.0, 108.0], "close": [100.0, 101.0, 111.0], "muted": [True, False, False]})
    # a winter match overlaid on a summer day: the open maps to the open, whatever the UTC offset
    out = overlay_bars({"bars": bars, "close_0929": 100.0}, "2026-01-05", "2026-06-10", anchor=20000.0)
    assert [t.strftime("%Y-%m-%d %H:%M") for t in out["timestamp_ny"].dt.tz_convert(ny)] == [
        "2026-06-10 09:29", "2026-06-10 09:30", "2026-06-10 10:00"]
    assert out["close"].tolist() == [20000.0, 20200.0, 22200.0]          # moves in percent from 09:29
    assert out["high"].iloc[2] == 22400.0 and "muted" not in out.columns
    assert overlay_bars({"bars": bars, "close_0929": None}, "2026-01-05", "2026-06-10", 20000.0) is None
    assert overlay_bars(None, "2026-01-05", "2026-06-10", 20000.0) is None


def test_default_view_and_overlay_in_the_spec():
    from dashboard.components.spec import build_chart_spec
    idx = pd.date_range("2026-06-10 09:15", "2026-06-10 16:14", freq="1min", tz="America/New_York")
    df = pd.DataFrame({"timestamp_ny": idx, "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1})
    epoch = lambda hhmm: int(pd.Timestamp(f"2026-06-10 {hhmm}").value // 10 ** 9)
    spec = build_chart_spec(df, visible_range=(idx[0], pd.Timestamp("2026-06-10 10:45", tz="America/New_York")),
                            overlay=df.head(3))
    assert spec["visible_range"] == {"from": epoch("09:15"), "to": epoch("10:45")}
    assert spec["fit"] is False and spec["keep_view"] is False
    assert [p["time"] for p in spec["overlay_candles"]] == [epoch("09:15"), epoch("09:16"), epoch("09:17")]
    kept = build_chart_spec(df, keep_view=True)
    assert "visible_range" not in kept and kept["keep_view"] is True and kept["overlay_candles"] == []


def test_scenario_backfill_stores_walk_forward(market):
    conn, _, sessions = market
    from database.queries import get_analogue_matches
    from forecaster import analogue
    from forecaster.client import MODEL_VERSION
    days = [s.session_date.isoformat() for s in sessions]
    log = []
    counts = analogue.backfill(conn, days[0], days[-1], log=log.append)
    assert counts["stored"] >= 3 and counts["stored"] + counts["unavailable"] + counts["skipped"] == len(days)
    last = get_day_prediction(conn, "NQ", days[-1], NQ_CID)
    assert last["model_version"] == MODEL_VERSION
    matches = [str(m["match_date"]) for m in get_analogue_matches(conn, last["prediction_id"])]
    assert matches and all(d < days[-1] for d in matches)            # earlier sessions only
    again = analogue.backfill(conn, days[0], days[-1], log=log.append)
    assert again["stored"] == 0 and again["skipped"] == counts["stored"]


def test_backtests_page_rows():
    from dashboard.views.backtests import first_hour_rows, scenario_rows, verdict
    s = lambda g, se: {"model": 0.3, "base": 0.3 + g, "gain": g, "gain_se": se}
    assert verdict(0.03, 0.01) == "better" and verdict(-0.03, 0.01) == "worse" and verdict(0.01, 0.01) == "no difference"
    assert verdict(float("nan"), float("nan")) == "too few sessions"
    rows = first_hour_rows({"range_matches": s(0.024, 0.0135), "range_regression": s(0.05, 0.01),
                            "first_break": s(-0.009, 0.011), "breakout": s(-0.005, 0.007)})
    assert [r["verdict"] for r in rows] == ["no difference", "better", "no difference", "no difference"]
    sc = scenario_rows({"probabilities": s(0.0, 0.01), "hits": s(0.02, 0.05), "hit_rate": 0.424,
                        "base_hit_rate": 0.407})
    assert sc[1]["forecast"] == "42.4 %" and sc[1]["gain"].startswith("+1.7 pp")


def test_range_nowcast_reads_active_contracts_and_vix(market):
    # Last in the module: it stores VIX, which the tests above do not expect.
    conn, md, sessions = market
    from database.queries import active_contract_bars, preopen_levels
    from forecaster import range_nowcast as rn
    from tests.synthetic import VIX_CID
    days = [s.session_date.isoformat() for s in sessions]

    def history():
        return rn.sessions_from_bars(pd.DataFrame([dict(r) for r in active_contract_bars(conn, "NQ")]))

    set_active_contracts(conn, "NQ", {d: NQ_CID for d in days}, "test")
    got = history()
    assert [g.day for g in got] == days
    assert {g.contract_id for g in got} == {NQ_CID}               # never the June or December copies of a day
    set_active_contracts(conn, "NQ", {days[-1]: NQ_DEC}, "test")  # the roll: the day now comes from December
    rolled = history()[-1]
    assert rolled.contract_id == NQ_DEC and rolled.close[-1] == pytest.approx(got[-1].close[-1] + 50.0)
    set_active_contracts(conn, "NQ", {days[-1]: NQ_CID}, "test")
    s, last = sessions[-1], got[-1]
    nq = md._bars[(NQ_CID, "TRADES")]
    rth = nq[(nq["bar_start_at"] >= s.rth_open_at) & (nq["bar_start_at"] < s.scheduled_close_at)]
    pre = nq[(nq["bar_start_at"] >= s.overnight_start_at) & (nq["bar_start_at"] < s.cutoff_at)]
    assert last.held == 390 and last.open == pytest.approx(rth["open"].iloc[0])
    assert last.close[-1] == pytest.approx(rth["close"].iloc[-1])      # nothing from 16:00 on is read
    assert last.pre_price == pytest.approx(pre["close"].iloc[-1])      # the 09:28 bar
    upsert_contract(conn, VIX_CID, "VIX", None, "CBOE", sec_type="IND")
    _store(conn, md._bars[(VIX_CID, "TRADES")], VIX_CID)
    vix = md._bars[(VIX_CID, "TRADES")]
    day_vix = vix[(vix["bar_start_at"] >= s.rth_open_at - timedelta(hours=7)) & (vix["bar_start_at"] < s.cutoff_at)]
    levels = {str(r["trading_day"]): r for r in preopen_levels(conn, VIX_CID)}
    level = levels[s.session_date.isoformat()]
    assert level["pre"] == pytest.approx(day_vix["close"].iloc[-1])
    assert level["last"] == pytest.approx(vix[vix["bar_start_at"] < s.scheduled_close_at + timedelta(minutes=15)]
                                          ["close"].iloc[-1])
    one = preopen_levels(conn, VIX_CID, trading_day=s.session_date.isoformat())
    assert [str(r["trading_day"]) for r in one] == [s.session_date.isoformat()]
