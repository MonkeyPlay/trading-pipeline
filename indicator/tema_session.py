# indicator/tema_session.py
"""
Python port of the "TEMA & Session Levels" Pine v6 indicator ((c) Ununseptium, MPL-2.0).

Overlays a smoothed TEMA, a smoothed trigger EMA and a trend EMA on the candles,
plus previous-RTH, overnight and premarket high/low/open levels.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List, Optional

import pandas as pd

from indicator.pine import parse_session, pine_ema, pine_sma, session_mask
from indicator.types import DASH_BY_NAME, IndicatorResult, LevelStyle, LineStyle, SessionLevel

NAME = "TEMA & Session Levels"

# TradingView's built-in colour constants, so the port matches the chart it came from.
_LIME = "#00E676"
_RED = "#FF5252"
_GRAY = "#787B86"
_AQUA = "#00BCD4"
_ORANGE = "#FF9800"
_BLUE = "#2962FF"
_FUCHSIA = "#E040FB"
_YELLOW = "#FFEB3B"
_PURPLE = "#9C27B0"


@dataclass
class TemaSessionSettings:
    """Mirrors the Pine ``input`` block, with the same defaults."""

    # TEMA & EMA 9 Settings
    tema_length: int = 14
    tema_smoothing_length: int = 3
    trigger_ema_length: int = 14
    ema9_smoothing_length: int = 3

    # Trend Bands
    trend_ema_length: int = 100

    # Session Times
    timezone: str = "America/New_York"
    rth_session: str = "0930-1615"
    overnight_session: str = "1800-0930"
    premarket_session: str = "0800-0930"

    # Previous RTH
    show_prev_rth: bool = True
    color_prev_high: str = _LIME
    color_prev_low: str = _RED
    color_prev_open: str = _GRAY

    # Overnight (ON)
    show_overnight: bool = True
    color_on_high: str = _AQUA
    color_on_low: str = _ORANGE
    color_on_open: str = _BLUE

    # Premarket
    show_premarket: bool = True
    color_pm_high: str = _FUCHSIA
    color_pm_low: str = _YELLOW

    # Style
    line_width: int = 1
    line_style: str = "Solid"
    show_labels: bool = True
    label_offset: int = 5


def _display_timestamps(df: pd.DataFrame) -> pd.DatetimeIndex:
    """Bar timestamps on the chart's own timeline."""
    column = "timestamp_ny" if "timestamp_ny" in df.columns else "timestamp_utc"
    timestamps = pd.to_datetime(df[column], errors="coerce")
    if timestamps.dt.tz is None:
        timestamps = timestamps.dt.tz_localize("UTC")
    return pd.DatetimeIndex(timestamps)


def _nz_max(current: float, candidate: float) -> float:
    return candidate if math.isnan(current) else max(current, candidate)


def _nz_min(current: float, candidate: float) -> float:
    return candidate if math.isnan(current) else min(current, candidate)


def _level(key, label, value, color, anchor) -> Optional[SessionLevel]:
    if value is None or math.isnan(value):
        return None
    return SessionLevel(key=key, label=label, value=float(value), color=color, anchor=anchor)


