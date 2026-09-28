# forecaster/first_hour_model.py
"""
First-hour model: from the pre-open only, a trained model generates the sixty
1-minute candles of 09:30-10:30.

What it learns (every stored session, P = the 09:28 close, the last price known
at 09:29)
    moves   ln(close / P) at the 09:44, 09:59 and 10:29 closes - where the
            price is at 09:45, 10:00 and 10:30 - in usual first-hour ranges
    ranges  ln(high / low) of the first 15, 30 and 60 minutes, P included, as
            the log of its ratio to the usual
    (the usual: the median over the previous USUAL_SESSIONS sessions)

from preopen.PRE_INPUTS - volatility inputs (recent ranges, overnight and
last-hour range and volume, VIX, weekday) and direction inputs (overnight,
last-hour and last-15-minute returns, the previous session's return, where P
and the previous close sit in their ranges).

The model
    One ridge regression per target on the standardised inputs, fitted on every
    session before the forecast day - so each actual first hour joins the
    training for the next day - with recent sessions weighing more (a session's
    weight halves every HALF_LIFE sessions back) and the penalty chosen by
    leave-one-out on those sessions. Where the inputs carry no signal the
    penalty grows and the prediction shrinks toward the average: the ranges
    stay well predicted, and the moves - the direction - come out as strong as
    the model has earned from the outcomes so far (walk-forward so far: about a
    tenth of a typical move, right about half the time).

The candles
    The expected path runs straight (in log price) from P through the predicted
    moves at 09:45, 10:00 and 10:30. Each minute's candle takes that path for
    its body and, for its wicks, the minute's expected range: the usual range of
    that minute of the session, scaled by the predicted range of its window.
    The likely hour high and low split the predicted hour range around the
    predicted move. The expected path is smoother than any real hour, whose
    extremes the high / low lines show.

``scorecard`` replays it walk-forward on every stored session - what the
dashboard shows for a past day is exactly what it would have forecast before
that day's open.
"""

import bisect
import math
import warnings
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

import numpy as np
import pandas as pd

from forecaster import preopen as po

MINUTES = 60
ANCHORS = (15, 30, 60)
TARGETS = tuple(f"move_{k}" for k in ANCHORS) + tuple(f"range_{k}" for k in ANCHORS)
MIN_TRAIN = 60                    # sessions with inputs and outcomes before the first model
HALF_LIFE = 120.0                 # sessions
ALPHAS = np.logspace(-1, 5, 25)
TICK_LOG = 1e-5                   # a minute's log range is floored at about a tick


# --------------------------------------------------------------------------
# Outcomes
# --------------------------------------------------------------------------

@dataclass
class Outcome:
    """A session's first hour measured from P, in log units."""
    moves: np.ndarray             # at the 09:44, 09:59 and 10:29 closes
    ranges: np.ndarray            # of the first 15, 30 and 60 minutes, P included
    minute_ranges: np.ndarray     # of each of the 60 minutes
    path: np.ndarray              # each minute's close


def outcome(s: po.Session) -> Optional[Outcome]:
    """The first hour of a session holding it, from its P; None otherwise."""
    if s.held < MINUTES or not s.pre_price:
        return None
    P = s.pre_price
    path = np.log(s.close[:MINUTES] / P)
    return Outcome(moves=np.array([path[k - 1] for k in ANCHORS]),
                   ranges=np.array([math.log(max(P, s.high[:k].max()) / min(P, s.low[:k].min())) for k in ANCHORS]),
                   minute_ranges=np.maximum(np.log(s.high[:MINUTES] / s.low[:MINUTES]), TICK_LOG), path=path)


# --------------------------------------------------------------------------
# Model
# --------------------------------------------------------------------------

