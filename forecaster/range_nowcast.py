# forecaster/range_nowcast.py
"""
Range nowcast: how far the price still travels before the end of the opening
range (09:45), the first hour (10:30) and the session (16:00), re-estimated every
minute from the bars so far.

Why a range and no direction
    Nothing known at 09:29 has predicted which way NQ goes - not the pre-open
    state (``metric-study``: no robust association with any direction metric;
    the pre-open matches' first hours are no closer to the day's than random
    days'), and not the session's own first 15 or 30 minutes either. How far it
    goes is predictable: from the recent daily ranges before the open, and more
    so from the volatility realised since the open. So this forecasts the
    remaining range and draws it as a cone around the current price, with no
    lean either way.

The quantity
    At minute t of a horizon ending at minute H (minutes from 09:30), with p the
    price at t (the 09:30 open at t = 0, else the close of minute t - 1):

        remaining range = ln max(p, highest high in [t, H)) - ln min(p, lowest low in [t, H))

    At t = 0 it is the horizon's whole range from the open. It is forecast in
    logs, against the usual: its median over the previous USUAL_SESSIONS
    sessions at the same minute.

The model (one per horizon)
    ln remaining range - ln usual = ridge(x, x * t/H, x * sqrt(t/H), t/H, sqrt(t/H))

    x are the inputs below, each the log of today's value over its usual (the
    median over the previous USUAL_SESSIONS sessions), clipped to RATIO_CLIP:

      before the open  previous RTH range; mean RTH range of the last 5 and 22
                       sessions; previous first-hour range; mean first-hour range
                       of the last 5; overnight range and volume (18:00 to
                       09:29); range and volume of the last hour before 09:29;
                       spot VIX (and its log level, and its change since the
                       previous day's last print); Monday; Friday
      since the open   realised volatility (root sum of squared 1-minute log
                       returns) since 09:30, over the last 15 and the last 5
                       minutes, and the range so far - each against the usual
                       at the same minute (0 at t = 0)

    fitted (Ridge, ALPHA, on standardised columns; a missing input counts as its
    mean) on every ``grid``-th minute of each earlier session. The t/H terms let
    the pre-open inputs fade and the intraday ones take over as the horizon
    runs out.

Bands, final range, projected high / low and the cone
    Filtered historical simulation. Each earlier session with a walk-forward
    forecast is one scenario: its own path from minute t (log prices relative to
    its price at t), scaled by today's forecast over its own forecast at that
    minute. Quantiles across the scenarios give the remaining-range band, the
    horizon's final range, high and low (a scenario beyond the extremes so far),
    and the cone - price quantiles minute by minute from the current price.
    Scaling by the ratio of forecasts makes the bands as wide as the forecast's
    own walk-forward errors; ``backtest`` checks that they hold what they say.

Walk-forward
    A session's forecast comes from a model fitted on the sessions before it
    only (refitted every REFIT sessions), and its scenarios are earlier sessions
    only, so what the dashboard shows for a past day is what ``backtest`` scored.

Everything is computed from the stored 1-minute bars of each day's active
contract, and spot VIX when it is collected: no feature snapshot is needed, and
any forecast instrument works. Early-close sessions are left out of the history
and have no session horizon; the opening range and first hour still apply.
"""

import bisect
import math
import warnings
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from features import calendar as cal

USUAL_SESSIONS = 40      # the usual: median over this many previous sessions
MIN_TRAIN = 60           # sessions with inputs before the first model is fitted
REFIT = 5                # sessions between walk-forward refits
ALPHA = 1000.0           # ridge penalty on the standardised design
MIN_SCENARIOS = 30       # earlier sessions with a forecast before a nowcast is given
RATIO_CLIP = (0.1, 10.0)
MAX_MISSING = 5          # regular-session minutes a stored day may lack and still count as complete
QUANTILES = (10, 25, 50, 75, 90)
SESSION_MINUTES = 390
VIX_FRESH_MINUTES = 20


@dataclass(frozen=True)
class Horizon:
    key: str
    label: str
    end: int          # minutes from 09:30
    grid: int         # every grid-th minute of a session is a training row


HORIZONS: Tuple[Horizon, ...] = (
    Horizon("opening_range", "Opening range · 09:30–09:45", 15, 1),
    Horizon("first_hour", "First hour · 09:30–10:30", 60, 5),
    Horizon("session", "Session · 09:30–16:00", SESSION_MINUTES, 15),
)
BY_KEY = {h.key: h for h in HORIZONS}

PRE_INPUTS = ("prev_rth_range", "rth_range_5d", "rth_range_22d", "prev_first_hour_range", "first_hour_range_5d",
              "overnight_range", "overnight_volume", "last_hour_range", "last_hour_volume",
              "vix_log_level", "vix_vs_usual", "vix_change", "monday", "friday")
