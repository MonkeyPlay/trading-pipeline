# forecaster/fan_benchmark.py
"""
The benchmark price fan (contracts/fan.FAN, docs/fan.md): pure functions on
minute grids - no database, no machine learning.

A trading day is a grid of 1440 one-minute slots from 18:00 ET the prior evening
(slot 0) - the same day the store files bars under. ``Day.closes[s]`` is the
close of the bar that started at slot s (NaN without one); the last price is that
grid forward-filled, and a minute without a trade keeps the price.

  seasonal(history)              S: the usual variance of each minute of the day
  event_multipliers(S, history)  E's multipliers per release group and minute bucket
  fit(target, history)           everything a forecast for the target session needs
                                 from before it: S, E on its releases, the levels
  horizon_variances(model, r)    the variance to t + h for every origin t and score
                                 horizon h, for the full model and its references
  fan_from(model, r, t, price)   the fan from one origin: every later minute's sigma
                                 and the issued quantiles

Only the target session's own returns up to the origin enter a forecast from that
origin (the short level reads the 60 minutes up to and including it).
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from functools import cached_property
from statistics import NormalDist
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

from contracts import fan as F
from features import calendar as cal

DAY_SLOTS = 1440
MINUTE = timedelta(minutes=1)
VARIANTS = ("flat", "seasonal", "seasonal_events", "full")


class InsufficientHistory(ValueError):
    """Too few earlier complete sessions to estimate the intraday pattern."""


# --------------------------------------------------------------------------
# The grid
# --------------------------------------------------------------------------

def slot_of_time(t: time) -> int:
    """The slot of an ET wall-clock time (18:00 is slot 0). No DST change falls inside a trading day: the clocks
    change at 02:00 on a Sunday, when every session is closed."""
    return ((t.hour * 60 + t.minute) - 18 * 60) % DAY_SLOTS


def day_start(d: date) -> datetime:
    """The UTC instant of slot 0 of session ``d``: 18:00 ET the calendar day before."""
    return cal.ny_instant(d - timedelta(days=1), time(18, 0))


def slot_at(d: date, instant: datetime) -> int:
    """The slot holding ``instant`` on session ``d`` (may fall outside [0, 1440))."""
    return int((instant - day_start(d)).total_seconds() // 60)


def slot_instant(d: date, slot: int) -> datetime:
    return day_start(d) + slot * MINUTE


def end_slot(sec_type: str, schedule: str) -> int:
    """The first slot after the instrument type's trading day (contracts/fan.DAY_END_ET)."""
    ends = F.DAY_END_ET.get(sec_type)
    if ends is None:
        raise ValueError(f"no fan for sec_type {sec_type!r}: futures and stocks only")
    if schedule not in ends:
        raise ValueError(f"no fan on a {schedule} session")
    return slot_of_time(ends[schedule]) or DAY_SLOTS


PHASE_SLOTS: Tuple[int, ...] = tuple(sorted(slot_of_time(t) for t in F.PHASE_BOUNDARIES_ET))


@dataclass(frozen=True)
class Release:
    """A scheduled release on a session's grid."""
    slot: int
    group: str
    name: str
    at: datetime
    source: Optional[str] = None        # its economic_events source (fan_rw_v2 places earnings by it)


@dataclass
class Day:
    """One instrument's trading day on the grid."""
    session_date: date
    schedule: str                       # full | early_close
    end: int                            # first slot after the trading day
    closes: np.ndarray                  # (1440,) close of the bar starting at each slot, NaN without one
    complete: bool = True               # the store holds the whole day (session_days COMPLETE)
    releases: Tuple[Release, ...] = ()
    releases_known: bool = False        # inside the economic calendar's coverage
    contract_id: Optional[int] = None

    @cached_property
    def last_price(self) -> np.ndarray:
        c = self.closes
        idx = np.where(np.isfinite(c), np.arange(len(c)), -1)
        idx = np.maximum.accumulate(idx)
        return np.where(idx >= 0, c[np.maximum(idx, 0)], np.nan)

    @cached_property
    def returns(self) -> np.ndarray:
        """1-minute log returns of the last price; NaN for slot 0, before the first bar and from the day's end."""
        lp = np.log(self.last_price)
        r = np.full(DAY_SLOTS, np.nan)
        r[1:] = lp[1:] - lp[:-1]
        r[self.end:] = np.nan
        return r


