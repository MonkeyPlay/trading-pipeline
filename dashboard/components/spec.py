# dashboard/components/spec.py
"""
Builds Lightweight Charts specs from the pipeline's own data structures.

Everything the chart shows is described here as plain JSON-able dicts: candles,
the volume pane and the pre-open reference levels. The component diffs the spec
against what it has already drawn, so building a *whole* spec on every
interaction is the intended usage — it is the component, not the caller, that
decides what needs to change on the chart.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

import numpy as np
import pandas as pd

_UP = "rgba(38, 166, 154, 0.5)"
_DOWN = "rgba(239, 83, 80, 0.5)"

# Bars outside the regular session (the 15 minutes either side of it) are drawn
# muted: grey candles and volume on a grey background band.
MUTED_CANDLE = "#6b6f7a"
MUTED_VOLUME = "rgba(120, 123, 134, 0.35)"
MUTED_BACKGROUND = "rgba(120, 123, 134, 0.14)"

# Pre-open reference levels drawn from the feature snapshot, in draw order.
_FEATURE_LEVELS = (
    ("previous_rth_close", "Prev RTH Close", "#29b6f6", 2, 2),
    ("overnight_high", "Overnight High", "#ffa726", 1, 1),
    ("overnight_low", "Overnight Low", "#ffa726", 1, 1),
)


def to_epoch(timestamps) -> np.ndarray:
    """
    Converts timestamps to the integer seconds Lightweight Charts expects.

    The library has no timezone support and always renders UTC, so a tz-aware
    index is flattened to its *wall clock* first. The axis then reads New York
    time, and because the offset is taken per timestamp, DST changes land in the
    right place instead of shifting a whole session by an hour.
    """
    index = pd.DatetimeIndex(timestamps)
    if index.tz is not None:
        index = index.tz_localize(None)
    # A DatetimeIndex may carry any resolution, so normalise the unit rather
    # than assuming asi8 counts nanoseconds.
    return index.as_unit("s").asi8


def level_points(start: int, end: int, value: float) -> List[Dict[str, Any]]:
    """
    A horizontal level as the two endpoints of its segment.

    Lightweight Charts joins consecutive points with a straight line, so a
    constant series needs no intermediate points at all — a dozen levels would
    otherwise ship tens of thousands of identical numbers to the browser.
    """
    point = round(float(value), 2)
    if start >= end:
        return [{"time": int(end), "value": point}]
    return [{"time": int(start), "value": point}, {"time": int(end), "value": point}]


def _times(df: pd.DataFrame) -> np.ndarray:
    column = "timestamp_ny" if "timestamp_ny" in df.columns else "timestamp_utc"
    return to_epoch(pd.to_datetime(df[column]))


def _muted(df: pd.DataFrame) -> np.ndarray:
    """The optional boolean ``muted`` column (bars outside the regular session)."""
    if "muted" not in df.columns:
        return np.zeros(len(df), dtype=bool)
    return df["muted"].fillna(False).astype(bool).to_numpy()


def candle_points(df: pd.DataFrame) -> List[Dict[str, Any]]:
    """OHLC rows in the shape Lightweight Charts wants; muted bars are grey."""
    times = _times(df)
    out = []
    for time, o, h, low, c, muted in zip(
        times, df["open"], df["high"], df["low"], df["close"], _muted(df)
    ):
        point = {"time": int(time), "open": float(o), "high": float(h), "low": float(low), "close": float(c)}
        if muted:
            point["color"] = point["wickColor"] = MUTED_CANDLE
        out.append(point)
    return out


def volume_points(df: pd.DataFrame) -> List[Dict[str, Any]]:
    """Volume bars, tinted by whether the bar closed up or down (grey when muted)."""
    times = _times(df)
    return [
        {
            "time": int(time),
            "value": float(v),
            "color": MUTED_VOLUME if muted else _UP if c >= o else _DOWN,
        }
        for time, v, o, c, muted in zip(times, df["volume"], df["open"], df["close"], _muted(df))
    ]


def shade_ranges(df: pd.DataFrame) -> List[Dict[str, int]]:
    """Each run of consecutive muted bars as ``{'from': first bar time, 'to': last bar time}``."""
    times, muted = _times(df), _muted(df)
    ranges: List[Dict[str, int]] = []
    start = None
    for i, m in enumerate(muted):
        if m and start is None:
            start = i
        if start is not None and (not m or i == len(muted) - 1):
            end = i if m else i - 1
            ranges.append({"from": int(times[start]), "to": int(times[end])})
            start = None
    return ranges


def _level_series(times: np.ndarray, value: float, label: str, color: str,
                  width: int, dash: int) -> Dict[str, Any]:
    return {
        "points": level_points(times[0], times[-1], value),
        "style": {
            "color": color,
            "width": width,
            "dash": dash,
            "title": label,
            "axis_label": True,
        },
    }


def build_chart_spec(
    df: pd.DataFrame,
    *,
    features: Optional[Dict[str, Any]] = None,
    show_vwap: bool = True,
    fit: bool = False,
) -> Dict[str, Any]:
    """
    Assembles the full chart spec for one session. Rows with a true ``muted``
    column are drawn grey on a shaded background (``shades``).
    """
    if df is None or df.empty:
        return {"candles": [], "volume": [], "series": {}, "bands": {}, "legend": [], "shades": []}

    df = df.sort_values("timestamp_ny" if "timestamp_ny" in df.columns else "timestamp_utc")
    times = _times(df)

    series: Dict[str, Any] = {}
    bands: Dict[str, Any] = {}
    legend: List[Dict[str, Any]] = []

    if features:
        for key, label, color, width, dash in _FEATURE_LEVELS:
            value = features.get(key)
            if value is None:
                continue
            series_key = f"features:{key}"
            series[series_key] = _level_series(times, value, label, color, width, dash)
            legend.append({"key": series_key, "label": label, "color": color})

    if show_vwap and "vwap" in df.columns and df["vwap"].notna().any():
        series["features:vwap"] = {
            "points": [
                {"time": int(t)} if pd.isna(v) else {"time": int(t), "value": round(float(v), 2)}
                for t, v in zip(times, df["vwap"])
            ],
            "style": {
                "color": "#ab47bc",
                "width": 2,
                "dash": 0,
                "title": "Session VWAP",
                "axis_label": True,
            },
        }
        legend.append({"key": "features:vwap", "label": "Session VWAP", "color": "#ab47bc"})

    return {
        "candles": candle_points(df),
        "volume": volume_points(df),
        "series": series,
        "bands": bands,
        "legend": legend,
        "shades": shade_ranges(df),
        "shade_color": MUTED_BACKGROUND,
        "fit": fit,
    }


def build_accuracy_spec(dates: Sequence[str], accuracy: Sequence[float]) -> Dict[str, Any]:
    """
    Spec for the evaluation view's cumulative-accuracy line.

    Uses the same component as the candle chart, so there is one charting stack
    in the app rather than two. There are no candles here — only the two line
    series — which the component handles by simply having nothing in that pane.
    """
    points = [
        {"time": str(date), "value": round(float(value), 2)}
        for date, value in zip(dates, accuracy)
    ]
    baseline = [{"time": str(date), "value": 50.0} for date in dates]

    return {
        "candles": [],
        "volume": [],
        "series": {
            "accuracy": {
                "points": points,
                "style": {
                    "color": "#26a69a",
                    "width": 3,
                    "dash": 0,
                    "title": "Cumulative accuracy",
                    "axis_label": True,
                },
            },
            "baseline": {
                "points": baseline,
                "style": {
                    "color": "#ffa726",
                    "width": 1,
                    "dash": 2,
                    "title": "Coin flip",
                    "axis_label": False,
                },
            },
        },
        "bands": {},
        "legend": [
            {"key": "accuracy", "label": "Cumulative accuracy %", "color": "#26a69a"},
            {"key": "baseline", "label": "Coin-flip baseline", "color": "#ffa726"},
        ],
        "options": {"timeScale": {"timeVisible": False, "rightOffset": 2}},
        "fit": True,
    }
