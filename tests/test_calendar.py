# tests/test_calendar_indicators.py
from datetime import date, datetime

import numpy as np
import pandas as pd
import pytest

from features import calendar as cal
from features.indicators import (aggregate_clock, ema_trailing, path_efficiency, ratio,
                                 true_ranges, vwap_hlc3, wilder_atr_trailing, zscore)


# --- calendar ---------------------------------------------------------------

def test_cutoff_is_0929_et_across_dst():
    assert cal.session("2026-01-12").cutoff_at.strftime("%H:%M") == "14:29"   # EST
    assert cal.session("2026-07-13").cutoff_at.strftime("%H:%M") == "13:29"   # EDT


def test_monday_overnight_starts_sunday_evening():
    s = cal.session("2026-03-09")          # the day after the DST switch
    assert s.overnight_start_at == cal.ny_instant(date(2026, 3, 8), cal.OVERNIGHT_START)
    assert (s.cutoff_at - s.overnight_start_at).total_seconds() / 60 == 929


def test_schedules():
    assert cal.session("2026-09-07").schedule == "closed"            # Labor Day
    assert cal.session("2026-09-12").schedule == "closed"            # Saturday
    early = cal.session("2025-11-28")
    assert early.schedule == "early_close"
    assert early.scheduled_close_at == cal.ny_instant(date(2025, 11, 28), cal.EARLY_CLOSE)
    assert cal.previous_session("2025-12-01").session_date == date(2025, 11, 28)
    assert cal.previous_session("2026-09-08").session_date == date(2026, 9, 4)


def test_outside_coverage_raises():
    with pytest.raises(cal.CalendarCoverageError):
        cal.session("2023-06-01")


def test_opex_week_moves_off_good_friday():
    assert cal.monthly_opex_date(2025, 4) == date(2025, 4, 17)
    assert cal.monthly_opex_week("2025-04-14")
    assert not cal.monthly_opex_week("2025-04-21")


def test_roll_transition_window_skips_holidays():
    # Sep 2026 expiry 09-18, roll 8 days earlier on Thu 09-10; Labor Day 09-07 is closed.
    flagged = [d for d in ("2026-09-04", "2026-09-08", "2026-09-10", "2026-09-14", "2026-09-15")
               if cal.roll_transition(d, "HMUZ", 8)]
    assert flagged == ["2026-09-08", "2026-09-10", "2026-09-14"]


# --- indicators -----------------------------------------------------------------

def test_ema_seeded_with_sma_over_fixed_window():
    vals = list(range(1, 101))
    assert ema_trailing(vals[:14], 3, 5) is None                     # needs 15
    out = ema_trailing(vals, 3, 5)
    window = vals[-15:]
    e = np.mean(window[:3])
    for v in window[3:]:
        e = 0.5 * v + 0.5 * e
    assert out == [pytest.approx(e)]
    # Only the trailing window matters.
    assert ema_trailing([999.0] * 50 + vals, 3, 5) == out


def test_wilder_atr():
    # warmup 1: the window is the last 14 TRs, i.e. just the seed mean
    assert wilder_atr_trailing([2.0] * 14 + [16.0], 14, 1) == pytest.approx((13 * 2.0 + 16.0) / 14)
    assert wilder_atr_trailing([1.0] * 69, 14, 5) is None
    assert wilder_atr_trailing([1.0] * 69 + [15.0], 14, 5) == pytest.approx((13 * 1.0 + 15.0) / 14)
    assert list(true_ranges([10], [8], [12])) == [4.0]


def test_aggregate_clock_marks_incomplete_buckets():
    start = pd.Timestamp("2026-06-10 13:00", tz="UTC")
    idx = pd.date_range(start, periods=15, freq="1min")
    df = pd.DataFrame({"bar_start_at": idx, "open": 1.0, "high": 2.0, "low": 0.5,
                       "close": np.arange(15.0), "volume": 1.0}).drop(index=[7])
    b = aggregate_clock(df, 5)
    assert list(b["complete"]) == [True, False, True]
    assert list(b["n"]) == [5, 4, 5]
    assert b["close"].iloc[0] == 4.0 and b["bucket_end"].iloc[0] == start + pd.Timedelta(minutes=5)


def test_small_helpers():
    assert path_efficiency([5, 5, 5]) == 0.0                        # constant path
    assert path_efficiency([0, 2, 1]) == pytest.approx(1 / 3)
    assert zscore(1.0, [2.0, 2.0, 2.0]) is None                     # zero SD
    assert zscore(3.0, [1.0, 2.0, 3.0]) == pytest.approx(1.0)
    assert ratio(1.0, 0.0) is None and ratio(None, 1.0) is None
    bars = pd.DataFrame({"high": [2.0], "low": [1.0], "close": [1.5], "volume": [0.0]})
    assert vwap_hlc3(bars) is None                                  # undefined, not 0