def release_windows(day: Day) -> List[Tuple[str, int, int, int]]:
    """``(group, bucket, first slot, end slot)`` of every release window of the day, clipped to [1, end)."""
    out = []
    for rel in day.releases:
        for b, (a, z) in enumerate(F.EVENT_GROUPS[rel.group]["buckets"]):
            lo, hi = max(1, rel.slot + a), min(day.end, rel.slot + z)
            if lo < hi:
                out.append((rel.group, b, lo, hi))
    return out


# --------------------------------------------------------------------------
# Estimation
# --------------------------------------------------------------------------

def _windowed_mean(values: np.ndarray, half: int, bounds: Sequence[int]) -> np.ndarray:
    """A centred mean over +-half slots ignoring NaN, never across ``bounds``; NaN where the window holds nothing."""
    out = np.full(len(values), np.nan)
    edges = sorted({0, len(values), *[b for b in bounds if 0 < b < len(values)]})
    for a, b in zip(edges[:-1], edges[1:]):
        seg = values[a:b]
        ok = np.isfinite(seg)
        cs = np.concatenate([[0.0], np.cumsum(np.where(ok, seg, 0.0))])
        cn = np.concatenate([[0], np.cumsum(ok)])
        i = np.arange(b - a)
        lo, hi = np.maximum(0, i - half), np.minimum(b - a, i + half + 1)
        n = cn[hi] - cn[lo]
        with np.errstate(invalid="ignore", divide="ignore"):
            out[a:b] = np.where(n > 0, (cs[hi] - cs[lo]) / np.maximum(n, 1), np.nan)
    return out


def seasonal(history: Sequence[Day], windows=None) -> np.ndarray:
    """
    S (contracts/fan.FAN 'seasonal'): per slot the mean squared 1-minute log return over ``history``, release
    windows left out (``windows(day)``: tuples ending in each window's first and end slot; v1's by default),
    squared returns capped at OUTLIER_CAP x the median of their +-7-slot neighbourhood, a slot with fewer than
    SEASONAL_MIN_SESSIONS valid returns left to the smoothing, smoothed within the phases. A slot nothing trades in
    (a futures halt) is 0.
    """
    hw = F.SEASONAL_HALF_WINDOW
    windows = windows or release_windows
    R = np.full((len(history), DAY_SLOTS), np.nan)
    for i, d in enumerate(history):
        x = d.returns ** 2
        for *_, lo, hi in windows(d):
            x[lo:hi] = np.nan
        R[i] = x
    padded = np.pad(R, ((0, 0), (hw, hw)), constant_values=np.nan)
    windows = np.lib.stride_tricks.sliding_window_view(padded, 2 * hw + 1, axis=1)   # (days, slots, window)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)                               # all-NaN neighbourhoods
        med = np.nanmedian(windows.transpose(1, 0, 2).reshape(DAY_SLOTS, -1), axis=1)
    cap = np.where(med > 0, F.OUTLIER_CAP * med, np.inf)
    R = np.minimum(R, cap)
    n = np.isfinite(R).sum(axis=0)
    with np.errstate(invalid="ignore", divide="ignore"):
        mean = np.where(n >= F.SEASONAL_MIN_SESSIONS, np.nansum(R, axis=0) / np.maximum(n, 1), np.nan)
    smooth = _windowed_mean(mean, hw, PHASE_SLOTS)
    # A minute no session traded in (a halt) or ever moved in (a closed stock market) stays 0: the smoothing must
    # not spread its neighbours' variance into it.
    still = (n == 0) | (np.nan_to_num(np.nansum(R, axis=0)) == 0)
    return np.where(still, 0.0, np.nan_to_num(smooth, nan=0.0))


