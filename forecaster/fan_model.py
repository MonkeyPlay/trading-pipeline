# forecaster/fan_model.py
"""
The learned fan of the intermarket experiment (docs/fan_experiment.md, chunk 5):
fan_rw_v2 with its width scaled at every origin by a multiplier read from the features
(forecaster/fan_features.py). v2's median (zero drift) and its shape are kept - every
candidate here learns the size of moves, never their direction.

  GBMScale        per horizon, gradient boosting (scikit-learn's HistGradientBoosting:
                  missing values read as missing) of z^2 - the squared realised move in
                  v2's sigmas - with Poisson deviance and its log link; the multiplier is
                  sqrt(pred(x) / mean of pred over the training rows): over the training
                  rows the mean *squared* multiplier is 1 (before clipping) - v2's average
                  variance is kept, and so the average width multiplier is a little below 1
  LinearScale     the same target and normalisation from a regularised linear Poisson
                  model: numeric features standardised, missing values imputed with the
                  training median beside a missing flag, the minute of the day as origin
                  phases and the weekday as days
  CRPSBoost       gradient boosting of log(multiplier) on the manifest's CRPS itself (the
                  quantile form, K = 200, in the issued fan's units - so weighted by the
                  fan's sigma as the score is): trees of GBMScale's size fitted to the
                  negative gradient, each leaf's step found by an exact line search; it
                  learns the average width as well
  ConstantCRPS    one multiplier per horizon minimising the training rows' CRPS - the
                  constant-scale benchmark (no features)
  ShapeOOS        a base candidate with its own symmetric shape per horizon, estimated from
                  chronological out-of-sample residuals inside the training period
  CRPSKit         the exact CRPS of a scaled baseline fan and its derivative in the scale,
                  each row with its own session's shape, O(log K) a row

A horizon without its own model (the pre-open slice's 16, 31 and 61 minutes) uses the
nearest trained horizon in log minutes. Every multiplier is clipped to BOUNDS.

Every candidate's features are an explicit list of names (FEATURE_SETS), fixed when the
candidate was named - never a selector that would take in features added later - and a
run records the list and its hash (feature_hash). families_at() rebuilds an instrument's
features as they were at an earlier feature format, so the earlier trials can be run again
exactly as they were defined.

  MODELS                        name -> Spec: the candidates of the checks
  TRIALS                        name -> Spec: every earlier development trial that can be
                                rebuilt, for the multiple-comparison review (fan_search.py)
  candidate(name, table, frames)   a fresh-candidate factory (one per check)
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence

import numpy as np
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.linear_model import PoissonRegressor
from sklearn.tree import DecisionTreeRegressor

from config import Config
from forecaster import fan_experiment as fx
from forecaster import fan_harness as fh
from forecaster.fan_features import Feature, FeatureTable
from forecaster.fan_scoring import PHASE_OF_SLOT, PHASES

# Attempt 2 of 2026-10-06 (attempt 1: min_samples_leaf 2000, every feature at every split): a leaf needs about 15
# sessions' worth of 5-minute origins, and each split sees 30 % of the features.
SETTINGS = {"learning_rate": 0.05, "max_iter": 150, "max_depth": 3, "min_samples_leaf": 4000,
            "l2_regularization": 1.0, "max_bins": 64, "max_features": 0.3}
LINEAR_ALPHA = 1e-3              # LinearScale's L2 penalty on standardised features, fixed before it was run
BOUNDS = (0.5, 2.0)
# The target's own options-implied volatility index: its iv_rv is the cross-market quantity the _iv candidates add.
IMPLIED = {"NQ": "VXN", "ES": "VIX", "RTY": "VIX"}
# The target's own features of format 2 - what gbm_own read when it was named.
OWN_FAMILIES = ("rv5", "rv15", "rv60", "rv240", "ret15", "ret60", "chg", "vol60", "age", "day_rv")
INSTRUMENTS = tuple(s for members in fx.GROUPS.values() for s in members)


def families_at(symbol: str, fmt: int) -> List[str]:
    """An instrument's feature families at feature format ``fmt`` (forecaster/fan_features.py): 1 the chunk-4 set,
    2 with iv_rv, 3 with rv5d."""
    fams = ["rv5", "rv15", "rv60", "rv240", "ret15", "ret60", "chg"]
    if Config.instrument(symbol).sec_type != "IND":
        fams.append("vol60")
    fams += ["age", "day_rv"] + (["rv5d"] if fmt >= 3 else [])
    if symbol in fx.GROUPS["volatility"]:
        fams += ["level"] + (["iv_rv"] if fmt >= 2 else [])
    return fams


def _every(fmt: int, symbols=INSTRUMENTS) -> List[str]:
    return [f"{s}.{fam}" for s in symbols for fam in families_at(s, fmt)]


def _plus_group(group: str, fmt: int) -> Callable[[str], List[str]]:
    return lambda t: FEATURE_SETS["own"](t) + _every(fmt, [s for s in fx.GROUPS[group] if s != t])


FEATURE_SETS: Dict[str, Callable[[str], List[str]]] = {
    "own": lambda t: [f"{t}.{fam}" for fam in OWN_FAMILIES],
    "own_iv": lambda t: FEATURE_SETS["own"](t) + [f"{IMPLIED[t]}.iv_rv"],
    # variant E of the ablation: both sides of the implied-realised comparison beside their ratio
    "own_ivx": lambda t: FEATURE_SETS["own"](t) + [f"{t}.rv5d", f"{IMPLIED[t]}.level", f"{IMPLIED[t]}.iv_rv"],
    # the same information for a linear model: the ratio is a linear combination of the two logged sides
    "own_ivx_linear": lambda t: FEATURE_SETS["own"](t) + [f"{t}.rv5d", f"{IMPLIED[t]}.level"],
    # every instrument's features as they were at each format (gbm_all reads format 3's)
    "all_f1": lambda t: _every(1), "all_f2": lambda t: _every(2), "all_f3": lambda t: _every(3),
    # the earlier trials' sets (TRIALS)
    **{f"own_plus_{g}_f1": _plus_group(g, 1) for g in fx.GROUPS},
    "own_both_iv": lambda t: FEATURE_SETS["own"](t) + ["VXN.iv_rv", "VIX.iv_rv"],
    "own_volatility_f2": _plus_group("volatility", 2),
    "own_rv5d": lambda t: FEATURE_SETS["own"](t) + [f"{t}.rv5d"],
    "own_level": lambda t: FEATURE_SETS["own"](t) + [f"{IMPLIED[t]}.level"],
    "own_rv5d_level": lambda t: FEATURE_SETS["own"](t) + [f"{t}.rv5d", f"{IMPLIED[t]}.level"],
}


def feature_hash(names: Sequence[str]) -> str:
    """The hash of a candidate's feature list (column order included)."""
    return hashlib.sha256("\n".join(names).encode()).hexdigest()[:16]