INTRADAY_INPUTS = ("rv_since_open", "rv_last_15m", "rv_last_5m", "range_since_open")
INPUT_LABELS = {
    "prev_rth_range": "previous session's range", "rth_range_5d": "5-session range",
    "rth_range_22d": "22-session range", "prev_first_hour_range": "previous first hour",
    "first_hour_range_5d": "5-session first hour", "overnight_range": "overnight range",
    "overnight_volume": "overnight volume", "last_hour_range": "last pre-open hour's range",
    "last_hour_volume": "last pre-open hour's volume", "vix_vs_usual": "VIX",
    "rv_since_open": "volatility since the open", "rv_last_15m": "volatility, last 15 min",
    "rv_last_5m": "volatility, last 5 min", "range_since_open": "range since the open",
}


# --------------------------------------------------------------------------
# Sessions from bars
# --------------------------------------------------------------------------

@dataclass
class Session:
    """One day's regular session so far, and what was known before its open."""
    day: str
    minutes: int                          # scheduled regular-session minutes (390; 210 on an early close)
    open_at: pd.Timestamp                 # 09:30 ET
    open: float                           # the 09:30 bar's open
    close: np.ndarray                     # per minute held; a minute without a print repeats the previous close
    high: np.ndarray
    low: np.ndarray
    bars: int = 0                         # regular-session bars actually stored
    pre_price: Optional[float] = None     # the 09:28 bar's close
    overnight_range: Optional[float] = None    # ln high - ln low, 18:00 -> 09:29
    overnight_volume: Optional[float] = None
    last_hour_range: Optional[float] = None    # the same over [08:29, 09:29)
    last_hour_volume: Optional[float] = None
    vix: Optional[float] = None           # spot VIX before 09:29
    vix_close: Optional[float] = None     # the day's last VIX print
    contract_id: Optional[int] = None

    @property
    def held(self) -> int:
        """Regular-session minutes held: the latest bar's minute + 1 (a live day: so far)."""
        return len(self.close)

    @property
    def complete(self) -> bool:
        return self.held >= self.minutes - MAX_MISSING and self.bars >= self.minutes - MAX_MISSING

    def price_at(self, t: int) -> float:
        """The price at minute ``t``: the open at 0, else the close of minute t - 1."""
        return self.open if t <= 0 else float(self.close[min(t, self.held) - 1])

    def time_at(self, t: int) -> pd.Timestamp:
        return self.open_at + pd.Timedelta(minutes=int(t))