def event_multipliers(S: np.ndarray, history: Sequence[Day]) -> Tuple[Dict[str, np.ndarray], Dict[str, int]]:
    """
    Per release group the multiplier of each minute bucket over every earlier release in ``history``: squared
    returns over S in the bucket's minutes, shrunk towards the group's fallback with EVENT_PRIOR_RELEASES releases'
    weight (one extreme release cannot carry it), floored at 1; the fallback without earlier releases. Returns
    ``(multipliers, occurrences)``.
    """
    num = {g: np.zeros(len(v["buckets"])) for g, v in F.EVENT_GROUPS.items()}
    den = {g: np.zeros(len(v["buckets"])) for g, v in F.EVENT_GROUPS.items()}
    occ = {g: 0 for g in F.EVENT_GROUPS}
    for d in history:
        if not d.releases_known:
            continue
        r2 = d.returns ** 2
        for g, b, lo, hi in release_windows(d):
            x = r2[lo:hi]
            ok = np.isfinite(x)
            num[g][b] += x[ok].sum()
            den[g][b] += S[lo:hi][ok].sum()
        for g, slot in {(rel.group, rel.slot) for rel in d.releases}:
            if 1 <= slot < d.end:
                occ[g] += 1
    mult = {}
    for g, v in F.EVENT_GROUPS.items():
        fallback = np.array(v["fallback"], dtype=float)
        if not occ[g]:
            mult[g] = fallback
            continue
        w = F.EVENT_PRIOR_RELEASES * den[g] / occ[g]          # the prior's expected variance
        with np.errstate(invalid="ignore", divide="ignore"):
            est = (num[g] + w * fallback) / (den[g] + w)
        mult[g] = np.where(den[g] > 0, np.maximum(1.0, est), fallback)
    return mult, occ


def event_profile(day: Day, mult: Dict[str, np.ndarray]) -> np.ndarray:
    """E on the day's grid: 1, or the largest multiplier of the release windows covering a slot."""
    E = np.ones(DAY_SLOTS)
    if not day.releases_known:
        return E
    for rel in day.releases:
        for b, (a, z) in enumerate(F.EVENT_GROUPS[rel.group]["buckets"]):
            lo, hi = max(0, rel.slot + a), min(DAY_SLOTS, rel.slot + z)
            if lo < hi:
                E[lo:hi] = np.maximum(E[lo:hi], mult[rel.group][b])
    return E


def _clip_level(x: float) -> float:
    lo, hi = F.LEVEL_BOUNDS
    return float(min(hi, max(lo, x)))


def long_level(recent: Sequence[Day], S: np.ndarray, mult: Dict[str, np.ndarray]) -> float:
    """Realised over expected (S x E) variance across ``recent`` sessions; 1 without expected variance."""
    rv = ev = 0.0
    for d in recent:
        r2 = d.returns ** 2
        ok = np.isfinite(r2)
        rv += r2[ok].sum()
        ev += (S * event_profile(d, mult))[ok].sum()
    return _clip_level(rv / ev) if ev > 0 else 1.0


def short_levels(r: np.ndarray, Sp: np.ndarray, level: float, prior: float) -> np.ndarray:
    """The short level at every origin t: realised over expected variance of the LEVEL_WINDOW slots up to and
    including t, with ``prior`` slots-worth of expected variance at the long ``level``."""
    r2 = r ** 2
    ok = np.isfinite(r2)
    cr = np.concatenate([[0.0], np.cumsum(np.where(ok, r2, 0.0))])
    ce = np.concatenate([[0.0], np.cumsum(np.where(ok, Sp, 0.0))])
    t = np.arange(DAY_SLOTS)
    lo = np.maximum(0, t - F.LEVEL_WINDOW + 1)
    rv, ev = cr[t + 1] - cr[lo], ce[t + 1] - ce[lo]
    with np.errstate(invalid="ignore", divide="ignore"):
        x = (rv + level * prior) / (ev + prior)
    lo_b, hi_b = F.LEVEL_BOUNDS
    return np.clip(np.where(np.isfinite(x), x, level), lo_b, hi_b)