def _nearest(trained: Sequence[int], h: int) -> int:
    t = np.array(sorted(trained))
    return int(t[np.argmin(np.abs(np.log(t) - np.log(h)))])


class _Features:
    """A candidate's explicit feature list on a table: base columns always, then ``names`` in the table's order."""

    def __init__(self, table: Optional[FeatureTable], names: Optional[Sequence[str]]) -> None:
        self.table = table
        self.names = None if names is None else set(names)
        if table is not None and names is not None:
            missing = sorted(self.names - {f.name for f in table.features})
            if missing:
                raise ValueError(f"the feature table has no {', '.join(missing)}")
        self.feature_names: List[str] = []

    def matrix(self, rows: fh.Rows):
        X, feats = self.table.matrix(rows, None if self.names is None else (lambda f: f.name in self.names))
        self.feature_names = [f.name for f in feats]
        return X, feats


# --------------------------------------------------------------------------
# Poisson models of z^2: gradient boosting and linear
# --------------------------------------------------------------------------

class GBMScale(fh.Candidate, _Features):
    """v2's width times sqrt(E[z^2 | x] / its training mean), E[z^2 | x] by Poisson gradient boosting per horizon."""

    def __init__(self, table: FeatureTable, features: Optional[Sequence[str]] = None, name: str = "gbm",
                 settings: Optional[Dict] = None, horizons=fh.REPORT_HORIZONS) -> None:
        _Features.__init__(self, table, features)
        self.name = name
        self.settings = dict(SETTINGS if settings is None else settings)
        self.horizons = tuple(horizons)
        self.models: Dict[int, HistGradientBoostingRegressor] = {}
        self.norm: Dict[int, float] = {}
        self.features: List[Feature] = []

    def fit(self, rows: fh.Rows) -> None:
        for h in self.horizons:
            sub = rows.take(rows.horizon == h)
            if not len(sub):
                continue
            X, self.features = self.matrix(sub)
            model = HistGradientBoostingRegressor(loss="poisson", early_stopping=False, random_state=0,
                                                  **self.settings)
            model.fit(X, sub.z ** 2)
            self.models[h] = model
            self.norm[h] = float(np.mean(model.predict(X)))
        if not self.models:
            raise ValueError("no training rows at the model's horizons")

    def predict(self, rows: fh.Rows) -> np.ndarray:
        out = np.ones(len(rows))
        for h in np.unique(rows.horizon):
            near = _nearest(self.models, h)
            sel = rows.horizon == h
            X, _ = self.matrix(rows.take(sel))
            out[sel] = np.clip(np.sqrt(self.models[near].predict(X) / self.norm[near]), *BOUNDS)
        return out


