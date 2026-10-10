# forecaster/ml_bundle.py
"""
The seven-target bundles (contracts/nq_ml_bundle.py): one artifact per arm, a head per target.

  fit_bundle(data, arm, train_dates)   every head of the arm on the training dates only: its rows
                                       are the target's classifiable labels of the arm's training
                                       population (NQ for N and M; NQ, ES and RTY for P), its
                                       family and parameters chosen inside chronological inner
                                       folds by session date on NQ's validation rows
                                       (forecaster/ml_split.py), refitted on the whole window
  Head.predict(X, C)                   the class probabilities in the target's canonical order:
                                       the estimator's, every class of the fixed vocabulary
                                       present, smoothed by the declared pseudo-counts; first
                                       level: candidates the label contract rules out at 0
  exact(p, classes)                    the probabilities as exact fractions over 1,000,000 summing
                                       to 1 (the largest class takes the rounding remainder; a
                                       structural zero stays 0)
  save / load / manifest               data/models/nq_ml/<version>/bundle.joblib and manifest.json:
                                       targets, classes, label / feature / market-label versions,
                                       per head its status, family, parameters, preprocessing,
                                       calibration, smoothing, training dates and counts, unseen
                                       classes and selection scores, the data and fold digests,
                                       code and software versions, the artifact's sha256; load
                                       refuses bytes that differ from the manifest or the
                                       registration, and targets or classes that are not the
                                       contract's

A head that cannot be trained (too few labelled sessions, a single class, for P too few ES / RTY rows)
is stored unavailable with its reason; the other heads are unaffected.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from contracts import market_labels as mlab
from contracts import nq_ml_bundle as mb
from contracts import nq_prompt_v2 as defs
from forecaster import ml_model as mm
from forecaster import ml_split as sp

MIN_TRAIN_SESSIONS = 40            # a head needs this many labelled NQ training sessions


class BundleError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# Estimators: every family returns probabilities over the head's classes in canonical order
# --------------------------------------------------------------------------

IDENTITY_SCALE = 10.0              # the instrument intercepts enter x10: an effective L2 penalty 1/100 of a feature's
SCALE_OFFSET = 0.1                 # the scale family models log(|z| + 0.1), z the move in threshold units
POOLABLE = ("logit", "decomp", "decomp_sym", "scale", "scale_loc", "cand")
INTERACTIONS = ("ret_30_atr2m", "ret_pm", "range_pos")       # the candidate scorer's side x path terms


class Prep:
    """Median imputation and standard scaling fitted on the training rows only (an all-missing column: 0)."""

    def fit(self, A: np.ndarray) -> "Prep":
        with np.errstate(all="ignore"):
            import warnings
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", RuntimeWarning)
                med = np.nanmedian(A, axis=0) if len(A) else np.zeros(A.shape[1])
        self.med = np.where(np.isnan(med), 0.0, med)
        F = np.where(np.isnan(A), self.med, A)
        self.mu = F.mean(axis=0) if len(F) else np.zeros(A.shape[1])
        sd = F.std(axis=0) if len(F) else np.ones(A.shape[1])
        self.sd = np.where(sd > 0, sd, 1.0)
        return self

    def transform(self, A: np.ndarray) -> np.ndarray:
        return (np.where(np.isnan(A), self.med, A) - self.mu) / self.sd


def _base(X: pd.DataFrame, columns: Sequence[str]) -> np.ndarray:
    return X.reindex(columns=list(columns)).to_numpy(float)


def _pool(Z: np.ndarray, instruments: Optional[Sequence[str]], gamma: float, pooled: bool) -> np.ndarray:
    """The pooled design (contracts/nq_ml_bundle.POOLING): shared columns Z, NQ deviations gamma x Z x is_nq (none at
    gamma 0: complete pooling), and the instrument intercepts. Not pooled: Z."""
    if not pooled:
        return Z
    inst = np.asarray(instruments if instruments is not None else ["NQ"] * len(Z))
    parts = [Z]
    if gamma > 0:
        parts.append(gamma * Z * (inst == "NQ").astype(float)[:, None])
    parts += [IDENTITY_SCALE * (inst == s).astype(float)[:, None] for s in ("ES", "RTY")]
    return np.hstack(parts)


class _Head:
    """A family on the head's columns: ``fit(X, y, instruments, moves)``, ``predict(X)`` -> probabilities over the
    head's classes in canonical order (before the structural zeros, calibration and smoothing of ``finish``)."""

    def __init__(self, family: str, params: Dict[str, Any], columns: Sequence[str], classes: Sequence[str],
                 pooled: bool):
        self.family, self.params, self.columns, self.classes = family, dict(params), list(columns), tuple(classes)
        self.pooled = pooled
        self.gamma = float(params.get("gamma", 0.0)) if pooled else 0.0

    def _design(self, X, instruments, fit: bool) -> np.ndarray:
        A = _base(X, self.columns)
        if fit:
            self.prep = Prep().fit(A)
        return _pool(self.prep.transform(A), instruments, self.gamma, self.pooled)

    def _full(self, model, raw: np.ndarray) -> np.ndarray:
        out = np.zeros((raw.shape[0], len(self.classes)))
        for j, c in enumerate(model.classes_):
            out[:, self.classes.index(c)] = raw[:, j]
        return out


