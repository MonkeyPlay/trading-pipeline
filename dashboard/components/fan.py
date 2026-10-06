# dashboard/components/fan.py
"""
The benchmark price fan (fan_rw_v1: forecaster/fan_benchmark.py, docs/fan.md) as the
Session Explorer draws it - on the current session only, to the right of its latest
candle, or of an earlier one in playback.

  current_session(now)   the trading day in progress: the only one with a fan
  load_context(...)      per instrument and session: the model fitted on the sessions
                         before it and the fan's measured accuracy over the last
                         ACCURACY_SESSIONS sessions - a few seconds, so the explorer
                         runs it off the event loop, once per instrument and day
  load_day(...)          the session's own minute grid, read again for new bars
  fan_payload(...)       the chart's ``fan`` spec from an origin at a timeframe: one
                         column per future candle (at most FAN_CANDLES, to the day's
                         end) - the distribution of its close as the fan's quantiles,
                         its confidence, the releases ahead
  attach(spec, payload)  the payload into a chart spec, with blank candles extending
                         the time axis into the future
  describe(payload)      the line under the playback controls

Two fades (lightweight_chart.js, DensityFan): the price fade - each column's opacity
follows the density of its distribution, the median fully opaque - and the accuracy
fade, the column's confidence: the fan's CRPS skill against a flat random walk (one
volatility for every minute) at that horizon, measured walk-forward over the last
sessions, relative to its skill one minute ahead and never below CONFIDENCE_FLOOR -
where the fan knows no more than that random walk it stays a faint band. A horizon
whose measured 90 % band held less than 90 % is drawn widened to hold it (never
narrowed); the issued fan is unchanged. With zero drift the median stays at the
origin's price: the fan makes no claim about direction, only about how far.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from statistics import NormalDist
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from contracts import fan as F
from dashboard.components.spec import to_epoch
from features import calendar as cal
from features.session_windows import get_trading_day_date
from forecaster.fan_benchmark import Day, FanModel, InsufficientHistory, end_slot, fan_from, fit, slot_instant
from forecaster.fan_data import history_start, load_days
from forecaster.fan_scoring import recent_accuracy

ACCURACY_SESSIONS = 30      # the sessions before the day the accuracy fade is measured on
FAN_CANDLES = 120           # future candles drawn, at the chart's timeframe (to the day's end at most)
CONFIDENCE_FLOOR = 0.25     # the accuracy fade never goes below this: a faint band, not nothing
FAN_RGB = "209, 212, 220"   # the chart's text ink: a neutral fog - no series' identity, no direction
FAN_ALPHA = 0.36            # the opacity of the most likely price one minute ahead
CAPTION_MINUTES = (15, 30, 60)

Z: List[float] = [NormalDist().inv_cdf(q) for q in F.QUANTILES]
MEDIAN = F.QUANTILES.index(0.5)
_Z90 = NormalDist().inv_cdf(0.95)
_P5, _P95 = F.QUANTILES.index(0.05), F.QUANTILES.index(0.95)
_NY = "America/New_York"
_TF_MINUTES = {"1m": 1, "2m": 2, "5m": 5, "15m": 15, "30m": 30}


def current_session(now: Optional[datetime] = None) -> Optional[str]:
    """
    The trading day in progress at ``now``, from its 18:00 ET Globex open to the futures' day end (17:00 ET; the
    early close's on a short day) - None between sessions and on a closed day.
    """
    now = now or datetime.now(timezone.utc)
    day = date.fromisoformat(get_trading_day_date(now.astimezone(cal.NY_TZ)))
    try:
        s = cal.session(day)
    except cal.CalendarCoverageError:
        return None
    if not s.is_open:
        return None
    return day.isoformat() if s.overnight_start_at <= now < slot_instant(day, end_slot("FUT", s.schedule)) else None


@dataclass
class FanContext:
    """What the fans of one instrument's session share: the model and the measured accuracy - or why there is none."""
    symbol: str
    session_date: date
    model: Optional[FanModel] = None
    accuracy: Dict[str, Any] = field(default_factory=lambda: {"sessions": 0, "horizons": []})
    error: Optional[str] = None
    horizons: np.ndarray = field(default_factory=lambda: np.zeros(0))
    confidence: np.ndarray = field(default_factory=lambda: np.zeros(0))
    widen: np.ndarray = field(default_factory=lambda: np.zeros(0))

    def __post_init__(self) -> None:
        rows = [r for r in self.accuracy.get("horizons", []) if r.get("skill") is not None]
        if not rows:
            return
        self.horizons = np.array([float(r["horizon"]) for r in rows])
        skill = np.clip(np.array([float(r["skill"]) for r in rows]), 0.0, None)
        top = float(skill.max())
        self.confidence = skill / top if top > 0 else np.zeros(len(rows))
        self.widen = np.array([_widening(float(r["cover90"])) for r in rows])

    def at(self, minutes: np.ndarray, values: np.ndarray, default: float) -> np.ndarray:
        """``values`` (per measured horizon) at ``minutes`` ahead: interpolated in log minutes, held beyond."""
        if not len(self.horizons):
            return np.full(len(minutes), default)
        return np.interp(np.log(minutes), np.log(self.horizons), values)


