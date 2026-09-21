# features/session_windows.py
"""
Timezone and Trading Session classifier for the NQ Opening Forecast System.
Converts UTC database records to America/New_York and classifies each candle
as Regular Trading Hours (RTH) or Overnight/Extended Trading Hours (ETH).
"""

from datetime import time
import numpy as np
import pandas as pd
import pytz

# Target Timezone for NQ CME/NYSE trading
NY_TZ = pytz.timezone("America/New_York")

RTH_START = time(9, 30)
RTH_END = time(16, 0)

# The electronic session opens at 18:00 ET the prior evening.
SESSION_OPEN = time(18, 0)


def _as_minutes(t):
    return t.hour * 60 + t.minute


_RTH_START_MIN = _as_minutes(RTH_START)
_RTH_END_MIN = _as_minutes(RTH_END)
_SESSION_OPEN_MIN = _as_minutes(SESSION_OPEN)


def convert_utc_to_ny(utc_dt_str):
    """
    Converts a UTC ISO timestamp string (or datetime object) to an
    America/New_York datetime. Returns None for empty input.
    """
    if utc_dt_str is None or (isinstance(utc_dt_str, float) and pd.isna(utc_dt_str)):
        return None
    if isinstance(utc_dt_str, str):
        if not utc_dt_str:
            return None
        dt = pd.to_datetime(utc_dt_str)
    else:
        dt = pd.Timestamp(utc_dt_str)

    if dt.tzinfo is None:
        dt = dt.tz_localize("UTC")
    else:
        dt = dt.tz_convert("UTC")

    return dt.tz_convert(NY_TZ)


def classify_session_scope(dt_ny):
    """Classifies an America/New_York datetime as 'RTH' or 'ETH'."""
    if dt_ny is None:
        return "ETH"
    if dt_ny.weekday() in (5, 6):
        return "ETH"

    t = dt_ny.time()
    if RTH_START <= t < RTH_END:
        return "RTH"
    return "ETH"


def get_trading_day_date(dt_ny):
    """
    Returns the associated trade date string (YYYY-MM-DD) for a given datetime.
    Sessions run from 18:00 ET the prior calendar evening through 17:00 ET.
    So any bar at/after 18:00 ET belongs to the *next* calendar day's session.
    """
    if dt_ny is None:
        return None

    w = dt_ny.weekday()
    t = dt_ny.time()

    # Sunday 18:00+ opens Monday's session.
    if w == 6:
        if t >= time(18, 0):
            return (dt_ny + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
        return dt_ny.strftime("%Y-%m-%d")

    # Mon-Fri 18:00+ rolls into the next calendar day's session.
    if w in (0, 1, 2, 3, 4) and t >= time(18, 0):
        return (dt_ny + pd.Timedelta(days=1)).strftime("%Y-%m-%d")

    return dt_ny.strftime("%Y-%m-%d")


def to_ny_series(values):
    """
    Converts a column of UTC timestamps to America/New_York, all at once.

    Naive values are read as UTC, matching :func:`convert_utc_to_ny`. Anything
    unparseable becomes NaT rather than raising, so one bad row cannot take out
    a whole session.
    """
    timestamps = pd.to_datetime(values, errors="coerce", utc=True)
    return timestamps.dt.tz_convert(NY_TZ)


def minutes_of_day(ny_times):
    """Minutes since midnight for a New York datetime Series (NaT -> NaN)."""
    return ny_times.dt.hour * 60 + ny_times.dt.minute


def enrich_candle_timezones(df):
    """
    Enriches a candles DataFrame by converting timestamp_utc to America/New_York
    and adding session classifications (RTH vs ETH) and trading-day dates.

    Always recomputes session_scope / trading_day from timestamp_utc so that the
    values are consistent regardless of how the raw rows were stored. The work is
    vectorised because the dashboard re-enriches a full history on every redraw;
    the scalar helpers above remain for one-off conversions.
    """
    if df is None or df.empty:
        return df
    df = df.copy()
    if "timestamp_utc" not in df.columns:
        return df

    ny_times = to_ny_series(df["timestamp_utc"])
    df["timestamp_ny"] = ny_times

    minutes = minutes_of_day(ny_times)
    weekday = ny_times.dt.weekday

    is_rth = (weekday < 5) & (minutes >= _RTH_START_MIN) & (minutes < _RTH_END_MIN)
    df["session_scope"] = np.where(is_rth.to_numpy(), "RTH", "ETH")

    # A bar at or after 18:00 ET belongs to the next calendar day's session.
    # Saturday evening is the exception: there is no session to roll into.
    rolls = ((weekday != 5) & (minutes >= _SESSION_OPEN_MIN)).to_numpy()
    session_times = ny_times + pd.to_timedelta(rolls.astype("int64"), unit="D")

    # Truncating to a date in numpy is a C-level cast; strftime would walk the
    # rows one at a time, which is the whole cost of enriching a long history.
    local = session_times.dt.tz_localize(None).to_numpy()
    trading_day = local.astype("datetime64[D]").astype(str)

    df["trading_day"] = np.where(ny_times.isna().to_numpy(), None, trading_day)
    return df