class LogitHead(_Head):
    """Multinomial logistic regression (L2) - partially pooled for P (gamma)."""

    def fit(self, X, y, instruments=None, moves=None):
        from sklearn.linear_model import LogisticRegression
        self.model = LogisticRegression(C=self.params["C"], max_iter=3000).fit(self._design(X, instruments, True),
                                                                              np.asarray(y))
        return self

    def predict(self, X):
        return self._full(self.model, self.model.predict_proba(self._design(X, None, False)))


class GbmHead(_Head):
    """Shallow gradient boosting on median-imputed features; for P complete pooling, the identity a feature."""

    def fit(self, X, y, instruments=None, moves=None):
        from sklearn.ensemble import HistGradientBoostingClassifier
        from sklearn.impute import SimpleImputer
        from sklearn.pipeline import Pipeline
        self.model = Pipeline([("impute", SimpleImputer(strategy="median", keep_empty_features=True)),
                               ("model", HistGradientBoostingClassifier(
                                   max_depth=2, min_samples_leaf=20, l2_regularization=1.0, learning_rate=0.05,
                                   max_iter=self.params["max_iter"], early_stopping=False, random_state=0))])
        self.model.fit(self._raw(X, instruments), np.asarray(y))
        return self

    def _raw(self, X, instruments):
        A = _base(X, self.columns)
        if self.pooled:
            inst = np.asarray(instruments if instruments is not None else ["NQ"] * len(X))
            A = np.column_stack([A] + [(inst == s).astype(float) for s in ("ES", "RTY")])
        return A

    def predict(self, X):
        return self._full(self.model, self.model.predict_proba(self._raw(X, None)))


class _Binary:
    """A binary logistic fit, or the training frequency when only one outcome occurred: ``self(D) -> P(1)``."""

    def __init__(self, C: float, D: np.ndarray, t: np.ndarray):
        from sklearn.linear_model import LogisticRegression
        t = np.asarray(t, dtype=bool)
        self.model, self.rate = None, (float(t.mean()) if len(t) else 0.5)
        if len(set(t.tolist())) == 2:
            self.model = LogisticRegression(C=C, max_iter=3000).fit(D, t.astype(int))

    def __call__(self, A: np.ndarray) -> np.ndarray:
        if self.model is None:
            return np.full(len(A), self.rate)
        return self.model.predict_proba(A)[:, list(self.model.classes_).index(1)]


def _binary(C: float, D: np.ndarray, t: np.ndarray) -> _Binary:
    return _Binary(C, D, t)


class DecompHead(_Head):
    """Direction targets: q = P(outside the band), r = P(up | outside) - r = 0.5 for the symmetric version."""

    def fit(self, X, y, instruments=None, moves=None):
        y = np.asarray(y)
        D = self._design(X, instruments, True)
        out = y != "neutral_band"
        self.q = _binary(self.params["C"], D, out)
        self.symmetric = self.family == "decomp_sym"
        self.r = None if self.symmetric else _binary(self.params["C"], D[out], (y[out] == "bullish"))
        return self

    def predict(self, X):
        D = self._design(X, None, False)
        q = self.q(D)
        r = np.full(len(D), 0.5) if self.symmetric else self.r(D)
        p = {"bullish": q * r, "bearish": q * (1 - r), "neutral_band": 1 - q}
        return np.column_stack([p[c] for c in self.classes])


