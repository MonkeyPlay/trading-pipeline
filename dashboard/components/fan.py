# dashboard/components/fan.py
"""
The price fan as the Session Explorer draws it - on the current session only, to the right
of its latest candle, or of an earlier one in playback (docs/fan.md, docs/fan_experiment.md,
chunk 9):

  fan_rw_v2     the intermarket experiment's baseline, for every instrument with a fan: v1's
                variance with releases by name and earnings at the close, and its own
                fat-tailed shape (forecaster/fan_v2.py)
  the model     for the experiment's target only (NQ), the frozen learned fan at exactly the
                horizons whose own holdout interval lay below zero - 5 and 15 minutes - as
                brackets on those candles; v2 draws every other horizon (the manifest's
                drawing rule; forecaster/fan_live.py)

The fan starts at the newest stored close. On a delayed feed that close is minutes old: the
line under the chart says how old, so a fan from an older price is never mistaken for a live
one.

Recorded or recomputed. Where the forward record issued the model from the origin shown
(every 15 minutes, and 09:29 ET), the brackets are that issue - the recorded forecast, read
from the journal, with when it was recorded. Anywhere else they are computed from the bars
stored now: at the newest candle the current forecast, not recorded; in playback a
recomputed historical preview - reading only bars dated before the origin, but as stored
now, so it cannot show what a revised or late-arriving bar would have changed. Only a
recorded forecast says what the model showed at the time. The grey fog is always computed.

  current_session(now)   the trading day in progress: the only one with a fan
  load_context(...)      per instrument and session: v2 fitted on the sessions before it,
                         its shape, its measured accuracy over the last ACCURACY_SESSIONS
                         sessions (cached per day), and for the target the frozen model -
                         seconds (minutes the first time an instrument's errors are
                         computed), so the explorer runs it off the event loop, once per
                         instrument and day
  load_marks(...)        the frozen model's brackets from one origin, recorded or computed
                         (off the event loop)
  load_day(...)          the session's own minute grid, read again for new bars
  fan_payload(...)       the chart's ``fan`` spec from an origin at a timeframe: one
                         column per future candle (at most FAN_CANDLES, to the day's
                         end) - the distribution of its close as v2 issues it, its
                         confidence, the releases ahead, and the model's brackets
  attach(spec, payload)  the payload into a chart spec, with blank candles extending
                         the time axis into the future
  describe(payload)      the line under the playback controls

Both fans are drawn as issued and scored - no display adjustment - so the brackets and the
fog compare directly: a multiplier of x1.10 is a bracket 10 % wider than v2 at that
horizon. Two fades (lightweight_chart.js, DensityFan): the price fade - each column's
opacity follows the density of its distribution, the median fully opaque - and the
accuracy fade, the column's confidence: v2's CRPS skill against a flat random walk with
normal errors (one volatility for every minute) at that horizon, measured walk-forward over
the last sessions with the shape each was issued with (fan_live.v2_accuracy), relative to
its skill one minute ahead and never below CONFIDENCE_FLOOR - where the fan knows no more
than that random walk it stays a faint band. The line under the chart gives how often the
90 % band held, measured the same way. With zero drift the median stays at the origin's
price: the fan makes no claim about direction, only about how far.
"""

from __future__ import annotations

import os
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
from forecaster import fan_live
from forecaster import fan_v2
from forecaster.fan_benchmark import Day, FanModel, InsufficientHistory, end_slot, slot_instant
from forecaster.fan_data import history_start, load_days
from forecaster.fan_harness import FRAME_HORIZONS

ACCURACY_SESSIONS = 30      # the sessions before the day the accuracy fade is measured on
CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "data",
                         "fan_cache")                  # v2's per-session errors and the day's accuracy (not in git)
FAN_CANDLES = 120           # future candles drawn, at the chart's timeframe (to the day's end at most)
CONFIDENCE_FLOOR = 0.25     # the accuracy fade never goes below this: a faint band, not nothing
FAN_RGB = "209, 212, 220"   # the chart's text ink: a neutral fog - no series' identity, no direction
FAN_ALPHA = 0.36            # the opacity of the most likely price one minute ahead
CAPTION_MINUTES = (15, 30, 60)
DELAYED_MINUTES = 3         # an origin this much older than the clock is said to be on a delayed feed

