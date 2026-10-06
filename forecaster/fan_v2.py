# forecaster/fan_v2.py
"""
The benchmark fan, version 2 (fan_rw_v2, contracts/fan.FAN_V2): fan_rw_v1
(forecaster/fan_benchmark.py) with three changes chosen on the intermarket
experiment's development sessions before its checks (docs/fan.md, "Version 2"):

  releases by name       the event bump per release name and minute bucket, from its own
                         earlier releases, shrunk towards its group's multiplier
  earnings at the close  8-K earnings releases form their own group, placed at 16:00 ET
                         of their trading day (their minute is not known in advance)
  a fat-tailed shape     the issued distribution of the log price at t + h is
                         sigma_h x Q_h: Q_h the symmetric empirical quantiles of the
                         standardised errors of the last SHAPE_SESSIONS sessions

The variance - the intraday pattern, the level, the zero drift - is v1's machinery
(horizon_variances, fan_from) run on v2's model.

  release_windows(day)           (name, group, bucket, first slot, end slot) of each window
  fit(target, history)           the model, from the sessions before the target only
  errors(model, day)             a session's standardised errors y / sigma per horizon
  Shape, shape_from(errors)      the standardised quantiles per horizon (the normal's with
                                 too few sessions)
  fan_from(model, shape, ...)    the issued fan: the chart's quantiles from the shape
  walk_forward(days, first, last) every session's fit and shape in date order
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from statistics import NormalDist
from typing import Dict, Iterator, List, Optional, Sequence, Tuple

import numpy as np

from contracts import fan as F
from forecaster import fan_benchmark as fb
from forecaster.fan_benchmark import DAY_SLOTS, Day, FanModel, InsufficientHistory, slot_of_time

TAU = (np.arange(F.SHAPE_LEVELS) + 0.5) / F.SHAPE_LEVELS
NORMAL_Q = np.array([NormalDist().inv_cdf(t) for t in TAU])
MIN_VARIANCE = 1e-14
_ANCHOR = slot_of_time(F.EARNINGS_ANCHOR_ET)
_AFTER = slot_of_time(F.EARNINGS_AFTER_ET)


@dataclass
class FanModelV2(FanModel):
    """v1's model with the per-name multipliers: name -> (group, multiplier per bucket)."""
    name_multipliers: Dict[str, Tuple[str, np.ndarray]] = field(default_factory=dict)


# --------------------------------------------------------------------------
# Releases
# --------------------------------------------------------------------------

def _is_earnings(rel: fb.Release) -> bool:
    return rel.source == F.EARNINGS_SOURCE


def placed(day: Day) -> List[Tuple[str, str, int]]:
    """``(name, group, slot)`` of the day's releases as v2 places them (see the module docstring)."""
    out = []
    for rel in day.releases if day.releases_known else ():
        if _is_earnings(rel):
            if _AFTER <= rel.slot < day.end:
                out.append((rel.name, F.EARNINGS_GROUP, _ANCHOR))
        else:
            out.append((rel.name, rel.group, rel.slot))
    return out


def release_windows(day: Day) -> List[Tuple[str, str, int, int, int]]:
    """``(name, group, bucket, first slot, end slot)`` of every release window, clipped to [1, end)."""
    out = []
    for name, group, slot in placed(day):
        for b, (a, z) in enumerate(F.EVENT_GROUPS_V2[group]["buckets"]):
            lo, hi = max(1, slot + a), min(day.end, slot + z)
            if lo < hi:
                out.append((name, group, b, lo, hi))
    return out


