# research/ml_study.py
"""
ml_study_v1 - a bounded, reproducible development study: is there conditional information beyond
the prior in NQ's direction, before the open (direction_15m) and through the regular session
(rolling 5/15/30/60-minute targets)? docs/reports/ml_study.md is its report; research/ml_study_data.py
builds its data and research/ml_study_run.py runs it (scripts/ml_study.py).

Development research only: every session it uses was inspected before (hist_dev_v1, p1_pool_tuning_v1,
the fan experiments, the ML development comparison), so a candidate it finds is a candidate for a
prospective study, never a result.

  PROTOCOL        the design, fixed before any outer prediction: tasks, targets and bands, folds, every
                  family's complete configuration grid (each trial counted), metrics, bootstrap, power;
                  protocol_hash() pins it in every output
  outer_folds /   chronological folds grouped by session date: outer test blocks of 20 sessions after an
  inner_folds     initial 120, one embargoed session between; inside each outer training window three
                  inner blocks select a configuration and fit the blend - never an outer test session
  families        the candidate ladder's estimators: fitted on training rows only, then class
                  probabilities in CLASSES order (a class the training rows lack gets its prior share)
  metrics         the unhalved multiclass Brier score from the full probability vector, log loss,
                  reliability, dispersion and divergence from the prior
  paired / power  per-session paired differences (sessions weigh equally, however many rows each has)
                  with the project's moving-block bootstrap; sample sizes from development variability
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

from contracts import nq_ml as ml

VERSION = "ml_study_v1"
CLASSES: Tuple[str, ...] = tuple(ml.CLASSES)             # bearish, bullish, neutral_band - every vector's order
K = len(CLASSES)
SEED = 20261010

# --------------------------------------------------------------------------
# The protocol
# --------------------------------------------------------------------------

PREOPEN_FEATURES = {"nq": list(ml.CONFIGS["nq_only"]), "multi": list(ml.CONFIGS["multi"])}
# the RTH task's compact NQ state (G0) and the intermarket group (G1) added by ablation, research/ml_study_data.py
RTH_G0 = ["ret_5", "ret_15", "ret_30", "ret_60", "ret_open", "ret_on", "rv_30", "rv_open", "eff_30", "vwap_dist",
          "range_pos", "range_sofar", "dist_prev_high", "dist_prev_low", "rel_vol", "elapsed", "to_close",
          "ev_recent", "ev_ahead"]
RTH_G1 = ["es_ret_15", "resid_open", "rty_ret_open", "vxn_chg", "es_missing", "rty_missing", "vxn_missing"]
RTH_FEATURES = {"nq": RTH_G0, "multi": RTH_G0 + RTH_G1}

RTH_CUTOFFS = tuple(range(30, 361, 30))                   # minutes after the open: 10:00 .. 15:30 ET
RTH_HORIZONS = (15, 5, 30, 60)                            # 15 primary
RTH_ORIGINS = {"cutoff": 0, "delayed": 14}               # the window starts this many minutes after the cutoff
RTH_PHASES = {"morning": (30, 120), "midday": (150, 240), "afternoon": (270, 360)}

CP_GRID_PREOPEN = [{"grain": "none", "context": c, "kappa": k}
                   for c in (None, "nq_rv_on", "abs_gap", "vix_level") for k in (5, 20, 80)]
CP_GRID_RTH = [{"grain": g, "context": c, "kappa": k}
               for g in ("clock", "phase") for c in (None, "rv_30", "rv_open") for k in (10, 50, 200)]
LR_GRID = [{"features": f, "C": c} for f in ("nq", "multi") for c in (0.001, 0.003, 0.01, 0.03, 0.1, 0.3, 1.0)]
GB_GRID = [{"features": f, "max_depth": d, "learning_rate": lr, "max_iter": it}
           for f in ("nq", "multi") for d in (1, 2) for lr, it in ((0.03, 50), (0.03, 150), (0.1, 50))]
RES_GRID = [{"features": f, "C": c} for f in ("nq", "multi") for c in (0.003, 0.01, 0.03, 0.1, 0.3, 1.0)]

PROTOCOL: Dict[str, Any] = {
    "version": VERSION,
    "status": "development research on inspected sessions: a candidate is a candidate for a prospective study, "
              "never a result; nothing is registered, promoted or delivered",
    "classes": list(CLASSES),
    "tasks": {
        "preopen": {
            "target": "direction_15m exactly as labelled (contracts/nq_prompt_v2): the 09:44 bar's close minus the "
                      "09:30 open against the snapshot's frozen T - its own task, never mixed with the RTH targets",
            "sessions": "the research_0929 pool's historical snapshots with a recorded label",
            "features": {k: v for k, v in PREOPEN_FEATURES.items()},
            "feature_version": ml.FEATURE_VERSION,
            "prior": "A_s: the training sessions' class frequencies, Laplace-smoothed ((n_c + 1) / (n + 3)) - a "
                     "versioned comparator beside the frozen A, never a rewrite of it",
        },
        "rth": {
            "target": "the direction of NQ's move over the window [S, S + h) after the cutoff: (close of the bar "
                      "ending at S + h - close of the bar ending at S) against the band, bullish above +band, "
                      "bearish below -band, else neutral_band - the next h minutes, never the day's first 15",
            "cutoffs_minutes": list(RTH_CUTOFFS),
            "horizons": list(RTH_HORIZONS), "primary_horizon": 15,
            "origins": {"cutoff": "S = the cutoff (cutoff-origin research: the window starts as the data ends)",
                        "delayed": f"S = the cutoff + {RTH_ORIGINS['delayed']} minutes: the feed's ~10-minute "
                                   "delay, the confirming bar and the 2nd full minute after the build "
                                   "(contracts/rth_session.LEAD_MINUTES) - features still end at the cutoff, the "
                                   "unseen minutes are never an input"},
            "band": "0.5 x the session's two-minute ATR frozen at the cutoff (features/nq_evidence, the last 71 "
                    "complete 2m buckets) x sqrt(h / 15): direction_15m's rule at 15 minutes, scaled with the "
                    "horizon; fixed ex ante from as-of volatility, never tuned on outcomes",
            "window_rule": "S + h must be by the scheduled RTH close; a window that would cross it has no row "
                           "(never shortened); a row missing a bar of its window or of the cutoff has no label "
                           "(counted, never filled in)",
            "features": {k: v for k, v in RTH_FEATURES.items()},
            "prior": "CLOCK: the training sessions' class frequencies at the same cutoff, horizon and origin, "
                     "Laplace-smoothed - the same-clock history",
            "analogues": "B_rth: the 20 highest similarities of nq_match_rth_v3's ranking at the cutoff (the whole "
                         "earlier pool re-scored, never a top-five subset), each member's own move over [S, S + h) "
                         "classified with the target's band in daily-ATR units, Laplace-smoothed; B_rth_recent: the "
                         "same with the similarity averaged with a recent-path similarity (the last 30 minutes "
                         "re-anchored, path tolerance at 30 minutes) - expanding-only against "
                         "expanding-plus-recent context",
            "sessions": "every NQ session with RTH bars from 2025-07-08 (twenty sessions after the IB history "
                        "floor, for the as-of statistics) to the last labelled session",
        },
    },
    "folds": {
        "outer": "chronological by session date: train on every session before the test block less the "
                 "embargo, test the next 20 sessions; the first test block starts after 120 sessions - for the "
                 "pre-open task exactly the development comparison's folds (contracts/nq_ml.DEV_EVALUATION)",
        "embargo_sessions": 1,
        "purge": "every label window ends inside its own session (09:45 for direction_15m, by the close for the "
                 "RTH targets) and features read only earlier sessions and the session's bars to its cutoff, so no "
                 "training label overlaps a test session; the embargoed session is dropped anyway",
        "inner": "inside each outer training window: the last three blocks of 20 sessions, each predicted by a "
                 "fit on the sessions before it less one embargoed session; configurations are chosen by the mean "
                 "inner Brier score over sessions (sessions weigh equally), refitted on the whole outer window",
        "storage": "every outer prediction is written, with its sha256, before the scoring stage reads an outcome",
    },
    "families": {
        "frozen": "A and B (their stored historical-replay runs), N, M, P (walk-forward refits under their frozen "
                  "definitions: family, grid and 75/25 tuning of contracts/nq_ml) - pre-open task only",
        "CP": {"what": "smoothed conditional prior: the training frequencies in coarse context cells (time of day "
                       "and/or a volatility tercile, the terciles fitted on the training rows), shrunk to the global "
                       "training frequencies with weight kappa: (n_cell,c + kappa g_c) / (n_cell + kappa)",
               "grid_preopen": CP_GRID_PREOPEN, "grid_rth": CP_GRID_RTH},
        "LR": {"what": "multinomial logistic regression, L2, median imputation and standard scaling fitted on the "
                       "training rows (scikit-learn, lbfgs)", "grid": LR_GRID},
        "GB": {"what": "shallow gradient-boosted trees (scikit-learn HistGradientBoostingClassifier, "
                       "min_samples_leaf 20, l2_regularization 1.0, median imputation; the number of iterations "
                       "chosen on the inner folds - early stopping by chronological validation)", "grid": GB_GRID},
        "RES": {"what": "prior plus residual: softmax(log p_prior + x W), no intercept, W penalised (L2, C as in "
                        "the logistic grid), x median-imputed and standardised on the training rows; as C falls it "
                        "returns the prior exactly", "grid": RES_GRID},
        "TPF": {"what": "TabPFN v2 classifier (tabpfn 9.1.0, weights tabpfn-v2-classifier-finetuned-zk73skhh.ckpt, "
                        "Prior Labs License: Apache 2.0 with attribution), CPU, defaults, the compact NQ features "
                        "('nq'); one configuration, no tuning; RTH task at the primary horizon only; run in "
                        ".venv-research", "grid": [{"features": "nq"}]},
        "BL": {"what": "(1 - w) prior + w best, best the family with the lowest inner score among CP, LR, GB and RES, "
                       "w in [0, 1] minimising the Brier score of the inner (earlier, out-of-fold) predictions; "
                       "w = 0 means no incremental signal", "grid": [{"w": "fitted"}]},
        "budget": "at most 20 configurations per tuned family; every configuration and feature set is counted in "
                  "the trial manifest; no other family, feature set or grid point is tried",
    },
    "metrics": {
        "primary": "unhalved multiclass Brier score sum_c (p_c - [c realised])^2, 0 to 2, from the full vector",
        "secondary": ["log loss (natural log)", "class-wise reliability (10 bins) and expected calibration error",
                      "dispersion: each class probability's standard deviation over sessions, the mean total "
                      "variation distance from the prior", "skill: 1 - Brier / Brier of the reference (A, B)"],
        "aggregation": "per session the mean over its rows (RTH: its cutoffs at one horizon and origin), then the "
                       "mean over sessions; paired differences per session",
        "uncertainty": "moving-block bootstrap of the per-session paired differences in date order (blocks of 5, "
                       "2000 resamples, seed 20261010), 95 % percentile intervals - descriptive: development data, "
                       "many comparisons",
        "operational": "every row counts for every arm: an arm that cannot forecast a row falls back to the prior "
                       "and the fallback is counted (policy scores); matched scores use only rows no arm fell back on",
    },
    "controls": {
        "shuffled": "session labels permuted across sessions (a session's rows keep their cutoffs, they move "
                    "together) - the whole nested procedure rerun; no reproducible skill should remain",
        "synthetic": "labels drawn from softmax(log p_prior + beta z), z a fixed standardised combination of two "
                     "features, beta for an oracle Brier gain of about 0.04 (strong) and 0.015 (weak) - must be "
                     "learnable",
        "future_bars": "features, analogue ranks and predictions recomputed with every bar after a cutoff removed "
                       "or altered must not change",
    },
    "power": "per comparison the standard deviation and the bootstrap design effect of the per-session "
             "differences; sessions needed for 80 % power under the registered rule of p1_ml_forward_v2 (point at "
             "most -0.01, 98.33 % upper bound below zero) and under a stricter material-edge rule (98.33 % upper "
             "bound below -0.01); normal approximation",
    "bootstrap": {"block": 5, "resamples": 2000, "seed": SEED, "interval": 0.95},
}


def protocol_hash() -> str:
    return hashlib.sha256(json.dumps(PROTOCOL, sort_keys=True, default=str).encode()).hexdigest()[:16]


# --------------------------------------------------------------------------
# Folds
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Fold:
    """Session positions (in the task's date-ordered session list): fit on ``train``, predict ``test``; ``embargo``
    lies between them and is used by neither."""
    train: Tuple[int, ...]
    test: Tuple[int, ...]
    embargo: Tuple[int, ...]


def outer_folds(n: int, initial: int = 120, block: int = 20, embargo: int = 1) -> List[Fold]:
    """Train on [0, start - embargo), test [start, start + block): contracts/nq_ml.DEV_EVALUATION's folds."""
    out, start = [], initial
    while start < n:
        out.append(Fold(tuple(range(0, start - embargo)), tuple(range(start, min(n, start + block))),
                        tuple(range(start - embargo, start))))
        start += block
    return out


def inner_folds(train: Sequence[int], blocks: int = 3, block: int = 20, embargo: int = 1,
                min_train: int = 40) -> List[Fold]:
    """The last ``blocks`` blocks of ``block`` sessions of an outer training window (positions ``train``, in date
    order), each predicted by a fit on the window's sessions before it less ``embargo``."""
    train = list(train)
    m = len(train)
    out = []
    for b in range(blocks, 0, -1):
        start = m - b * block
        if start - embargo < min_train:
            continue
        out.append(Fold(tuple(train[:start - embargo]), tuple(train[start:start + block]),
                        tuple(train[start - embargo:start])))
    return out


def check_fold(fold: Fold, session_ends: Sequence[Any], session_starts: Sequence[Any]) -> None:
    """Raises unless every training session precedes the embargo and the test sessions, and every training label
    window (ending ``session_ends[i]``) ends before the first test session starts."""
    if set(fold.train) & set(fold.test) or set(fold.train) & set(fold.embargo):
        raise AssertionError("a session is both training and test/embargo")
    if fold.train and fold.test:
        if max(fold.train) >= min(fold.test) or (fold.embargo and max(fold.train) >= min(fold.embargo)):
            raise AssertionError("a training session is not earlier than the test block")
        first_test = min(session_starts[i] for i in fold.test)
        if max(session_ends[i] for i in fold.train) >= first_test:
            raise AssertionError("a training label window reaches the test block")


# --------------------------------------------------------------------------
# Metrics
# --------------------------------------------------------------------------

def onehot(y: np.ndarray) -> np.ndarray:
    """Class indices (0..K-1) to one-hot rows."""
    out = np.zeros((len(y), K))
    out[np.arange(len(y)), y] = 1.0
    return out


def brier(P: np.ndarray, y: np.ndarray) -> np.ndarray:
    """Per row the unhalved multiclass Brier score, sum over every class of (p - outcome)^2 (0 to 2) - from the full
    vector, never from the realised class's probability alone."""
    return ((np.asarray(P, float) - onehot(y)) ** 2).sum(axis=1)


def logloss(P: np.ndarray, y: np.ndarray) -> np.ndarray:
    py = np.asarray(P, float)[np.arange(len(y)), y]
    with np.errstate(divide="ignore"):
        return np.where(py > 0, -np.log(np.maximum(py, 1e-300)), np.inf)


def reliability(P: np.ndarray, y: np.ndarray, bins: int = 10) -> Dict[str, Any]:
    """Per class the bins (mean predicted against observed frequency) and the expected calibration error."""
    P = np.asarray(P, float)
    out, eces = {}, []
    edges = np.linspace(0, 1, bins + 1)
    for j, c in enumerate(CLASSES):
        p, o = P[:, j], (y == j).astype(float)
        idx = np.clip(np.digitize(p, edges) - 1, 0, bins - 1)
        rows, ece = [], 0.0
        for b in range(bins):
            m = idx == b
            if m.any():
                rows.append({"bin": f"{edges[b]:.1f}-{edges[b + 1]:.1f}", "n": int(m.sum()),
                             "predicted": float(p[m].mean()), "observed": float(o[m].mean())})
                ece += m.sum() / len(p) * abs(p[m].mean() - o[m].mean())
        out[c] = {"bins": rows, "ece": float(ece)}
        eces.append(ece)
    return {"classes": out, "ece_mean": float(np.mean(eces))}


def tv(P: np.ndarray, Q: np.ndarray) -> np.ndarray:
    """Total variation distance per row, 0.5 x sum |p - q| (0 to 1)."""
    return 0.5 * np.abs(np.asarray(P, float) - np.asarray(Q, float)).sum(axis=1)


def kl(P: np.ndarray, Q: np.ndarray) -> np.ndarray:
    P, Q = np.asarray(P, float), np.asarray(Q, float)
    with np.errstate(divide="ignore", invalid="ignore"):
        t = np.where(P > 0, P * np.log(P / np.maximum(Q, 1e-300)), 0.0)
    return t.sum(axis=1)


def session_means(values: np.ndarray, sessions: Sequence[str]) -> Dict[str, float]:
    """The mean of ``values`` per session (sessions weigh equally in everything built on this)."""
    acc: Dict[str, List[float]] = {}
    for v, s in zip(values, sessions):
        acc.setdefault(s, []).append(float(v))
    return {s: float(np.mean(v)) for s, v in acc.items()}


# --------------------------------------------------------------------------
# Priors and families
# --------------------------------------------------------------------------

def laplace(y: np.ndarray, alpha: float = 1.0) -> np.ndarray:
    counts = np.bincount(np.asarray(y, int), minlength=K).astype(float)
    return (counts + alpha) / (counts.sum() + K * alpha)


def keyed_prior(keys_train: Sequence[Any], y_train: np.ndarray, keys: Sequence[Any], alpha: float = 1.0) -> np.ndarray:
    """Per row the Laplace-smoothed training frequencies of its key (the same clock; one key for the pre-open
    task), the global ones for a key the training rows lack."""
    glob = laplace(y_train, alpha)
    by: Dict[Any, List[int]] = {}
    for k, v in zip(keys_train, y_train):
        by.setdefault(k, []).append(int(v))
    table = {k: laplace(np.array(v), alpha) for k, v in by.items()}
    return np.array([table.get(k, glob) for k in keys])


def _terciles(x: np.ndarray) -> Optional[Tuple[float, float]]:
    ok = x[~np.isnan(x)]
    if len(ok) < 30:
        return None
    return float(np.quantile(ok, 1 / 3)), float(np.quantile(ok, 2 / 3))


def _bucket(x: np.ndarray, edges: Optional[Tuple[float, float]]) -> np.ndarray:
    if edges is None:
        return np.zeros(len(x), int)
    b = np.where(x <= edges[0], 0, np.where(x <= edges[1], 1, 2))
    return np.where(np.isnan(x), 3, b)                       # missing: a cell of its own


def _prep(X: np.ndarray, med: Optional[np.ndarray] = None, mu: Optional[np.ndarray] = None,
          sd: Optional[np.ndarray] = None):
    """Median imputation and standard scaling fitted on the rows given (training) unless the statistics are given."""
    X = np.asarray(X, float)
    if med is None:
        with np.errstate(all="ignore"):
            med = np.nanmedian(X, axis=0)
        med = np.where(np.isnan(med), 0.0, med)
    Xi = np.where(np.isnan(X), med, X)
    if mu is None:
        mu, sd = Xi.mean(axis=0), Xi.std(axis=0)
        sd = np.where(sd > 0, sd, 1.0)
    return (Xi - mu) / sd, med, mu, sd


def softmax(Z: np.ndarray) -> np.ndarray:
    Z = Z - Z.max(axis=1, keepdims=True)
    E = np.exp(Z)
    return E / E.sum(axis=1, keepdims=True)


def fit_residual(X: np.ndarray, y: np.ndarray, offset: np.ndarray, C: float) -> Dict[str, Any]:
    """softmax(offset + x W) with no intercept, minimising C x sum of log losses + 0.5 |W|^2 (L-BFGS); x imputed and
    standardised on these rows."""
    from scipy.optimize import minimize
    Xs, med, mu, sd = _prep(X)
    Y = onehot(y)
    k = Xs.shape[1]

    def f(w):
        W = w.reshape(k, K)
        Z = offset + Xs @ W
        Zm = Z.max(axis=1, keepdims=True)
        lse = Zm[:, 0] + np.log(np.exp(Z - Zm).sum(axis=1))
        loss = -(Y * Z).sum() + lse.sum()
        P = np.exp(Z - lse[:, None])
        grad = C * (Xs.T @ (P - Y)) + W
        return C * loss + 0.5 * (W ** 2).sum(), grad.ravel()

    res = minimize(f, np.zeros(k * K), jac=True, method="L-BFGS-B", options={"maxiter": 2000, "gtol": 1e-8})
    return {"W": res.x.reshape(k, K), "med": med, "mu": mu, "sd": sd, "converged": bool(res.success)}


def predict_residual(model: Dict[str, Any], X: np.ndarray, offset: np.ndarray) -> np.ndarray:
    Xs, *_ = _prep(X, model["med"], model["mu"], model["sd"])
    return softmax(offset + Xs @ model["W"])


def _sk_probs(model, X: np.ndarray) -> np.ndarray:
    """A scikit-learn model's probabilities in CLASSES order (by class, never by position); a class its training
    rows lacked keeps 0, as the production models do."""
    raw = model.predict_proba(X)
    out = np.zeros((raw.shape[0], K))
    for j, c in enumerate(model.classes_):
        out[:, int(c)] = raw[:, j]
    return out


def make_sklearn(family: str, cfg: Dict[str, Any]):
    from sklearn.ensemble import HistGradientBoostingClassifier
    from sklearn.impute import SimpleImputer
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import StandardScaler
    if family == "LR":
        return Pipeline([("impute", SimpleImputer(strategy="median", keep_empty_features=True)),
                         ("scale", StandardScaler()), ("model", LogisticRegression(C=cfg["C"], max_iter=2000))])
    if family == "GB":
        return Pipeline([("impute", SimpleImputer(strategy="median", keep_empty_features=True)),
                         ("model", HistGradientBoostingClassifier(
                             max_depth=cfg["max_depth"], min_samples_leaf=20, l2_regularization=1.0,
                             learning_rate=cfg["learning_rate"], max_iter=cfg["max_iter"], early_stopping=False,
                             random_state=0))])
    raise ValueError(family)


@dataclass
class Problem:
    """One prediction problem: rows (one per session for the pre-open task; one per session and cutoff for an RTH
    horizon and origin), each with its session position, clock key, features and label (class index, -1 when
    there is none)."""
    name: str
    sessions: List[str]                       # date-ordered
    row_session: np.ndarray                   # session position per row
    keys: np.ndarray                          # the prior's key per row (the cutoff minute; 0 before the open)
    phase: np.ndarray                         # the CP's coarse time-of-day key per row
    X: Any                                    # pandas DataFrame of every feature column
    y: np.ndarray                             # class index per row, -1 without a label
    feature_sets: Dict[str, List[str]]
    cp_grid: List[Dict[str, Any]]
    cp_context: Dict[str, np.ndarray]         # the CP's context variables per row


def _rows(problem: Problem, sessions: Sequence[int], labelled: bool) -> np.ndarray:
    m = np.isin(problem.row_session, np.asarray(sessions, int))
    if labelled:
        m &= problem.y >= 0
    return np.flatnonzero(m)


def base_prior(problem: Problem, train: np.ndarray, rows: np.ndarray) -> np.ndarray:
    return keyed_prior(problem.keys[train], problem.y[train], problem.keys[rows])


def fit_predict(problem: Problem, family: str, cfg: Dict[str, Any], train: np.ndarray, rows: np.ndarray
                ) -> np.ndarray:
    """``family`` with ``cfg`` fitted on the rows ``train`` (labelled), its probabilities for ``rows``."""
    y = problem.y[train]
    prior_rows = base_prior(problem, train, rows)
    if family == "PRIOR":
        return prior_rows
    if family == "CP":
        glob = laplace(y)
        grain = {"none": np.zeros(len(problem.keys), int), "clock": problem.keys,
                 "phase": problem.phase}[cfg["grain"]]
        if cfg["context"] is None:
            ctx_tr, ctx_rows = np.zeros(len(train), int), np.zeros(len(rows), int)
        else:
            x = problem.cp_context[cfg["context"]]
            edges = _terciles(x[train])
            ctx_tr, ctx_rows = _bucket(x[train], edges), _bucket(x[rows], edges)
        cells: Dict[Tuple[Any, int], np.ndarray] = {}
        for g, b, v in zip(grain[train], ctx_tr, y):
            cells.setdefault((g, int(b)), np.zeros(K))[v] += 1
        kappa = float(cfg["kappa"])
        out = []
        for g, b in zip(grain[rows], ctx_rows):
            n = cells.get((g, int(b)), np.zeros(K))
            out.append((n + kappa * glob) / (n.sum() + kappa))
        return np.array(out)
    cols = problem.feature_sets[cfg["features"]]
    Xtr = problem.X.iloc[train][cols].to_numpy(float)
    Xr = problem.X.iloc[rows][cols].to_numpy(float)
    if family in ("LR", "GB"):
        if len(set(y.tolist())) < 2:
            return prior_rows
        model = make_sklearn(family, cfg).fit(Xtr, y)
        return _sk_probs(model, Xr)
    if family == "RES":
        offset_tr = np.log(base_prior(problem, train, train))
        model = fit_residual(Xtr, y, offset_tr, cfg["C"])
        return predict_residual(model, Xr, np.log(prior_rows))
    raise ValueError(family)


GRIDS = {"CP": None, "LR": LR_GRID, "GB": GB_GRID, "RES": RES_GRID}


def grid_of(problem: Problem, family: str) -> List[Dict[str, Any]]:
    return problem.cp_grid if family == "CP" else GRIDS[family]


def inner_score(problem: Problem, P: np.ndarray, rows: np.ndarray) -> float:
    """The mean over sessions of the per-session mean Brier score (sessions weigh equally)."""
    b = brier(P, problem.y[rows])
    per = session_means(b, problem.row_session[rows].tolist())
    return float(np.mean(list(per.values())))


def nested(problem: Problem, family: str, fold: Fold) -> Dict[str, Any]:
    """Every configuration of ``family`` scored on the inner folds of ``fold``'s training window; the best refitted
    on the whole window and its outer predictions; the trials, and the best one's inner (out-of-fold) predictions
    beside the prior's on the same rows (the blend's data)."""
    inner = []
    for f in inner_folds(fold.train):
        tr, va = _rows(problem, f.train, True), _rows(problem, f.test, True)
        if len(tr) and len(va):
            inner.append((tr, va))
    rows_in = np.concatenate([va for _, va in inner])
    prior_in = np.vstack([fit_predict(problem, "PRIOR", {}, tr, va) for tr, va in inner])
    trials, inner_preds = [], []
    for cfg in grid_of(problem, family):
        P_in = np.vstack([fit_predict(problem, family, cfg, tr, va) for tr, va in inner])
        trials.append({"config": cfg, "inner_brier": inner_score(problem, P_in, rows_in)})
        inner_preds.append(P_in)
    best = min(range(len(trials)), key=lambda i: (round(trials[i]["inner_brier"], 12), i))
    train = _rows(problem, fold.train, True)
    test = _rows(problem, fold.test, False)
    P = fit_predict(problem, family, trials[best]["config"], train, test)
    return {"family": family, "rows": test, "P": P, "best": trials[best]["config"], "trials": trials,
            "inner_rows": rows_in, "inner_P": inner_preds[best], "inner_prior": prior_in}


def blend_weight(P_cand: np.ndarray, P_base: np.ndarray, y: np.ndarray) -> float:
    """The w in [0, 1] minimising the Brier score of (1 - w) base + w candidate on these (earlier, out-of-fold)
    predictions - closed form, then clipped."""
    D = P_cand - P_base
    R = onehot(y) - P_base
    den = float((D * D).sum())
    if den <= 0:
        return 0.0
    return float(min(1.0, max(0.0, (D * R).sum() / den)))


def outer_fold(problem: Problem, fold: Fold, families: Sequence[str] = ("CP", "LR", "GB", "RES")) -> Dict[str, Any]:
    """One outer fold of a problem: the prior, every family's nested selection and the blend; the predictions and
    the trial records (nothing here reads an outer test label)."""
    train = _rows(problem, fold.train, True)
    test = _rows(problem, fold.test, False)
    out: Dict[str, Any] = {"rows": test, "preds": {}, "trials": {}, "best": {}}
    out["preds"]["PRIOR"] = fit_predict(problem, "PRIOR", {}, train, test)
    inner_best = {}
    for fam in families:
        r = nested(problem, fam, fold)
        assert np.array_equal(r["rows"], test)
        out["preds"][fam] = r["P"]
        out["trials"][fam] = r["trials"]
        out["best"][fam] = r["best"]
        inner_best[fam] = (min(t["inner_brier"] for t in r["trials"]), r)
    # the blend: the family with the lowest inner score, its weight against the prior on the inner predictions
    fam = min(inner_best, key=lambda f: (inner_best[f][0], list(families).index(f)))
    r = inner_best[fam][1]
    w = blend_weight(r["inner_P"], r["inner_prior"], problem.y[r["inner_rows"]])
    out["preds"]["BL"] = (1 - w) * out["preds"]["PRIOR"] + w * out["preds"][fam]
    out["best"]["BL"] = {"family": fam, "w": w}
    out["trials"]["BL"] = [{"config": {"family": fam, "w": w}, "inner_brier": None}]
    return out


# --------------------------------------------------------------------------
# Paired comparisons, bootstrap, power
# --------------------------------------------------------------------------

BOOT = PROTOCOL["bootstrap"]


def interval(diffs: Sequence[float], level: float = BOOT["interval"]) -> Optional[Tuple[float, float]]:
    from forecaster.experiments import block_bootstrap
    return block_bootstrap(list(diffs), BOOT["block"], BOOT["resamples"], BOOT["seed"], level)


def paired(a: Dict[str, float], b: Dict[str, float], level: float = BOOT["interval"]) -> Dict[str, Any]:
    """``a - b`` over the sessions both have (date order): mean, interval, n."""
    days = sorted(set(a) & set(b))
    d = [a[x] - b[x] for x in days]
    return {"n": len(days), "mean": float(np.mean(d)) if d else None, "interval": interval(d, level) if d else None,
            "sessions": days, "diffs": d}


def design_effect(diffs: Sequence[float], resamples: int = 2000, seed: int = SEED) -> float:
    """The moving-block bootstrap variance of the mean over the independent-sessions variance sd^2 / n."""
    d = np.asarray(diffs, float)
    n = len(d)
    if n < 10 or d.std(ddof=1) == 0:
        return 1.0
    rng = np.random.default_rng(seed)
    b = BOOT["block"]
    means = []
    for _ in range(resamples):
        starts = rng.integers(0, n - b + 1, size=math.ceil(n / b))
        means.append(np.concatenate([d[s:s + b] for s in starts])[:n].mean())
    return float(np.var(means, ddof=1) / (d.var(ddof=1) / n))


def power(n: int, sd: float, delta: float, level: float, point: float = 0.0, margin: float = 0.0,
          deff: float = 1.0) -> float:
    """The probability that a true mean difference of -``delta`` (an improvement) gives a point estimate at most
    -``point`` and a two-sided ``level`` upper bound below -``margin``: Phi((delta - max(point, margin + z se)) / se),
    se = sd sqrt(deff / n) - the normal approximation."""
    from scipy.stats import norm
    se = sd * math.sqrt(deff / n)
    z = norm.ppf(1 - (1 - level) / 2)
    return float(norm.cdf((delta - max(point, margin + z * se)) / se))


def sessions_needed(sd: float, delta: float, level: float, target: float = 0.8, point: float = 0.0,
                    margin: float = 0.0, deff: float = 1.0, cap: int = 100000) -> Optional[int]:
    if delta <= max(point, margin):
        return None
    n = 2
    while n <= cap:
        if power(n, sd, delta, level, point, margin, deff) >= target:
            return n
        n = n + 1 if n < 200 else int(n * 1.02) + 1
    return None


def detectable(n: int, sd: float, level: float, target: float = 0.8, point: float = 0.0, margin: float = 0.0,
               deff: float = 1.0) -> float:
    """The smallest true improvement with ``target`` power at ``n`` sessions."""
    lo, hi = max(point, margin), 5.0
    for _ in range(80):
        mid = (lo + hi) / 2
        if power(n, sd, mid, level, point, margin, deff) >= target:
            hi = mid
        else:
            lo = mid
    return hi


# --------------------------------------------------------------------------
# Controls
# --------------------------------------------------------------------------

def shuffled(problem: Problem, seed: int) -> Problem:
    """The problem with every session's labels moved to another session (a permutation of the sessions; a session's
    rows keep their cutoffs and move together - the labels of a cutoff the receiving session lacks are dropped)."""
    rng = np.random.default_rng(seed)
    n = len(problem.sessions)
    perm = rng.permutation(n)
    lab: Dict[Tuple[int, Any], int] = {(int(s), k): int(v) for s, k, v in
                                       zip(problem.row_session, problem.keys, problem.y)}
    y = np.array([lab.get((int(perm[s]), k), -1) for s, k in zip(problem.row_session, problem.keys)])
    return Problem(**{**problem.__dict__, "name": f"{problem.name}/shuffled{seed}", "y": y})


def synthetic(problem: Problem, columns: Tuple[str, str], beta: float, seed: int) -> Tuple[Problem, np.ndarray]:
    """The problem with labels drawn from softmax(log p + beta z * (-1, +1, 0)) (bearish, bullish, neutral), p the
    rows' overall class frequencies and z the standardised sum of two feature columns (missing: 0) - a known
    signal; returns the problem and the oracle probabilities."""
    rng = np.random.default_rng(seed)
    x = problem.X[list(columns)].to_numpy(float)
    x = np.where(np.isnan(x), np.nanmedian(x, axis=0), x)
    z = (x - x.mean(axis=0)) / np.where(x.std(axis=0) > 0, x.std(axis=0), 1)
    z = z.sum(axis=1)
    z = (z - z.mean()) / (z.std() or 1)
    p = laplace(problem.y[problem.y >= 0])
    effect = np.array([-1.0, 1.0, 0.0])
    P = softmax(np.log(p)[None, :] + beta * z[:, None] * effect[None, :])
    u = rng.random(len(z))
    y = (u[:, None] > np.cumsum(P, axis=1)).sum(axis=1)
    y = np.where(problem.y >= 0, y, -1)
    return Problem(**{**problem.__dict__, "name": f"{problem.name}/synthetic{beta}/{seed}", "y": y}), P


def oracle_gain(problem: Problem, P_oracle: np.ndarray) -> float:
    """The oracle's expected Brier gain over the constant overall frequencies p on the labelled rows: the expected
    Brier score of a forecast q under the true p_true is sum q^2 - 2 q.p_true + 1, so the gain is the mean of
    |p_true - p|^2."""
    m = problem.y >= 0
    p = laplace(problem.y[m])
    return float(((P_oracle[m] - p[None, :]) ** 2).sum(axis=1).mean())


def beta_for(problem: Problem, columns: Tuple[str, str], gain: float) -> float:
    """The synthetic control's beta whose oracle gain (oracle_gain) is ``gain`` - the gain depends on beta and the
    features only, not on the draw, so the protocol's targets are met by construction (bisection)."""
    lo, hi = 0.0, 5.0
    for _ in range(60):
        mid = (lo + hi) / 2
        _, P = synthetic(problem, columns, mid, 0)
        if oracle_gain(problem, P) < gain:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


SYNTHETIC_GAINS = (0.04, 0.015)                             # the protocol's strong and weak planted signals


def run_problem(problem: Problem, folds: Sequence[Fold], families: Sequence[str] = ("CP", "LR", "GB", "RES"),
                jobs: int = 1) -> Dict[str, Any]:
    """Every outer fold of ``problem`` (in parallel worker processes when ``jobs`` > 1); the outer predictions per
    family by row, the chosen configurations and the trials per fold."""
    from joblib import Parallel, delayed
    done = (Parallel(n_jobs=jobs)(delayed(outer_fold)(problem, f, families) for f in folds) if jobs > 1 else
            [outer_fold(problem, f, families) for f in folds])
    preds: Dict[str, Dict[int, np.ndarray]] = {}
    out = {"preds": preds, "folds": []}
    for f, r in zip(folds, done):
        for fam, P in r["preds"].items():
            got = preds.setdefault(fam, {})
            for i, row in enumerate(r["rows"]):
                got[int(row)] = P[i]
        out["folds"].append({"train": [problem.sessions[f.train[0]], problem.sessions[f.train[-1]], len(f.train)],
                             "test": [problem.sessions[f.test[0]], problem.sessions[f.test[-1]], len(f.test)],
                             "best": r["best"], "trials": r["trials"]})
    return out