class LinearScale(fh.Candidate, _Features):
    """LinearScale (see the module docstring): GBMScale's target and normalisation from a linear Poisson model."""

    def __init__(self, table: FeatureTable, features: Sequence[str], name: str = "linear",
                 alpha: float = LINEAR_ALPHA, horizons=fh.REPORT_HORIZONS) -> None:
        _Features.__init__(self, table, features)
        self.name, self.alpha, self.horizons = name, alpha, tuple(horizons)
        self.models: Dict[int, Dict[str, Any]] = {}

    def _design(self, rows: fh.Rows, state: Dict[str, Any], fit: bool) -> np.ndarray:
        X, feats = self.matrix(rows)
        names = [f.name for f in feats]
        num = X[:, [i for i, n in enumerate(names) if n not in ("base.minute", "base.weekday")]].astype(float)
        if fit:
            state["median"] = np.nanmedian(np.where(np.isfinite(num), num, np.nan), axis=0)
            state["median"] = np.nan_to_num(state["median"])
            state["flag"] = np.flatnonzero((~np.isfinite(num)).any(axis=0))
        miss = ~np.isfinite(num)
        num = np.where(miss, state["median"], num)
        num = np.hstack([num, miss[:, state["flag"]].astype(float)])
        if fit:
            state["mu"], sd = num.mean(axis=0), num.std(axis=0)
            state["sd"] = np.where(sd > 0, sd, 1.0)
        num = (num - state["mu"]) / state["sd"]
        phase = PHASE_OF_SLOT[X[:, names.index("base.minute")].astype(int)]
        day = X[:, names.index("base.weekday")].astype(int)
        dummies = [(phase == p).astype(float) for p in range(1, len(PHASES))]
        dummies += [(day == d).astype(float) for d in range(1, 5)]
        if fit:
            kept = [n for n in names if n not in ("base.minute", "base.weekday")]
            state["columns"] = (kept + [f"missing:{kept[i]}" for i in state["flag"]]
                                + [f"phase:{PHASES[p][0]}" for p in range(1, len(PHASES))]
                                + [f"weekday:{d}" for d in range(1, 5)])
        return np.column_stack([num] + dummies)

    def describe(self) -> Dict[str, Any]:
        """The fitted state per horizon - everything predict() reads: the design's columns, the imputation medians,
        the standardisation, the coefficients (on standardised columns), the intercept and the normaliser."""
        out = {}
        for h, st in self.models.items():
            m = st["model"]
            out[str(h)] = {"columns": st["columns"], "intercept": float(m.intercept_),
                           "coef": [float(c) for c in m.coef_], "median": [float(x) for x in st["median"]],
                           "flag": [int(i) for i in st["flag"]], "mu": [float(x) for x in st["mu"]],
                           "sd": [float(x) for x in st["sd"]], "norm": st["norm"], "alpha": self.alpha}
        return out

    def fit(self, rows: fh.Rows) -> None:
        for h in self.horizons:
            sub = rows.take(rows.horizon == h)
            if not len(sub):
                continue
            state: Dict[str, Any] = {}
            D = self._design(sub, state, fit=True)
            model = PoissonRegressor(alpha=self.alpha, max_iter=1000).fit(D, sub.z ** 2)
            state["model"], state["norm"] = model, float(np.mean(model.predict(D)))
            self.models[h] = state
        if not self.models:
            raise ValueError("no training rows at the model's horizons")

    def predict(self, rows: fh.Rows) -> np.ndarray:
        out = np.ones(len(rows))
        for h in np.unique(rows.horizon):
            st = self.models[_nearest(self.models, h)]
            sel = rows.horizon == h
            pred = st["model"].predict(self._design(rows.take(sel), st, fit=False))
            out[sel] = np.clip(np.sqrt(pred / st["norm"]), *BOUNDS)
        return out