@dataclass
class Model:
    """Ridge per target on standardised inputs (a missing input counts as its mean)."""
    mean: np.ndarray
    scale: np.ndarray
    coef: np.ndarray              # (targets, inputs)
    intercept: np.ndarray
    alpha: np.ndarray
    sessions: int
    trained_through: str

    def predict(self, x: np.ndarray) -> np.ndarray:
        z = (np.asarray(x, dtype=float) - self.mean) / self.scale
        z[~np.isfinite(z)] = 0.0
        return self.coef @ z + self.intercept


def fit(X: np.ndarray, Y: np.ndarray, weights: np.ndarray, trained_through: str) -> Model:
    from sklearn.linear_model import RidgeCV
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)       # all-missing columns
        mean, scale = np.nanmean(X, axis=0), np.nanstd(X, axis=0)
    mean[~np.isfinite(mean)] = 0.0
    scale[~(scale > 0)] = 1.0
    Z = (X - mean) / scale
    Z[~np.isfinite(Z)] = 0.0
    coef, intercept, alpha = [], [], []
    for t in range(Y.shape[1]):
        ok = np.isfinite(Y[:, t])
        r = RidgeCV(alphas=ALPHAS).fit(Z[ok], Y[ok, t], sample_weight=weights[ok])
        coef.append(r.coef_)
        intercept.append(float(r.intercept_))
        alpha.append(float(r.alpha_))
    return Model(mean=mean, scale=scale, coef=np.array(coef), intercept=np.array(intercept), alpha=np.array(alpha),
                 sessions=len(Y), trained_through=trained_through)


# --------------------------------------------------------------------------
# History: every stored session, walk-forward
# --------------------------------------------------------------------------

@dataclass
class History:
    """
    The complete sessions of one instrument, oldest first, with their inputs,
    first hours, usual figures and walk-forward forecasts ([session, target]).
    """
    symbol: str
    sessions: List[po.Session]
    days: List[str]
    daily: Dict[str, np.ndarray]
    X: np.ndarray                         # (N, inputs)
    Y: np.ndarray                         # (N, targets) - normalised; NaN without a usual yet
    outcomes: List[Optional[Outcome]]
    usual_hour: np.ndarray                # (N,) the unit of the moves: usual first-hour range from P
    usual_ranges: np.ndarray              # (N, 3)
    usual_minutes: np.ndarray             # (N, 60)
    forecasts: np.ndarray                 # (N, targets) walk-forward; NaN before ``start``
    start: int
    events: po.Events = field(default_factory=po.Events)
    models: Dict[int, Model] = field(default_factory=dict)

    @property
    def n(self) -> int:
        return len(self.sessions)

    def position(self, day: str) -> int:
        """How many sessions of the history lie before ``day``."""
        return bisect.bisect_left(self.days, str(day))

    def model(self, position: int) -> Optional[Model]:
        """The model for a day at ``position``: fitted on every session before it (cached)."""
        if position < self.start:
            return None
        if position not in self.models:
            train = [j for j in range(position) if np.all(np.isfinite(self.Y[j]))]
            weights = 0.5 ** ((position - 1 - np.array(train)) / HALF_LIFE)
            self.models[position] = fit(self.X[train], self.Y[train], weights, self.days[train[-1]])
        return self.models[position]

    def usual_before(self, position: int):
        """(usual hour range, usual 15/30/60 ranges, usual minute ranges) from the sessions before ``position``."""
        w = [o for o in self.outcomes[max(0, position - po.USUAL_SESSIONS):position] if o is not None]
        if len(w) < po.USUAL_SESSIONS // 2:
            return math.nan, np.full(len(ANCHORS), np.nan), np.full(MINUTES, np.nan)
        ranges = np.array([o.ranges for o in w])
        return (float(np.median(ranges[:, -1])), np.median(ranges, axis=0),
                np.median(np.array([o.minute_ranges for o in w]), axis=0))