class ScaleHead(_Head):
    """Direction targets: a conditional distribution of the move z in threshold units - a scale s(x) from a ridge
    regression of log(|z| + 0.1), the standardised training moves z / s(x) as an empirical distribution (symmetrised;
    scale_loc adds a ridge location and keeps the residuals' asymmetry); the classes are its mass beyond +1 / s(x),
    below -1 / s(x) and between - the target's exact threshold, no tail shape assumed."""

    def fit(self, X, y, instruments=None, moves=None):
        from sklearn.linear_model import Ridge
        z = np.asarray(moves, float)
        keep = np.isfinite(z)
        if keep.sum() < 20:
            raise ValueError(f"{int(keep.sum())} measured moves: too few for the scale model")
        D = self._design(X, instruments, True)[keep]
        z = z[keep]
        self.scale = Ridge(alpha=self.params["alpha"]).fit(D, np.log(np.abs(z) + SCALE_OFFSET))
        s = np.exp(self.scale.predict(D))
        u = z / s
        self.location = self.family == "scale_loc"
        if self.location:
            self.loc = Ridge(alpha=self.params["alpha"]).fit(D, u)
            self.resid = np.sort(u - self.loc.predict(D))
        else:
            self.resid = np.sort(np.concatenate([u, -u]))
        return self

    def predict(self, X):
        D = self._design(X, None, False)
        s = np.exp(self.scale.predict(D))
        m = self.loc.predict(D) if self.location else np.zeros(len(D))
        n = len(self.resid)
        up = 1 - np.searchsorted(self.resid, 1 / s - m, side="right") / n          # P(e > 1/s - m)
        down = np.searchsorted(self.resid, -1 / s - m, side="left") / n            # P(e < -1/s - m)
        p = {"bullish": up, "bearish": down, "neutral_band": np.clip(1 - up - down, 0, 1)}
        return np.column_stack([p[c] for c in self.classes])


class CandHead(_Head):
    """First level: a conditional-logit candidate scorer. Each frozen candidate's score is its geometry (shared by
    every candidate), its side times the recent path, and its identity; softmax over the session's possible
    candidates; L2 on every coefficient. Partially pooled for P like the logistic family."""

    FEATS = ("dist_atr", "abs_dist_atr", "above", "nearest_side", "rank_side", "coincident")

    def _tensor(self, X):
        n, K = len(X), len(self.classes)
        F = np.zeros((n, K, len(self.FEATS) + len(INTERACTIONS)))
        for k, c in enumerate(self.classes):
            for j, f in enumerate(self.FEATS):
                F[:, k, j] = X.get(f"c_{c}_{f}", pd.Series(np.nan, index=X.index)).to_numpy(float)
            above = F[:, k, self.FEATS.index("above")]
            for j, f in enumerate(INTERACTIONS):
                F[:, k, len(self.FEATS) + j] = above * X.get(f, pd.Series(np.nan, index=X.index)).to_numpy(float)
        return F

    def _scaled(self, X, instruments, fit: bool):
        F = self._tensor(X)
        allowed = possible_mask("first_level_tested", X)
        if fit:
            flat = F[allowed]
            self.prep = Prep().fit(flat)
        Z = self.prep.transform(F.reshape(-1, F.shape[2])).reshape(F.shape)
        Z = np.where(allowed[:, :, None], Z, 0.0)
        if self.pooled and self.gamma > 0:
            inst = np.asarray(instruments if instruments is not None else ["NQ"] * len(X))
            nq = (inst == "NQ").astype(float)[:, None, None]
            Z = np.concatenate([Z, self.gamma * Z * nq], axis=2)
        return Z, allowed

    def fit(self, X, y, instruments=None, moves=None):
        from scipy.optimize import minimize
        Z, allowed = self._scaled(X, instruments, True)
        n, K, d = Z.shape
        Y = np.array([[1.0 if c == lab else 0.0 for c in self.classes] for lab in y])
        if (Y * ~allowed).sum() > 0:
            raise ValueError("a realised first level the label contract rules out")
        lam = self.params["l2"]

        def loss(theta):
            w, b = theta[:d], theta[d:]
            S = Z @ w + b[None, :]
            S = np.where(allowed, S, -np.inf)
            mx = S.max(axis=1, keepdims=True)
            E = np.where(allowed, np.exp(S - mx), 0.0)
            tot = E.sum(axis=1, keepdims=True)
            P = E / tot
            ll = -(np.log(tot[:, 0]) + mx[:, 0] - (np.where(allowed, S, 0) * Y).sum(axis=1)).sum()
            G = P - Y
            gw = np.einsum("nk,nkd->d", G, Z)
            gb = G.sum(axis=0)
            return -ll + 0.5 * lam * theta @ theta, np.concatenate([gw, gb]) + lam * theta
        res = minimize(loss, np.zeros(d + K), jac=True, method="L-BFGS-B", options={"maxiter": 500})
        self.theta, self.d = res.x, d
        return self

    def predict(self, X):
        Z, allowed = self._scaled(X, None, False)
        w, b = self.theta[:self.d], self.theta[self.d:]
        S = np.where(allowed, Z @ w + b[None, :], -np.inf)
        E = np.where(allowed, np.exp(S - S.max(axis=1, keepdims=True)), 0.0)
        return E / E.sum(axis=1, keepdims=True)