Z: List[float] = [NormalDist().inv_cdf(q) for q in F.QUANTILES]
MEDIAN = F.QUANTILES.index(0.5)
_P5, _P95 = F.QUANTILES.index(0.05), F.QUANTILES.index(0.95)
_P25, _P75 = F.QUANTILES.index(0.25), F.QUANTILES.index(0.75)
MODEL_RGB = "77, 182, 255"  # the learned fan's brackets: a colour of their own, apart from the fog and the candles
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
    """What the fans of one instrument's session share: v2's model, shape and measured accuracy, and for the
    experiment's target the frozen model to draw - or why there is none."""
    symbol: str
    session_date: date
    model: Optional[FanModel] = None
    accuracy: Dict[str, Any] = field(default_factory=lambda: {"sessions": 0, "horizons": []})
    error: Optional[str] = None
    shape: Optional[fan_v2.Shape] = None
    learned: Optional[fan_live.ModelDraw] = None
    learned_note: str = ""
    horizons: np.ndarray = field(default_factory=lambda: np.zeros(0))
    confidence: np.ndarray = field(default_factory=lambda: np.zeros(0))

    def __post_init__(self) -> None:
        rows = [r for r in self.accuracy.get("horizons", []) if r.get("skill") is not None]
        if not rows:
            return
        self.horizons = np.array([float(r["horizon"]) for r in rows])
        skill = np.clip(np.array([float(r["skill"]) for r in rows]), 0.0, None)
        top = float(skill.max())
        self.confidence = skill / top if top > 0 else np.zeros(len(rows))

    def at(self, minutes: np.ndarray, values: np.ndarray, default: float) -> np.ndarray:
        """``values`` (per measured horizon) at ``minutes`` ahead: interpolated in log minutes, held beyond."""
        if not len(self.horizons):
            return np.full(len(minutes), default)
        return np.interp(np.log(minutes), np.log(self.horizons), values)


def load_context(conn, symbol: str, day: str, cache_dir: Optional[str] = CACHE_DIR) -> FanContext:
    """v2 for ``symbol``'s session ``day``, its shape and accuracy before it, and the frozen model where it draws
    (see the module docstring). ``cache_dir`` keeps v2's per-session errors and the day's accuracy."""
    d = date.fromisoformat(day)
    try:
        days = load_days(conn, symbol, history_start(d, F.EVENT_SESSIONS + F.SHAPE_SESSIONS), d)
    except ValueError as e:                                  # an instrument without a fan
        return FanContext(symbol, d, error=str(e))
    target = next((x for x in days if x.session_date == d), None)
    if target is None:
        return FanContext(symbol, d, error=f"no {symbol} bars of {day} are stored yet")
    before = [x for x in days if x.session_date < d]
    try:
        model = fan_v2.fit(target, before)
    except InsufficientHistory as e:
        return FanContext(symbol, d, error=str(e))
    shape = fan_live.v2_shape(before, symbol, cache_dir)
    last = max((x.session_date for x in before if x.complete), default=None)      # a later final session: new key
    acc_path = (os.path.join(cache_dir, "live", f"accuracy_{symbol}_{day}_{last}_"
                             f"{F.fan_v2_record()['definition_hash'][:12]}_f{fan_live.ACCURACY_FORMAT}.json")
                if cache_dir else None)
    accuracy = fan_live.v2_accuracy(days, d, symbol, ACCURACY_SESSIONS, cache_dir, acc_path)
    learned, note = fan_live.load_model(conn, symbol, d)
    return FanContext(symbol, d, model, accuracy, shape=shape, learned=learned, learned_note=note)