def _widening(cover90: float) -> float:
    """The factor that would have made the measured 90 % band hold 90 %; never below 1."""
    if not 0 < cover90 < 1:
        return 1.0
    return max(1.0, _Z90 / NormalDist().inv_cdf((1 + cover90) / 2))


def load_context(conn, symbol: str, day: str) -> FanContext:
    """The model for ``symbol``'s session ``day`` and the fan's accuracy before it (see the module docstring)."""
    d = date.fromisoformat(day)
    try:
        days = load_days(conn, symbol, history_start(d, F.EVENT_SESSIONS), d)
    except ValueError as e:                                  # an instrument without a fan
        return FanContext(symbol, d, error=str(e))
    target = next((x for x in days if x.session_date == d), None)
    if target is None:
        return FanContext(symbol, d, error=f"no {symbol} bars of {day} are stored yet")
    try:
        model = fit(target, [x for x in days if x.session_date < d])
    except InsufficientHistory as e:
        return FanContext(symbol, d, error=str(e))
    return FanContext(symbol, d, model, recent_accuracy(days, d, ACCURACY_SESSIONS))


def load_day(conn, symbol: str, day: str) -> Optional[Day]:
    """``symbol``'s session ``day`` on the minute grid, as stored now."""
    days = load_days(conn, symbol, day, day)
    return days[-1] if days else None


def latest_slot(day: Day) -> Optional[int]:
    """The slot of the session's last stored bar."""
    seen = np.flatnonzero(np.isfinite(day.closes))
    return int(seen[-1]) if len(seen) else None


def chart_times(session_date: date, slots) -> np.ndarray:
    """The chart's epoch seconds (New York wall clock, as dashboard/components/spec.to_epoch) of bars at ``slots``."""
    instants = pd.DatetimeIndex([slot_instant(session_date, int(s)) for s in slots]).tz_convert(_NY)
    return to_epoch(instants)