FAMILY_CLASSES = {"logit": LogitHead, "gbm": GbmHead, "decomp": DecompHead, "decomp_sym": DecompHead,
                  "scale": ScaleHead, "scale_loc": ScaleHead, "cand": CandHead}


def make(family: str, params: Dict[str, Any], target: str, arm: str, pooled: Optional[bool] = None):
    cols = mb.head_features(target, arm)
    return FAMILY_CLASSES[family](family, params, cols, mb.CLASSES[target], arm == "P" if pooled is None else pooled)


def configurations(target: str, arm: str, families: Optional[Sequence[str]] = None,
                   gammas: Optional[Sequence[float]] = None) -> List[Tuple[str, Dict[str, Any]]]:
    """The declared selection budget of a head (contracts/nq_ml_bundle.FAMILIES in FAMILY_ORDER; for P the logistic-
    type families at every pooling strength of POOLING['gamma_grid']). ``families`` / ``gammas`` restrict it (the
    evaluation's comparators); a comparator-only family (decomp_sym) is in it only when asked for."""
    kind = mb.HEADS[target]["kind"]
    out = []
    for fam in mb.FAMILY_ORDER:
        spec = mb.FAMILIES.get(fam)
        if spec is None or kind not in spec["kinds"] or fam not in FAMILY_CLASSES:
            continue
        if families is None and spec.get("comparator"):
            continue
        if families is not None and fam not in families:
            continue
        for p in spec["grid"]:
            if arm == "P" and fam in POOLABLE:
                for g in (gammas if gammas is not None else mb.POOLING["gamma_grid"]):
                    out.append((fam, {**p, "gamma": g}))
            else:
                out.append((fam, dict(p)))
    return out


# --------------------------------------------------------------------------
# Post-processing: the fixed vocabulary, structural zeros, smoothing
# --------------------------------------------------------------------------

def possible_mask(target: str, X: pd.DataFrame) -> np.ndarray:
    """Per row and class, whether the label contract allows the class (first level: the candidate is valid and first
    in the precedence among those at its price; every other target: every class)."""
    K = len(mb.CLASSES[target])
    if target != "first_level_tested":
        return np.ones((len(X), K), dtype=bool)
    cols = [f"c_{c}_possible" for c in mb.CLASSES[target]]
    m = X.reindex(columns=cols).to_numpy(float) > 0.5
    m[~m.any(axis=1)] = True                       # no candidate table: nothing ruled out (the service refuses it)
    return m


def temper(P: np.ndarray, tau: float) -> np.ndarray:
    """The temperature calibration p^(1/tau), renormalised (tau 1: unchanged)."""
    if tau == 1.0:
        return P
    Q = np.power(np.clip(P, 0, None), 1.0 / tau)
    s = Q.sum(axis=1, keepdims=True)
    return np.where(s > 0, Q / np.where(s > 0, s, 1), P)


def finish(target: str, P: np.ndarray, X: pd.DataFrame, n: int, tau: float = 1.0) -> np.ndarray:
    """The temperature (when calibrated), structural zeros, renormalisation and the Jeffreys smoothing
    (contracts/nq_ml_bundle.CALIBRATION, SMOOTHING)."""
    allowed = possible_mask(target, X)
    P = np.where(allowed, np.clip(temper(P, tau), 0, None), 0.0)
    s = P.sum(axis=1, keepdims=True)
    k = allowed.sum(axis=1, keepdims=True)
    P = np.where(s > 0, P / np.where(s > 0, s, 1), allowed / k)
    a = mb.SMOOTHING["pseudo_count"]
    return np.where(allowed, (n * P + a) / (n + a * k), 0.0)


def brier(P: np.ndarray, y: Sequence[str], classes: Sequence[str]) -> np.ndarray:
    onehot = np.array([[1.0 if c == lab else 0.0 for c in classes] for lab in y])
    return ((P - onehot) ** 2).sum(axis=1)


