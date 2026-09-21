# indicator/auto_anchored_vwap.py
"""
Python port of the "Auto Anchored VWAP [v6]" Pine v6 indicator.

Picks an anchor bar (period boundary, or the extreme high/low/volume within a
lookback), then accumulates a volume-weighted average price forward from it with
optional standard-deviation or percentage bands.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd

from indicator.lwc import rgba
from indicator.types import IndicatorResult, LineStyle

NAME = "Auto Anchored VWAP"

ANCHOR_PERIOD_OPTIONS = (
    "Auto",
    "Highest High",
    "Lowest Low",
    "Highest Volume",
    "Session",
    "Week",
    "Month",
    "Quarter",
    "Year",
)
SOURCE_OPTIONS = ("open", "high", "low", "close", "hl2", "hlc3", "ohlc4")
CALC_MODE_OPTIONS = ("Standard Deviation", "Percentage")

# Pine transparency values from the script, expressed as opacity.
_BAND1_ALPHA = 0.60
_BAND2_ALPHA = 0.50
_BAND3_ALPHA = 0.40
_FILL_ALPHA = 0.10


@dataclass
class AutoAnchoredVwapSettings:
    """Mirrors the Pine ``input`` block, with the same defaults."""

    # Anchor Settings
    anchor_period: str = "Auto"
    lookback_length: int = 100
    source: str = "hlc3"
    vwap_color: str = "#FF9800"
    vwap_width: int = 2

    # Bands Configuration
    calc_mode: str = "Standard Deviation"
    show_band1: bool = True
    mult1: float = 1.0
    color_band1: str = "#2962FF"
    show_band2: bool = False
    mult2: float = 2.0
    color_band2: str = "#E91E63"
    show_band3: bool = False
    mult3: float = 3.0
    color_band3: str = "#FF9800"
    fill_bands: bool = True
    fill_color: str = "#2962FF"


def _display_timestamps(df: pd.DataFrame) -> pd.DatetimeIndex:
    column = "timestamp_ny" if "timestamp_ny" in df.columns else "timestamp_utc"
    timestamps = pd.to_datetime(df[column], errors="coerce")
    if timestamps.dt.tz is None:
        timestamps = timestamps.dt.tz_localize("UTC")
    return pd.DatetimeIndex(timestamps)


def _source_series(frame: pd.DataFrame, source: str) -> np.ndarray:
    o = frame["open"].astype(float)
    h = frame["high"].astype(float)
    low = frame["low"].astype(float)
    c = frame["close"].astype(float)

    if source == "open":
        values = o
    elif source == "high":
        values = h
    elif source == "low":
        values = low
    elif source == "close":
        values = c
    elif source == "hl2":
        values = (h + low) / 2.0
    elif source == "hlc3":
        values = (h + low + c) / 3.0
    elif source == "ohlc4":
        values = (o + h + low + c) / 4.0
    else:
        raise ValueError(f"Unknown source {source!r}; expected one of {SOURCE_OPTIONS}")

    return values.to_numpy(dtype=float)


def _session_dates(timestamps: pd.DatetimeIndex) -> pd.DatetimeIndex:
    """
    The exchange session each bar belongs to.

    CME index futures roll at 18:00 ET, so shifting forward six hours puts a
    session's evening open onto the same calendar date as its cash hours -- the
    same boundary ``features.session_windows.get_trading_day_date`` applies.
    """
    return (timestamps + pd.Timedelta(hours=6)).normalize()


def _period_keys(timestamps: pd.DatetimeIndex, period: str) -> np.ndarray:
    sessions = _session_dates(timestamps)

    if period == "Session":
        return sessions.astype("int64")
    if period == "Week":
        iso = sessions.isocalendar()
        return (iso["year"].to_numpy() * 100 + iso["week"].to_numpy()).astype("int64")
    if period == "Month":
        return (sessions.year * 100 + sessions.month).to_numpy().astype("int64")
    if period == "Quarter":
        return (sessions.year * 10 + sessions.quarter).to_numpy().astype("int64")
    if period == "Year":
        return sessions.year.to_numpy().astype("int64")

    raise ValueError(f"Unknown anchor period {period!r}")


def _resolve_auto_period(bar_delta: Optional[pd.Timedelta]) -> str:
    """Pine: intraday -> Session, daily -> Month, weekly -> Quarter, else Year."""
    if bar_delta is None or bar_delta < pd.Timedelta(days=1):
        return "Session"
    if bar_delta < pd.Timedelta(days=7):
        return "Month"
    if bar_delta < pd.Timedelta(days=28):
        return "Quarter"
    return "Year"


def _most_recent_extreme(values: np.ndarray, window: int, use_max: bool) -> int:
    """
    Index of the extreme value within the last ``window`` bars.

    ``ta.highestbars`` / ``ta.lowestbars`` report the most recent occurrence when
    the extreme repeats, so ties resolve to the latest bar.
    """
    n = len(values)
    start = max(0, n - window)
    segment = values[start:n][::-1]
    offset = int(np.nanargmax(segment) if use_max else np.nanargmin(segment))
    return n - 1 - offset


def compute(df: pd.DataFrame, settings: Optional[AutoAnchoredVwapSettings] = None) -> IndicatorResult:
    """
    Runs the indicator over an OHLCV frame.

    Pine evaluates this only on the last bar, walking forward from the anchor, so
    the series here begin at the anchor and are na before it. Feed in more history
    than you display when the anchor period reaches past the visible window.
    """
    settings = settings or AutoAnchoredVwapSettings()

    if df is None or df.empty:
        return IndicatorResult()

    frame = df.copy()
    frame["_timestamp"] = _display_timestamps(frame)
    frame = frame.dropna(subset=["_timestamp"]).sort_values("_timestamp").reset_index(drop=True)
    if frame.empty:
        return IndicatorResult()

    display_index = pd.DatetimeIndex(frame["_timestamp"])
    n = len(frame)

    bar_delta = None
    if n > 1:
        deltas = pd.Series(display_index).diff().dropna()
        if not deltas.empty:
            bar_delta = deltas.median()

    source = _source_series(frame, settings.source)
    volume = frame["volume"].astype(float).to_numpy()
    volume = np.where(np.isnan(volume), 1.0, volume)

    # ---- Anchor resolution --------------------------------------------------
    last_bar = n - 1
    period = settings.anchor_period
    if period == "Auto":
        period = _resolve_auto_period(bar_delta)

    if period in ("Highest High", "Lowest Low", "Highest Volume"):
        window = max(1, min(settings.lookback_length, last_bar))
        if period == "Highest High":
            anchor = _most_recent_extreme(frame["high"].astype(float).to_numpy(), window, use_max=True)
        elif period == "Lowest Low":
            anchor = _most_recent_extreme(frame["low"].astype(float).to_numpy(), window, use_max=False)
        else:
            anchor = _most_recent_extreme(volume, window, use_max=True)
    else:
        keys = _period_keys(display_index, period)
        boundaries = np.flatnonzero(np.diff(keys) != 0) + 1
        anchor = int(boundaries[-1]) if len(boundaries) else 0

    anchor = int(max(0, min(anchor, last_bar)))

    # ---- Cumulative VWAP forward from the anchor ----------------------------
    p = source[anchor:]
    v = volume[anchor:]

    cum_pv = np.cumsum(p * v)
    cum_vol = np.cumsum(v)
    cum_pv2 = np.cumsum(p * p * v)

    has_volume = cum_vol > 0
    vwap = np.where(has_volume, np.divide(cum_pv, cum_vol, out=np.zeros_like(cum_pv), where=has_volume), p)

    if settings.calc_mode == "Percentage":
        # A multiplier of 1.0 means 1% of the running VWAP.
        dev = vwap * 0.01
    else:
        mean_square = np.divide(cum_pv2, cum_vol, out=np.zeros_like(cum_pv2), where=has_volume)
        dev = np.sqrt(np.maximum(0.0, mean_square - vwap * vwap))

    def _padded(values: np.ndarray) -> np.ndarray:
        out = np.full(n, np.nan)
        out[anchor:] = values
        return out

    lines = pd.DataFrame(index=display_index)
    line_styles = {}
    unit = "%" if settings.calc_mode == "Percentage" else "σ"

    bands = (
        (1, settings.show_band1, settings.mult1, settings.color_band1, _BAND1_ALPHA),
        (2, settings.show_band2, settings.mult2, settings.color_band2, _BAND2_ALPHA),
        (3, settings.show_band3, settings.mult3, settings.color_band3, _BAND3_ALPHA),
    )

    for number, show, mult, color, alpha in bands:
        if not show:
            continue
        upper, lower = f"aavwap_u{number}", f"aavwap_l{number}"
        lines[upper] = _padded(vwap + mult * dev)
        lines[lower] = _padded(vwap - mult * dev)

        label = f"AAVWAP ±{mult:g}{unit}"
        group = f"aavwap_band{number}"
        line_styles[upper] = LineStyle(
            label=label, color=rgba(color, alpha), width=1, legend_group=group
        )
        # Pine fills only the inner channel, between band 1's two edges.
        fill = "tonexty" if (number == 1 and settings.fill_bands) else None
        line_styles[lower] = LineStyle(
            label=label,
            color=rgba(color, alpha),
            width=1,
            fill=fill,
            fill_color=rgba(settings.fill_color, _FILL_ALPHA) if fill else None,
            legend_group=group,
            show_legend=False,
        )

    lines["aavwap"] = _padded(vwap)
    line_styles["aavwap"] = LineStyle(
        label="Anchored VWAP", color=settings.vwap_color, width=settings.vwap_width
    )

    return IndicatorResult(
        lines=lines,
        line_styles=line_styles,
        bar_delta=bar_delta,
        meta={
            "anchor_period": period,
            "anchor_timestamp": display_index[anchor],
            "anchor_bars_back": last_bar - anchor,
        },
    )
