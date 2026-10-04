# tests/test_dashboard_data.py
"""
Data behind the Session Explorer: which contract it opens on, the contracts it
offers for a day, the coverage map, and what the chart draws.

Needs a disposable PostgreSQL + TimescaleDB database whose name contains
"test", which it RESETS:

    TEST_DATABASE_URL=postgresql://trading:trading@localhost:5432/trading_pipeline_test pytest
"""

import os

import pandas as pd
import psycopg
import pytest

from database.connection import get_db_connection, reset_database
from database.queries import get_latest_contract, save_bars_by_day, set_active_contracts, upsert_contract
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
    bars, sessions = make_market(last_day=LAST_DAY, n_sessions=8)
    upsert_contract(conn, NQ_CID, "NQ", "20260918", "CME")
    upsert_contract(conn, NQ_DEC, "NQ", "20261218", "CME")
    upsert_contract(conn, NQ_JUN, "NQ", "20260619", "CME")
    upsert_contract(conn, ES_CID, "ES", "20260918", "CME")
    nq = bars[NQ_CID]
    _store(conn, nq, NQ_CID)
    warmup = [s for s in sessions[-3:]]
    start = warmup[0].overnight_start_at
    _store(conn, nq[nq["bar_start_at"] >= start].assign(close=lambda d: d["close"] + 50.0), NQ_DEC)
    _store(conn, nq[nq["bar_start_at"] < sessions[2].overnight_start_at].assign(open=lambda d: d["open"] - 30.0),
           NQ_JUN)
    _store(conn, bars[ES_CID], ES_CID)
    days = [s.session_date.isoformat() for s in sessions]
    set_active_contracts(conn, "NQ", {d: NQ_CID for d in days}, "test")
    yield conn, bars, sessions
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


def test_contract_order_for_a_day(monkeypatch):
    from database import queries
    held = [{"contract_id": i, "expiry": e} for i, e in
            ((1, "20260320"), (2, "202606"), (3, "20260918"), (4, "20261218"))]   # nearest expiry first
    monkeypatch.setattr(queries, "contracts_with_day", lambda conn, symbol, day: held)
    order = lambda day: [c["contract_id"] for c in queries.contracts_for_day(None, "NQ", day)]
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
    assert len(opts["series"][0]["data"]) == 3 * 3 and opts["yAxis"][0]["data"] == ["NQ", "ES", "RTY"]
    assert data["newest"] == date(2026, 6, 12)
    assert opts["xAxis"][0]["axisLabel"][":interval"] == "(i) => (2 - i) % 1 === 0"  # the newest week is labelled

    # The last ten trading days that have ended, one dot each, on the same rows.
    assert data["days"][0] == date(2026, 6, 1) and data["days"][-1] == date(2026, 6, 12) and len(data["days"]) == 10
    day_cells = data["day_cells"]
    assert day_cells[("NQ", date(2026, 6, 12))]["category"] == 5
    assert day_cells[("NQ", date(2026, 6, 1))]["category"] == 1                  # before the stored sessions
    assert "not collected" in day_cells[("NQ", date(2026, 6, 1))]["tip"]
    partial_day = coverage_weeks(conn, ["NQ"], today=date(2026, 6, 15))["day_cells"][("NQ", date(2026, 6, 9))]
    assert partial_day["category"] == 3 and "partial (50 %)" in partial_day["tip"]     # 690 of 1380 bars
    dots = opts["series"][1]
    assert dots["type"] == "scatter" and len(dots["data"]) == 3 * 10 and dots["yAxisIndex"] == 1
    assert opts["xAxis"][1]["axisLabel"][":interval"] == "(i) => [4, 9].includes(i)"   # under each Friday
    assert opts["visualMap"]["seriesIndex"] == 0


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
    from dashboard.views.candles import day_contracts
    from database.queries import contracts_for_day, set_active_contracts
    recent = sessions[-1].session_date.isoformat()
    held = contracts_for_day(conn, "NQ", recent)
    assert len(held) >= 2
    later = held[1]["contract_id"]
    set_active_contracts(conn, "NQ", {recent: later}, "test")
    assert [c["contract_id"] for c in day_contracts(conn, "NQ", recent)][0] == later


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


