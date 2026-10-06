# forecaster/fan_features.py
"""
The intermarket fan experiment's features (docs/fan_experiment.md, chunk 4): what a
candidate reads at an origin - from the point-in-time panel (forecaster/fan_panel.py)
and the baseline's rows (forecaster/fan_harness.py) - each tagged with its instrument
and group, so the attribution (chunk 6) can drop or add an instrument or a group.

Per instrument, at origin slot t of a session, from values known at t only:

  rv{w}    realised variance of its 1-minute log returns over the last w minutes over the
           usual for those minutes: log(ratio + 0.01) - moving more than usual for the time
           of day
  ret{w}   its log return over the last w minutes, in the usual sigma of those minutes
  chg      its log change since the previous session's regular close (16:00 ET, 13:00 on an
           early close), in the usual sigma of that change at this minute
  vol60    its volume over the last 60 minutes over the usual: log((v + 1) / (usual + 1)) -
           futures and stocks only (an index has no volume)
  age      log(1 + minutes since its last bar closed)
  day_rv   the previous session's whole realised variance over its usual: log(ratio + 0.01)
  rv5d     the log of its mean realised daily variance over the previous IV_RV_SESSIONS sessions
           (all of them with data, else missing) - the level iv_rv compares against
  level    the log of its value - the volatility indices only (VIX and VXN are implied
           volatilities; another instrument's price level says nothing about size)
  iv_rv    the volatility indices only: the daily variance its value implies, (value / 100)^2
           / 252, over the target's mean realised daily variance of the previous IV_RV_SESSIONS
           sessions (log ratio) - what the options market expects against what the target has
           done; the one cross-market quantity the target's own history cannot hold

"Usual": the mean over the previous NORM_SESSIONS full sessions in which the instrument has
data, NaN with fewer than NORM_MIN - a feature that needs history is missing until it has
enough. An instrument's features are missing before its first complete session (the
manifest's 'first_complete'); within a day a stale value is the panel's carried one with its
age beside it, never filled in. Missing is NaN: the model reads it as missing.

The base columns, in every model (group 'base'): base.log_sigma (the baseline's sigma to the
row's horizon), base.release_ahead, base.minute (the origin's slot), base.weekday.

Groups: the target's own instrument is 'own'; every other its manifest group
(forecaster/fan_experiment.GROUPS).

  Feature, catalogue(symbols, target)   the features and their tags
  build(panel, target, first_complete)  the table: sessions x 1440 slots x features (pure).
                                        The panel holds every open session in date order,
                                        so a session's previous one is the trading day before
  FeatureTable.matrix(rows, keep)       the rows' features: base columns, then those ``keep``
                                        selects
  audit(table, rows)                    per feature: coverage and rank correlation with |z|
  write_audit(...)                      docs/reports/fan_features_<experiment>_<target>.md
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, time
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np

from config import Config
from features import calendar as cal
from forecaster import fan_experiment as fx
from forecaster.fan_benchmark import DAY_SLOTS, slot_of_time
from forecaster.fan_panel import Panel

NORM_SESSIONS = 20
NORM_MIN = 10
RV_WINDOWS = (5, 15, 60, 240)
RET_WINDOWS = (15, 60)
VOLUME_WINDOW = 60
LEVEL_GROUPS = ("volatility",)
IV_RV_SESSIONS = 5            # the target's realised daily variance behind iv_rv
TRADING_DAYS = 252            # an implied volatility is annual: its daily variance is (value / 100)^2 / 252
FEATURE_FORMAT = 3            # bump when a feature's definition changes (2: iv_rv; 3: rv5d)
RATIO_FLOOR = 0.01            # log(ratio + 0.01): a minute without trades stays finite
BASE = ("base.log_sigma", "base.release_ahead", "base.minute", "base.weekday")
_CLOSE_SLOT = {"full": slot_of_time(time(16, 0)) - 1, "early_close": slot_of_time(time(13, 0)) - 1}


@dataclass(frozen=True)
class Feature:
    name: str                    # '<instrument>.<family>' or 'base.<what>'
    instrument: Optional[str]    # None for the base columns
    group: str                   # base | own | a manifest group
    family: str                  # rv15, ret60, chg, ...


def _families(symbol: str) -> List[str]:
    fams = [f"rv{w}" for w in RV_WINDOWS] + [f"ret{w}" for w in RET_WINDOWS] + ["chg"]
    inst = Config.instrument(symbol)
    if inst is None:
        raise ValueError(f"unknown instrument {symbol!r}")
    if inst.sec_type != "IND":
        fams.append(f"vol{VOLUME_WINDOW}")
    fams += ["age", "day_rv", "rv5d"]
    if _group(symbol) in LEVEL_GROUPS:
        fams += ["level", "iv_rv"]
    return fams


def _group(symbol: str) -> str:
    found = [g for g, members in fx.GROUPS.items() if symbol in members]
    if len(found) != 1:
        raise ValueError(f"{symbol} must belong to exactly one instrument group (forecaster/fan_experiment.GROUPS)")
    return found[0]


def catalogue(symbols: Sequence[str], target: str) -> List[Feature]:
    """The base columns, then every instrument's features in ``symbols`` order."""
    out = [Feature(n, None, "base", n.split(".", 1)[1]) for n in BASE]
    for s in symbols:
        g = "own" if s == target else _group(s)
        out += [Feature(f"{s}.{f}", s, g, f) for f in _families(s)]
    return out