def build_history(symbol: str, sessions: Sequence[po.Session], events: Optional[po.Events] = None) -> History:
    """
    The walk-forward history of ``symbol`` from its complete sessions
    (``preopen.complete_sessions``): every session from ``start`` on gets the
    forecast of the model fitted on all the sessions before it.
    """
    sessions = list(sessions)
    n = len(sessions)
    d = po.daily(sessions)
    X = np.vstack([po.pre_inputs({k: v[:i] for k, v in d.items()}, s) for i, s in enumerate(sessions)]) \
        if n else np.zeros((0, len(po.PRE_INPUTS)))
    outcomes = [outcome(s) for s in sessions]
    history = History(symbol=symbol, sessions=sessions, days=[s.day for s in sessions], daily=d, X=X,
                      Y=np.full((n, len(TARGETS)), np.nan), outcomes=outcomes, usual_hour=np.full(n, np.nan),
                      usual_ranges=np.full((n, len(ANCHORS)), np.nan), usual_minutes=np.full((n, MINUTES), np.nan),
                      forecasts=np.full((n, len(TARGETS)), np.nan), start=n, events=events or po.Events())
    for i in range(n):
        hour, ranges, minutes = history.usual_before(i)
        history.usual_hour[i], history.usual_ranges[i], history.usual_minutes[i] = hour, ranges, minutes
        o = outcomes[i]
        if o is not None and np.isfinite(hour) and np.isfinite(X[i][0]):     # the usual before it exists
            history.Y[i] = np.concatenate([o.moves / hour, np.log(o.ranges / ranges)])
    valid = np.flatnonzero(np.all(np.isfinite(history.Y), axis=1))
    history.start = int(valid[MIN_TRAIN]) if len(valid) > MIN_TRAIN else n
    for i in range(history.start, n):
        history.forecasts[i] = history.model(i).predict(X[i])
    return history


def load_history(conn, symbol: str) -> History:
    """``build_history`` from the stored bars of ``symbol``'s active contracts, spot VIX and the calendar."""
    return build_history(symbol, po.load_sessions(conn, symbol), po.load_events(conn))


# --------------------------------------------------------------------------
# The generated hour
# --------------------------------------------------------------------------

def generate(P: float, moves: np.ndarray, ranges: np.ndarray, usual_ranges: np.ndarray,
             usual_minutes: np.ndarray) -> np.ndarray:
    """
    (60, 4) open/high/low/close of the generated minutes: the expected path
    through P and ``moves`` (log, at 15/30/60), each minute's wicks its usual
    range scaled by the predicted range of its window over the usual.
    """
    path = np.interp(np.arange(MINUTES + 1), (0,) + ANCHORS, np.concatenate([[0.0], moves]))
    window = np.searchsorted(np.array(ANCHORS), np.arange(MINUTES), side="right")
    expected = usual_minutes * (ranges / usual_ranges)[window]
    o, c = path[:-1], path[1:]
    wick = np.maximum(expected - np.abs(c - o), 0.0) / 2
    return P * np.exp(np.column_stack([o, np.maximum(o, c) + wick, np.minimum(o, c) - wick, c]))


def likely_extremes(P: float, move: float, hour_range: float) -> tuple:
    """(likely hour high, low): the predicted hour range split around the predicted move."""
    up = min(max(hour_range / 2 + move / 2, 0.0), hour_range)
    return P * math.exp(up), P * math.exp(-(hour_range - up))


