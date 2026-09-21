# features/calculations.py
"""
Feature engineering engine for the NQ Opening Forecast System.
Calculates previous session levels, overnight metrics, gaps, pre-open directions,
volatility, and VWAP. Uses pandas and NumPy.

All pre-open features are frozen at 09:30 ET of the target trading day. No bar
at or after 09:30 ET of the target day is used, so the snapshot is free of
look-ahead bias.
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
    "calculate_pre_open_snapshot",
]


_RTH_START_MIN = RTH_START.hour * 60 + RTH_START.minute


def _before_open(ny_times):
    """
    Mask of timestamps falling strictly before 09:30 ET.

    Vectorised: this runs over a full multi-day history on every dashboard
    redraw. NaT compares false, as a missing timestamp is not "before open".
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


def calculate_pre_open_snapshot(df, target_trading_day):
    """
    Computes a feature snapshot frozen at 09:30 AM ET for a target trading day.
    Requires at least the previous trading day's RTH bars and the current day's
    overnight (pre-09:30 ET) bars.
    """
    if df is None or df.empty:
        return {}
    df = enrich_candle_timezones(df)
    df = df.sort_values("timestamp_utc")

    all_days = sorted(d for d in df["trading_day"].dropna().unique())
    if target_trading_day not in all_days:
        return {"error": f"Target trading day {target_trading_day} not found in dataset."}

    target_idx = all_days.index(target_trading_day)
    if target_idx == 0:
        return {"error": f"Cannot calculate features for {target_trading_day}; no previous trading day available."}

    prev_trading_day = all_days[target_idx - 1]

    # --- Previous Day RTH ---
    prev_rth = df[(df["trading_day"] == prev_trading_day) & (df["session_scope"] == "RTH")]
    if prev_rth.empty:
        return {"error": f"No RTH data found for previous trading day {prev_trading_day}."}

    prev_rth_high = float(prev_rth["high"].max())
    prev_rth_low = float(prev_rth["low"].min())
    prev_rth_close = float(prev_rth.iloc[-1]["close"])

    # --- Current Day overnight, strictly BEFORE 09:30 ET (no look-ahead) ---
    before_open = _before_open(df["timestamp_ny"])
    current_eth = df[
        (df["trading_day"] == target_trading_day)
        & (df["session_scope"] == "ETH")
        & before_open
    ]
    if current_eth.empty:
        overnight_high = prev_rth_close
        overnight_low = prev_rth_close
    else:
        overnight_high = float(current_eth["high"].max())
        overnight_low = float(current_eth["low"].min())

    overnight_range = overnight_high - overnight_low

    # --- Opening bar (09:30 ET) — this is the cutoff itself, not future data ---
    target_rth = df[(df["trading_day"] == target_trading_day) & (df["session_scope"] == "RTH")]
    if target_rth.empty:
        return {"error": f"No RTH opening candle found for target trading day {target_trading_day}."}

    rth_open = float(target_rth.iloc[0]["open"])

    # --- Gap + direction ---
    gap = rth_open - prev_rth_close
    threshold = abs(prev_rth_close) * 0.0005
    if gap > threshold:
        direction = "UP"
    elif gap < -threshold:
        direction = "DOWN"
    else:
        direction = "FLAT"

    # --- Overnight VWAP as of just before the open ---
    vwap_df = calculate_vwap(df)
    pre_open_vwap_rows = vwap_df[
        (vwap_df["trading_day"] == target_trading_day)
        & _before_open(vwap_df["timestamp_ny"])
    ]
    vwap_val = float(pre_open_vwap_rows.iloc[-1]["vwap"]) if not pre_open_vwap_rows.empty else rth_open

    # --- Historical volatility: stddev of prior daily RTH log returns ---
    rth_closes = []
    for day in all_days[:target_idx]:
        day_rth = df[(df["trading_day"] == day) & (df["session_scope"] == "RTH")]
        if not day_rth.empty:
            rth_closes.append(float(day_rth.iloc[-1]["close"]))

    if len(rth_closes) >= 3:
        returns = np.diff(np.log(rth_closes))
        volatility = float(np.std(returns))
    else:
        volatility = 0.01

    return {
        "trading_day": target_trading_day,
        "previous_rth_high": prev_rth_high,
        "previous_rth_low": prev_rth_low,
        "previous_rth_close": prev_rth_close,
        "overnight_high": overnight_high,
        "overnight_low": overnight_low,
        "overnight_range": overnight_range,
        "gap": gap,
        "pre_open_direction": direction,
        "vwap": vwap_val,
        "historical_volatility": volatility,
    }