def _session(day: str, ts: pd.DatetimeIndex, o, h, l, c, v, contract_id=None) -> Optional[Session]:
    try:
        sched = cal.session(day)
    except cal.CalendarCoverageError:
        return None
    if sched.rth_open_at is None or sched.scheduled_close_at is None:
        return None
    open_at = pd.Timestamp(sched.rth_open_at).tz_convert("UTC")
    minutes = int((pd.Timestamp(sched.scheduled_close_at) - pd.Timestamp(sched.rth_open_at)).total_seconds() // 60)
    m = np.asarray((ts - open_at).total_seconds() // 60, dtype=int)
    first = np.flatnonzero(m == 0)
    rth = (m >= 0) & (m < minutes)
    pre = m < -1                     # bars starting before 09:29: the last is the 09:28 bar
    last_hour = pre & (m >= -61)     # [08:29, 09:29)
    if len(first):
        held = int(m[rth].max()) + 1
        opening = float(o[first[0]])
    elif not rth.any() and pre.any():
        held, opening = 0, float(c[pre][-1])      # before the open: the last pre-open price stands in
    else:
        return None
    close, high, low = (np.full(held, np.nan) for _ in range(3))
    close[m[rth]], high[m[rth]], low[m[rth]] = c[rth], h[rth], l[rth]
    close = pd.Series(close, dtype=float).ffill().to_numpy()
    high = np.where(np.isnan(high), close, high)
    low = np.where(np.isnan(low), close, low)

    def rng(mask):
        return float(np.log(h[mask].max()) - np.log(l[mask].min())) if mask.any() else None

    return Session(day=day, minutes=minutes, open_at=open_at, open=opening, close=close, high=high,
                   low=low, bars=int(rth.sum()), pre_price=float(c[pre][-1]) if pre.any() else None,
                   overnight_range=rng(pre), overnight_volume=float(v[pre].sum()) if pre.any() else None,
                   last_hour_range=rng(last_hour),
                   last_hour_volume=float(v[last_hour].sum()) if last_hour.any() else None,
                   contract_id=contract_id)


def sessions_from_bars(bars: pd.DataFrame) -> List[Session]:
    """
    Sessions from 1-minute bars (``trading_day``, ``timestamp_utc``, OHLC,
    ``volume``; optionally ``contract_id``) holding one contract per day - the
    whole Globex day, from 18:00 ET the evening before. A day without a
    scheduled regular session, or with regular-session bars but not the 09:30
    one, is left out; a day with only pre-open bars yet holds no minute, its
    last pre-open close standing in for the open.
    """
    if bars is None or bars.empty:
        return []
    df = bars.assign(ts=pd.to_datetime(bars["timestamp_utc"], utc=True, format="ISO8601"),
                     trading_day=bars["trading_day"].astype(str))
    df = df.sort_values(["trading_day", "ts"], kind="stable")
    days = df["trading_day"].to_numpy()
    ts = pd.DatetimeIndex(df["ts"])
    cols = {k: df[k].to_numpy(dtype=float) for k in ("open", "high", "low", "close", "volume")}
    cids = df["contract_id"].to_numpy() if "contract_id" in df.columns else None
    edges = np.flatnonzero(days[1:] != days[:-1]) + 1
    out = []
    for a, b in zip(np.concatenate([[0], edges]), np.concatenate([edges, [len(days)]])):
        s = _session(str(days[a]), ts[a:b], *(cols[k][a:b] for k in ("open", "high", "low", "close", "volume")),
                     contract_id=int(cids[a]) if cids is not None else None)
        if s is not None:
            out.append(s)
    return out


def _padded(s: Session) -> Session:
    """A complete session with its last few missing minutes repeating the last close."""
    if s.held >= s.minutes:
        return s
    pad = s.minutes - s.held
    last = s.close[-1]
    return Session(**{**s.__dict__, "close": np.concatenate([s.close, np.full(pad, last)]),
                      "high": np.concatenate([s.high, np.full(pad, last)]),
                      "low": np.concatenate([s.low, np.full(pad, last)])})


# --------------------------------------------------------------------------
# Inputs
# --------------------------------------------------------------------------

def _log_ratio(value, usual) -> float:
    if value is None or usual is None or not np.isfinite(value) or not np.isfinite(usual) or usual <= 0 or value < 0:
        return math.nan
    return math.log(min(max(value / usual, RATIO_CLIP[0]), RATIO_CLIP[1]))


def _nanmedian(a) -> float:
    a = np.asarray(a, dtype=float)
    a = a[np.isfinite(a)]
    return float(np.median(a)) if len(a) else math.nan


def _nanmean(a) -> float:
    a = np.asarray(a, dtype=float)
    a = a[np.isfinite(a)]
    return float(a.mean()) if len(a) else math.nan


def session_ranges(s: Session) -> Tuple[float, float]:
    """(regular-session range, first-hour range) of a complete session: ln high - ln low, open included."""
    def rng(n):
        return float(np.log(max(s.open, s.high[:n].max())) - np.log(min(s.open, s.low[:n].min())))
    return rng(s.minutes), rng(min(60, s.minutes))


def _daily(sessions: Sequence[Session]) -> Dict[str, np.ndarray]:
    """Per-session figures the pre-open inputs compare against, oldest first."""
    ranges = [session_ranges(s) for s in sessions]
    f = lambda v: np.nan if v is None else float(v)
    return {"rth_range": np.array([r[0] for r in ranges]), "first_hour_range": np.array([r[1] for r in ranges]),
            "overnight_range": np.array([f(s.overnight_range) for s in sessions]),
            "overnight_volume": np.array([f(s.overnight_volume) for s in sessions]),
            "last_hour_range": np.array([f(s.last_hour_range) for s in sessions]),
            "last_hour_volume": np.array([f(s.last_hour_volume) for s in sessions]),
            "vix": np.array([f(s.vix) for s in sessions]), "vix_close": np.array([f(s.vix_close) for s in sessions])}


def pre_inputs(prev: Dict[str, np.ndarray], today: Session) -> np.ndarray:
    """
    PRE_INPUTS of ``today`` from the figures of the sessions before it (``_daily``,
    oldest first) - all NaN with fewer than USUAL_SESSIONS of them.
    """
    if len(prev["rth_range"]) < USUAL_SESSIONS:
        return np.full(len(PRE_INPUTS), np.nan)
    usual = {k: _nanmedian(v[-USUAL_SESSIONS:]) for k, v in prev.items()}
    rth, fh = prev["rth_range"], prev["first_hour_range"]
    weekday = date.fromisoformat(today.day).weekday()
    vix = today.vix if today.vix and today.vix > 0 else None
    return np.array([
        _log_ratio(rth[-1], usual["rth_range"]),
        _log_ratio(_nanmean(rth[-5:]), usual["rth_range"]),
        _log_ratio(_nanmean(rth[-22:]), usual["rth_range"]),
        _log_ratio(fh[-1], usual["first_hour_range"]),
        _log_ratio(_nanmean(fh[-5:]), usual["first_hour_range"]),
        _log_ratio(today.overnight_range, usual["overnight_range"]),
        _log_ratio(today.overnight_volume, usual["overnight_volume"]),
        _log_ratio(today.last_hour_range, usual["last_hour_range"]),
        _log_ratio(today.last_hour_volume, usual["last_hour_volume"]),
        math.log(vix) if vix else math.nan,
        _log_ratio(vix, usual["vix"]),
        _log_ratio(vix, prev["vix_close"][-1]),
        float(weekday == 0), float(weekday == 4),
    ])


def intraday_stats(s: Session) -> Dict[str, np.ndarray]:
    """
    Each INTRADAY_INPUTS statistic at every minute t in [0, held]: computed from
    the bars before t only (t = 0: nothing yet, all zero).
    """
    r = np.diff(np.log(np.concatenate([[s.open], s.close])))
    cs = np.concatenate([[0.0], np.cumsum(r * r)])
    t = np.arange(len(cs))
    hi = np.concatenate([[s.open], np.maximum.accumulate(np.maximum(s.high, s.open))])
    lo = np.concatenate([[s.open], np.minimum.accumulate(np.minimum(s.low, s.open))])
    return {"rv_since_open": np.sqrt(cs),
            "rv_last_15m": np.sqrt(np.maximum(cs - cs[np.maximum(t - 15, 0)], 0.0)),
            "rv_last_5m": np.sqrt(np.maximum(cs - cs[np.maximum(t - 5, 0)], 0.0)),
            "range_since_open": np.log(hi) - np.log(lo)}


def intraday_inputs(stats: Dict[str, np.ndarray], usual: Dict[str, np.ndarray], t: np.ndarray) -> np.ndarray:
    """(len(t), 4): each statistic at minute t against its usual there, as a clipped log ratio; 0 at t = 0."""
    cols = []
    with np.errstate(divide="ignore", invalid="ignore"):
        for k in INTRADAY_INPUTS:
            x = np.log(np.clip(stats[k][t] / usual[k][t], *RATIO_CLIP))
            x[t == 0] = 0.0
            cols.append(x)
    return np.column_stack(cols)


def remaining_range(s: Session, end: int) -> np.ndarray:
    """The remaining range (log units) at every minute t in [0, end) of a session holding ``end`` minutes."""
    ref = np.log(np.concatenate([[s.open], s.close[:end - 1]]))
    fut_hi = np.maximum.accumulate(np.log(s.high[:end])[::-1])[::-1]
    fut_lo = np.minimum.accumulate(np.log(s.low[:end])[::-1])[::-1]
    return np.maximum(ref, fut_hi) - np.minimum(ref, fut_lo)


def design(pre: np.ndarray, intra: np.ndarray, t: np.ndarray, end: int) -> np.ndarray:
    """The model's rows at minutes ``t``: inputs, inputs x t/H, inputs x sqrt(t/H), t/H, sqrt(t/H)."""
    phi = (np.asarray(t, dtype=float) / end)[:, None]
    x = np.hstack([np.broadcast_to(pre, (len(phi), len(pre))), intra])
    return np.hstack([x, x * phi, x * np.sqrt(phi), phi, np.sqrt(phi)])


# --------------------------------------------------------------------------
# Model
# --------------------------------------------------------------------------

@dataclass
class LinearModel:
    """Ridge on standardised columns; a missing value counts as the column's mean."""
    mean: np.ndarray
    scale: np.ndarray
    coef: np.ndarray
    intercept: float
    rows: int = 0

    def predict(self, X: np.ndarray) -> np.ndarray:
        Z = (X - self.mean) / self.scale
        Z[~np.isfinite(Z)] = 0.0
        return Z @ self.coef + self.intercept


def fit_linear(X: np.ndarray, y: np.ndarray, alpha: float = ALPHA) -> LinearModel:
    from sklearn.linear_model import Ridge
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)       # all-missing columns
        mean = np.nanmean(X, axis=0)
        scale = np.nanstd(X, axis=0)
    mean[~np.isfinite(mean)] = 0.0
    scale[~(scale > 0)] = 1.0
    Z = (X - mean) / scale
    Z[~np.isfinite(Z)] = 0.0
    r = Ridge(alpha=alpha).fit(Z, y)
    return LinearModel(mean=mean, scale=scale, coef=r.coef_, intercept=float(r.intercept_), rows=len(y))


# --------------------------------------------------------------------------
# History: every stored session, walk-forward
# --------------------------------------------------------------------------

@dataclass
class History:
    """
    The complete full-day sessions of one instrument, oldest first, with their
    inputs, their remaining ranges and the walk-forward forecasts of them.
    Arrays are indexed [session, minute].
    """
    symbol: str
    sessions: List[Session]
    days: List[str]
    daily: Dict[str, np.ndarray]
    pre: np.ndarray                              # (N, len(PRE_INPUTS))
    stats: Dict[str, np.ndarray]                 # (N, 391) intraday statistics
    usual_stats: Dict[str, np.ndarray]           # (N, 391) their usual
    y: Dict[str, np.ndarray]                     # horizon -> (N, H) ln remaining range
    usual_y: Dict[str, np.ndarray]               # horizon -> (N, H) its usual
    forecast: Dict[str, np.ndarray]              # horizon -> (N, H) walk-forward forecast of y (NaN: none)
    log_ref: np.ndarray                          # (N, 390) ln price at minute t
    log_close: np.ndarray                        # (N, 390)
    log_high: np.ndarray
    log_low: np.ndarray
    start: int                                   # first session with a walk-forward forecast
    alpha: float = ALPHA
    vix: Dict[str, Tuple[Optional[float], Optional[float]]] = field(default_factory=dict)
    models: Dict[Tuple[str, int], LinearModel] = field(default_factory=dict)

    @property
    def n(self) -> int:
        return len(self.sessions)

    def position(self, day: str) -> int:
        """How many sessions of the history lie before ``day``."""
        return bisect.bisect_left(self.days, str(day))

    def block(self, position: int) -> int:
        """The first session of the walk-forward block holding ``position``: its model saw the sessions before it."""
        return self.start + ((position - self.start) // REFIT) * REFIT

    def model(self, horizon: Horizon, position: int) -> Optional[LinearModel]:
        """The walk-forward model for a session at ``position`` (fitted on first use)."""
        if position < self.start:
            return None
        b = self.block(position)
        key = (horizon.key, b)
        if key not in self.models:
            self.models[key] = self._fit(horizon, b)
        return self.models[key]

    def _fit(self, horizon: Horizon, before: int) -> LinearModel:
        rows = self._training_rows(horizon)
        X = np.vstack([rows[j][0] for j in range(USUAL_SESSIONS, before)])
        y = np.concatenate([rows[j][1] for j in range(USUAL_SESSIONS, before)])
        return fit_linear(X, y, self.alpha)

    def _training_rows(self, horizon: Horizon) -> Dict[int, Tuple[np.ndarray, np.ndarray]]:
        """{session: (design rows, targets)} on the horizon's grid minutes - the same for every refit."""
        cache = self.__dict__.setdefault("_rows", {})
        if horizon.key not in cache:
            t = np.arange(0, horizon.end, horizon.grid)
            rows = {}
            for j in range(USUAL_SESSIONS, self.n):
                target = self.y[horizon.key][j, t] - self.usual_y[horizon.key][j, t]
                X = design(self.pre[j], intraday_inputs(self._stats(j), self._usual(j), t), t, horizon.end)
                ok = np.isfinite(target)
                rows[j] = (X[ok], target[ok])
            cache[horizon.key] = rows
        return cache[horizon.key]

    def _stats(self, j: int) -> Dict[str, np.ndarray]:
        return {k: v[j] for k, v in self.stats.items()}

    def _usual(self, j: int) -> Dict[str, np.ndarray]:
        return {k: v[j] for k, v in self.usual_stats.items()}

    def usual_before(self, position: int) -> Tuple[Dict[str, np.ndarray], Dict[str, np.ndarray]]:
        """(usual intraday statistics, usual ln remaining range per horizon) for a day at ``position``."""
        w = slice(max(0, position - USUAL_SESSIONS), position)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            stats = {k: np.nanmedian(v[w], axis=0) for k, v in self.stats.items()}
            ys = {k: np.nanmedian(v[w], axis=0) for k, v in self.y.items()}
        return stats, ys


def build_history(symbol: str, sessions: Sequence[Session], vix: Optional[Dict[str, Tuple]] = None,
                  alpha: float = ALPHA) -> History:
    """
    The walk-forward history of ``symbol`` from its sessions (complete full days
    are kept) and spot VIX per day ({day: (pre, last)}). Every session from
    ``start`` on gets the forecasts of the model fitted on the sessions before
    its block.
    """
    vix = vix or {}
    kept = []
    for s in sessions:
        if s.minutes == SESSION_MINUTES and s.complete:
            s = _padded(s)
            s.vix, s.vix_close = (vix.get(s.day) or (None, None))[:2]
            kept.append(s)
    kept.sort(key=lambda s: s.day)
    n = len(kept)
    daily = _daily(kept)
    pre = np.vstack([pre_inputs({k: v[:i] for k, v in daily.items()}, s) for i, s in enumerate(kept)]) \
        if n else np.zeros((0, len(PRE_INPUTS)))
    per = [intraday_stats(s) for s in kept]
    stats = {k: np.vstack([p[k] for p in per]) if n else np.zeros((0, SESSION_MINUTES + 1)) for k in INTRADAY_INPUTS}
    y = {h.key: np.log(np.maximum(np.vstack([remaining_range(s, h.end) for s in kept]), 1e-6)) if n
         else np.zeros((0, h.end)) for h in HORIZONS}

    def rolling_usual(a):
        out = np.full_like(a, np.nan, dtype=float)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            for i in range(USUAL_SESSIONS, len(a)):
                out[i] = np.nanmedian(a[i - USUAL_SESSIONS:i], axis=0)
        return out

    lc = np.vstack([np.log(s.close) for s in kept]) if n else np.zeros((0, SESSION_MINUTES))
    history = History(
        symbol=symbol, sessions=kept, days=[s.day for s in kept], daily=daily, pre=pre, stats=stats,
        usual_stats={k: rolling_usual(v) for k, v in stats.items()}, y=y,
        usual_y={k: rolling_usual(v) for k, v in y.items()},
        forecast={h.key: np.full((n, h.end), np.nan) for h in HORIZONS},
        log_ref=np.hstack([np.log([[s.open] for s in kept]), lc[:, :-1]]) if n else lc,
        log_close=lc, log_high=np.vstack([np.log(s.high) for s in kept]) if n else lc,
        log_low=np.vstack([np.log(s.low) for s in kept]) if n else lc,
        start=USUAL_SESSIONS + MIN_TRAIN, alpha=alpha, vix=dict(vix))
    for h in HORIZONS:
        t = np.arange(h.end)
        for i in range(history.start, n):
            model = history.model(h, i)
            X = design(pre[i], intraday_inputs(history._stats(i), history._usual(i), t), t, h.end)
            history.forecast[h.key][i] = history.usual_y[h.key][i] + model.predict(X)
    return history


def load_history(conn, symbol: str, alpha: float = ALPHA) -> History:
    """``build_history`` from the stored bars of ``symbol``'s active contracts and spot VIX."""
    from database.queries import active_contract_bars
    bars = pd.DataFrame([dict(r) for r in active_contract_bars(conn, symbol)])
    return build_history(symbol, sessions_from_bars(bars), load_vix(conn), alpha)


def day_session(conn, rows: Sequence[Any], history: History) -> Optional[Session]:
    """
    One day's Session from its stored 1-minute bars on one contract
    (``get_day_bars`` rows: the contract shown, whichever it is), with spot VIX -
    read again when the history has no pre-open level for the day yet (a live
    session whose 09:29 print came after the history was loaded).
    """
    found = sessions_from_bars(pd.DataFrame([dict(r) for r in rows])) if rows else []
    if not found:
        return None
    s = found[0]
    vix = history.vix.get(s.day)
    if vix is None or vix[0] is None:
        vix = load_vix(conn, s.day).get(s.day) or vix
    s.vix, s.vix_close = vix or (None, None)
    return s


def load_vix(conn, day: Optional[str] = None) -> Dict[str, Tuple[Optional[float], Optional[float]]]:
    """{day: (spot VIX before 09:29, the day's last VIX print)}; {} when VIX is not collected."""
    from config import Config
    from database.queries import get_contract_by_expiry, preopen_levels
    if "VIX" not in Config.CONTEXT_SYMBOLS:
        return {}
    contract = get_contract_by_expiry(conn, "VIX", Config.expiry_for("VIX"))
    if contract is None:
        return {}
    f = lambda v: None if v is None else float(v)
    return {str(r["trading_day"]): (f(r["pre"]), f(r["last"]))
            for r in preopen_levels(conn, contract["contract_id"], VIX_FRESH_MINUTES, day)}


# --------------------------------------------------------------------------
# Scenarios and the nowcast
# --------------------------------------------------------------------------

@dataclass
class Scenarios:
    """Earlier sessions' outcomes from minute t, scaled to a day's forecast (log units from the price at t)."""
    k: np.ndarray            # today's forecast over each session's own
    up: np.ndarray           # how far above the price at t the rest of the horizon reached
    down: np.ndarray         # how far below
    end: np.ndarray          # where the horizon closed

    @property
    def remaining(self) -> np.ndarray:
        return self.up + self.down


def scenarios(history: History, horizon: Horizon, position: int, t: int, forecast: float,
              path: bool = False) -> Tuple[Optional[Scenarios], Optional[np.ndarray]]:
    """
    The scenarios for a day at ``position`` at minute ``t`` from the sessions
    before it with a walk-forward forecast; (None, None) with fewer than
    MIN_SCENARIOS. With ``path``, also the scaled paths, (sessions, H - t).
    """
    j = np.arange(history.start, min(position, history.n))
    own = history.forecast[horizon.key][j, t] if len(j) else np.zeros(0)
    ok = np.isfinite(own)
    j, own = j[ok], own[ok]
    if len(j) < MIN_SCENARIOS:
        return None, None
    k = np.exp(forecast - own)
    ref = history.log_ref[j, t][:, None]
    up = np.maximum(0.0, (history.log_high[j, t:horizon.end] - ref).max(axis=1)) * k
    down = np.maximum(0.0, -(history.log_low[j, t:horizon.end] - ref).min(axis=1)) * k
    closes = (history.log_close[j, t:horizon.end] - ref) * k[:, None] if path else None
    end = closes[:, -1] if path else (history.log_close[j, horizon.end - 1] - ref[:, 0]) * k
    return Scenarios(k=k, up=up, down=down, end=end), closes


def _quantiles(values: np.ndarray) -> Dict[int, float]:
    return {q: float(v) for q, v in zip(QUANTILES, np.percentile(values, QUANTILES))}


def nowcast(history: History, today: Session, t: Optional[int] = None) -> Dict[str, Any]:
    """
    The nowcast for ``today`` (possibly partial) at minute ``t`` - by default
    the latest it holds - from the sessions of ``history`` before it only.
    Prices and ranges are in the instrument's points, from ``today``'s own
    contract. Horizons already over report what happened ('done').
    """
    position = history.position(today.day)
    t = today.held if t is None else int(max(0, min(t, today.held, today.minutes)))
    out: Dict[str, Any] = {"day": today.day, "t": t, "as_of": today.time_at(t), "available": False,
                           "position": position, "horizons": []}
    if position < history.start + MIN_SCENARIOS:
        out["reason"] = (f"needs {history.start + MIN_SCENARIOS} earlier complete sessions of {history.symbol}, "
                         f"has {position}")
        return out
    prev = {k: v[:position] for k, v in history.daily.items()}
    pre = pre_inputs(prev, today)
    stats = intraday_stats(today)
    usual_stats, usual_y = history.usual_before(position)
    price = today.price_at(t)
    hi_so_far = max(today.open, float(today.high[:t].max())) if t else None
    lo_so_far = min(today.open, float(today.low[:t].min())) if t else None
    intra = intraday_inputs(stats, usual_stats, np.array([t]))[0]
    out.update(available=True, price=price, high_so_far=hi_so_far, low_so_far=lo_so_far,
               inputs=_explain(pre, intra))
    for h in HORIZONS:
        row: Dict[str, Any] = {"key": h.key, "label": h.label, "end": h.end, "end_time": today.time_at(h.end)}
        out["horizons"].append(row)
        if today.minutes < h.end:
            row.update(status="unavailable", reason="early close")
            continue
        if today.held >= h.end:
            row["actual_final"] = float(max(today.open, today.high[:h.end].max()) - min(today.open, today.low[:h.end].min()))
        if t >= h.end:
            row["status"] = "done"
            continue
        model = history.model(h, position)
        x = design(pre, intra[None, :], np.array([t]), h.end)
        forecast = float(usual_y[h.key][t] + model.predict(x)[0])
        sc, paths = scenarios(history, h, position, t, forecast, path=True)
        if sc is None:
            row.update(status="unavailable", reason="too few earlier sessions")
            continue
        highs, lows = price * np.exp(sc.up), price * np.exp(-sc.down)
        f_hi = np.maximum(hi_so_far, highs) if hi_so_far is not None else highs
        f_lo = np.minimum(lo_so_far, lows) if lo_so_far is not None else lows
        cone = np.percentile(price * np.exp(paths), QUANTILES, axis=0)
        row.update(
            status="open", scenarios=len(sc.k),
            remaining=_quantiles(highs - lows),
            usual=float(price * math.exp(usual_y[h.key][t])),
            final=_quantiles(f_hi - f_lo), final_high=_quantiles(f_hi), final_low=_quantiles(f_lo),
            so_far=(hi_so_far - lo_so_far) if t else 0.0,
            cone={q: cone[i] for i, q in enumerate(QUANTILES)},
        )
        if today.held >= h.end:
            fut_hi = max(price, float(today.high[t:h.end].max()))
            fut_lo = min(price, float(today.low[t:h.end].min()))
            row["actual_remaining"] = fut_hi - fut_lo
    return out


def _explain(pre: np.ndarray, intra: Optional[np.ndarray]) -> Dict[str, Optional[float]]:
    """The inputs as multiples of their usual (VIX: its level), for display."""
    out: Dict[str, Optional[float]] = {}
    for name, v in zip(PRE_INPUTS, pre):
        if name in INPUT_LABELS:
            out[name] = math.exp(v) if np.isfinite(v) else None
    out["vix_level"] = math.exp(pre[PRE_INPUTS.index("vix_log_level")]) \
        if np.isfinite(pre[PRE_INPUTS.index("vix_log_level")]) else None
    if intra is not None:
        for name, v in zip(INTRADAY_INPUTS, intra):
            out[name] = math.exp(v) if np.isfinite(v) else None
    return out


def band_position(value: Optional[float], q: Dict[int, float]) -> Optional[str]:
    """Where ``value`` fell against a forecast's quantiles: 'inside 50 %', 'inside 80 %' or 'outside'."""
    if value is None or not q:
        return None
    if q[25] <= value <= q[75]:
        return "inside the 50 % band"
    if q[10] <= value <= q[90]:
        return "inside the 80 % band"
    return "outside the 80 % band"


# --------------------------------------------------------------------------
# Backtest
# --------------------------------------------------------------------------

CHECKPOINTS = {"opening_range": (0, 5, 10), "first_hour": (0, 15, 30, 45),
               "session": (0, 30, 90, 150, 210, 270, 330)}


def backtest(history: History) -> Dict[str, Any]:
    """
    Every session with a nowcast, on every ``grid``-th minute of each horizon,
    from earlier sessions only:

      |log error| of the median remaining range against the usual (the median
      of the previous USUAL_SESSIONS sessions at the same minute): mean over a
      session's minutes, then over sessions (gain = usual - nowcast, > 0 better)
      coverage: how often the remaining range, the horizon's final range and its
      closing price fell inside the 50 % and 80 % bands (ideal: 50 % and 80 %)
    """
    out: Dict[str, Any] = {"symbol": history.symbol, "sessions": 0, "horizons": {}}
    first = history.start + MIN_SCENARIOS
    for h in HORIZONS:
        grid = np.arange(0, h.end, h.grid)
        per_session_m, per_session_u = [], []
        by_t = {t: ([], []) for t in CHECKPOINTS[h.key]}
        inside = {k: [0, 0, 0] for k in ("remaining", "final", "close")}     # 50 %, 80 %, n
        for i in range(first, history.n):
            fc = history.forecast[h.key][i]
            s = history.sessions[i]
            full_hi = math.log(max(s.open, s.high[:h.end].max()))
            full_lo = math.log(min(s.open, s.low[:h.end].min()))
            em, eu = [], []
            for t in grid:
                sc, _ = scenarios(history, h, i, int(t), float(fc[t]))
                if sc is None:
                    continue
                actual = history.y[h.key][i, t]
                m = abs(math.log(np.median(sc.remaining)) - actual)
                u = abs(history.usual_y[h.key][i, t] - actual)
                em.append(m)
                eu.append(u)
                if t in by_t:
                    by_t[t][0].append(m)
                    by_t[t][1].append(u)
                ref = history.log_ref[i, t]
                hs = math.log(max(s.open, s.high[:t].max())) if t else ref
                ls = math.log(min(s.open, s.low[:t].min())) if t else ref
                final = np.maximum(hs, ref + sc.up) - np.minimum(ls, ref - sc.down)
                for key, sample, value in (("remaining", sc.remaining, math.exp(actual)),
                                           ("final", final, full_hi - full_lo),
                                           ("close", sc.end, history.log_close[i, h.end - 1] - ref)):
                    # inclusive, as the dashboard reads a band: late in a horizon many scenarios
                    # (and the day itself) add nothing to the range so far - a tie at the band's edge
                    q10, q25, q75, q90 = np.percentile(sample, (10, 25, 75, 90))
                    inside[key][0] += q25 - 1e-12 <= value <= q75 + 1e-12
                    inside[key][1] += q10 - 1e-12 <= value <= q90 + 1e-12
                    inside[key][2] += 1
            if em:
                per_session_m.append(np.mean(em))
                per_session_u.append(np.mean(eu))
        n = len(per_session_m)
        out["sessions"] = max(out["sessions"], n)
        out["horizons"][h.key] = {
            "label": h.label, "sessions": n, "error": _gain(per_session_m, per_session_u),
            "checkpoints": {t: _gain(m, u) for t, (m, u) in by_t.items()},
            "coverage": {k: {"50": c[0] / c[2] if c[2] else math.nan, "80": c[1] / c[2] if c[2] else math.nan}
                         for k, c in inside.items()},
        }
    out["first_day"] = history.days[first] if history.n > first else None
    out["last_day"] = history.days[-1] if history.n else None
    return out


def _gain(model: Sequence[float], usual: Sequence[float]) -> Dict[str, float]:
    m, u = np.asarray(model, float), np.asarray(usual, float)
    g = u - m
    n = len(g)
    return {"n": n, "model": float(m.mean()) if n else math.nan, "base": float(u.mean()) if n else math.nan,
            "gain": float(g.mean()) if n else math.nan,
            "gain_se": float(g.std(ddof=1) / math.sqrt(n)) if n > 1 else math.nan}


def typical_miss(log_error: float) -> str:
    """A mean |log error| as the typical miss, '±34 %'."""
    return f"±{(math.exp(log_error) - 1) * 100:.0f} %" if np.isfinite(log_error) else "—"


def format_backtest(r: Dict[str, Any]) -> str:
    lines = [f"Range nowcast backtest ({r['symbol']}): {r['sessions']} session(s) "
             f"{r.get('first_day') or ''} .. {r.get('last_day') or ''}, walk-forward, earlier sessions only.",
             "Remaining range: mean |log error| of the median, against the usual range at the same minute "
             "(gain > 0: the nowcast is better). Coverage ideal: 50 % / 80 %.", ""]
    for key, hr in r["horizons"].items():
        e = hr["error"]
        verdict = ("better" if e["gain"] > 2 * e["gain_se"] else "worse" if e["gain"] < -2 * e["gain_se"]
                   else "no difference")
        lines.append(f"{hr['label']}: {hr['sessions']} sessions - nowcast {e['model']:.4f} "
                     f"({typical_miss(e['model'])}), usual {e['base']:.4f} ({typical_miss(e['base'])}), "
                     f"gain {e['gain']:+.4f} ± {e['gain_se']:.4f}  {verdict}")
        for t, c in hr["checkpoints"].items():
            if c["n"]:
                lines.append(f"    at +{t:>3} min  nowcast {c['model']:.4f}  usual {c['base']:.4f}  "
                             f"gain {c['gain']:+.4f} ± {c['gain_se']:.4f}")
        cov = hr["coverage"]
        lines.append("    inside the 50 % / 80 % bands: " + ", ".join(
            f"{k} {cov[k]['50'] * 100:.0f} % / {cov[k]['80'] * 100:.0f} %" for k in ("remaining", "final", "close")))
    return "\n".join(lines)