def forecast(history: History, today: po.Session) -> Dict[str, Any]:
    """
    The generated first hour of ``today`` (a Session: complete, live, or only
    its pre-open) from the sessions of ``history`` before it. Prices and
    ranges in the instrument's points; with the day's first hour stored, its
    actual moves and ranges beside them.
    """
    position = history.position(today.day)
    out: Dict[str, Any] = {"day": today.day, "available": False}
    model = history.model(position)
    if model is None:
        out["reason"] = (f"needs {MIN_TRAIN} earlier sessions with a first hour and the usual before them; "
                         f"{history.symbol} has {position} earlier sessions")
        return out
    P = today.pre_price
    if not P:
        out["reason"] = "no pre-open price (the 09:28 bar) stored for the day"
        return out
    x = po.pre_inputs({k: v[:position] for k, v in history.daily.items()}, today)
    hour, usual_ranges, usual_minutes = history.usual_before(position)
    y = model.predict(x)
    moves, ranges = y[:3] * hour, usual_ranges * np.exp(y[3:])
    ohlc = generate(P, moves, ranges, usual_ranges, usual_minutes)
    high, low = likely_extremes(P, moves[-1], ranges[-1])
    out.update(
        available=True, price=P, trained_sessions=model.sessions, trained_through=model.trained_through,
        direction_shrinkage={k: float(a) for k, a in zip(ANCHORS, model.alpha[:3])},
        moves={k: P * (math.exp(m) - 1) for k, m in zip(ANCHORS, moves)},
        anchors={k: P * math.exp(m) for k, m in zip(ANCHORS, moves)},
        ranges={k: P * r for k, r in zip(ANCHORS, ranges)}, usual={k: P * r for k, r in zip(ANCHORS, usual_ranges)},
        likely_high=high, likely_low=low,
        candles=pd.DataFrame(ohlc, columns=["open", "high", "low", "close"]).assign(
            timestamp_ny=[today.time_at(t).tz_convert("America/New_York") for t in range(MINUTES)]),
        releases=None if (r := history.events.of(today.day)) is None else [
            {"time": hhmm, "name": name, "tier": tier, "minute": m} for m, tier, name, hhmm in r],
    )
    actual = outcome(today)
    if actual is not None:
        out["actual"] = {"moves": {k: P * (math.exp(m) - 1) for k, m in zip(ANCHORS, actual.moves)},
                         "ranges": {k: P * r for k, r in zip(ANCHORS, actual.ranges)},
                         "direction_right": {k: bool(np.sign(a) == np.sign(p))
                                             for k, a, p in zip(ANCHORS, actual.moves, moves)}}
    return out


# --------------------------------------------------------------------------
# Scorecard
# --------------------------------------------------------------------------

def _gain(model: Sequence[float], base: Sequence[float]) -> Dict[str, float]:
    m, b = np.asarray(model, float), np.asarray(base, float)
    g = b - m
    n = len(g)
    return {"n": n, "model": float(m.mean()) if n else math.nan, "base": float(b.mean()) if n else math.nan,
            "gain": float(g.mean()) if n else math.nan,
            "gain_se": float(g.std(ddof=1) / math.sqrt(n)) if n > 1 else math.nan}


