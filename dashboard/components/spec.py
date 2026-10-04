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

from features.calculations import (
    TEMA_LENGTH,
    TEMA_SMOOTHING,
    TREND_EMA_LENGTH,
    TRIGGER_EMA_LENGTH,
    TRIGGER_SMOOTHING,
)

_UP = "rgba(38, 166, 154, 0.5)"
_DOWN = "rgba(239, 83, 80, 0.5)"

# Bars outside the regular session (the 15 minutes either side of it) are drawn
# muted: grey candles and volume on a grey background band.
MUTED_CANDLE = "#6b6f7a"
MUTED_VOLUME = "rgba(120, 123, 134, 0.35)"
MUTED_BACKGROUND = "rgba(120, 123, 134, 0.14)"

# Opening range: a grey box over its own minutes, then its high-low channel.
OR_BOX = "rgba(150, 153, 164, 0.30)"
OR_FILL = "rgba(38, 166, 154, 0.13)"
OR_LINE = "#26a69a"

# Pre-open reference levels (features.calculations.pre_open_levels), in draw order.
_REFERENCE_LEVELS = (
    ("previous_rth_close", "Prev RTH Close", "#29b6f6", 2, 2),
    ("overnight_high", "Overnight High", "#ffa726", 1, 1),
    ("overnight_low", "Overnight Low", "#ffa726", 1, 1),
)

# The session VWAP line (and the Review page's cutoff VWAP level).
VWAP_COLOR = "#fdd835"

# Moving averages (features.calculations.calculate_moving_averages): (column, label,
# colour, width), with the TradingView script's plot colours and widths.
_MOVING_AVERAGES = (
    ("tema", f"TEMA {TEMA_LENGTH} (SMA {TEMA_SMOOTHING})", "#9c27b0", 2),
    ("ema_trend", f"EMA {TREND_EMA_LENGTH}", "#2962ff", 2),
    ("ema_trigger", f"EMA {TRIGGER_EMA_LENGTH} (SMA {TRIGGER_SMOOTHING})", "#ff9800", 1),
)


_RESAMPLE_FREQ = {"2m": "2min", "5m": "5min", "15m": "15min", "30m": "30min"}


def resample(df: pd.DataFrame, timeframe: str) -> pd.DataFrame:
    """Aggregates 1-minute bars (with ``timestamp_ny``) up to a display interval: 1m, 2m, 5m, 15m or 30m."""
    if timeframe == "1m" or df is None or df.empty:
        return df

    agg_rules = {
        "timestamp_utc": "first",
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "volume": "sum",
        "price_type": "first",
    }
    resampled = df.sort_values("timestamp_ny").set_index("timestamp_ny")
    return resampled.resample(_RESAMPLE_FREQ[timeframe]).agg(agg_rules).dropna().reset_index()


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


def _curve_series(times: np.ndarray, values: pd.Series, label: str, color: str, width: int) -> Dict[str, Any]:
    """A line through one value per bar; a missing value leaves a gap."""
    return {
        "points": [
            {"time": int(t)} if pd.isna(v) else {"time": int(t), "value": round(float(v), 2)}
            for t, v in zip(times, values)
        ],
        "style": {"color": color, "width": width, "dash": 0, "title": label, "axis_label": True},
    }


def _hidden(points: List[Dict[str, Any]]) -> Dict[str, Any]:
    """An invisible line - one edge of a filled box."""
    return {"points": points, "style": {"color": OR_LINE, "width": 1, "dash": 0, "title": "",
                                        "axis_label": False, "line_visible": False}}