def fan_payload(ctx: FanContext, day: Day, origin: int, timeframe: str) -> Optional[Dict[str, Any]]:
    """
    The fan from the bar at slot ``origin`` (its close) drawn at ``timeframe``: a column per candle starting after
    the origin's candle, its close's distribution - the fan at the candle's last minute. None without a model, a
    price at the origin or a minute left in the day.
    """
    if ctx.model is None or not 0 <= origin < len(day.closes):
        return None
    price = float(day.last_price[origin])
    if not np.isfinite(price):
        return None
    fan = fan_from(ctx.model, day.returns, origin, price)
    H = len(fan.minutes)
    tf = _TF_MINUTES.get(timeframe, 1)
    starts = np.arange((origin // tf + 1) * tf, origin + H + 1, tf)
    d = starts + tf - 1 - origin                           # minutes ahead at each candle's close
    keep = d <= H
    starts, d = starts[keep][:FAN_CANDLES], d[keep][:FAN_CANDLES]
    if not len(d):
        return None
    sigma = fan.sigma[d - 1] * ctx.at(d, ctx.widen, 1.0)
    confidence = np.maximum(CONFIDENCE_FLOOR, ctx.at(d, ctx.confidence, 0.0))
    prices = price * np.exp(np.outer(sigma, Z))
    times = chart_times(day.session_date, starts)
    span = (origin, int(starts[-1]) + tf - 1)
    releases = [{"time": int(chart_times(day.session_date, [r.slot // tf * tf])[0]),
                 "label": f"{r.at.astimezone(cal.NY_TZ):%H:%M} {r.name}"}
                for r in day.releases if span[0] < r.slot <= span[1]]
    return {
        "version": F.FAN_VERSION, "symbol": ctx.symbol, "timeframe": timeframe,
        "origin": {"time": int(chart_times(day.session_date, [origin // tf * tf])[0]), "price": price,
                   "slot": origin, "at_et": f"{slot_instant(day.session_date, origin + 1).astimezone(cal.NY_TZ):%H:%M}"},
        "z": [round(z, 4) for z in Z], "median": MEDIAN, "band": [_P5, _P95], "rgb": FAN_RGB, "alpha": FAN_ALPHA,
        "columns": [{"time": int(t), "minutes": int(m), "q": [round(float(p), 2) for p in row],
                     "conf": round(float(c), 3)}
                    for t, m, row, c in zip(times, d, prices, confidence)],
        "releases": releases,
        "releases_known": day.releases_known,
        "levels": {"long": round(fan.level_long, 2), "short": round(fan.level_short, 2)},
    }


def attach(spec: Dict[str, Any], payload: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """``spec`` with the fan (None removes it) and a blank candle at every column after its last candle."""
    spec["fan"] = payload
    if payload and spec.get("candles"):
        last = spec["candles"][-1]["time"]
        spec["candles"] = spec["candles"] + [{"time": c["time"]} for c in payload["columns"] if c["time"] > last]
    return spec


def describe(ctx: Optional[FanContext], payload: Optional[Dict[str, Any]]) -> str:
    """One line: where the fan starts, its 90 % range ahead, how its fades were measured and the releases ahead."""
    if ctx is None:
        return "Loading the fan…"
    if ctx.error:
        return f"No fan: {ctx.error}."
    if payload is None:
        return "No fan from here: no minute of the trading day is left to forecast."
    o, cols = payload["origin"], payload["columns"]
    by_minutes = {c["minutes"]: c for c in cols}
    ahead = []
    for m in CAPTION_MINUTES:
        c = by_minutes.get(m) or next((x for x in cols if x["minutes"] >= m), None)
        if c is not None and c["minutes"] not in [a[0] for a in ahead]:
            ahead.append((c["minutes"], c["q"][_P5], c["q"][_P95]))
    parts = [f"Fan {payload['version']} from the {o['at_et']} ET close at {o['price']:,.2f}"]
    if ahead:
        parts.append("90 %: " + "; ".join(f"{m} min {lo:,.2f}–{hi:,.2f}" for m, lo, hi in ahead))
    acc = ctx.accuracy
    rows = [r for r in acc.get("horizons", []) if r.get("skill") is not None]
    if rows:
        held = [r["cover90"] for r in rows]
        parts.append(f"fades with its skill against a flat random walk, {100 * rows[0]['skill']:.1f} % at "
                     f"{rows[0]['horizon']} min to {100 * rows[-1]['skill']:.1f} % at {rows[-1]['horizon']} min over "
                     f"{acc['sessions']} sessions to {acc['last']}; its 90 % band held "
                     f"{100 * min(held):.0f}–{100 * max(held):.0f} %")
    else:
        parts.append("accuracy not measured yet: drawn faint")
    if payload["releases"]:
        parts.append("ahead: " + ", ".join(r["label"] for r in payload["releases"]))
    elif not payload["releases_known"]:
        parts.append("releases unknown (outside the economic calendar)")
    return " · ".join(parts)