def scorecard(history: History) -> Dict[str, Any]:
    """
    The walk-forward forecasts of every session from ``start`` scored on its
    actual first hour (errors: lower is better; gain = simple guess - model):

      direction   per anchor, how often the predicted move had the right sign
                  (and how often the hour went up), and the squared error of the
                  move against predicting no move at all
      ranges      |log error| of the 15 / 30 / 60 minute range against the usual
      candles     |log error| of each minute's size against the usual minute
      path        mean |error| of the expected path's closes, in usual first-hour
                  ranges, against a flat line at P
    """
    idx = [i for i in range(history.start, history.n) if np.all(np.isfinite(history.forecasts[i]))
           and np.all(np.isfinite(history.Y[i]))]
    F, Y = history.forecasts[idx], history.Y[idx]
    out: Dict[str, Any] = {"symbol": history.symbol, "sessions": len(idx),
                           "first_day": history.days[idx[0]] if idx else None,
                           "last_day": history.days[idx[-1]] if idx else None, "direction": {}, "ranges": {}}
    if not idx:
        return out
    for j, k in enumerate(ANCHORS):
        pred, act = F[:, j], Y[:, j]
        out["direction"][k] = {"hit_rate": float(np.mean(np.sign(pred) == np.sign(act))),
                               "up_share": float(np.mean(act > 0)),
                               "move": _gain((act - pred) ** 2, act ** 2)}
        out["ranges"][k] = _gain(np.abs(Y[:, 3 + j] - F[:, 3 + j]), np.abs(Y[:, 3 + j]))
    size_m, size_b, path_m, path_b = [], [], [], []
    for row, i in enumerate(idx):
        o, hour = history.outcomes[i], history.usual_hour[i]
        moves, ranges = F[row, :3] * hour, history.usual_ranges[i] * np.exp(F[row, 3:])
        window = np.searchsorted(np.array(ANCHORS), np.arange(MINUTES), side="right")
        expected = history.usual_minutes[i] * (ranges / history.usual_ranges[i])[window]
        size_m.append(np.mean(np.abs(np.log(expected / o.minute_ranges))))
        size_b.append(np.mean(np.abs(np.log(history.usual_minutes[i] / o.minute_ranges))))
        path = np.interp(np.arange(1, MINUTES + 1), (0,) + ANCHORS, np.concatenate([[0.0], moves]))
        path_m.append(np.mean(np.abs(o.path - path)) / hour)
        path_b.append(np.mean(np.abs(o.path)) / hour)
    out["candles"], out["path"] = _gain(size_m, size_b), _gain(path_m, path_b)
    return out


def typical_miss(log_error: float) -> str:
    """A mean |log error| as the typical miss, '±34 %'."""
    return f"±{(math.exp(log_error) - 1) * 100:.0f} %" if np.isfinite(log_error) else "—"


def _verdict(s: Dict[str, float]) -> str:
    if not np.isfinite(s["gain_se"]):
        return "too few sessions"
    return "better" if s["gain"] > 2 * s["gain_se"] else "worse" if s["gain"] < -2 * s["gain_se"] else "no difference"


def format_scorecard(r: Dict[str, Any], model: Optional[Model] = None) -> str:
    lines = [f"First-hour model ({r['symbol']}): {r['sessions']} session(s) {r.get('first_day') or ''} .. "
             f"{r.get('last_day') or ''}, each forecast from the sessions before it (walk-forward)."]
    if model is not None:
        lines.append(f"Latest model: trained on {model.sessions} sessions through {model.trained_through}; ridge "
                     "penalty per target " + ", ".join(f"{t} {a:.3g}" for t, a in zip(TARGETS, model.alpha))
                     + " (a large penalty on a move: little direction learned).")
    if not r["sessions"]:
        return "\n".join(lines + ["Too few sessions to score yet."])
    lines.append("")
    for k in ANCHORS:
        d = r["direction"][k]
        lines.append(f"direction at +{k:>2} min: right {d['hit_rate'] * 100:.1f} % (hour up {d['up_share'] * 100:.1f} %); "
                     f"squared error vs no move {d['move']['model']:.4f} vs {d['move']['base']:.4f}, "
                     f"gain {d['move']['gain']:+.4f} ± {d['move']['gain_se']:.4f}  {_verdict(d['move'])}")
    for k in ANCHORS:
        s = r["ranges"][k]
        lines.append(f"range, first {k:>2} min: typical miss {typical_miss(s['model'])} vs usual "
                     f"{typical_miss(s['base'])}, gain {s['gain']:+.4f} ± {s['gain_se']:.4f}  {_verdict(s)}")
    c, p = r["candles"], r["path"]
    lines.append(f"minute candle size: typical miss {typical_miss(c['model'])} vs usual {typical_miss(c['base'])}, "
                 f"gain {c['gain']:+.4f} ± {c['gain_se']:.4f}  {_verdict(c)}")
    lines.append(f"expected path vs a flat line at P: {p['model']:.4f} vs {p['base']:.4f} usual hours, "
                 f"gain {p['gain']:+.4f} ± {p['gain_se']:.4f}  {_verdict(p)}")
    return "\n".join(lines)
