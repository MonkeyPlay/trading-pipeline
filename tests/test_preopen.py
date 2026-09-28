# tests/test_preopen.py
"""What is known before the open, from bars: sessions (minutes, gaps, P, the
overnight and last-hour figures), and the pre-open inputs against the sessions
before them - volatility and direction."""

import math

import numpy as np
import pandas as pd
import pytest

from features import calendar as cal
from forecaster import preopen as po
from tests.synthetic import full_days, session_bars


def _one_day(day, closes, highs=None, lows=None, pre=((-90, 100.0),), skip=()):
    """Bars of one day: pre-open (minute, close) pairs, then regular-session closes from minute 0."""
    open_at = pd.Timestamp(cal.session(day).rth_open_at).tz_convert("UTC")
    rows = [(m, c, c + 1, c - 1) for m, c in pre]
    rows += [(m, c, highs[m] if highs else c, lows[m] if lows else c) for m, c in enumerate(closes) if m not in skip]
    return pd.DataFrame([{"trading_day": day, "timestamp_utc": (open_at + pd.Timedelta(minutes=m)).isoformat(),
                          "open": c, "high": h, "low": l, "close": c, "volume": 10.0} for m, c, h, l in rows])


def test_session_minutes_preopen_and_gaps():
    day = "2025-06-10"
    pre = [(-120, 100.0), (-62, 99.0), (-61, 104.0), (-60, 98.0), (-17, 102.5), (-2, 101.0), (-1, 250.0)]
    s = po.sessions_from_bars(_one_day(day, [101.0, 102.0, 103.0, 104.0], pre=pre, skip={2}))[0]
    assert (s.minutes, s.held, s.bars, s.open) == (390, 4, 3, 101.0)
    assert list(s.close) == [101.0, 102.0, 102.0, 104.0]            # a minute without a print repeats the close
    assert s.pre_price == 101.0                                     # the 09:28 bar, never the 09:29 one
    assert (s.overnight_open, s.overnight_high, s.overnight_low) == (100.0, 105.0, 97.0)
    assert s.overnight_range == pytest.approx(math.log(105.0 / 97.0))
    assert s.last_hour_range == pytest.approx(math.log(105.0 / 97.0))   # [08:29, 09:29): -61 .. -2
    assert (s.close_0828, s.close_0913) == (99.0, 102.5)
    s2 = po.sessions_from_bars(_one_day(day, [101.0], pre=[(-120, 100.0), (-2, 101.0)]))[0]
    assert s2.last_hour_range == pytest.approx(math.log(102.0 / 100.0))   # 07:29 is not in the last hour
    assert s2.close_0828 == 100.0 and s2.close_0913 == 100.0         # the last close at or before each


def test_early_close_preopen_only_and_complete_days():
    early = next(d for d in [s.session_date.isoformat() for s in cal.sessions_between("2025-11-20", "2025-12-05")]
                 if cal.session(d).schedule == "early_close")
    assert po.sessions_from_bars(_one_day(early, [100.0]))[0].minutes == 210
    s = po.sessions_from_bars(_one_day("2025-06-10", [], pre=[(-30, 99.0), (-2, 100.5)]))[0]
    assert (s.held, s.open, s.pre_price) == (0, 100.5, 100.5)       # before the open: P stands in
    assert po.sessions_from_bars(_one_day("2025-06-10", [100.0, 101.0], skip={0})) == []   # no 09:30 bar
    days = full_days("2025-06-02", "2025-06-30")
    kept = po.complete_sessions(po.sessions_from_bars(session_bars(days)), {days[0]: (18.0, 19.0)})
    assert [k.day for k in kept] == days and kept[0].vix == 18.0 and kept[1].vix is None
    partial = po.sessions_from_bars(session_bars(days[:1]).iloc[:-30])       # its last 30 minutes missing
    assert po.complete_sessions(partial) == []


def test_pre_inputs_against_the_previous_sessions(monkeypatch):
    monkeypatch.setattr(po, "USUAL_SESSIONS", 10)
    sessions = po.complete_sessions(po.sessions_from_bars(session_bars(full_days("2025-06-02", "2025-06-30"),
                                                                       sigmas=[0.0004] * 20)))
    for s in sessions:
        s.vix, s.vix_close = 20.0, 20.0
    d = po.daily(sessions)
    assert np.isnan(po.pre_inputs({k: v[:5] for k, v in d.items()}, sessions[5])).all()   # too few before it
    today = sessions[12]
    x = dict(zip(po.PRE_INPUTS, po.pre_inputs({k: v[:12] for k, v in d.items()}, today)))
    assert x["vix_log_level"] == pytest.approx(math.log(20.0)) and x["vix_change"] == 0.0 and x["vix_vs_usual"] == 0.0
    assert abs(x["rth_range_22d"]) < 0.3                            # a steady market sits near its usual
    usual = np.median(d["rth_range"][2:12])
    assert x["overnight_return"] == pytest.approx(math.log(today.pre_price / today.overnight_open) / usual)
    assert x["last_15m_return"] == pytest.approx(math.log(today.pre_price / today.close_0913) / usual)
    assert x["prev_session_return"] == pytest.approx(d["session_return"][11] / usual)
    assert -0.5 <= x["overnight_position"] <= 0.5 and -0.5 <= x["prev_close_location"] <= 0.5
    assert x["monday"] + x["friday"] == float(pd.Timestamp(today.day).weekday() in (0, 4))