def multipliers(S: np.ndarray, history: Sequence[Day]):
    """
    ``(groups, names, occurrences)``: per group v1's estimator over every earlier release of the group (shrunk
    towards its fallback), per release name its own earlier releases shrunk towards its group's - both with
    EVENT_PRIOR_RELEASES releases' weight, floored at 1.
    """
    groups = F.EVENT_GROUPS_V2
    gnum = {g: np.zeros(len(v["buckets"])) for g, v in groups.items()}
    gden = {g: np.zeros(len(v["buckets"])) for g, v in groups.items()}
    gocc = {g: 0 for g in groups}
    nnum, nden, nocc, ngroup = {}, {}, defaultdict(int), {}
    for d in history:
        if not d.releases_known:
            continue
        r2 = d.returns ** 2
        for name, g, b, lo, hi in release_windows(d):
            x = r2[lo:hi]
            ok = np.isfinite(x)
            num, den = float(x[ok].sum()), float(S[lo:hi][ok].sum())
            gnum[g][b] += num
            gden[g][b] += den
            if name not in nnum:
                nnum[name], nden[name], ngroup[name] = np.zeros(len(gnum[g])), np.zeros(len(gnum[g])), g
            nnum[name][b] += num
            nden[name][b] += den
        for name, g, slot in placed(d):
            if 1 <= slot < d.end:
                gocc[g] += 1
                nocc[name] += 1
    gm = {}
    for g, v in groups.items():
        fallback = np.array(v["fallback"], dtype=float)
        if not gocc[g]:
            gm[g] = fallback
            continue
        w = F.EVENT_PRIOR_RELEASES * gden[g] / gocc[g]
        with np.errstate(invalid="ignore", divide="ignore"):
            est = (gnum[g] + w * fallback) / (gden[g] + w)
        gm[g] = np.where(gden[g] > 0, np.maximum(1.0, est), fallback)
    nm = {}
    for name, g in ngroup.items():
        if not nocc[name]:
            continue
        w = F.EVENT_PRIOR_RELEASES * nden[name] / nocc[name]
        with np.errstate(invalid="ignore", divide="ignore"):
            est = (nnum[name] + w * gm[g]) / (nden[name] + w)
        nm[name] = (g, np.where(nden[name] > 0, np.maximum(1.0, est), gm[g]))
    return gm, nm, {**gocc, **{f"name:{k}": v for k, v in nocc.items()}}


def profile(day: Day, gm: Dict[str, np.ndarray], nm: Dict[str, Tuple[str, np.ndarray]]) -> np.ndarray:
    """E on the day's grid: 1, or the largest multiplier of the release windows covering a slot - the release's
    own when it has earlier releases, else its group's."""
    E = np.ones(DAY_SLOTS)
    for name, g, b, lo, hi in release_windows(day):
        m = nm[name][1][b] if name in nm else gm[g][b]
        E[lo:hi] = np.maximum(E[lo:hi], m)
    return E


def long_level(recent: Sequence[Day], S: np.ndarray, gm, nm) -> float:
    """v1's long level with v2's release profile."""
    rv = ev = 0.0
    for d in recent:
        r2 = d.returns ** 2
        ok = np.isfinite(r2)
        rv += r2[ok].sum()
        ev += (S * profile(d, gm, nm))[ok].sum()
    return fb._clip_level(rv / ev) if ev > 0 else 1.0


def fit(target: Day, history: Sequence[Day]) -> FanModelV2:
    """The model for ``target`` from the sessions of ``history`` strictly before it - v1's fit with v2's release
    windows, groups and per-name multipliers."""
    earlier = sorted((d for d in history if d.session_date < target.session_date and d.complete),
                     key=lambda d: d.session_date)
    full = [d for d in earlier if d.schedule == "full"]
    seas = full[-F.SEASONAL_SESSIONS:]
    if len(seas) < F.SEASONAL_MIN_SESSIONS:
        raise InsufficientHistory(f"{target.session_date}: {len(seas)} earlier complete full sessions, "
                                  f"{F.SEASONAL_MIN_SESSIONS} needed")
    S = fb.seasonal(seas, windows=release_windows)
    gm, nm, occ = multipliers(S, earlier[-F.EVENT_SESSIONS:])
    E = profile(target, gm, nm)
    level = long_level(full[-F.LEVEL_SESSIONS:], S, gm, nm)
    Sp = S * E
    active = Sp[1:target.end]
    prior = F.LEVEL_PRIOR_MINUTES * float(np.median(active[active > 0])) if (active > 0).any() else 0.0
    r2 = np.concatenate([d.returns[1:d.end] ** 2 for d in seas])
    flat = float(np.nanmean(r2)) if np.isfinite(r2).any() else 0.0
    return FanModelV2(target.session_date, target.end, S, E, gm, occ, level, prior, flat,
                      [d.session_date.isoformat() for d in seas], target.releases_known, nm)


# --------------------------------------------------------------------------
# The shape
# --------------------------------------------------------------------------