# --------------------------------------------------------------------------
# The CRPS itself: the exact score, models trained on it
# --------------------------------------------------------------------------

_K = len(fh.TAU)
_BIG = 1000.0                    # a standardised shape lies well inside +-_BIG / 2


@dataclass
class _Prepared:
    qid: np.ndarray              # each row's shape, an index into the arrays below
    flat: np.ndarray             # every shape's quantiles + qid x _BIG, one sorted array
    SQ: np.ndarray               # (shapes, K + 1): SQ[q, j] = the sum of shape q's quantiles from j on
    TQ: np.ndarray               # (shapes,): sum over k of tau_k x Q_k
    y: np.ndarray
    sb: np.ndarray               # the baseline's sigma


class CRPSKit:
    """
    The manifest's CRPS of the baseline fan scaled to sigma s - (2/K) sum_k rho_tau_k(y - s Q_k) with the row's own
    session shape Q - and its derivative in s, for many rows at once. With j the first quantile above y / s:

        CRPS(s) = (2/K) [y K/2 - s TQ - (K - j) y + s SQ_j],   dCRPS/ds = (2/K) (SQ_j - TQ)
    """

    def __init__(self, frames: Dict[str, fh.Frame]) -> None:
        self.frames = frames

    def prepare(self, rows: fh.Rows) -> _Prepared:
        keys = list(zip(rows.session.astype(str), rows.horizon.astype(int)))
        ids: Dict[tuple, int] = {}
        qid = np.array([ids.setdefault(k, len(ids)) for k in keys], dtype=np.int64)
        Q = np.stack([self.frames[s].shape[fh.FRAME_HORIZONS.index(h)] for s, h in ids])
        SQ = np.concatenate([np.cumsum(Q[:, ::-1], axis=1)[:, ::-1], np.zeros((len(Q), 1))], axis=1)
        flat = (Q + _BIG * np.arange(len(Q))[:, None]).ravel()
        return _Prepared(qid, flat, SQ, (Q * fh.TAU).sum(axis=1), rows.y.astype(float), np.sqrt(rows.var))

    def loss_grad(self, P: _Prepared, s: np.ndarray, idx: Optional[np.ndarray] = None):
        """``(CRPS, dCRPS/ds)`` per row at sigma ``s`` (rows ``idx`` of ``P``, all by default), log-price units."""
        q = P.qid if idx is None else P.qid[idx]
        y = P.y if idx is None else P.y[idx]
        z = np.clip(y / s, -_BIG / 2 + 1, _BIG / 2 - 1)
        j = np.clip(np.searchsorted(P.flat, z + _BIG * q, side="right") - q * _K, 0, _K)
        SQj, TQ = P.SQ[q, j], P.TQ[q]
        return (2.0 / _K) * (y * _K / 2 - s * TQ - (_K - j) * y + s * SQj), (2.0 / _K) * (SQj - TQ)

    def best_shift(self, P: _Prepared, f: np.ndarray, idx: Optional[np.ndarray] = None, lo: float = -1.5,
                   hi: float = 1.5, steps: int = 20) -> float:
        """The shift d of log(multiplier) minimising the rows' summed CRPS at sigma sb x exp(f + d): bisection on
        the derivative's sign - the sum is convex in exp(d), so the sign changes once."""
        sb = P.sb if idx is None else P.sb[idx]
        fi = f if idx is None else f[idx]

        def slope(d: float) -> float:
            s = sb * np.exp(fi + d)
            return float(np.sum(self.loss_grad(P, s, idx)[1] * s))

        if slope(lo) >= 0:
            return lo
        if slope(hi) <= 0:
            return hi
        for _ in range(steps):
            mid = (lo + hi) / 2
            lo, hi = (lo, mid) if slope(mid) > 0 else (mid, hi)
        return (lo + hi) / 2