def exact(probs: Sequence[float], classes: Sequence[str]) -> Dict[str, str]:
    """``{class: "n/1000000"}`` summing exactly to 1, without ever breaking a tie or creating a class: every class gets
    floor(p x 1,000,000) and the remaining units go one each to the classes with the largest fractional parts - a
    group of exactly equal probabilities only as a whole (equal probabilities keep equal fractions, so an exact tie
    stays an ambiguous prediction); what no whole group can take goes to the smallest non-zero classes outside the
    top. Every possible class tied: exactly 1/k each. An exact zero (a class the label contract rules out) stays
    zero."""
    d = mb.PROBABILITY_DENOMINATOR
    p = np.clip(np.asarray(probs, dtype=float), 0, None)
    p = p / p.sum()
    positive = [j for j in range(len(p)) if p[j] > 0]
    if len({float(p[j]) for j in positive}) == 1:              # every possible class tied: exactly 1/k each
        return {c: (f"1/{len(positive)}" if p[j] > 0 else f"0/{d}") for j, c in enumerate(classes)}
    units = [int(math.floor(x * d)) for x in p]
    rem = d - sum(units)
    groups: Dict[float, List[int]] = {}
    for j, x in enumerate(p):
        if x > 0:
            groups.setdefault(float(x), []).append(j)
    for value in sorted(groups, key=lambda v: (-(v * d - math.floor(v * d)), -v)):
        if rem >= len(groups[value]):
            for j in groups[value]:
                units[j] += 1
            rem -= len(groups[value])
    if rem:
        top = groups[max(groups)]
        rest = sorted((j for j in positive if j not in top), key=lambda j: (p[j], j))
        for k in range(rem):
            units[rest[k % len(rest)]] += 1
    return {c: f"{u}/{d}" for c, u in zip(classes, units)}


# --------------------------------------------------------------------------
# Heads
# --------------------------------------------------------------------------

@dataclass
class Head:
    target: str
    arm: str
    status: str                                     # trained | unavailable
    reason: Optional[str] = None
    family: Optional[str] = None
    params: Dict[str, Any] = field(default_factory=dict)
    estimator: Any = None
    n: int = 0                                      # labelled NQ training sessions (the smoothing's n)
    rows: Dict[str, int] = field(default_factory=dict)
    class_counts: Dict[str, int] = field(default_factory=dict)
    unseen: List[str] = field(default_factory=list)
    selection: List[Dict[str, Any]] = field(default_factory=list)
    folds: List[Dict[str, Any]] = field(default_factory=list)
    calibration: Dict[str, Any] = field(default_factory=lambda: {"chosen": "none"})
    training: Dict[str, Any] = field(default_factory=dict)

    def predict(self, X: pd.DataFrame) -> np.ndarray:
        if self.status != "trained":
            raise BundleError(f"{self.target}: head unavailable ({self.reason})")
        return finish(self.target, self.estimator.predict(X), X, self.n, float(self.calibration.get("tau", 1.0)))

    def describe(self) -> Dict[str, Any]:
        cols = getattr(self.estimator, "columns", None)
        return {"target": self.target, "status": self.status, "reason": self.reason, "family": self.family,
                "params": self.params, "estimator": None if self.family is None else mb.FAMILIES[self.family][
                    "estimator"], "columns": cols, "classes": list(mb.CLASSES[self.target]),
                "preprocessing": "median imputation (and standard scaling for every family but the boosted one), "
                                 "fitted on the head's training rows inside the estimator",
                "pooled": bool(getattr(self.estimator, "pooled", False)),
                "gamma": getattr(self.estimator, "gamma", None), "n": self.n, "rows": self.rows, "class_counts": self.class_counts,
                "unseen": self.unseen, "smoothing": mb.SMOOTHING["rule"], "calibration": self.calibration,
                "selection": self.selection, "folds": self.folds, "training": self.training}


def _rows(data, target: str, arm: str, dates: Sequence[str], population: Optional[Sequence[str]] = None
          ) -> np.ndarray:
    pop = population or mb.ARMS[arm].get("training_symbols", ("NQ",))
    return np.flatnonzero(data.mask(target, instruments=pop, dates=dates))


def _fit(fam, params, target, arm, data, idx, pooled: Optional[bool] = None):
    est = make(fam, params, target, arm, pooled)
    moves = data.moves[target].to_numpy()[idx] if target in data.moves else None
    return est.fit(data.rows.iloc[idx], [data.labels[target].iloc[i] for i in idx],
                   instruments=data.instruments[idx], moves=moves)


