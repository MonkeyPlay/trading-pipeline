# forecaster/evaluator.py
"""
Outcome Evaluator for the NQ Opening Forecast System.
Calculates actual session performance milestones (first 15/30 mins, Initial Balance,
and full RTH metrics) once the trading session has concluded.
"""

import pandas as pd

from features.session_windows import enrich_candle_timezones


def evaluate_session_outcomes(df, target_date):
    """
    Computes realized market outcomes for a target trading day (YYYY-MM-DD)
    using 1-minute historical candles.
    """
    if df is None or df.empty:
        return {}

    df = enrich_candle_timezones(df)

    rth_df = df[
        (df["trading_day"] == target_date) & (df["session_scope"] == "RTH")
    ].copy()

    if rth_df.empty:
        return {"error": f"No RTH data found for trading day {target_date}."}

    rth_df = rth_df.sort_values("timestamp_utc").reset_index(drop=True)
    ny_times = rth_df["timestamp_ny"]

    # Anchor the opening windows to 09:30 ET regardless of the first bar present.
    opening_base_dt = ny_times.iloc[0].replace(hour=9, minute=30, second=0, microsecond=0)

    def _window(minutes):
        end = opening_base_dt + pd.Timedelta(minutes=minutes)
        return rth_df[ny_times < end]

    rth_15m = _window(15)
    rth_30m = _window(30)
    rth_ib = _window(60)

    def _hi(frame):
        return float(frame["high"].max()) if not frame.empty else None

    def _lo(frame):
        return float(frame["low"].min()) if not frame.empty else None

    def _close(frame):
        return float(frame.iloc[-1]["close"]) if not frame.empty else None

    outcomes = {
        "session_date": target_date,
        "first_15_minute_high": _hi(rth_15m),
        "first_15_minute_low": _lo(rth_15m),
        "first_15_minute_close": _close(rth_15m),

        "first_30_minute_high": _hi(rth_30m),
        "first_30_minute_low": _lo(rth_30m),
        "first_30_minute_close": _close(rth_30m),

        "initial_balance_high": _hi(rth_ib),
        "initial_balance_low": _lo(rth_ib),

        "rth_high": float(rth_df["high"].max()),
        "rth_low": float(rth_df["low"].min()),
        "rth_close": float(rth_df.iloc[-1]["close"]),

        "raw_outcomes": {
            "session_total_volume": int(rth_df["volume"].sum()),
            "rth_open": float(rth_df.iloc[0]["open"]),
            "trend_type": "UP" if rth_df.iloc[-1]["close"] > rth_df.iloc[0]["open"] else "DOWN",
        },
    }

    return outcomes