class ConstantCRPS(fh.Candidate):
    """One multiplier per horizon minimising the training rows' CRPS (with each row's own shape): the constant-scale
    benchmark."""
    feature_names: List[str] = []

    def __init__(self, frames: Dict[str, fh.Frame], name: str = "crps_scale", horizons=fh.REPORT_HORIZONS) -> None:
        self.kit, self.name, self.horizons = CRPSKit(frames), name, tuple(horizons)
        self.scale: Dict[int, float] = {}

    def fit(self, rows: fh.Rows) -> None:
        for h in self.horizons:
            sub = rows.take(rows.horizon == h)
            if len(sub):
                P = self.kit.prepare(sub)
                self.scale[h] = float(np.exp(self.kit.best_shift(P, np.zeros(len(sub)))))

    def predict(self, rows: fh.Rows) -> np.ndarray:
        out = np.ones(len(rows))
        for h in np.unique(rows.horizon):
            out[rows.horizon == h] = self.scale[_nearest(self.scale, h)]
        return np.clip(out, *BOUNDS)


@dataclass
class _Boosted:
    f0: float
    trees: List[DecisionTreeRegressor] = field(default_factory=list)
    steps: List[np.ndarray] = field(default_factory=list)       # per tree, the step of each node (leaves only used)


class CRPSBoost(fh.Candidate, _Features):
    """CRPSBoost (see the module docstring): log(multiplier) boosted on the exact CRPS with GBMScale's tree size."""

    def __init__(self, table: FeatureTable, frames: Dict[str, fh.Frame], features: Optional[Sequence[str]],
                 name: str = "crps", settings: Optional[Dict] = None, horizons=fh.REPORT_HORIZONS) -> None:
        _Features.__init__(self, table, features)
        self.kit, self.name, self.horizons = CRPSKit(frames), name, tuple(horizons)
        s = dict(SETTINGS if settings is None else settings)
        self.rate, self.rounds = s["learning_rate"], s["max_iter"]
        self.tree = {"max_depth": s["max_depth"], "min_samples_leaf": s["min_samples_leaf"],
                     "max_features": s["max_features"]}
        self.models: Dict[int, _Boosted] = {}

    def fit(self, rows: fh.Rows) -> None:
        for h in self.horizons:
            sub = rows.take(rows.horizon == h)
            if not len(sub):
                continue
            X, _ = self.matrix(sub)
            P = self.kit.prepare(sub)
            f = np.zeros(len(sub))
            m = _Boosted(self.kit.best_shift(P, f))
            f += m.f0
            for it in range(self.rounds):
                s = P.sb * np.exp(f)
                g = self.kit.loss_grad(P, s)[1] * s                     # dCRPS / d log(multiplier)
                tree = DecisionTreeRegressor(random_state=it, **self.tree).fit(X, -g)
                leaf = tree.apply(X)
                step = np.zeros(tree.tree_.node_count)
                for node in np.unique(leaf):
                    idx = np.flatnonzero(leaf == node)
                    step[node] = self.kit.best_shift(P, f, idx, lo=-0.5, hi=0.5, steps=14)
                f += self.rate * step[leaf]
                m.trees.append(tree)
                m.steps.append(step)
            self.models[h] = m
        if not self.models:
            raise ValueError("no training rows at the model's horizons")

    def predict(self, rows: fh.Rows) -> np.ndarray:
        out = np.ones(len(rows))
        for h in np.unique(rows.horizon):
            m = self.models[_nearest(self.models, h)]
            sel = rows.horizon == h
            X, _ = self.matrix(rows.take(sel))
            f = np.full(int(sel.sum()), m.f0)
            for tree, step in zip(m.trees, m.steps):
                f += self.rate * step[tree.apply(X)]
            out[sel] = np.clip(np.exp(f), *BOUNDS)
        return out