def select(data, target: str, arm: str, train_dates: Sequence[str],
           configs: Optional[List[Tuple[str, Dict[str, Any]]]] = None):
    """The head's family and parameters by inner folds (forecaster/ml_split.inner_folds by session date, NQ rows
    scored): ``(chosen, scores, folds, oof)`` - ``oof`` the chosen configuration's out-of-fold predictions per inner
    fold (raw, labels), for the calibration. Out-of-fold predictions are post-processed exactly as at issue time."""
    b = mb.BUDGET["inner_folds"]
    nq_dates = sorted({d for d, s in zip(data.dates, data.instruments) if s == "NQ" and d in set(train_dates)})
    folds = sp.inner_folds(nq_dates, b["blocks"], b["block_sessions"], b["embargo_sessions"], b["min_train_sessions"])
    if not folds:
        folds = [sp.inner_split(nq_dates, 0.25, b["embargo_sessions"])]
    scores, oofs = [], {}
    for k, (fam, params) in enumerate(configs if configs is not None else configurations(target, arm)):
        losses, ok, oof = [], True, []
        for f in folds:
            tr = _rows(data, target, arm, f.train)
            va = np.flatnonzero(data.mask(target, instruments=("NQ",), dates=f.test))
            n_tr = int(np.sum(data.instruments[tr] == "NQ"))
            if len(va) == 0 or n_tr < 10 or len({data.labels[target].iloc[i] for i in tr}) < 2:
                continue
            try:
                est = _fit(fam, params, target, arm, data, tr)
                X = data.rows.iloc[va]
                raw = est.predict(X)
                P = finish(target, raw, X, n_tr)
            except Exception as e:                           # a configuration that cannot fit is out, recorded
                scores.append({"family": fam, "params": params, "error": f"{type(e).__name__}: {e}"})
                ok = False
                break
            y = [data.labels[target].iloc[i] for i in va]
            losses += list(brier(P, y, mb.CLASSES[target]))
            oof.append({"raw": raw, "X": X, "y": y, "n": n_tr})
        if ok and losses:
            scores.append({"family": fam, "params": params, "brier": float(np.mean(losses)), "n": len(losses)})
            oofs[len(scores) - 1] = oof
    good = [i for i, s in enumerate(scores) if "brier" in s]
    if not good:
        return None, scores, [f.describe() for f in folds], []
    best = min(good, key=lambda i: (round(scores[i]["brier"], 12), mb.FAMILY_ORDER.index(scores[i]["family"]),
                                    json.dumps(scores[i]["params"], sort_keys=True)))
    return (scores[best]["family"], scores[best]["params"]), scores, [f.describe() for f in folds], oofs[best]


TAUS = tuple(np.round(np.linspace(0.5, 3.0, 26), 2))


def calibrate(target: str, oof: List[Dict[str, Any]]) -> Dict[str, Any]:
    """contracts/nq_ml_bundle.CALIBRATION: a temperature is kept only if, fitted on the other inner blocks, it lowers
    each held-out block's Brier score on average; then refitted on every block's out-of-fold predictions."""
    if len(oof) < 2:
        return {"chosen": "none", "why": f"{len(oof)} inner block(s): a temperature cannot be cross-fitted"}

    def score(blocks, tau):
        return float(np.mean(np.concatenate([brier(finish(target, b["raw"], b["X"], b["n"], tau), b["y"],
                                                    mb.CLASSES[target]) for b in blocks])))

    def fit(blocks):
        return min(TAUS, key=lambda tau: (score(blocks, tau), abs(tau - 1)))
    gain = []
    for k in range(len(oof)):
        rest = oof[:k] + oof[k + 1:]
        tau = fit(rest)
        gain.append(score([oof[k]], 1.0) - score([oof[k]], tau))
    if np.mean(gain) <= 0:
        return {"chosen": "none", "cross_fitted_gain": float(np.mean(gain)), "blocks": len(oof)}
    tau = fit(oof)
    return {"chosen": "temperature", "tau": float(tau), "cross_fitted_gain": float(np.mean(gain)), "blocks": len(oof)}


