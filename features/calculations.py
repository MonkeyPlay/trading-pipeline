# features/calculations.py
"""
What the Session Explorer's chart draws besides the bars: the session VWAP, the
pre-open reference levels (the previous session's RTH high, low and close, and
the overnight high and low) and the three moving averages of the TradingView
indicator "TEMA & Session Levels". Uses pandas and NumPy.
"""

import numpy as np
import pandas as pd

from features.session_windows import (
    RTH_START,
    enrich_candle_timezones,
    minutes_of_day,
)

# Re-exported for backwards compatibility with earlier imports.
__all__ = [
    "enrich_candle_timezones",
    "calculate_vwap",
    "calculate_moving_averages",
    "pre_open_levels",
]


_RTH_START_MIN = RTH_START.hour * 60 + RTH_START.minute

# The moving averages of the TradingView indicator "TEMA & Session Levels" (Pine
# v6), at its default inputs. The script's plot titles "EMA 50" and "EMA 9
# Smoothed" are the 100- and 14-bar EMAs below.
TEMA_LENGTH, TEMA_SMOOTHING = 14, 3              # TEMA(14), then SMA(3)
TREND_EMA_LENGTH = 100                           # EMA(100)
TRIGGER_EMA_LENGTH, TRIGGER_SMOOTHING = 14, 3    # EMA(14), then SMA(3)
# Bars before the first one shown. TradingView runs the averages over the whole
# chart history; after 1000 bars the EMA(100)'s starting value weighs (99/101)^1000,
# about 2e-9, so the lines match whatever history TradingView started from.
MA_WARMUP_BARS = 1000


def _before_open(ny_times):
    """
    Mask of timestamps falling strictly before 09:30 ET.

    Vectorised: this runs over the loaded bars on every dashboard redraw. NaT
    compares false, as a missing timestamp is not "before open".
    """
    return minutes_of_day(ny_times) < _RTH_START_MIN


def calculate_vwap(df):
    """
    Calculates the Volume-Weighted Average Price (VWAP), cumulative within each
    trading day (which starts at 18:00 ET the prior evening).
    """
    if df is None or df.empty:
        return df
    df = enrich_candle_timezones(df)
    df = df.sort_values("timestamp_utc").copy()

    typical_price = (df["high"] + df["low"] + df["close"]) / 3.0
    df["tp_v"] = typical_price * df["volume"]

    grouped = df.groupby("trading_day")
    cum_pv = grouped["tp_v"].cumsum()
    cum_vol = grouped["volume"].cumsum()

    df["vwap"] = cum_pv / cum_vol.replace(0, np.nan)
    df["vwap"] = df["vwap"].ffill().fillna(df["close"])
    df = df.drop(columns=["tp_v"])
    return df


def _ema(series, length):
    """Pine's ta.ema: alpha = 2 / (length + 1), seeded with the first value."""
    return series.ewm(span=length, adjust=False).mean()


def calculate_moving_averages(df):
    """
    Adds the indicator's three lines to bars at the chart's timeframe, run over
    every row of ``df`` in time order (gaps such as the daily break and weekends
    are simply skipped, as on TradingView):

      tema         TEMA(14) = 3 (e1 - e2) + e3, e1 = EMA(close), e2 = EMA(e1),
                   e3 = EMA(e2), then SMA(3)          plot "TEMA Smoothed"
      ema_trend    EMA(100) of close                  plot "EMA 50"
      ema_trigger  EMA(14) of close, then SMA(3)      plot "EMA 9 Smoothed"

    ``df`` should start ``MA_WARMUP_BARS`` bars before the first bar shown.
    """
    if df is None or df.empty:
        return df
    df = df.sort_values("timestamp_utc").copy()
    close = df["close"].astype(float)
    e1 = _ema(close, TEMA_LENGTH)
    e2 = _ema(e1, TEMA_LENGTH)
    e3 = _ema(e2, TEMA_LENGTH)
    df["tema"] = (3 * (e1 - e2) + e3).rolling(TEMA_SMOOTHING).mean()
    df["ema_trend"] = _ema(close, TREND_EMA_LENGTH)
    df["ema_trigger"] = _ema(close, TRIGGER_EMA_LENGTH).rolling(TRIGGER_SMOOTHING).mean()
    return df


def pre_open_levels(df, target_trading_day):
    """
    The reference levels the chart draws for ``target_trading_day``: the
    previous trading day's RTH high, low and close, and the overnight high and
    low - the target day's bars strictly before 09:30 ET (None when it has none).
    ``df`` must hold the previous trading day and the target day. None when the
    previous day or its RTH bars are missing.
    """
    if df is None or df.empty:
        return None
    df = enrich_candle_timezones(df)
    df = df.sort_values("timestamp_utc")

    all_days = sorted(d for d in df["trading_day"].dropna().unique())
    if target_trading_day not in all_days or all_days.index(target_trading_day) == 0:
        return None
    prev_trading_day = all_days[all_days.index(target_trading_day) - 1]

    prev_rth = df[(df["trading_day"] == prev_trading_day) & (df["session_scope"] == "RTH")]
    if prev_rth.empty:
        return None

    overnight = df[
        (df["trading_day"] == target_trading_day)
        & (df["session_scope"] == "ETH")
        & _before_open(df["timestamp_ny"])
    ]
    return {
        "previous_rth_high": float(prev_rth["high"].max()),
        "previous_rth_low": float(prev_rth["low"].min()),
        "previous_rth_close": float(prev_rth.iloc[-1]["close"]),
        "overnight_high": float(overnight["high"].max()) if not overnight.empty else None,
        "overnight_low": float(overnight["low"].min()) if not overnight.empty else None,
    }