@dataclass
class FanModel:
    """Everything a forecast for one session uses from before it (and its scheduled releases)."""
    session_date: date
    end: int
    S: np.ndarray
    E: np.ndarray
    multipliers: Dict[str, np.ndarray]
    occurrences: Dict[str, int]
    level_long: float
    prior: float                         # the short level's prior weight, in expected variance
    flat: float                          # the flat reference's variance per trading minute
    seasonal_sessions: List[str] = field(default_factory=list)
    releases_known: bool = False

    @property
    def Sp(self) -> np.ndarray:
        return self.S * self.E


def fit(target: Day, history: Sequence[Day]) -> FanModel:
    """
    The model for ``target`` from ``history`` (any order; only sessions strictly before the target are read): S
    from the last SEASONAL_SESSIONS complete full sessions, E's multipliers from the complete sessions within
    EVENT_SESSIONS, the long level from the last LEVEL_SESSIONS. Raises InsufficientHistory with fewer than
    SEASONAL_MIN_SESSIONS usable sessions.
    """
    earlier = sorted((d for d in history if d.session_date < target.session_date and d.complete),
                     key=lambda d: d.session_date)
    full = [d for d in earlier if d.schedule == "full"]
    seas = full[-F.SEASONAL_SESSIONS:]
    if len(seas) < F.SEASONAL_MIN_SESSIONS:
        raise InsufficientHistory(f"{target.session_date}: {len(seas)} earlier complete full sessions, "
                                  f"{F.SEASONAL_MIN_SESSIONS} needed")
    S = seasonal(seas)
    mult, occ = event_multipliers(S, earlier[-F.EVENT_SESSIONS:])
    E = event_profile(target, mult)
    level = long_level(full[-F.LEVEL_SESSIONS:], S, mult)
    Sp = S * E
    active = Sp[1:target.end]
    prior = F.LEVEL_PRIOR_MINUTES * float(np.median(active[active > 0])) if (active > 0).any() else 0.0
    r2 = np.concatenate([d.returns[1:d.end] ** 2 for d in seas])
    flat = float(np.nanmean(r2)) if np.isfinite(r2).any() else 0.0
    return FanModel(target.session_date, target.end, S, E, mult, occ, level, prior, flat,
                    [d.session_date.isoformat() for d in seas], target.releases_known)


# --------------------------------------------------------------------------
# Forecasts
# --------------------------------------------------------------------------

def _window_sums(X: np.ndarray, h: int, end: int) -> np.ndarray:
    """sum_{d=1..h} X[t + d] for every origin t with t + h < end; NaN elsewhere."""
    C = np.concatenate([[0.0], np.cumsum(X)])
    out = np.full(DAY_SLOTS, np.nan)
    t = np.arange(0, max(0, end - h))
    out[t] = C[t + h + 1] - C[t + 1]
    return out


def horizon_variances(model: FanModel, r: np.ndarray, horizons: Sequence[int] = F.SCORE_HORIZONS
                      ) -> Dict[str, np.ndarray]:
    """
    ``{variant: (len(horizons), 1440)}``: the log-price variance from every origin t to t + h under the full model
    and its references (contracts/fan.FAN score.references); NaN where t + h is past the day's end. ``r`` is the
    target session's return grid - the short level at t reads it up to t only.
    """
    S, Sp, end = model.S, model.Sp, model.end
    flat = np.where(S > 0, model.flat, 0.0)
    short = short_levels(r, Sp, model.level_long, model.prior)
    tau = F.LEVEL_REVERSION_MINUTES
    B = np.zeros(DAY_SLOTS)
    decayed = {}
    for d in range(1, max(horizons) + 1):
        B[:DAY_SLOTS - d] += np.exp(-d / tau) * Sp[d:]
        if d in horizons:
            decayed[d] = B.copy()
    out = {v: np.full((len(horizons), DAY_SLOTS), np.nan) for v in VARIANTS}
    for i, h in enumerate(horizons):
        out["flat"][i] = _window_sums(flat, h, end)
        out["seasonal"][i] = _window_sums(S, h, end)
        A = _window_sums(Sp, h, end)
        out["seasonal_events"][i] = A
        out["full"][i] = model.level_long * A + (short - model.level_long) * np.where(np.isfinite(A), decayed[h], np.nan)
    return out