def fit_head(data, target: str, arm: str, train_dates: Sequence[str],
             configs: Optional[List[Tuple[str, Dict[str, Any]]]] = None,
             fixed: Optional[Tuple[str, Dict[str, Any]]] = None, population: Optional[Sequence[str]] = None,
             calibration: bool = True) -> Head:
    """One head on ``train_dates`` (see the module docstring); never reads a row of another date. ``configs``
    restricts the selection (the comparators); ``fixed`` skips it; ``population`` overrides the arm's training
    population (P's NQ-only fit of the same family)."""
    pop = tuple(population or mb.ARMS[arm].get("training_symbols", ("NQ",)))
    idx = _rows(data, target, arm, train_dates, pop)
    inst = data.instruments[idx]
    labs = [data.labels[target].iloc[i] for i in idx]
    nq = [lab for lab, s in zip(labs, inst) if s == "NQ"]
    head = Head(target, arm, "unavailable", n=len(nq), rows={s: int(np.sum(inst == s)) for s in pop},
                class_counts={c: nq.count(c) for c in mb.CLASSES[target]})
    head.unseen = [c for c in mb.CLASSES[target] if head.class_counts[c] == 0]
    dates = sorted({data.dates[i] for i in idx if data.instruments[i] == "NQ"})
    head.training = {"from": dates[0] if dates else None, "to": dates[-1] if dates else None, "sessions": len(dates)}
    if len(nq) < MIN_TRAIN_SESSIONS:
        head.reason = f"{len(nq)} labelled NQ training sessions, fewer than {MIN_TRAIN_SESSIONS}"
        return head
    if len(set(labs)) < 2:
        head.reason = "a single class in the training rows: no model to fit"
        return head
    if arm == "P" and population is None:
        other = sum(v for s, v in head.rows.items() if s != "NQ")
        if other < mb.POOLING["min_other_rows"]:
            head.reason = (f"{other} usable ES / RTY rows, fewer than {mb.POOLING['min_other_rows']}: no pooled "
                           "training (an NQ-only fit is arm N's, never shown as pooled)")
            return head
    pooled = arm == "P" and len(pop) > 1
    if fixed is not None:
        chosen, oof = fixed, []
        head.selection = [{"family": fixed[0], "params": fixed[1], "fixed": True}]
    else:
        chosen, scores, folds, oof = select(data, target, arm, train_dates, configs)
        head.selection, head.folds = scores, folds
        if chosen is None:
            head.reason = "no configuration could be scored on the inner folds"
            return head
    head.family, head.params = chosen
    head.estimator = _fit(chosen[0], chosen[1], target, arm, data, idx, pooled)
    head.calibration = calibrate(target, oof) if calibration and oof else {"chosen": "none", "why": "no inner "
                                                                                                    "out-of-fold "
                                                                                                    "predictions"}
    head.status = "trained"
    return head


@dataclass
class Bundle:
    version: str
    arm: str
    heads: Dict[str, Head]
    training: Dict[str, Any]

    def predict(self, target: str, X: pd.DataFrame) -> np.ndarray:
        return self.heads[target].predict(X)


def fit_bundle(data, arm: str, train_dates: Sequence[str], targets: Sequence[str] = mb.TARGETS,
               **head_options) -> Bundle:
    """Every head of ``arm`` on ``train_dates``; a head that fails is unavailable with the reason, the rest stand.
    ``head_options`` go to fit_head (the evaluation's comparators)."""
    heads = {}
    for t in targets:
        try:
            heads[t] = fit_head(data, t, arm, train_dates, **({k: v(t) if callable(v) else v
                                                              for k, v in head_options.items()}))
        except Exception as e:                               # one head's failure never blocks another
            heads[t] = Head(t, arm, "unavailable", reason=f"training failed: {type(e).__name__}: {e}")
    nq_dates = sorted({d for d, s in zip(data.dates, data.instruments) if s == "NQ" and d in set(train_dates)})
    rows = {s: int(np.sum(np.isin(data.dates, list(train_dates)) & (data.instruments == s)))
            for s in sorted(set(data.instruments))}
    return Bundle(mb.VERSIONS[arm], arm, heads, {"from": nq_dates[0] if nq_dates else None,
                                                  "to": nq_dates[-1] if nq_dates else None,
                                                  "sessions": len(nq_dates), "rows": rows})


# --------------------------------------------------------------------------
# Artifacts
# --------------------------------------------------------------------------

def artifact_dir(version: str, root: Optional[str] = None) -> str:
    return os.path.join(root or mm.MODELS_DIR, version)


def _digest(obj) -> str:
    return hashlib.sha256(defs.canonical_json(obj).encode()).hexdigest()


def data_digest(data, train_dates: Sequence[str]) -> str:
    """sha256 of what the bundle was trained on: every training row's date, instrument, labels and features (12
    significant digits; NaN as null)."""
    m = np.isin(data.dates, list(train_dates))
    rows = []
    for i in np.flatnonzero(m):
        feats = {k: (None if isinstance(v, float) and math.isnan(v) else float(f"{float(v):.12g}"))
                 for k, v in data.rows.iloc[i].items() if k not in ("date", "instrument")}
        rows.append([data.dates[i], data.instruments[i], {t: data.labels[t].iloc[i] for t in mb.TARGETS}, feats])
    return _digest(rows)


