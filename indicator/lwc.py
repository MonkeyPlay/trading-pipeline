# indicator/lwc.py
"""
Lightweight Charts rendering for indicator results.

Works off :class:`IndicatorResult` alone, so every indicator in this package
draws through the same path. Where the Plotly renderer this replaced returned a
figure, these helpers return plain JSON-able dicts describing *desired state*;
the chart component reconciles them against what it has already drawn.
"""

from __future__ import annotations

from typing import Any, Dict, List, Tuple

import numpy as np
import pandas as pd

from indicator.types import IndicatorResult

# Lightweight Charts' LineStyle enum.
_DASH_TO_LWC = {"solid": 0, "dot": 1, "dash": 2, "longdash": 3, "sparsedot": 4}


def rgba(hex_color: str, alpha: float) -> str:
    """Converts ``#rrggbb`` to an rgba() string at the given opacity."""
    raw = hex_color.lstrip("#")
    r, g, b = (int(raw[i : i + 2], 16) for i in (0, 2, 4))
    return f"rgba({r}, {g}, {b}, {alpha})"


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


def _points(times: np.ndarray, values) -> List[Dict[str, Any]]:
    """
    Builds line data, emitting whitespace points for gaps.

    A point with no ``value`` is how Lightweight Charts represents "no data
    here", which keeps a series' warm-up period and any NaN run from being
    joined across with a straight line.
    """
    series = pd.Series(values).to_numpy(dtype=float, na_value=np.nan)
    out: List[Dict[str, Any]] = []
    for time, value in zip(times, series):
        if np.isnan(value):
            out.append({"time": int(time)})
        else:
            out.append({"time": int(time), "value": round(float(value), 2)})
    return out


def level_points(start: int, end: int, value: float) -> List[Dict[str, Any]]:
    """
    A horizontal level as the two endpoints of its segment.

    Lightweight Charts joins consecutive points with a straight line, so a
    constant series needs no intermediate points at all.
    """
    point = round(float(value), 2)
    if start >= end:
        return [{"time": int(end), "value": point}]
    return [{"time": int(start), "value": point}, {"time": int(end), "value": point}]


def indicator_payload(
    result: IndicatorResult,
    prefix: str,
) -> Tuple[Dict[str, Any], Dict[str, Any], List[Dict[str, Any]]]:
    """
    Translates one indicator result into ``(series, bands, legend)``.

    Series keys are namespaced by ``prefix`` so two indicators can expose the
    same column name without colliding, and so the component can tell which
    series belong to an indicator that has just been switched off.

    A line style carrying ``fill`` fills against the style registered
    immediately before it — the Plotly ``tonexty`` convention the indicators
    were written against — which becomes a band entry here.
    """
    series: Dict[str, Any] = {}
    bands: Dict[str, Any] = {}
    legend: List[Dict[str, Any]] = []

    if result is None:
        return series, bands, legend

    if not result.lines.empty:
        times = to_epoch(result.lines.index)
        previous_key = None

        for column, style in result.line_styles.items():
            if column not in result.lines.columns:
                continue
            values = result.lines[column]
            if values.dropna().empty:
                continue

            key = f"{prefix}:{column}"
            series[key] = {
                "points": _points(times, values),
                "style": {
                    "color": style.color,
                    "width": style.width,
                    "dash": _DASH_TO_LWC.get(style.dash, 0),
                    "title": style.label,
                    "axis_label": False,
                },
            }
            if style.show_legend:
                legend.append({"key": key, "label": style.label, "color": style.color})

            if style.fill and previous_key is not None:
                bands[f"{key}:fill"] = {
                    "upper": previous_key,
                    "lower": key,
                    "color": style.fill_color or rgba("#2962FF", 0.1),
                }
            previous_key = key

    if result.levels:
        level_style = result.level_style
        # Levels need a time axis of their own: the lines frame may be empty when
        # an indicator only produces horizontal levels.
        index = result.lines.index if not result.lines.empty else None
        if index is not None and len(index):
            times = to_epoch(index)
            for level in result.levels:
                key = f"{prefix}:level:{level.key}"
                # Pine extends a level right from the bar it was set on. The value
                # never changes, so two points draw the same line as one per bar
                # would — and keep a chart of ten levels from shipping ten
                # thousand identical numbers to the browser.
                start = times[0]
                if level.anchor is not None:
                    anchored = to_epoch(pd.DatetimeIndex([level.anchor]))[0]
                    start = max(start, min(anchored, times[-1]))

                series[key] = {
                    "points": level_points(start, times[-1], level.value),
                    "style": {
                        "color": level.color,
                        "width": level_style.width,
                        "dash": _DASH_TO_LWC.get(level_style.dash, 0),
                        "title": level.label,
                        "axis_label": level_style.show_labels,
                    },
                }
                # Deliberately not added to the legend: a level already writes its
                # own value on the price axis, and a dozen of them would cover the
                # candles they are drawn against.

    return series, bands, legend