def compute(df: pd.DataFrame, settings: Optional[TemaSessionSettings] = None) -> IndicatorResult:
    """
    Runs the indicator over an OHLC frame.

    The frame should carry ``timestamp_ny`` (or ``timestamp_utc``) plus open/high/
    low/close. Feed it more history than you intend to display: the EMAs need a
    warm-up run and the previous-RTH levels need the prior session's bars.
    """
    settings = settings or TemaSessionSettings()

    if df is None or df.empty:
        return IndicatorResult()

    frame = df.copy()
    frame["_timestamp"] = _display_timestamps(frame)
    frame = frame.dropna(subset=["_timestamp"]).sort_values("_timestamp").reset_index(drop=True)
    if frame.empty:
        return IndicatorResult()

    display_index = pd.DatetimeIndex(frame["_timestamp"])

    # ---- TEMA & EMA calculations -------------------------------------------
    close = frame["close"].astype(float)
    ema1 = pine_ema(close, settings.tema_length)
    ema2 = pine_ema(ema1, settings.tema_length)
    ema3 = pine_ema(ema2, settings.tema_length)
    tema_raw = 3.0 * (ema1 - ema2) + ema3

    lines = pd.DataFrame(index=display_index)
    lines["tema_smoothed"] = pine_sma(tema_raw, settings.tema_smoothing_length).to_numpy()
    lines["ema_trend"] = pine_ema(close, settings.trend_ema_length).to_numpy()
    lines["ema_trigger_smoothed"] = pine_sma(
        pine_ema(close, settings.trigger_ema_length), settings.ema9_smoothing_length
    ).to_numpy()

    line_styles = {
        "tema_smoothed": LineStyle(label=f"TEMA {settings.tema_length} Smoothed", color=_PURPLE, width=2),
        "ema_trend": LineStyle(label=f"EMA {settings.trend_ema_length}", color=_BLUE, width=2),
        "ema_trigger_smoothed": LineStyle(
            label=f"EMA {settings.trigger_ema_length} Smoothed", color=_ORANGE, width=1
        ),
    }

    # ---- Session detection --------------------------------------------------
    session_times = display_index.tz_convert(settings.timezone)
    in_rth = session_mask(session_times, parse_session(settings.rth_session))
    in_on = session_mask(session_times, parse_session(settings.overnight_session))
    in_pm = session_mask(session_times, parse_session(settings.premarket_session))

    highs = frame["high"].astype(float).to_numpy()
    lows = frame["low"].astype(float).to_numpy()
    opens = frame["open"].astype(float).to_numpy()

    nan = float("nan")
    rth_high = rth_low = rth_open = nan
    prev_rth_high = prev_rth_low = prev_rth_open = nan
    rth_anchor = None
    on_high = on_low = on_open = nan
    on_anchor = None
    pm_high = pm_low = nan
    pm_anchor = None

    for i in range(len(frame)):
        timestamp = display_index[i]

        if in_rth[i] and not (i > 0 and in_rth[i - 1]):
            prev_rth_high, prev_rth_low, prev_rth_open = rth_high, rth_low, rth_open
            rth_high, rth_low, rth_open = highs[i], lows[i], opens[i]
            rth_anchor = timestamp
        elif in_rth[i]:
            rth_high = _nz_max(rth_high, highs[i])
            rth_low = _nz_min(rth_low, lows[i])

        if in_on[i] and not (i > 0 and in_on[i - 1]):
            on_high, on_low, on_open = highs[i], lows[i], opens[i]
            on_anchor = timestamp
        elif in_on[i]:
            on_high = _nz_max(on_high, highs[i])
            on_low = _nz_min(on_low, lows[i])

        if in_pm[i] and not (i > 0 and in_pm[i - 1]):
            pm_high, pm_low = highs[i], lows[i]
            pm_anchor = timestamp
        elif in_pm[i]:
            pm_high = _nz_max(pm_high, highs[i])
            pm_low = _nz_min(pm_low, lows[i])

    candidates: List[Optional[SessionLevel]] = []
    if settings.show_prev_rth:
        candidates += [
            _level("prev_rth_high", "Prev RTH High", prev_rth_high, settings.color_prev_high, rth_anchor),
            _level("prev_rth_low", "Prev RTH Low", prev_rth_low, settings.color_prev_low, rth_anchor),
            _level("prev_rth_open", "Prev RTH Open", prev_rth_open, settings.color_prev_open, rth_anchor),
        ]
    if settings.show_overnight:
        candidates += [
            _level("on_high", "ON High", on_high, settings.color_on_high, on_anchor),
            _level("on_low", "ON Low", on_low, settings.color_on_low, on_anchor),
            _level("on_open", "Overnight Open", on_open, settings.color_on_open, on_anchor),
        ]
    if settings.show_premarket:
        candidates += [
            _level("pm_high", "Premarket High", pm_high, settings.color_pm_high, pm_anchor),
            _level("pm_low", "Premarket Low", pm_low, settings.color_pm_low, pm_anchor),
        ]

    bar_delta = None
    if len(display_index) > 1:
        deltas = pd.Series(display_index).diff().dropna()
        if not deltas.empty:
            bar_delta = deltas.median()

    return IndicatorResult(
        lines=lines,
        line_styles=line_styles,
        levels=[level for level in candidates if level is not None],
        level_style=LevelStyle(
            width=settings.line_width,
            dash=DASH_BY_NAME.get(settings.line_style, "solid"),
            show_labels=settings.show_labels,
            label_offset=settings.label_offset,
        ),
        bar_delta=bar_delta,
    )