def save(bundle: Bundle, data, root: Optional[str] = None) -> Dict[str, Any]:
    """Writes bundle.joblib and manifest.json; returns the manifest. An existing artifact is never overwritten."""
    import joblib
    from forecaster.provenance import code_revision
    d = artifact_dir(bundle.version, root)
    path = os.path.join(d, "bundle.joblib")
    if os.path.exists(path):
        raise mm.ArtifactError(f"{bundle.version} already has an artifact in {d}: a retrained bundle needs a new "
                               "version name")
    os.makedirs(d, exist_ok=True)
    joblib.dump({"version": bundle.version, "arm": bundle.arm, "heads": bundle.heads, "targets": list(mb.TARGETS),
                 "classes": {t: list(c) for t, c in mb.CLASSES.items()}, "feature_version": mb.FEATURE_VERSION},
                path)
    heads = {t: h.describe() for t, h in bundle.heads.items()}
    train_dates = [x for x in data.days if bundle.training["from"] and bundle.training["from"] <= x
                   <= bundle.training["to"]]
    manifest = {
        "version": bundle.version, "arm": bundle.arm, "targets": list(mb.TARGETS),
        "classes": {t: list(c) for t, c in mb.CLASSES.items()}, "label_version": defs.LABEL_VERSION,
        "market_labels": mlab.VERSION, "feature_version": mb.FEATURE_VERSION, "schema_version": mb.SCHEMA_VERSION,
        "heads": heads, "heads_digest": _digest(heads),
        "training": {**bundle.training, "coverage": data.coverage(), "sources": data.sources},
        "data_digest": data_digest(data, train_dates),
        "folds_digest": _digest({t: h["folds"] for t, h in heads.items()}),
        "code_revision": code_revision(), "software": mm.software(),
        "path": os.path.join("data", "models", "nq_ml", bundle.version, "bundle.joblib"),
        "sha256": mm._sha256(path)}
    with open(os.path.join(d, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, sort_keys=True, default=str)
    return manifest


def manifest(version: str, root: Optional[str] = None) -> Optional[Dict[str, Any]]:
    path = os.path.join(artifact_dir(version, root), "manifest.json")
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load(version: str, root: Optional[str] = None, expected_sha256: Optional[str] = None) -> Dict[str, Any]:
    """The bundle and its manifest, checked byte for byte against the manifest's sha256 (and the registered one when
    given), and its targets and classes against the contract's."""
    import joblib
    m = manifest(version, root)
    if m is None:
        raise mm.ArtifactError(f"no artifact for {version} in {artifact_dir(version, root)}")
    path = os.path.join(artifact_dir(version, root), "bundle.joblib")
    sha = mm._sha256(path)
    if sha != m["sha256"] or (expected_sha256 is not None and sha != expected_sha256):
        raise mm.ArtifactError(f"{version}: the artifact's sha256 {sha[:12]} differs from its manifest's "
                               f"{m['sha256'][:12]}" + (f" or the registered {expected_sha256[:12]}"
                                                        if expected_sha256 else ""))
    art = joblib.load(path)
    if (art["targets"] != list(mb.TARGETS) or art["classes"] != {t: list(c) for t, c in mb.CLASSES.items()}
            or m["targets"] != list(mb.TARGETS) or art["feature_version"] != mb.FEATURE_VERSION):
        raise mm.ArtifactError(f"{version}: the artifact's targets, classes or feature version are not the contract's")
    return {**art, "manifest": m}


def records(root: Optional[str] = None) -> List[Dict[str, Any]]:
    """The registered definitions the bundles need (features, schema, market labels) and every bundle whose artifact
    exists."""
    out = [mb.features_record(), mb.schema_record(), mlab.record()]
    for version in mb.VERSIONS.values():
        m = manifest(version, root)
        if m is not None:
            out.append(defs._record(version, "forecast_algorithm", mb.algorithm_definition(version, m)))
    return out


def train(conn, root: Optional[str] = None, data=None, until: Optional[str] = None,
          arms: Sequence[str] = ("N", "M", "P"), progress=print) -> Dict[str, Dict[str, Any]]:
    """Trains and saves each arm's bundle on every pool session up to ``until``; returns the manifests."""
    from forecaster import ml_bundle_data as bd
    data = data or bd.build(conn, until=until, pooled="P" in arms, progress=progress)
    days = [d for d in data.days if until is None or d <= until]
    out = {}
    for arm in arms:
        bundle = fit_bundle(data, arm, days)
        out[mb.VERSIONS[arm]] = save(bundle, data, root)
        progress(f"{mb.VERSIONS[arm]}: " + ", ".join(f"{t} {h.status}" + (f" ({h.family})" if h.family else "")
                                                     for t, h in bundle.heads.items()))
    return out