# --------------------------------------------------------------------------
# Building the table (pure)
# --------------------------------------------------------------------------

def _usual(A: np.ndarray, src: np.ndarray, k: int = NORM_SESSIONS, kmin: int = NORM_MIN) -> np.ndarray:
    """Per session i, the mean of ``A`` (sessions first) over the last ``k`` sessions before i that ``src`` marks,
    value by value ignoring NaN; NaN where fewer than ``kmin`` contribute."""
    idx = np.flatnonzero(src)
    a = A[idx]
    ok = np.isfinite(a)
    zero = np.zeros((1,) + A.shape[1:])
    P = np.concatenate([zero, np.cumsum(np.where(ok, a, 0.0), axis=0)])
    N = np.concatenate([zero, np.cumsum(ok, axis=0)])
    J = np.searchsorted(idx, np.arange(len(A)))          # the sources before each session
    lo = np.maximum(0, J - k)
    n = N[J] - N[lo]
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(n >= kmin, (P[J] - P[lo]) / np.maximum(n, 1), np.nan)


def _log_ratio(x: np.ndarray, usual: np.ndarray) -> np.ndarray:
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(usual > 0, np.log(x / usual + RATIO_FLOOR), np.nan)


def _standardised(x: np.ndarray, usual: np.ndarray) -> np.ndarray:
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(usual > 0, x / np.sqrt(usual), np.nan)


def instrument_features(close: np.ndarray, age: np.ndarray, volume: np.ndarray, has: np.ndarray, full: np.ndarray,
                        close_slot: np.ndarray, families: Sequence[str],
                        target_day_rv: Optional[np.ndarray] = None) -> Dict[str, np.ndarray]:
    """One instrument's ``families`` (sessions x 1440 each) from its panel rows; ``has``: the sessions it has data
    for, ``full``: full-schedule sessions (the usual is read from those with data), ``close_slot``: each session's
    regular-close bar; ``target_day_rv``: per session the target's mean realised daily variance of the sessions
    before it (iv_rv)."""
    n = len(close)
    t = np.arange(DAY_SLOTS)
    with np.errstate(invalid="ignore", divide="ignore"):
        lp = np.log(close)
    r = np.zeros_like(lp)
    r[:, 1:] = lp[:, 1:] - lp[:, :-1]
    r = np.nan_to_num(r)                                   # no value yet, or no trade: no move
    C = np.concatenate([np.zeros((n, 1)), np.cumsum(r * r, axis=1)], axis=1)
    src = has & full
    out: Dict[str, np.ndarray] = {}
    windows = {int(f[2:]) for f in families if f.startswith("rv") and f[2:].isdigit()}
    windows |= {int(f[3:]) for f in families if f.startswith("ret") and f[3:].isdigit()}
    for w in sorted(windows):
        lo = np.maximum(0, t - w + 1)
        rv = C[:, t + 1] - C[:, lo]
        usual = _usual(rv, src)
        if f"rv{w}" in families:
            out[f"rv{w}"] = _log_ratio(rv, usual)
        if f"ret{w}" in families:
            out[f"ret{w}"] = _standardised(lp - lp[:, np.maximum(0, t - w)], usual)
    if "chg" in families:
        prev = np.full(n, np.nan)
        prev[1:] = lp[np.arange(n - 1), close_slot[:-1]]
        prev[1:][~has[:-1]] = np.nan
        d = lp - prev[:, None]
        out["chg"] = _standardised(d, _usual(d * d, src))
    vf = [f for f in families if f.startswith("vol")]
    if vf:
        w = int(vf[0][3:])
        CV = np.concatenate([np.zeros((n, 1)), np.cumsum(volume.astype(float), axis=1)], axis=1)
        v = CV[:, t + 1] - CV[:, np.maximum(0, t - w + 1)]
        usual = _usual(v, src)
        out[vf[0]] = np.where(np.isfinite(usual), np.log((v + 1) / (usual + 1)), np.nan)
    if "age" in families:
        out["age"] = np.log1p(age.astype(float))
    if "day_rv" in families:
        total = C[:, -1]
        ratio = _log_ratio(total, _usual(total, src))      # each session against the sessions before it
        day = np.full(n, np.nan)
        day[1:] = np.where(has[:-1], ratio[:-1], np.nan)   # known from the next session's first minute
        out["day_rv"] = np.repeat(day[:, None], DAY_SLOTS, axis=1)
    if "rv5d" in families:
        out["rv5d"] = np.repeat(np.log(_prior_days(C[:, -1], has))[:, None], DAY_SLOTS, axis=1)
    if "level" in families:
        out["level"] = lp.copy()
    if "iv_rv" in families and target_day_rv is not None:
        with np.errstate(invalid="ignore", divide="ignore"):
            out["iv_rv"] = np.log((close / 100) ** 2 / TRADING_DAYS / target_day_rv[:, None])
    for k in out:
        out[k] = np.where(has[:, None] & np.isfinite(lp), out[k], np.nan)
    return out