_Z = np.array([NormalDist().inv_cdf(q) for q in F.QUANTILES])


@dataclass
class Fan:
    """The fan from one origin: minute d after the origin's bar, its sigma (log price) and the issued quantiles."""
    session_date: date
    origin_slot: int
    price: float
    minutes: np.ndarray                  # 1..H
    sigma: np.ndarray                    # (H,)
    prices: np.ndarray                   # (H, len(QUANTILES))
    level_short: float
    level_long: float

    @property
    def slots(self) -> np.ndarray:
        return self.origin_slot + self.minutes

    def instants(self) -> List[datetime]:
        """The closing instant of each forecast minute's bar (the price is the last trade by then)."""
        return [slot_instant(self.session_date, int(s) + 1) for s in self.slots]


def fan_from(model: FanModel, r: np.ndarray, t: int, price: float, minutes: Optional[int] = None) -> Fan:
    """The fan from origin slot ``t`` (the last completed bar, close ``price``) to the day's end, or ``minutes``."""
    H = model.end - 1 - t
    if minutes is not None:
        H = min(H, minutes)
    H = max(0, H)
    short = float(short_levels(r, model.Sp, model.level_long, model.prior)[t]) if 0 <= t < DAY_SLOTS else model.level_long
    d = np.arange(1, H + 1)
    level = model.level_long + (short - model.level_long) * np.exp(-d / F.LEVEL_REVERSION_MINUTES)
    var = np.cumsum(level * model.Sp[t + 1:t + H + 1])
    sigma = np.sqrt(var)
    prices = price * np.exp(np.outer(sigma, _Z))
    return Fan(model.session_date, t, float(price), d, sigma, prices, short, model.level_long)


# --------------------------------------------------------------------------
# Scoring arithmetic
# --------------------------------------------------------------------------

_SQRT2 = np.sqrt(2.0)
_INV_SQRT_PI = 1.0 / np.sqrt(np.pi)


def _erf(x: np.ndarray) -> np.ndarray:
    """Abramowitz & Stegun 7.1.26 (absolute error below 1.5e-7) - numpy only, no scipy."""
    s = np.sign(x)
    a = np.abs(x)
    t = 1.0 / (1.0 + 0.3275911 * a)
    y = 1.0 - (((((1.061405429 * t - 1.453152027) * t) + 1.421413741) * t - 0.284496736) * t + 0.254829592) * t \
        * np.exp(-a * a)
    return s * y


def norm_cdf(z: np.ndarray) -> np.ndarray:
    return 0.5 * (1.0 + _erf(np.asarray(z, dtype=float) / _SQRT2))


def norm_pdf(z: np.ndarray) -> np.ndarray:
    z = np.asarray(z, dtype=float)
    return np.exp(-0.5 * z * z) / np.sqrt(2.0 * np.pi)


def crps_normal(y: np.ndarray, sigma: np.ndarray) -> np.ndarray:
    """The CRPS of N(0, sigma^2) at ``y`` (closed form; Gneiting & Raftery 2007), in the units of y."""
    z = y / sigma
    return sigma * (z * (2.0 * norm_cdf(z) - 1.0) + 2.0 * norm_pdf(z) - _INV_SQRT_PI)
