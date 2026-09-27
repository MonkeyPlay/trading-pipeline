# tests/test_range_nowcast.py
"""The range nowcast: sessions from bars, inputs that use earlier bars only, a
walk-forward model that learns persistent volatility, scenario bands, and the
dashboard's cone and table."""

import math

import numpy as np
import pandas as pd
import pytest

from features import calendar as cal
from forecaster import range_nowcast as rn


@pytest.fixture
def small(monkeypatch):
    """Short windows, so a synthetic history of a few months has nowcasts."""
    monkeypatch.setattr(rn, "USUAL_SESSIONS", 10)
    monkeypatch.setattr(rn, "MIN_TRAIN", 20)
    monkeypatch.setattr(rn, "MIN_SCENARIOS", 10)


def _days(start="2025-06-02", end="2025-09-30", full_only=True):
    return [s.session_date.isoformat() for s in cal.sessions_between(start, end)
            if s.schedule == "full" or not full_only]


def _bars(days, sigmas=None, seed=0, pre_minutes=90):
    """
    1-minute bars of ``days``: ``pre_minutes`` before the open and the regular
    session, each day's minute volatility from ``sigmas`` (default: a persistent
    random level), U-shaped through the session.
    """
    rng = np.random.default_rng(seed)
    rows, price, level = [], 20000.0, 0.0
    for i, d in enumerate(days):
        s = cal.session(d)
        level = 0.9 * level + rng.normal(0, 0.35)
        sigma = sigmas[i] if sigmas is not None else 0.0004 * math.exp(level)
        open_at = pd.Timestamp(s.rth_open_at).tz_convert("UTC")
        n = int((pd.Timestamp(s.scheduled_close_at) - pd.Timestamp(s.rth_open_at)).total_seconds() // 60)
        for m in range(-pre_minutes, n):
            vol = sigma * (0.5 if m < 0 else 1.0 + 0.8 * math.exp(-m / 30))
            o = price
            price = o * math.exp(rng.normal(0, vol))
            wick = abs(rng.normal(0, vol / 2))
            rows.append({"trading_day": d, "contract_id": 1,
                         "timestamp_utc": (open_at + pd.Timedelta(minutes=m)).strftime("%Y-%m-%d %H:%M:%S"),
                         "open": o, "high": max(o, price) * math.exp(wick), "low": min(o, price) * math.exp(-wick),
                         "close": price, "volume": float(rng.integers(50, 500))})
    return pd.DataFrame(rows)


def _one_day(day, closes, highs=None, lows=None, pre=((-90, 100.0),), skip=()):
    """Bars of one day: pre-open (minute, close) pairs, then regular-session closes from minute 0."""
    open_at = pd.Timestamp(cal.session(day).rth_open_at).tz_convert("UTC")
    rows = []
    for m, c in pre:
        rows.append((m, c, c + 1, c - 1))
    for m, c in enumerate(closes):
        if m not in skip:
            rows.append((m, c, highs[m] if highs else c, lows[m] if lows else c))
    return pd.DataFrame([{"trading_day": day, "timestamp_utc": (open_at + pd.Timedelta(minutes=m)).isoformat(),
                          "open": c, "high": h, "low": l, "close": c, "volume": 10.0} for m, c, h, l in rows])


# --------------------------------------------------------------------------
# Sessions from bars
# --------------------------------------------------------------------------

def test_session_minutes_preopen_and_gaps():
    day = "2025-06-10"
    pre = [(-120, 100.0), (-61, 104.0), (-60, 98.0), (-2, 101.0), (-1, 250.0)]     # -1: the 09:29 bar
    s = rn.sessions_from_bars(_one_day(day, [101.0, 102.0, 103.0, 104.0], pre=pre, skip={2}))[0]
    assert (s.minutes, s.held, s.bars, s.open) == (390, 4, 3, 101.0)
    assert list(s.close) == [101.0, 102.0, 102.0, 104.0]            # a minute without a print repeats the close
    assert s.pre_price == 101.0                                     # the 09:28 bar, never the 09:29 one
    assert s.overnight_range == pytest.approx(math.log(105.0 / 97.0))
    assert s.last_hour_range == pytest.approx(math.log(105.0 / 97.0))   # [08:29, 09:29): -61 .. -2
    s2 = rn.sessions_from_bars(_one_day(day, [101.0], pre=[(-120, 100.0), (-2, 101.0)]))[0]
    assert s2.last_hour_range == pytest.approx(math.log(102.0 / 100.0))   # 07:29 is not in the last hour


def test_early_close_and_preopen_only_days():
    early = next(d for d in _days("2025-11-20", "2025-12-05", full_only=False) if cal.session(d).schedule == "early_close")
    assert rn.sessions_from_bars(_one_day(early, [100.0]))[0].minutes == 210
    s = rn.sessions_from_bars(_one_day("2025-06-10", [], pre=[(-30, 99.0), (-2, 100.5)]))[0]
    assert (s.held, s.open, s.price_at(0)) == (0, 100.5, 100.5)     # before the open: the last pre-open price
    no_open = _one_day("2025-06-10", [100.0, 101.0], skip={0})
    assert rn.sessions_from_bars(no_open) == []                     # session bars without the 09:30 one


def test_remaining_range_from_the_price_at_each_minute():
    closes = [100.0, 104.0, 99.0, 101.0]
    s = rn.sessions_from_bars(_one_day("2025-06-10", closes, highs=[101.0, 105.0, 100.0, 102.0],
                                       lows=[99.5, 100.0, 98.0, 100.5]))[0]
    rr = rn.remaining_range(s, 4)
    assert rr[0] == pytest.approx(math.log(105.0 / 98.0))           # t = 0: the whole range from the open
    assert rr[3] == pytest.approx(math.log(102.0 / 99.0))           # from the minute-2 close to minute 3's high
    assert rr[2] == pytest.approx(math.log(104.0 / 98.0))           # the price itself counts


def test_intraday_statistics_use_earlier_bars_only():
    closes = list(100.0 + np.sin(np.arange(40)))
    a = rn.intraday_stats(rn.sessions_from_bars(_one_day("2025-06-10", closes))[0])
    changed = closes[:20] + [c * 1.05 for c in closes[20:]]
    b = rn.intraday_stats(rn.sessions_from_bars(_one_day("2025-06-10", changed))[0])
    for k in rn.INTRADAY_INPUTS:
        assert a[k][0] == 0.0
        np.testing.assert_allclose(a[k][:21], b[k][:21])            # minute 20's bar is not known at t = 20
        assert not np.allclose(a[k][21:], b[k][21:])


def test_pre_inputs_against_the_previous_sessions(small):
    sessions = rn.sessions_from_bars(_bars(_days()[:14], sigmas=[0.0004] * 14))
    for s in sessions:
        s.vix, s.vix_close = 20.0, 20.0
    daily = rn._daily(sessions)
    assert np.isnan(rn.pre_inputs({k: v[:5] for k, v in daily.items()}, sessions[5])).all()   # too few before it
    x = dict(zip(rn.PRE_INPUTS, rn.pre_inputs({k: v[:12] for k, v in daily.items()}, sessions[12])))
    assert x["vix_log_level"] == pytest.approx(math.log(20.0)) and x["vix_change"] == 0.0 and x["vix_vs_usual"] == 0.0
    assert abs(x["rth_range_22d"]) < 0.3                            # a steady market sits near its usual
    assert x["monday"] + x["friday"] == float(pd.Timestamp(sessions[12].day).weekday() in (0, 4))


# --------------------------------------------------------------------------
# Walk-forward history and the nowcast
# --------------------------------------------------------------------------

@pytest.fixture
def history(small):
    return rn.build_history("NQ", rn.sessions_from_bars(_bars(_days())))


def test_walk_forward_uses_earlier_sessions_only(small):
    days = _days()
    bars = _bars(days)
    base = rn.build_history("NQ", rn.sessions_from_bars(bars))
    shocked = bars.copy()
    shocked.loc[shocked["trading_day"] >= days[-8], "high"] *= 1.01     # the last 8 sessions' ranges change
    other = rn.build_history("NQ", rn.sessions_from_bars(shocked))
    changed = base.days.index(days[-8])
    assert base.start == rn.USUAL_SESSIONS + rn.MIN_TRAIN < changed
    for h in rn.HORIZONS:
        np.testing.assert_allclose(base.forecast[h.key][:changed], other.forecast[h.key][:changed])
        assert not np.allclose(base.forecast[h.key][-1], other.forecast[h.key][-1])


def test_nowcast_follows_the_volatility_so_far(history):
    day = history.sessions[-1]
    calm = rn.nowcast(history, day, 30)
    stormy_bars = _bars([day.day], sigmas=[0.0004 * 4], seed=7)
    stormy = rn.nowcast(history, rn.sessions_from_bars(stormy_bars)[0], 30)
    calm_bars = _bars([day.day], sigmas=[0.0004 / 4], seed=7)
    quiet = rn.nowcast(history, rn.sessions_from_bars(calm_bars)[0], 30)
    for nc in (calm, stormy, quiet):
        assert nc["available"]
    s_row = {h["key"]: h for h in stormy["horizons"]}["session"]
    q_row = {h["key"]: h for h in quiet["horizons"]}["session"]
    # the same minute, prices alike: four times the realised volatility forecasts a wider rest of the day
    assert s_row["remaining"][50] / stormy["price"] > 2 * q_row["remaining"][50] / quiet["price"]
    assert stormy["inputs"]["rv_since_open"] > 1.5 > 0.7 > quiet["inputs"]["rv_since_open"]


def test_nowcast_bands_final_range_and_cone(history):
    day = history.sessions[-1]
    nc = rn.nowcast(history, day, 20)
    rows = {h["key"]: h for h in nc["horizons"]}
    assert rows["opening_range"]["status"] == "done"
    assert rows["opening_range"]["actual_final"] == pytest.approx(
        max(day.open, day.high[:15].max()) - min(day.open, day.low[:15].min()))
    fh = rows["first_hour"]
    assert fh["status"] == "open" and fh["scenarios"] >= rn.MIN_SCENARIOS
    for key in ("remaining", "final", "final_high"):
        q = [fh[key][p] for p in rn.QUANTILES]
        assert q == sorted(q)
    assert fh["final"][10] >= fh["so_far"] - 1e-9                   # the final range holds what is already done
    assert fh["final_high"][10] >= nc["high_so_far"] - 1e-9 and fh["final_low"][90] <= nc["low_so_far"] + 1e-9
    assert fh["cone"][50].shape == (60 - 20,)
    assert fh["actual_remaining"] == pytest.approx(
        max(nc["price"], day.high[20:60].max()) - min(nc["price"], day.low[20:60].min()))
    assert rn.band_position(fh["actual_final"], fh["final"]) is not None


def test_a_past_day_shows_what_the_backtest_scored(history):
    i = history.n - 3
    day, h, t = history.sessions[i], rn.BY_KEY["first_hour"], 15
    shown = {r["key"]: r for r in rn.nowcast(history, day, t)["horizons"]}["first_hour"]
    sc, _ = rn.scenarios(history, h, i, t, float(history.forecast["first_hour"][i, t]))
    price = day.price_at(t)
    assert shown["remaining"][50] == pytest.approx(np.percentile(price * np.exp(sc.up) - price * np.exp(-sc.down), 50))


def test_nowcast_needs_history_and_early_close_has_no_session(history):
    early = rn.sessions_from_bars(_bars(["2025-11-28"], sigmas=[0.0004]))[0]
    nc = rn.nowcast(history, early, 10)
    assert {h["key"]: h["status"] for h in nc["horizons"]}["session"] == "unavailable"
    first = rn.nowcast(history, history.sessions[5], 0)
    assert not first["available"] and "earlier complete sessions" in first["reason"]


def test_backtest_beats_the_usual_on_persistent_volatility(history):
    r = rn.backtest(history)
    assert r["sessions"] > 0
    for key, hr in r["horizons"].items():
        assert hr["error"]["gain"] > 0, key
        for c in hr["coverage"].values():
            assert 0.0 <= c["50"] <= c["80"] <= 1.0
    text = rn.format_backtest(r)
    assert "First hour" in text and "inside the 50 % / 80 % bands" in text


# --------------------------------------------------------------------------
# Dashboard: the cone on the chart and the card's table
# --------------------------------------------------------------------------

def _cone(t, end, bar_minutes):
    open_ = pd.Timestamp("2026-06-10 09:30", tz="America/New_York")
    prices = {q: np.arange(t, end, dtype=float) + q for q in rn.QUANTILES}
    return {"open": open_, "t": t, "end": end, "price": 1.0, "bar_minutes": bar_minutes, "prices": prices}


def test_cone_reaches_past_the_last_bar():
    from dashboard.components.spec import build_chart_spec, cone_series, to_epoch
    one = cone_series(_cone(20, 60, 1))
    p50 = one["series"]["cone:p50"]["points"]
    assert len(p50) == 41 and p50[0]["value"] == 1.0                # the price at 09:50 on the 09:49 bar, then 40 minutes
    assert p50[1]["time"] == int(to_epoch(pd.DatetimeIndex([pd.Timestamp("2026-06-10 09:50")]))[0])
    assert p50[-1]["value"] == 59.0 + 50
    five = cone_series(_cone(22, 60, 5))["series"]["cone:p90"]["points"]
    assert len(five) == 8 and five[0]["value"] == 24.0 + 90         # mid-bar: no anchor; each bar at its last minute
    idx = pd.date_range("2026-06-10 09:15", "2026-06-10 09:40", freq="1min", tz="America/New_York")
    df = pd.DataFrame({"timestamp_ny": idx, "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1})
    spec = build_chart_spec(df, cone=_cone(20, 60, 1))
    assert (spec["bands"]["cone:inner"]["upper"], spec["bands"]["cone:outer"]["lower"]) == ("cone:p75", "cone:p10")
    assert cone_series(_cone(60, 60, 1))["series"] == {}


def test_nowcast_table_rows_and_inputs():
    from dashboard.views.candles import describe_inputs, nowcast_rows
    q = {10: 10.0, 25: 20.0, 50: 30.0, 75: 40.0, 90: 150.0}
    nc = {"t": 20, "horizons": [
        {"key": "opening_range", "label": "Opening range · 09:30–09:45", "status": "done", "actual_final": 42.25},
        {"key": "first_hour", "label": "First hour · 09:30–10:30", "status": "open", "so_far": 55.0, "remaining": q,
         "usual": 33.0, "final": q, "final_high": {50: 21050.5}, "final_low": {50: 20990.0}, "actual_final": 35.0},
        {"key": "session", "label": "Session · 09:30–16:00", "status": "unavailable", "reason": "early close"}]}
    rows = {r["key"]: r for r in nowcast_rows(nc)}
    assert rows["opening_range"]["remaining"] == "over" and rows["opening_range"]["actual"] == "final 42.2"
    fh = rows["first_hour"]
    assert (fh["remaining"], fh["band50"], fh["band80"]) == ("30.0", "20.0 – 40.0", "10.0 – 150")
    assert fh["high_low"] == "21,050.50 · 20,990.00" and fh["actual"] == "final 35.0, inside the 50 % band"
    assert rows["session"]["remaining"] == "early close"
    inputs = {"overnight_range": 1.3, "vix_vs_usual": 0.9, "vix_level": 17.24, "rv_since_open": 1.6}
    assert describe_inputs(inputs, 0) == "overnight range 1.30× · VIX 17.2 (0.90×)"
    assert describe_inputs(inputs, 5).endswith("volatility since the open 1.60×")


def test_matching_leaves_out_inputs_snapshots_never_have():
    from forecaster.analogue import MATCH_FEATURES, MIN_V2_DIMS
    assert "vxn_level" not in MATCH_FEATURES and "daily_volatility_ratio" not in MATCH_FEATURES
    assert len(MATCH_FEATURES) == 9 and MIN_V2_DIMS <= len(MATCH_FEATURES)
