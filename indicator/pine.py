# indicator/pine.py
"""
Pine Script compatible primitives.

Ports the TradingView built-ins the indicators rely on, preserving Pine's warm-up
and na-propagation semantics rather than the pandas defaults.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import time as dt_time
from typing import FrozenSet

import numpy as np
import pandas as pd

ALL_DAYS: FrozenSet[int] = frozenset({1, 2, 3, 4, 5, 6, 7})


def pine_sma(series: pd.Series, length: int) -> pd.Series:
    """``ta.sma``: rolling mean that stays na until ``length`` samples exist."""
    if length < 1:
        raise ValueError("length must be >= 1")
    return series.rolling(length, min_periods=length).mean()


def pine_ema(series: pd.Series, length: int) -> pd.Series:
    """
    ``ta.ema``: EMA seeded with the SMA of the first ``length`` valid samples.

    ``ewm(adjust=False)`` seeds from the first sample instead, which leaves a
    visible offset against TradingView across the warm-up range. Leading na values
    are skipped rather than consumed, so stacking this on its own output (as TEMA
    does) starts each stage where the previous one became valid.
    """
    if length < 1:
        raise ValueError("length must be >= 1")

    alpha = 2.0 / (length + 1.0)
    values = pd.to_numeric(series, errors="coerce").to_numpy(dtype=float)
    out = np.full(values.shape, np.nan)

    accumulator = np.nan
    seed_total = 0.0
    seed_count = 0

    for i, value in enumerate(values):
        if np.isnan(value):
            continue
        if np.isnan(accumulator):
            seed_total += value
            seed_count += 1
            if seed_count == length:
                accumulator = seed_total / length
                out[i] = accumulator
        else:
            accumulator = alpha * value + (1.0 - alpha) * accumulator
            out[i] = accumulator

    return pd.Series(out, index=series.index, name=series.name)


@dataclass(frozen=True)
class Session:
    """A parsed Pine session spec."""

    start: dt_time
    end: dt_time
    days: FrozenSet[int]

    @property
    def wraps_midnight(self) -> bool:
        return self.start > self.end


def _parse_hhmm(text: str) -> dt_time:
    text = text.strip()
    if len(text) != 4 or not text.isdigit():
        raise ValueError(f"Session time must be HHMM, got {text!r}")
    hour, minute = int(text[:2]), int(text[2:])
    if hour > 23 or minute > 59:
        raise ValueError(f"Session time out of range: {text!r}")
    return dt_time(hour, minute)


def parse_session(spec: str) -> Session:
    """
    Parses ``"0930-1615"`` or ``"0930-1615:23456"``.

    Day digits follow Pine's numbering (Sunday = 1 ... Saturday = 7) and default
    to every day when omitted.
    """
    body, _, day_part = spec.partition(":")
    start_text, separator, end_text = body.strip().partition("-")
    if not separator:
        raise ValueError(f"Malformed session spec: {spec!r}")

    days = frozenset(int(c) for c in day_part if c.isdigit()) or ALL_DAYS
    return Session(_parse_hhmm(start_text), _parse_hhmm(end_text), days)


def _pine_weekday(timestamp: pd.Timestamp) -> int:
    """Pine numbering: Sunday = 1 ... Saturday = 7."""
    return (timestamp.weekday() + 1) % 7 + 1


def session_mask(timestamps, session: Session) -> np.ndarray:
    """
    ``not na(time(timeframe.period, session, tz))`` for each bar.

    A bar belongs to the session when its opening time falls in ``[start, end)``.
    For a spec that wraps midnight the day filter applies to the calendar day the
    session opened on, not the day the bar prints.
    """
    index = pd.DatetimeIndex(timestamps)
    mask = np.zeros(len(index), dtype=bool)

    for i, timestamp in enumerate(index):
        if timestamp is pd.NaT:
            continue

        moment = timestamp.time()
        if session.wraps_midnight:
            if moment >= session.start:
                anchor = timestamp
            elif moment < session.end:
                anchor = timestamp - pd.Timedelta(days=1)
            else:
                continue
        else:
            if not (session.start <= moment < session.end):
                continue
            anchor = timestamp

        mask[i] = _pine_weekday(anchor) in session.days

    return mask
