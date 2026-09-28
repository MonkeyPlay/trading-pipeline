# tests/test_first_hour_model.py
"""The first-hour model: outcomes measured from P, candles generated through the
predicted prices, forecasts from earlier sessions only, a direction learned when
the outcomes carry one and shrunk away when they do not, and the card."""

import math

import numpy as np
import pytest

from forecaster import first_hour_model as fhm
from forecaster import preopen as po
from tests.synthetic import full_days, session_bars

DAYS = full_days("2025-06-02", "2025-12-31")


@pytest.fixture
def small(monkeypatch):
    """Short windows, so a synthetic half-year has walk-forward forecasts."""
    monkeypatch.setattr(po, "USUAL_SESSIONS", 10)
    monkeypatch.setattr(fhm, "MIN_TRAIN", 20)


def _history(bars):
    return fhm.build_history("NQ", po.complete_sessions(po.sessions_from_bars(bars)))


def test_outcome_is_measured_from_p():
    s = po.complete_sessions(po.sessions_from_bars(session_bars(DAYS[:1], seed=3)))[0]
    o = fhm.outcome(s)
    P = s.pre_price
    assert o.moves == pytest.approx([math.log(s.close[k - 1] / P) for k in (15, 30, 60)])
    assert o.ranges[0] == pytest.approx(math.log(max(P, s.high[:15].max()) / min(P, s.low[:15].min())))
    assert o.path[-1] == pytest.approx(o.moves[-1]) and len(o.minute_ranges) == 60
    assert (o.minute_ranges >= fhm.TICK_LOG).all() and list(o.ranges) == sorted(o.ranges)
    live = po.sessions_from_bars(session_bars(DAYS[:1], seed=3).iloc[:120])[0]      # 30 minutes held
    assert fhm.outcome(live) is None


def test_generated_candles_run_through_the_predicted_prices():
    P, moves = 20000.0, np.array([0.001, -0.002, 0.0015])
    usual_ranges, ranges = np.array([0.004, 0.006, 0.008]), np.array([0.008, 0.006, 0.004])
    usual_minutes = np.full(60, 0.0006)
    c = fhm.generate(P, moves, ranges, usual_ranges, usual_minutes)
    o, h, l, cl = c.T
    assert o[0] == pytest.approx(P) and (o[1:] == cl[:-1]).all()                 # a continuous path from P
    assert [cl[14], cl[29], cl[59]] == pytest.approx(list(P * np.exp(moves)))     # at 09:45, 10:00 and 10:30
    assert (h >= np.maximum(o, cl)).all() and (l <= np.minimum(o, cl)).all()
    size = np.log(h / l)
    assert size[:15] == pytest.approx(0.0006 * 2) and size[40:] == pytest.approx(0.0006 * 0.5)   # window's scale
    high, low = fhm.likely_extremes(P, 0.002, 0.01)
    assert (math.log(high / P), math.log(P / low)) == pytest.approx((0.006, 0.004))
    assert fhm.likely_extremes(P, 0.05, 0.01) == pytest.approx((P * math.exp(0.01), P))


def test_forecasts_use_earlier_sessions_only(small):
    bars = session_bars(DAYS, seed=1)
    base = _history(bars)
    shocked = bars.copy()
    shocked.loc[shocked["trading_day"] >= DAYS[-8], "high"] *= 1.01     # the last 8 sessions change
    other = _history(shocked)
    changed = base.days.index(DAYS[-8])
    assert base.start < changed
    np.testing.assert_allclose(base.forecasts[:changed], other.forecasts[:changed])
    assert not np.allclose(base.forecasts[-1], other.forecasts[-1])
    m = base.model(base.n)
    assert m.sessions == base.n - po.USUAL_SESSIONS and m.trained_through == base.days[-1]   # every outcome so far


def test_the_model_learns_a_direction_that_is_there_and_shrinks_one_that_is_not(small):
    signal = fhm.scorecard(_history(session_bars(DAYS, seed=2, follow=4.0)))
    noise_history = _history(session_bars(DAYS, seed=2))
    noise = fhm.scorecard(noise_history)
    assert signal["direction"][60]["hit_rate"] > 0.65                 # a coin flip: 0.5
    assert signal["direction"][60]["move"]["gain"] > 2 * signal["direction"][60]["move"]["gain_se"]
    # without one, the predicted moves stay a fraction of the actual ones and gain nothing
    i = noise_history.start
    pred, actual = noise_history.forecasts[i:, 2], noise_history.Y[i:, 2]
    assert np.median(np.abs(pred)) < 0.35 * np.median(np.abs(actual))
    assert noise["direction"][60]["move"]["gain"] < 2 * noise["direction"][60]["move"]["gain_se"]
    # sizes are learned either way: the synthetic volatility persists from day to day
    assert noise["ranges"][60]["gain"] > 0 and noise["candles"]["gain"] > 0


def test_forecast_before_the_open_and_after_it(small):
    history = _history(session_bars(DAYS[:-1], seed=4))
    last = DAYS[-1]
    preopen_only = po.sessions_from_bars(session_bars([last], seed=9, rth=False))[0]
    fc = fhm.forecast(history, preopen_only)
    assert fc["available"] and fc["trained_through"] == DAYS[-2] and "actual" not in fc
    candles = fc["candles"]
    assert len(candles) == 60 and candles["open"].iloc[0] == pytest.approx(fc["price"])
    assert candles["timestamp_ny"].iloc[0].strftime("%H:%M") == "09:30"
    assert candles["timestamp_ny"].iloc[-1].strftime("%H:%M") == "10:29"
    assert candles["close"].iloc[59] == pytest.approx(fc["anchors"][60])
    assert fc["likely_low"] < fc["price"] < fc["likely_high"]
    complete = po.sessions_from_bars(session_bars([last], seed=9))[0]
    done = fhm.forecast(history, complete)
    assert set(done["actual"]["direction_right"]) == {15, 30, 60}
    assert done["moves"] == pytest.approx(fc["moves"])              # the same pre-open, the same forecast
    too_early = fhm.forecast(history, history.sessions[5])
    assert not too_early["available"] and "earlier sessions" in too_early["reason"]


def test_scorecard_text_and_the_card(small):
    from dashboard.views.candles import describe_scorecard, first_hour_rows, forecast_bars
    history = _history(session_bars(DAYS, seed=5))
    sc = fhm.scorecard(history)
    assert sc["sessions"] == history.n - history.start
    assert all(0 <= sc["direction"][k]["hit_rate"] <= 1 for k in fhm.ANCHORS)
    text = fhm.format_scorecard(sc, history.model(history.n))
    assert "direction at +60 min" in text and "minute candle size" in text and "Latest model" in text
    assert describe_scorecard(sc).startswith(f"Walk-forward over {sc['sessions']} sessions: the direction was right")
    fc = fhm.forecast(history, history.sessions[-1])
    rows = first_hour_rows(fc)
    assert [r["at"] for r in rows] == ["09:45", "10:00", "10:30"]
    assert rows[2]["actual"].endswith(f"range {rows[2]['actual'].split('range ')[1]}")
    assert ("right way" in rows[2]["actual"]) == fc["actual"]["direction_right"][60]
    five = forecast_bars(fc, "5m")
    assert len(five) == 12 and five["high"].iloc[0] == pytest.approx(fc["candles"]["high"].iloc[:5].max())
    assert forecast_bars(fc, "1m") is fc["candles"] and forecast_bars({"available": False}) is None
