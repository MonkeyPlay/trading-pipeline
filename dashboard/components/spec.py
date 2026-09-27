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

# Opening range: a grey box over its own minutes, then its high-low channel.
OR_BOX = "rgba(150, 153, 164, 0.30)"
OR_FILL = "rgba(38, 166, 154, 0.13)"
OR_LINE = "#26a69a"

# Range nowcast cone: 10-90 % and 25-75 % bands of where the price may be, minute by minute.
CONE_OUTER = "rgba(255, 167, 38, 0.10)"
CONE_INNER = "rgba(255, 167, 38, 0.20)"
CONE_LINE = "#ffa726"

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


def cone_series(cone: Dict[str, Any]) -> Dict[str, Any]:
    """
    ``{'series', 'bands', 'legend'}`` drawing a range-nowcast cone: {'open': the
    session open as a New York timestamp, 't': the minute it is as of, 'end': the
    horizon's last minute + 1, 'price': the price at t, 'bar_minutes', 'prices':
    {10|25|50|75|90: the close of each minute t .. end - 1}, 'label'}. It has its
    own times - one point per bar of the timeframe, at the bar's last minute -
    so it reaches past the last bar of a live session. It starts from the price
    at t on the last completed bar.
    """
    out: Dict[str, Any] = {"series": {}, "bands": {}, "legend": []}
    if not cone or not cone.get("prices"):
        return out
    n, t0, end = int(cone.get("bar_minutes", 1)), int(cone["t"]), int(cone["end"])
    if t0 >= end:
        return out
    bars = range(t0 // n, (end - 1) // n + 1)
    times = to_epoch(pd.DatetimeIndex([cone["open"] + pd.Timedelta(minutes=k * n) for k in bars]))
    index = [min((k + 1) * n - 1, end - 1) - t0 for k in bars]
    anchor = []
    if t0 % n == 0:        # the price at t closes the bar before the cone's first
        at = to_epoch(pd.DatetimeIndex([cone["open"] + pd.Timedelta(minutes=(t0 // n - 1) * n)]))[0]
        anchor = [{"time": int(at), "value": round(float(cone["price"]), 2)}]

    def points(q):
        return anchor + [{"time": int(tm), "value": round(float(cone["prices"][q][i]), 2)}
                         for tm, i in zip(times, index)]

    for q in (10, 90, 25, 75):
        out["series"][f"cone:p{q}"] = _hidden(points(q))
    out["bands"]["cone:outer"] = {"upper": "cone:p90", "lower": "cone:p10", "color": CONE_OUTER}
    out["bands"]["cone:inner"] = {"upper": "cone:p75", "lower": "cone:p25", "color": CONE_INNER}
    out["series"]["cone:p50"] = {"points": points(50), "style": {"color": CONE_LINE, "width": 1, "dash": 2,
                                                                "title": "", "axis_label": False}}
    out["legend"].append({"key": "cone:p50", "label": cone.get("label") or "Range nowcast (median, 25-75 %, 10-90 %)",
                          "color": CONE_LINE})
    return out


def build_chart_spec(
    df: pd.DataFrame,
    *,
    features: Optional[Dict[str, Any]] = None,
    show_vwap: bool = True,
    fit: bool = False,
    opening_range: Optional[Dict[str, Any]] = None,
    cone: Optional[Dict[str, Any]] = None,
    overlay: Optional[pd.DataFrame] = None,
    visible_range: Optional[Sequence[Any]] = None,
    keep_view: bool = False,
) -> Dict[str, Any]:
    """
    Assembles the full chart spec for one session. Rows with a true ``muted``
    column are drawn grey on a shaded background (``shades``). ``overlay`` (bars
    with ``timestamp_ny`` and OHLC) is drawn as a second, desaturated candle
    series beneath the session's. ``visible_range`` ((start, end) timestamps)
    is the window shown instead of fitting everything; ``keep_view`` keeps the
    window currently shown across the new data (a timeframe change).
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

    for drawn in ((opening_range_series(df, opening_range) if opening_range and "timestamp_ny" in df.columns
                   else None),
                  cone_series(cone) if cone else None):
        if drawn:
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
        "overlay_candles": candle_points(overlay.drop(columns=["muted"], errors="ignore"))
        if overlay is not None and not overlay.empty else [],
        "fit": fit,
        "keep_view": keep_view,
    }
    if visible_range is not None:
        start, end = to_epoch(pd.DatetimeIndex([pd.Timestamp(t) for t in visible_range]))
        spec["visible_range"] = {"from": int(start), "to": int(end)}
    return spec
