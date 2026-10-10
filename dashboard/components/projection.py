# dashboard/components/projection.py
"""
The projection trend the Session Explorer draws on the current session's chart:
a visual extrapolation of the chart's TEMA 14 (SMA 3) and EMA 14 (SMA 3) lines - not a
forecast, and separate from the fan and the trained models.

  combined[t] = 0.5 x TEMA14[t] + 0.5 x EMA14[t]      (at the chart's timeframe)

From the last candle shown, H = 15 elapsed minutes ahead whatever the timeframe, one cubic
Bezier curve in (minutes after that candle's start, price):

  P0 = (0,      y0)                     y0   = the last combined value
  P1 = (H/3,    y0 + s0 x H/3)          s0   = the last segment's slope, points per minute
  P2 = (2H/3,   y0 + 2H x sAvg / 3)     sAvg = the last five segments' slopes weighted 1..5,
  P3 = (H,      y0 + H x sAvg)                 oldest to newest

so it leaves in the latest direction and ends on the weighted average. Only the candles shown
are read (in playback, those up to its cutoff - never the later ones); it needs the last six
candles contiguous at the timeframe with both lines defined, else it is not drawn. A session
ending inside H cuts the curve there (de Casteljau, so the part kept is the same curve). The
combined series itself is never drawn.

  trend_projection(rows, tf, session_end)   the curve's control points, or None and why
  extend_axis(spec)                          blank candles to the curve's end, so the time axis holds it
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from dashboard import theme

HORIZON_MINUTES = 15
SEGMENTS = 5
WEIGHTS = np.arange(1, SEGMENTS + 1, dtype=float)       # oldest to newest
LABEL = "Projection trend"
NOTE = ("Projection trend: TEMA 14 and EMA 14 averaged and extrapolated 15 minutes - a visual "
        "extrapolation, not a forecast and separate from the fan.")
RGB = theme.rgb(theme.PROJECTION)                       # teal: apart from the fan, the brackets and the MA lines


def _split(P: np.ndarray, u: float) -> np.ndarray:
    """The control points of the curve's part from 0 to ``u`` (de Casteljau)."""
    a = P[:-1] + u * (P[1:] - P[:-1])
    b = a[:-1] + u * (a[1:] - a[:-1])
    c = b[:-1] + u * (b[1:] - b[:-1])
    return np.array([P[0], a[0], b[0], c[0]])


def bezier(P: np.ndarray, u: np.ndarray) -> np.ndarray:
    """Points of the cubic with control points ``P`` (4 x 2) at parameters ``u``."""
    u = np.asarray(u, dtype=float)[:, None]
    return ((1 - u) ** 3 * P[0] + 3 * (1 - u) ** 2 * u * P[1] + 3 * (1 - u) * u ** 2 * P[2] + u ** 3 * P[3])


def trend_projection(rows: Optional[pd.DataFrame], tf: int,
                     session_end: Optional[pd.Timestamp] = None) -> Tuple[Optional[Dict[str, Any]], str]:
    """
    ``(projection, why)``: from ``rows`` - the candles shown, oldest first, with ``timestamp_ny`` (each candle's start),
    ``tema`` and ``ema_trigger``, at a timeframe of ``tf`` minutes - the curve's control points as ``[minutes, price]`` from the last candle's start, its
    start time, the slopes and its end (cut at ``session_end``, the trading day's end, when that comes sooner); or
    None and the reason it is not drawn.
    """
    if rows is None or len(rows) < SEGMENTS + 1:
        return None, "not drawn: fewer than six candles"
    last = rows.iloc[-(SEGMENTS + 1):]
    combined = 0.5 * last["tema"].astype(float).to_numpy() + 0.5 * last["ema_trigger"].astype(float).to_numpy()
    if not np.isfinite(combined).all():
        return None, "not drawn: TEMA 14 or EMA 14 is not defined on the last six candles"
    starts = pd.DatetimeIndex(last["timestamp_ny"])
    gaps = (starts[1:] - starts[:-1]).total_seconds().to_numpy() / 60
    if not np.allclose(gaps, tf):
        return None, "not drawn: the last six candles are not contiguous"
    slopes = np.diff(combined) / tf                           # points per minute
    y0, s0 = float(combined[-1]), float(slopes[-1])
    s_avg = float(np.dot(WEIGHTS, slopes) / WEIGHTS.sum())
    H = float(HORIZON_MINUTES)
    P = np.array([[0.0, y0], [H / 3, y0 + s0 * H / 3], [2 * H / 3, y0 + 2 * H * s_avg / 3], [H, y0 + H * s_avg]])
    origin = starts[-1]
    end_minutes = H
    if session_end is not None:
        left = (pd.Timestamp(session_end) - origin).total_seconds() / 60
        if left <= 0:
            return None, "not drawn: the session has ended"
        if left < H:
            P = _split(P, left / H)                          # x is linear in u: u = minutes / H
            end_minutes = left
    curve = bezier(P, np.linspace(0, 1, 61))[:, 1]
    return {"origin": origin, "timeframe_minutes": tf, "range": [round(float(curve.min()), 4), round(float(curve.max()), 4)], "control": [[round(float(x), 6), round(float(y), 4)] for x, y in P],
            "y0": round(y0, 4), "s0": round(s0, 6), "s_avg": round(s_avg, 6), "end_minutes": round(end_minutes, 4),
            "end_price": round(float(P[-1, 1]), 4), "label": LABEL, "rgb": RGB}, ""


def shown_candles(rows: Optional[pd.DataFrame]) -> Optional[pd.DataFrame]:
    """The candles a projection may read: the shown ones without the muted later candles of playback's reveal."""
    if rows is None or "muted" not in rows:
        return rows
    return rows[rows["muted"] != True]                       # noqa: E712 - NaN where not muted


def extend_axis(spec: Dict[str, Any]) -> Dict[str, Any]:
    """``spec`` with a blank candle at every timeframe step after its last candle up to the projection's end (as the
    fan's columns do, dashboard/components/fan.attach): the chart's time axis then reaches the curve's end."""
    p = spec.get("projection")
    if not p or not spec.get("candles"):
        return spec
    step = 60 * int(p["tf"])
    end = p["time"] + int(np.ceil(p["control"][-1][0] * 60 / step)) * step
    last = spec["candles"][-1]["time"]
    spec["candles"] = spec["candles"] + [{"time": t} for t in range(last + step, end + 1, step)]
    return spec
