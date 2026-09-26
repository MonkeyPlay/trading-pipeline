# features/indicators.py
"""
Numeric primitives for the v2 feature contract. Pure functions, no I/O.

Every recursive indicator is evaluated over a *fixed trailing window* of its
input, so the value depends only on the data inside that window and never on
how much history happened to be loaded:

  EMA(n)        alpha = 2 / (n + 1). The window holds ``warmup_multiple * n``
                inputs; the first n are averaged to seed it (SMA), the rest are
                applied recursively.
  Wilder ATR(n) the window holds ``warmup_multiple * n`` true ranges; the first
                n are averaged to seed it, then ATR = ((n - 1) * ATR + TR) / n.

Anything undefined (too little history, a zero denominator) is ``None`` -
never 0, never NaN.
"""

import math
from typing import List, Optional, Sequence

import numpy as np
import pandas as pd


def finite(x) -> Optional[float]:
    """``x`` as a float if it is a finite number, else None."""
    if x is None:
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def ratio(num, den) -> Optional[float]:
    """num / den, or None when either side is missing or den == 0."""
    num, den = finite(num), finite(den)
    if num is None or den is None or den == 0.0:
        return None
    return finite(num / den)


def ema_trailing(values: Sequence[float], n: int, warmup_multiple: int, tail: int = 1) -> Optional[List[float]]:
    """
    The last ``tail`` EMA(n) values of ``values``, each with at least
    ``warmup_multiple * n`` inputs (seed included) behind it, or None when the
    series is too short. The window used is exactly the trailing
    ``warmup_multiple * n + tail - 1`` inputs.
    """
    need = warmup_multiple * n + tail - 1
    if n < 1 or warmup_multiple < 1 or len(values) < need:
        return None
    window = np.asarray(values[len(values) - need:], dtype=float)
    alpha = 2.0 / (n + 1.0)
    e = float(window[:n].mean())
    out = [e]                       # EMA at window index n - 1
    for v in window[n:]:
        e = alpha * float(v) + (1.0 - alpha) * e
        out.append(e)
    return out[-tail:]


def true_ranges(high: Sequence[float], low: Sequence[float], prev_close: Sequence[float]) -> np.ndarray:
    """max(H - L, |H - Cprev|, |L - Cprev|), element-wise."""
    h, l, c = (np.asarray(a, dtype=float) for a in (high, low, prev_close))
    return np.maximum(h - l, np.maximum(np.abs(h - c), np.abs(l - c)))


def wilder_atr_trailing(tr: Sequence[float], n: int, warmup_multiple: int) -> Optional[float]:
    """Wilder ATR(n) over the trailing ``warmup_multiple * n`` true ranges."""
    need = warmup_multiple * n
    if n < 1 or len(tr) < need:
        return None
    window = np.asarray(tr[len(tr) - need:], dtype=float)
    if not np.all(np.isfinite(window)):
        return None
    atr = float(window[:n].mean())
    for t in window[n:]:
        atr = ((n - 1) * atr + float(t)) / n
    return finite(atr)


def aggregate_clock(bars: pd.DataFrame, minutes: int) -> pd.DataFrame:
    """
    One-minute bars -> ``minutes``-minute bars on fixed ET clock boundaries.

    ``bars`` has a tz-aware UTC ``bar_start_at`` and OHLCV columns. New York is
    always a whole number of hours from UTC and ``minutes`` divides 60, so an
    ET boundary is also a UTC boundary. A bucket is ``complete`` only when every
    one of its constituent minutes is present; callers must not feed an
    incomplete bucket into an indicator.
    """
    if 60 % minutes:
        raise ValueError("minutes must divide 60")
    cols = ["bucket_start", "bucket_end", "open", "high", "low", "close", "volume", "n", "complete"]
    if bars is None or bars.empty:
        return pd.DataFrame(columns=cols)
    df = bars.sort_values("bar_start_at")
    key = df["bar_start_at"].dt.floor(f"{minutes}min")
    g = df.groupby(key, sort=True)
    out = pd.DataFrame({
        "open": g["open"].first(),
        "high": g["high"].max(),
        "low": g["low"].min(),
        "close": g["close"].last(),
        "volume": g["volume"].sum(),
        "n": g["close"].size(),
    })
    out.index.name = "bucket_start"
    out = out.reset_index()
    out["bucket_end"] = out["bucket_start"] + pd.Timedelta(minutes=minutes)
    out["complete"] = out["n"] == minutes
    return out[cols]


def vwap_hlc3(bars: pd.DataFrame) -> Optional[float]:
    """sum(((H + L + C) / 3) * V) / sum(V) - a minute-bar VWAP approximation."""
    if bars is None or bars.empty:
        return None
    v = bars["volume"].astype(float)
    tp = (bars["high"] + bars["low"] + bars["close"]) / 3.0
    return ratio(float((tp * v).sum()), float(v.sum()))


def path_efficiency(closes: Sequence[float]) -> Optional[float]:
    """|c_last - c_0| / sum |c_i - c_{i-1}|; 0 for a constant path."""
    c = np.asarray(closes, dtype=float)
    if len(c) < 2 or not np.all(np.isfinite(c)):
        return None
    path = float(np.abs(np.diff(c)).sum())
    if path == 0.0:
        return 0.0
    return finite(abs(c[-1] - c[0]) / path)


def zscore(x, baseline: Sequence[float]) -> Optional[float]:
    """(x - mean) / sample SD of ``baseline``; None when the SD is zero."""
    x = finite(x)
    b = np.asarray(baseline, dtype=float)
    if x is None or len(b) < 2 or not np.all(np.isfinite(b)):
        return None
    sd = float(b.std(ddof=1))
    if sd == 0.0:
        return None
    return finite((x - float(b.mean())) / sd)