@dataclass
class FeatureTable:
    """Every instrument feature (catalogue order, base columns aside) at every slot of every session."""
    sessions: List[str]
    target: str
    features: List[Feature]      # the instrument features, in X's column order
    X: np.ndarray                # (sessions, 1440, len(features)) float32

    def session_index(self, sessions: np.ndarray) -> np.ndarray:
        """Each row's session as an index into ``sessions``; KeyError for one the table does not hold."""
        days = np.array(self.sessions, dtype="datetime64[D]")
        i = np.clip(np.searchsorted(days, sessions), 0, len(days) - 1)
        bad = days[i] != sessions
        if bad.any():
            raise KeyError(f"no features for session {sessions[bad][0]}")
        return i

    def matrix(self, rows, keep: Optional[Callable[[Feature], bool]] = None) -> Tuple[np.ndarray, List[Feature]]:
        """``(X, features)`` for ``rows`` (forecaster/fan_harness.Rows): the base columns, then the instrument
        features ``keep`` selects (all by default) - float32, NaN where missing."""
        cols = [j for j, f in enumerate(self.features) if keep is None or keep(f)]
        base = catalogue([], self.target)
        with np.errstate(divide="ignore"):
            log_sigma = 0.5 * np.log(rows.var)
        weekday = (rows.session.astype("datetime64[D]").astype(np.int64) + 3) % 7      # Monday 0
        B = np.column_stack([log_sigma, rows.ahead.astype(float), rows.slot.astype(float), weekday.astype(float)])
        X = self.X[self.session_index(rows.session), rows.slot][:, cols]
        return np.hstack([B.astype(np.float32), X]), base + [self.features[j] for j in cols]


def _prior_days(day: np.ndarray, has: np.ndarray, k: int = IV_RV_SESSIONS) -> np.ndarray:
    """Per session, the mean of ``day`` over the ``k`` sessions before it - NaN unless all ``k`` have data."""
    x = np.where(has, day, np.nan)
    out = np.full(len(x), np.nan)
    for i in range(k, len(x)):
        w = x[i - k:i]
        if np.isfinite(w).all():
            out[i] = w.mean()
    return out


def target_day_rv(panel: Panel, target: str, k: int = IV_RV_SESSIONS) -> Optional[np.ndarray]:
    """Per session, the mean over the ``k`` sessions before it of the target's realised daily variance (the sum of its
    squared 1-minute log returns over the day's grid); NaN for the first ``k``. None without the target."""
    if target not in panel.symbols:
        return None
    with np.errstate(invalid="ignore", divide="ignore"):
        lp = np.log(panel.close[:, panel.symbols.index(target)])
    r = np.zeros_like(lp)
    r[:, 1:] = lp[:, 1:] - lp[:, :-1]
    day = (np.nan_to_num(r) ** 2).sum(axis=1)
    out = np.full(len(day), np.nan)
    for i in range(k, len(day)):
        out[i] = day[i - k:i].mean()
    return out