def opening_range_series(df: pd.DataFrame, opening_range: Dict[str, Any]) -> Dict[str, Any]:
    """
    ``{'series', 'bands', 'legend'}`` drawing an opening range
    ({'high', 'low', 'start', 'end'}: its price extremes and its minutes
    [start, end) as New York timestamps): a grey box over the bars inside it
    (when there are at least two), then ORH and ORL lines with the channel
    between them filled, from the first bar after it to the last bar shown.
    """
    out: Dict[str, Any] = {"series": {}, "bands": {}, "legend": []}
    if df is None or df.empty or not opening_range:
        return out
    hi, lo = float(opening_range["high"]), float(opening_range["low"])
    ts = pd.to_datetime(df["timestamp_ny"])
    times = to_epoch(ts)
    start, end = opening_range["start"], opening_range["end"]
    inside = times[((ts >= start) & (ts < end)).to_numpy()]
    after = times[(ts >= end).to_numpy()]
    if len(inside) >= 2:
        out["series"]["or:box_high"] = _hidden(level_points(inside[0], inside[-1], hi))
        out["series"]["or:box_low"] = _hidden(level_points(inside[0], inside[-1], lo))
        out["bands"]["or:box"] = {"upper": "or:box_high", "lower": "or:box_low", "color": OR_BOX}
    if len(after) >= 1:
        for key, label, value in (("features:orh", "ORH", hi), ("features:orl", "ORL", lo)):
            out["series"][key] = {
                "points": level_points(after[0], after[-1], value),
                "style": {"color": OR_LINE, "width": 1, "dash": 0, "title": label, "axis_label": True},
            }
            out["legend"].append({"key": key, "label": label, "color": OR_LINE})
        if len(after) >= 2:
            out["bands"]["or:range"] = {"upper": "features:orh", "lower": "features:orl", "color": OR_FILL}
    return out


def build_chart_spec(
    df: pd.DataFrame,
    *,
    levels: Optional[Dict[str, Any]] = None,
    extra_levels: Optional[Sequence[Dict[str, Any]]] = None,
    show_vwap: bool = True,
    fit: bool = False,
    opening_range: Optional[Dict[str, Any]] = None,
    visible_range: Optional[Sequence[Any]] = None,
    keep_view: bool = False,
) -> Dict[str, Any]:
    """
    Assembles the full chart spec for one session. Rows with a true ``muted``
    column are drawn grey on a shaded background (``shades``). ``levels`` are the
    pre-open reference levels (``pre_open_levels``; a None value is not drawn);
    ``extra_levels`` more horizontal lines, each ``{'key', 'label', 'value', 'color',
    'dash'}`` (None values skipped).
    ``visible_range`` ((start, end) timestamps) is the window shown instead of
    fitting everything; ``keep_view`` keeps the window currently shown across the
    new data (a timeframe change).
    """
    if df is None or df.empty:
        return {"candles": [], "volume": [], "series": {}, "bands": {}, "legend": [], "shades": []}

    df = df.sort_values("timestamp_ny" if "timestamp_ny" in df.columns else "timestamp_utc")
    times = _times(df)

    series: Dict[str, Any] = {}
    bands: Dict[str, Any] = {}
    legend: List[Dict[str, Any]] = []

    if levels:
        for key, label, color, width, dash in _REFERENCE_LEVELS:
            value = levels.get(key)
            if value is None:
                continue
            series_key = f"features:{key}"
            series[series_key] = _level_series(times, value, label, color, width, dash)
            legend.append({"key": series_key, "label": label, "color": color})

    for extra in extra_levels or ():
        if extra.get("value") is None:
            continue
        series_key = f"extra:{extra['key']}"
        series[series_key] = _level_series(times, float(extra["value"]), extra["label"], extra["color"], 1,
                                           extra.get("dash", 2))
        legend.append({"key": series_key, "label": extra["label"], "color": extra["color"]})

    if show_vwap and "vwap" in df.columns and df["vwap"].notna().any():
        series["features:vwap"] = _curve_series(times, df["vwap"], "Session VWAP", VWAP_COLOR, 2)
        legend.append({"key": "features:vwap", "label": "Session VWAP", "color": VWAP_COLOR})

    for column, label, color, width in _MOVING_AVERAGES:
        if column in df.columns and df[column].notna().any():
            series[f"ma:{column}"] = _curve_series(times, df[column], label, color, width)
            legend.append({"key": f"ma:{column}", "label": label, "color": color})

    if opening_range and "timestamp_ny" in df.columns:
        drawn = opening_range_series(df, opening_range)
        series.update(drawn["series"])
        bands.update(drawn["bands"])
        legend.extend(drawn["legend"])

    spec = {
        "candles": candle_points(df),
        "volume": volume_points(df),
        "series": series,
        "bands": bands,
        "legend": legend,
        "shades": shade_ranges(df),
        "shade_color": MUTED_BACKGROUND,
        "fit": fit,
        "keep_view": keep_view,
    }
    if visible_range is not None:
        start, end = to_epoch(pd.DatetimeIndex([pd.Timestamp(t) for t in visible_range]))
        spec["visible_range"] = {"from": int(start), "to": int(end)}
    return spec