class ShapeOOS(fh.Candidate):
    """
    ``make_base()``'s multiplier with its own symmetric shape per horizon: the training sessions from the
    ``start``-th on, in chronological blocks of ``block``, each predicted by a base fitted on the sessions before it
    only; the pooled out-of-sample residuals z / m of a horizon give its shape - symmetric empirical quantiles at TAU,
    as v2's - used in place of v2's shape (the baseline's where a horizon has fewer than MIN_RESIDUALS).
    """
    MIN_RESIDUALS = 2000

    def __init__(self, make_base: Callable[[], fh.Candidate], name: str, block: int = 30, start: int = 60) -> None:
        self.make_base, self.name, self.block, self.start = make_base, name, block, start
        self.shapes: Dict[int, np.ndarray] = {}
        self.residuals: Dict[int, int] = {}
        self.feature_names: List[str] = []

    def fit(self, rows: fh.Rows) -> None:
        days = np.unique(rows.session)
        pool: Dict[int, List[np.ndarray]] = {h: [] for h in fh.REPORT_HORIZONS}
        for b0 in range(self.start, len(days), self.block):
            before, block = days[:b0], days[b0:b0 + self.block]
            base = self.make_base()
            base.fit(rows.take(np.isin(rows.session, before)))
            r = rows.take(np.isin(rows.session, block) & np.isin(rows.horizon, fh.REPORT_HORIZONS))
            m = base.predict(r)
            for h in pool:
                s = r.horizon == h
                pool[h].append(r.z[s] / m[s])
        for h, parts in pool.items():
            e = np.concatenate(parts) if parts else np.zeros(0)
            self.residuals[h] = int(len(e))
            if len(e) >= self.MIN_RESIDUALS:
                x = np.quantile(e, fh.TAU)
                self.shapes[h] = (x - x[::-1]) / 2
        self.base = self.make_base()
        self.base.fit(rows)
        self.feature_names = list(getattr(self.base, "feature_names", []))

    def predict(self, rows: fh.Rows) -> np.ndarray:
        return self.base.predict(rows)

    def shape(self, h: int) -> Optional[np.ndarray]:
        return self.shapes.get(_nearest(self.shapes, h)) if self.shapes else None

    def describe(self) -> Dict[str, Any]:
        out = {"residuals": {str(h): n for h, n in self.residuals.items()},
               "shape": {str(h): [float(x) for x in q] for h, q in self.shapes.items()}}
        base = getattr(self.base, "describe", None)
        return {**out, "base": base()} if base else out


# --------------------------------------------------------------------------
# The candidates
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Spec:
    kind: str                    # gbm | linear | crps_boost | crps_scale | shape_oos
    features: Optional[str]      # a FEATURE_SETS key; None: no features (crps_scale)
    settings: Dict[str, Any] = field(default_factory=dict)
    base: Optional[str] = None   # shape_oos: the MODELS candidate whose multiplier it keeps
    note: str = ""               # where a trial came from (docs/fan_experiment.md)

    @property
    def needs_frames(self) -> bool:
        return self.kind in ("crps_boost", "crps_scale") or (self.kind == "shape_oos" and MODELS[self.base].needs_frames)