def build(panel: Panel, target: str, first_complete: Dict[str, Optional[str]]) -> FeatureTable:
    """The feature table of ``panel`` (every open session in date order) for ``target``; an instrument's features
    are missing before its ``first_complete`` session."""
    sessions = panel.sessions
    schedule = [cal.session(date.fromisoformat(d)).schedule for d in sessions]
    full = np.array([s == "full" for s in schedule])
    close_slot = np.array([_CLOSE_SLOT.get(s, _CLOSE_SLOT["full"]) for s in schedule])
    specs = [f for f in catalogue(panel.symbols, target) if f.instrument is not None]
    col = {f.name: i for i, f in enumerate(specs)}
    X = np.full((len(sessions), DAY_SLOTS, len(specs)), np.nan, dtype=np.float32)
    day_rv = target_day_rv(panel, target)
    for j, sym in enumerate(panel.symbols):
        start = first_complete.get(sym)
        has = np.array([start is not None and d >= start for d in sessions]) & np.isfinite(panel.close[:, j]).any(axis=1)
        feats = instrument_features(panel.close[:, j], panel.age[:, j], panel.volume[:, j], has, full, close_slot,
                                    _families(sym), day_rv)
        for fam, arr in feats.items():
            X[:, :, col[f"{sym}.{fam}"]] = arr
    return FeatureTable(list(sessions), target, specs, X)


# --------------------------------------------------------------------------
# The audit
# --------------------------------------------------------------------------

def _rank(x: np.ndarray) -> np.ndarray:
    order = np.argsort(x, kind="stable")
    r = np.empty(len(x))
    r[order] = np.arange(len(x))
    # ties share their mean rank
    xs = x[order]
    edges = np.flatnonzero(np.diff(xs)) + 1
    for a, b in zip(np.r_[0, edges], np.r_[edges, len(x)]):
        if b - a > 1:
            r[order[a:b]] = (a + b - 1) / 2
    return r


def spearman(x: np.ndarray, y: np.ndarray) -> Optional[float]:
    ok = np.isfinite(x) & np.isfinite(y)
    if ok.sum() < 30 or np.ptp(x[ok]) == 0:
        return None
    return float(np.corrcoef(_rank(x[ok]), _rank(y[ok]))[0, 1])


def audit(table: FeatureTable, rows) -> List[Dict[str, Any]]:
    """Per feature over ``rows``: its share of rows with a value, and its rank correlation with |z| - the size of the
    realised move in the baseline's sigmas (positive: the baseline is too narrow when the feature is high)."""
    X, feats = table.matrix(rows)
    z = np.abs(rows.z)
    return [{"name": f.name, "instrument": f.instrument, "group": f.group, "family": f.family,
             "coverage": float(np.isfinite(X[:, i]).mean()), "spearman": spearman(X[:, i].astype(float), z)}
            for i, f in enumerate(feats)]


def write_audit(rows: List[Dict[str, Any]], meta: Dict[str, Any], report_dir: str) -> str:
    """The feature audit as docs/reports/fan_features_<experiment>_<target>.md; returns its path."""
    os.makedirs(report_dir, exist_ok=True)
    path = os.path.join(report_dir, f"fan_features_{meta['experiment']}_{meta['target']}.md")
    rho = lambda r: "-" if r["spearman"] is None else f"{r['spearman']:+.3f}"
    L = [f"# Features: {meta['experiment']}, target {meta['target']}", "",
         f"{len(rows)} features (forecaster/fan_features.py, format {meta['format']}); code {meta['code_revision'][:12]}. "
         f"Measured on {meta['sessions']} development sessions before the first check ({meta['first']} to "
         f"{meta['last']}), origins every {meta['every']} minutes, {meta['horizon']} minutes ahead: "
         f"{meta['rows']:,} rows.", "",
         "**Coverage:** the share of rows with a value. **Rank correlation** (Spearman) with |z|, the realised move "
         "in the baseline's sigmas: positive means the baseline is too narrow when the feature is high. A "
         "univariate screen, not a model - a feature can matter only together with others.", "",
         "## By group", "",
         "| Group | Features | Median coverage | Strongest | Its rank correlation |", "|---|---|---|---|---|"]
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for r in rows:
        groups.setdefault(r["group"], []).append(r)
    for g, rs in groups.items():
        best = max((r for r in rs if r["spearman"] is not None), key=lambda r: abs(r["spearman"]), default=None)
        L.append(f"| {g} | {len(rs)} | {100 * float(np.median([r['coverage'] for r in rs])):.0f} % | "
                 f"{best['name'] if best else '-'} | {rho(best) if best else '-'} |")
    L += ["", "## Every feature", "", "| Feature | Group | Coverage | Rank correlation with abs(z) |",
          "|---|---|---|---|"]
    for r in rows:
        L.append(f"| {r['name']} | {r['group']} | {100 * r['coverage']:.1f} % | {rho(r)} |")
    L.append("")
    with open(path, "w") as fh:
        fh.write("\n".join(L))
    return path