@dataclass
class Shape:
    """Standardised quantiles at TAU per horizon: ``q`` (len(horizons), SHAPE_LEVELS); ``sessions`` 0 = the normal."""
    horizons: np.ndarray
    q: np.ndarray
    sessions: int

    def at(self, minutes) -> np.ndarray:
        """The quantiles at ``minutes`` ahead (scalar or array), interpolated in log minutes, held beyond the grid."""
        m = np.atleast_1d(np.asarray(minutes, dtype=float))
        x = np.log(self.horizons.astype(float))
        pos = np.interp(np.log(np.clip(m, self.horizons[0], self.horizons[-1])), x, np.arange(len(x)))
        lo = np.floor(pos).astype(int)
        hi = np.minimum(lo + 1, len(x) - 1)
        w = (pos - lo)[:, None]
        out = (1 - w) * self.q[lo] + w * self.q[hi]
        return out[0] if np.ndim(minutes) == 0 else out


def normal_shape(horizons: Sequence[int] = F.SHAPE_HORIZONS) -> Shape:
    return Shape(np.array(horizons), np.tile(NORMAL_Q, (len(horizons), 1)), 0)


def errors(model: FanModel, day: Day, horizons: Sequence[int] = F.SHAPE_HORIZONS) -> Dict[int, np.ndarray]:
    """Per horizon, the session's standardised errors y / sigma at every origin with a price at both ends inside
    the trading day and a positive variance."""
    V = fb.horizon_variances(model, day.returns, horizons)["full"]
    lp = np.log(day.last_price)
    t = np.arange(DAY_SLOTS)
    out = {}
    for i, h in enumerate(horizons):
        ok = np.isfinite(lp) & (t + h < day.end)
        ok[ok] &= np.isfinite(lp[(t + h)[ok]])
        v = V[i]
        ok &= np.isfinite(v) & (np.nan_to_num(v) > MIN_VARIANCE)
        out[h] = (lp[t[ok] + h] - lp[t[ok]]) / np.sqrt(v[ok])
    return out


def shape_from(errs: Sequence[Dict[int, np.ndarray]], horizons: Sequence[int] = F.SHAPE_HORIZONS) -> Shape:
    """The symmetric empirical standardised quantiles of ``errs`` (one dict per session) - the normal's with fewer
    than SHAPE_MIN_SESSIONS sessions, and at a horizon with fewer than 100 errors."""
    if len(errs) < F.SHAPE_MIN_SESSIONS:
        return normal_shape(horizons)
    q = np.empty((len(horizons), F.SHAPE_LEVELS))
    for i, h in enumerate(horizons):
        pool = [e[h] for e in errs if len(e.get(h, ()))]
        pool = np.concatenate(pool) if pool else np.zeros(0)
        if len(pool) < 100:
            q[i] = NORMAL_Q
            continue
        x = np.quantile(pool, TAU)
        q[i] = (x - x[::-1]) / 2
    return Shape(np.array(horizons), q, len(errs))


# --------------------------------------------------------------------------
# Issuing and walking forward
# --------------------------------------------------------------------------

_QUANTILE_POS = np.interp(F.QUANTILES, TAU, np.arange(len(TAU)))


def fan_from(model: FanModel, shape: Shape, r: np.ndarray, t: int, price: float,
             minutes: Optional[int] = None) -> fb.Fan:
    """The fan from origin slot ``t`` (close ``price``): v1's sigma per minute ahead, the chart's quantiles
    (contracts/fan.QUANTILES) from the shape."""
    base = fb.fan_from(model, r, t, price, minutes)
    if not len(base.minutes):
        return base
    Q = shape.at(base.minutes)                                    # (H, SHAPE_LEVELS)
    lo = np.floor(_QUANTILE_POS).astype(int)
    hi = np.minimum(lo + 1, len(TAU) - 1)
    w = _QUANTILE_POS - lo
    q13 = (1 - w) * Q[:, lo] + w * Q[:, hi]
    base.prices = price * np.exp(base.sigma[:, None] * q13)
    return base


def walk_forward(days: Sequence[Day], first, last) -> Iterator[Tuple[Day, FanModelV2, Shape]]:
    """``(day, model, shape)`` of every complete full session from ``first`` to ``last``: each fitted on the
    sessions before it, its shape from the standardised errors of the SHAPE_SESSIONS sessions before it (each on
    its own walk-forward fit). The sessions before ``first`` are fitted too, for the shape only."""
    ordered = sorted(days, key=lambda d: d.session_date)
    errs: List[Dict[int, np.ndarray]] = []
    for i, d in enumerate(ordered):
        if d.session_date > last:
            break
        if not d.complete or d.schedule != "full":
            continue
        try:
            m = fit(d, ordered[:i])
        except InsufficientHistory:
            continue
        if d.session_date >= first:
            yield d, m, shape_from(errs[-F.SHAPE_SESSIONS:])
        errs.append(errors(m, d))