MODELS: Dict[str, Spec] = {
    "gbm_own": Spec("gbm", "own"),
    "gbm_own_iv": Spec("gbm", "own_iv"),
    "gbm_own_ivx": Spec("gbm", "own_ivx"),
    "gbm_all": Spec("gbm", "all_f3"),
    # the review round of 2026-10-06 (docs/fan_experiment.md): the same inputs as gbm_own_ivx in other model classes,
    # the CRPS as the training loss, and the constant-scale benchmark done right
    "gam_ivx": Spec("gbm", "own_ivx", {"interaction_cst": "no_interactions"}),
    "lin_pois_ivx": Spec("linear", "own_ivx_linear"),
    "crps_ivx": Spec("crps_boost", "own_ivx"),
    "crps_scale": Spec("crps_scale", None),
    # the third review round: the nominated linear candidate with its own out-of-sample shape
    "lin_pois_ivx_shape": Spec("shape_oos", "own_ivx_linear", base="lin_pois_ivx"),
}

_A1 = {"min_samples_leaf": 2000, "max_features": 1.0}          # attempt 1's settings
TRIALS: Dict[str, Spec] = {
    "trial_a1_gbm_own": Spec("gbm", "own", _A1, note="chunk 5, attempt 1"),
    "trial_a1_gbm_all_f1": Spec("gbm", "all_f1", _A1, note="chunk 5, attempt 1"),
    "trial_a2_gbm_all_f1": Spec("gbm", "all_f1", note="chunk 5, attempt 2"),
    "trial_a2b_gbm_all_f1": Spec("gbm", "all_f1", {"min_samples_leaf": 2000, "learning_rate": 0.03, "max_iter": 100},
                                 note="chunk 5, attempt 2b"),
    **{f"trial_add_{g}": Spec("gbm", f"own_plus_{g}_f1", note="research: own plus one group")
       for g in fx.GROUPS},
    "trial_own_both_iv": Spec("gbm", "own_both_iv", note="research: VXN and VIX iv_rv"),
    "trial_own_volatility_f2": Spec("gbm", "own_volatility_f2", note="research: own, the volatility group and iv_rv"),
    "trial_a3_gbm_all_f2": Spec("gbm", "all_f2", note="research: every feature with iv_rv (attempt 3)"),
    "trial_abl_b_rv5d": Spec("gbm", "own_rv5d", note="ablation B: the denominator"),
    "trial_abl_c_level": Spec("gbm", "own_level", note="ablation C: VXN's level"),
    "trial_abl_d_both_sides": Spec("gbm", "own_rv5d_level", note="ablation D: both sides, no ratio"),
}


def candidate(name: str, table: Optional[FeatureTable], frames: Optional[Dict[str, fh.Frame]] = None,
              settings: Optional[Dict] = None) -> Callable[[], fh.Candidate]:
    """A fresh-candidate factory for the checks (one per check); ``frames`` for the CRPS-trained ones."""
    spec = MODELS.get(name) or TRIALS.get(name)
    if spec is None:
        raise ValueError(f"unknown model {name!r} ({', '.join([*MODELS, *TRIALS])})")
    if spec.needs_frames and frames is None:
        raise ValueError(f"{name} trains on the CRPS and needs the baseline's frames")
    if spec.kind == "shape_oos":
        make_base = candidate(spec.base, table, frames, settings)
        return lambda: ShapeOOS(make_base, name)
    names = None if spec.features is None else FEATURE_SETS[spec.features](table.target)
    merged = {**SETTINGS, **spec.settings, **(settings or {})}
    if spec.kind == "gbm":
        _Features(table, names)                       # unknown names fail here, not in the first check
        return lambda: GBMScale(table, names, name, merged)
    if spec.kind == "linear":
        return lambda: LinearScale(table, names, name)
    if spec.kind == "crps_boost":
        return lambda: CRPSBoost(table, frames, names, name, merged)
    return lambda: ConstantCRPS(frames, name)