def load_marks(conn, ctx: FanContext, day: Day, origin: int) -> Optional[Dict[str, Any]]:
    """The frozen model's brackets from slot ``origin``: the forward record's issue from it when one was made
    (``source`` 'recorded', with when it was recorded - fan_live.recorded), else computed from the bars stored now
    (``source`` 'computed'). None where the model does not draw."""
    if ctx.learned is None or ctx.model is None or ctx.shape is None:
        return None
    rec = fan_live.recorded(conn, ctx.learned, origin)
    if rec is not None and rec["marks"]:
        return {"source": "recorded", **rec}
    return {"source": "computed", "marks": fan_live.model_marks(
        conn, ctx.learned, day, ctx.model, ctx.shape.at(np.array(FRAME_HORIZONS)), origin)}


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


def fan_payload(ctx: FanContext, day: Day, origin: int, timeframe: str,
                marks: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """
    The fan from the bar at slot ``origin`` (its close) drawn at ``timeframe``: a column per candle starting after
    the origin's candle, its close's distribution - v2's fan at the candle's last minute, its own shape, as issued -
    and the frozen model's brackets (``marks``, load_marks: recorded or computed, each with v2's quantiles at exactly
    its horizon) on the candles holding their horizons. None without a model, a price at the origin or a minute
    left in the day.
    """
    if ctx.model is None or ctx.shape is None or not 0 <= origin < len(day.closes):
        return None
    price = float(day.last_price[origin])
    if not np.isfinite(price):
        return None
    fan = fan_v2.fan_from(ctx.model, ctx.shape, day.returns, origin, price)
    H = len(fan.minutes)
    tf = _TF_MINUTES.get(timeframe, 1)
    starts = np.arange((origin // tf + 1) * tf, origin + H + 1, tf)
    d = starts + tf - 1 - origin                           # minutes ahead at each candle's close
    keep = d <= H
    starts, d = starts[keep][:FAN_CANDLES], d[keep][:FAN_CANDLES]
    if not len(d):
        return None
    confidence = np.maximum(CONFIDENCE_FLOOR, ctx.at(d, ctx.confidence, 0.0))
    prices = fan.prices[d - 1]                             # v2's quantiles as issued: no display adjustment
    times = chart_times(day.session_date, starts)
    span = (origin, int(starts[-1]) + tf - 1)
    releases = [{"time": int(chart_times(day.session_date, [r.slot // tf * tf])[0]),
                 "label": f"{r.at.astimezone(cal.NY_TZ):%H:%M} {r.name}"}
                for r in day.releases if span[0] < r.slot <= span[1]]
    model = None
    if marks is not None:
        rec = marks["source"] == "recorded"
        model = {"version": ctx.learned.version if ctx.learned else "", "label": "learned fan",
                 "name": ctx.learned.definition["candidate"] if ctx.learned else "", "source": marks["source"],
                 "error": marks.get("error"), "record": marks.get("record"),
                 "recorded_at_et": f"{marks['recorded_at'].astimezone(cal.NY_TZ):%H:%M:%S}" if rec else None,
                 "after_mark_minutes": round((marks["recorded_at"] - marks["mark_at"]).total_seconds() / 60, 1)
                 if rec else None,
                 "origin_price": marks.get("origin_price"),
                 "marks": [{"time": int(chart_times(day.session_date, [(origin + m["minutes"]) // tf * tf])[0]),
                            "minutes": m["minutes"], "q": m["q"], "multiplier": round(m["multiplier"], 3),
                            "base": m.get("base"), "class": m.get("class")}
                           for m in marks["marks"]]}
    return {
        "version": F.FAN_V2_VERSION, "symbol": ctx.symbol, "timeframe": timeframe, "model": model,
        "origin": {"time": int(chart_times(day.session_date, [origin // tf * tf])[0]), "price": price,
                   "slot": origin, "at_et": f"{slot_instant(day.session_date, origin + 1).astimezone(cal.NY_TZ):%H:%M}"},
        "z": [round(z, 4) for z in Z], "median": MEDIAN, "band": [_P5, _P95], "quartiles": [_P25, _P75],
        "rgb": FAN_RGB, "alpha": FAN_ALPHA, "model_rgb": MODEL_RGB,
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


_CLASS = {"live": "live", "delayed_origin": "delayed origin", "late": "late", "expired": "expired",
          "on_time": "v1 'on time'", "delayed": "v1 delayed", "legacy": "made under no rules"}


def _ranges(marks: List[Dict[str, Any]], key: str, multiplier: bool) -> str:
    return "; ".join(f"{k['minutes']} min {k[key][_P5]:,.2f}–{k[key][_P95]:,.2f}"
                     + (f" (x{k['multiplier']:.2f})" if multiplier else "") for k in marks)


def describe(ctx: Optional[FanContext], payload: Optional[Dict[str, Any]], now: Optional[datetime] = None,
             playback: bool = False) -> str:
    """One line: where the fan starts - in ``playback`` that it is a recomputed historical preview; at the newest
    candle, given ``now``, how long ago on a delayed feed - its 90 % range ahead; the learned fan at the horizons it
    draws: the recorded forecast where the forward record issued one from this origin (when, and each horizon's
    class under its rules, with v2's ranges as issued), else computed from the bars stored now - or why it does not
    draw; how the fades were measured and the releases ahead."""
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
    start = f"Fan {payload['version']} from the {o['at_et']} ET close at {o['price']:,.2f}"
    if playback:
        start = ("Recomputed historical preview: f" + start[1:] + ", from the bars stored now - not a record of what "
                 "was shown then")
    elif now is not None:
        closed = slot_instant(ctx.session_date, o["slot"] + 1)
        late = (now - closed).total_seconds() / 60
        if late >= DELAYED_MINUTES:
            start += f" - {late:.0f} min ago: the feed is delayed, so the fan starts in the past"
    parts = [start]
    if ahead:
        parts.append("90 %: " + "; ".join(f"{m} min {lo:,.2f}–{hi:,.2f}" for m, lo, hi in ahead))
    model = payload.get("model")
    if model and model["marks"] and model["source"] == "recorded":
        classes = ", ".join(f"{k['minutes']} min {_CLASS.get(k['class'], k['class'])}" for k in model["marks"])
        parts.append(f"recorded forecast - the forward record's issue from this origin, recorded "
                     f"{model['recorded_at_et']} ET, {model['after_mark_minutes']:.1f} min after its mark ({classes}): "
                     f"learned fan ({model['name']}, frozen) 90 %: {_ranges(model['marks'], 'q', True)}; v2 as issued "
                     f"90 %: {_ranges(model['marks'], 'base', False)}")
    elif model and model["marks"]:
        what = "recomputed" if playback else "computed now, not recorded"
        parts.append(f"learned fan ({model['name']}, frozen; the horizons it passed on the holdout; {what}) 90 %: "
                     + _ranges(model["marks"], "q", True))
    elif model is not None:
        parts.append(f"learned fan not drawn: {model['error']}" if model.get("error")
                     else "no learned fan from here: its horizons pass the day's end")
    elif ctx.learned is not None:
        parts.append("learned fan: computing")
    elif ctx.learned_note and not ctx.learned_note.startswith("the learned fan forecasts"):
        parts.append(f"no learned fan: {ctx.learned_note}")
    acc = ctx.accuracy
    rows = [r for r in acc.get("horizons", []) if r.get("skill") is not None]
    if rows:
        held = [r["cover90"] for r in rows]
        parts.append(f"fades with v2's CRPS skill against a flat random walk with normal errors, "
                     f"{100 * rows[0]['skill']:.1f} % at {rows[0]['horizon']} min to {100 * rows[-1]['skill']:.1f} % at "
                     f"{rows[-1]['horizon']} min; its 90 % band held {100 * min(held):.0f}–{100 * max(held):.0f} % - "
                     f"walk-forward over {acc['sessions']} sessions to {acc['last']}, each drawn with the shape it was "
                     "issued with")
    else:
        parts.append("accuracy not measured yet: drawn faint")
    if payload["releases"]:
        parts.append("ahead: " + ", ".join(r["label"] for r in payload["releases"]))
    elif not payload["releases_known"]:
        parts.append("releases unknown (outside the economic calendar)")
    return " · ".join(parts)
