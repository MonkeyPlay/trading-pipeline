# tests/test_projection.py
"""The projection trend (dashboard/components/projection.py): the curve from the chart's TEMA 14 and EMA 14."""

import numpy as np
import pandas as pd
import pytest

from dashboard.components import projection as proj


def _rows(tema, ema, tf=1, start="2026-10-07 06:00"):
    t = pd.date_range(pd.Timestamp(start, tz="America/New_York"), periods=len(tema), freq=f"{tf}min")
    return pd.DataFrame({"timestamp_ny": t, "tema": tema, "ema_trigger": ema})


def test_control_points_follow_the_definition():
    tema = [100, 101, 103, 104, 106, 110, 111.0]
    ema = [100, 100, 101, 102, 104, 106, 107.0]
    p, why = proj.trend_projection(_rows(tema, ema), 1)
    assert why == ""
    c = 0.5 * np.array(tema) + 0.5 * np.array(ema)
    slopes = np.diff(c[-6:])                                  # five segments, points per minute at 1m
    s0 = slopes[-1]
    s_avg = (slopes * [1, 2, 3, 4, 5]).sum() / 15
    y0 = c[-1]
    expect = [[0, y0], [5, y0 + s0 * 5], [10, y0 + 30 * s_avg / 3], [15, y0 + 15 * s_avg]]
    np.testing.assert_allclose(p["control"], expect, atol=1e-4)
    assert p["y0"] == pytest.approx(y0) and p["s0"] == pytest.approx(s0) and p["s_avg"] == pytest.approx(s_avg)
    assert p["origin"] == pd.Timestamp("2026-10-07 06:06", tz="America/New_York")   # the last candle's start
    assert p["end_minutes"] == 15


def test_fifteen_minutes_at_any_timeframe_and_slopes_per_minute():
    tema = [100, 102, 104, 106, 108, 110.0]
    for tf in (1, 5, 30):
        p, _ = proj.trend_projection(_rows(tema, tema, tf), tf)
        assert [x for x, _ in p["control"]] == [0, 5, 10, 15]
        assert p["s_avg"] == pytest.approx(2 / tf, abs=1e-6)       # 2 points a candle = 2 / tf a minute
        assert p["end_price"] == pytest.approx(110 + 15 * 2 / tf, abs=1e-4)


def test_not_drawn_without_six_contiguous_defined_candles():
    ok = [100, 101, 102, 103, 104, 105.0]
    assert proj.trend_projection(_rows(ok[:5], ok[:5]), 1)[0] is None
    nan = ok[:3] + [np.nan] + ok[4:]
    p, why = proj.trend_projection(_rows(nan, ok), 1)
    assert p is None and "not defined" in why
    gap = _rows(ok, ok)
    gap.loc[5, "timestamp_ny"] += pd.Timedelta(minutes=3)       # a missing candle before the last
    p, why = proj.trend_projection(gap, 1)
    assert p is None and "contiguous" in why
    early = [np.nan] * 3 + ok                                    # the warm-up is before the last six: drawn
    assert proj.trend_projection(_rows(early, early), 1)[0] is not None


def test_cut_at_the_session_end_keeps_the_same_curve():
    tema = [100, 101, 103, 104, 106, 110.0]
    ema = [100, 100, 101, 102, 104, 106.0]
    rows = _rows(tema, ema, start="2026-10-07 16:45")
    full, _ = proj.trend_projection(rows, 1)
    end = pd.Timestamp("2026-10-07 17:00", tz="America/New_York")   # 10 minutes after the 16:50 candle
    cut, _ = proj.trend_projection(rows, 1, end)
    assert cut["end_minutes"] == pytest.approx(10)
    P, Q = np.array(full["control"]), np.array(cut["control"])
    u = np.linspace(0, 1, 11)
    np.testing.assert_allclose(proj.bezier(Q, u), proj.bezier(P, u * 10 / 15), atol=1e-3)
    assert proj.trend_projection(rows, 1, rows["timestamp_ny"].iloc[-1])[0] is None


def test_playback_reads_no_later_candle():
    tema = [100, 101, 103, 104, 106, 110, 90, 80, 70.0]
    ema = [100, 100, 101, 102, 104, 106, 95, 85, 75.0]
    rows = _rows(tema, ema)
    cutoff = rows.iloc[:6]
    revealed = rows.copy()
    revealed["muted"] = [np.nan] * 6 + [True] * 3                # "Show what followed": later candles muted
    a, _ = proj.trend_projection(proj.shown_candles(cutoff), 1)
    b, _ = proj.trend_projection(proj.shown_candles(revealed), 1)
    assert a["control"] == b["control"] and a["origin"] == b["origin"]


def test_the_axis_reaches_the_curves_end():
    spec = {"candles": [{"time": 0}, {"time": 300}],
            "projection": {"time": 300, "tf": 5, "control": [[0, 1], [5, 1], [10, 1], [15, 1]]}}
    proj.extend_axis(spec)
    assert [c["time"] for c in spec["candles"]] == [0, 300, 600, 900, 1200]      # 15 minutes past the 300 s candle
    fan_blanks = {"candles": [{"time": 0}, {"time": 300}, {"time": 600}, {"time": 900}, {"time": 1200},
                              {"time": 1500}], "projection": spec["projection"]}
    assert len(proj.extend_axis(fan_blanks)["candles"]) == 6                     # already there: nothing added
    cut = {"candles": [{"time": 0}], "projection": {"time": 0, "tf": 1, "control": [[0, 1], [1, 1], [2, 1], [2.5, 1]]}}
    assert [c["time"] for c in proj.extend_axis(cut)["candles"]] == [0, 60, 120, 180]