def test_moving_averages_follow_the_pine_script():
    from dashboard.components.spec import build_chart_spec
    from features.calculations import calculate_moving_averages
    idx = pd.date_range("2026-06-10 09:00", periods=300, freq="1min", tz="America/New_York")
    close = [100 + 5 * (i % 23) / 23 + i / 50 for i in range(300)]
    df = pd.DataFrame({"timestamp_utc": idx.tz_convert("UTC"), "timestamp_ny": idx, "open": close, "high": close,
                       "low": close, "close": close, "volume": 1})

    def ema(xs, n):                       # Pine's ta.ema, written out
        alpha, out = 2 / (n + 1), []
        for x in xs:
            out.append(x if not out else alpha * x + (1 - alpha) * out[-1])
        return out

    e1 = ema(close, 14)
    e2 = ema(e1, 14)
    e3 = ema(e2, 14)
    tema = [3 * (a - b) + c for a, b, c in zip(e1, e2, e3)]
    sma3 = lambda xs, i: sum(xs[i - 2:i + 1]) / 3
    out = calculate_moving_averages(df.iloc[::-1])          # any row order: time order is used
    i = 250
    assert out["tema"].iloc[i] == pytest.approx(sma3(tema, i))
    assert out["ema_trend"].iloc[i] == pytest.approx(ema(close, 100)[i])
    assert out["ema_trigger"].iloc[i] == pytest.approx(sma3(e1, i))
    assert out["tema"].iloc[:2].isna().all() and out["ema_trigger"].iloc[2:].notna().all()

    spec = build_chart_spec(out)
    assert [item["label"] for item in spec["legend"] if item["key"].startswith("ma:")] == [
        "TEMA 14 (SMA 3)", "EMA 100", "EMA 14 (SMA 3)"]
    assert "value" not in spec["series"]["ma:tema"]["points"][0]     # SMA(3) not yet defined: a gap


def test_session_explorer_warms_the_moving_averages_up_on_earlier_days(market):
    conn, _, sessions = market
    from dashboard.views.candles import SessionExplorer, day_contracts
    view = SessionExplorer(conn)
    view.date, view.symbol, view.timeframe = LAST_DAY, "NQ", "5m"
    view.contract = next(c for c in day_contracts(conn, "NQ", LAST_DAY) if c["contract_id"] == NQ_CID)
    assert view._warmup_days() == 5                          # 1000 5-minute bars, plus a spare day
    assert str(view._history_start()) == sessions[-2].session_date.isoformat()
    assert str(view._history_start(100)) == sessions[0].session_date.isoformat()   # as many as there are
    view.load_day()
    first = view.day_df.iloc[0]
    assert view.day_df[["tema", "ema_trend", "ema_trigger"]].notna().all().all()
    assert first["ema_trend"] != first["close"]              # earlier bars behind it, not seeded on the day


def test_default_view_in_the_spec():
    from dashboard.components.spec import build_chart_spec
    idx = pd.date_range("2026-06-10 09:15", "2026-06-10 16:14", freq="1min", tz="America/New_York")
    df = pd.DataFrame({"timestamp_ny": idx, "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1})
    epoch = lambda hhmm: int(pd.Timestamp(f"2026-06-10 {hhmm}").value // 10 ** 9)
    spec = build_chart_spec(df, visible_range=(idx[0], pd.Timestamp("2026-06-10 10:45", tz="America/New_York")))
    assert spec["visible_range"] == {"from": epoch("09:15"), "to": epoch("10:45")}
    assert spec["fit"] is False and spec["keep_view"] is False
    kept = build_chart_spec(df, keep_view=True)
    assert "visible_range" not in kept and kept["keep_view"] is True
