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

def _design(X: pd.DataFrame, columns: Sequence[str], pooled: bool, instruments: Optional[Sequence[str]]
            ) -> np.ndarray:
    """The head's feature columns, plus the instrument identity for the pooled arm (NQ the reference)."""
    A = X.reindex(columns=list(columns)).to_numpy(float)
    if pooled:
        inst = np.asarray(instruments if instruments is not None else ["NQ"] * len(X))
        A = np.column_stack([A] + [(inst == s).astype(float) for s in ("ES", "RTY")])
    return A


class SkHead:
    """A scikit-learn family (contracts/nq_ml_bundle.FAMILIES 'logit' / 'gbm') on the head's columns."""

    def __init__(self, family: str, params: Dict[str, Any], columns: Sequence[str], classes: Sequence[str],
                 pooled: bool):
        self.family, self.params, self.columns, self.classes, self.pooled = family, dict(params), list(columns), \
            tuple(classes), pooled
        self.model = None

    def _pipeline(self):
        from sklearn.ensemble import HistGradientBoostingClassifier
        from sklearn.impute import SimpleImputer
        from sklearn.linear_model import LogisticRegression
        from sklearn.pipeline import Pipeline
        from sklearn.preprocessing import StandardScaler
        if self.family == "logit":
            return Pipeline([("impute", SimpleImputer(strategy="median", keep_empty_features=True)),
                             ("scale", StandardScaler()),
                             ("model", LogisticRegression(C=self.params["C"], max_iter=3000))])
        if self.family == "gbm":
            return Pipeline([("impute", SimpleImputer(strategy="median", keep_empty_features=True)),
                             ("model", HistGradientBoostingClassifier(
                                 max_depth=2, min_samples_leaf=20, l2_regularization=1.0, learning_rate=0.05,
                                 max_iter=self.params["max_iter"], early_stopping=False, random_state=0))])
        raise ValueError(f"unknown family {self.family!r}")

    def fit(self, X: pd.DataFrame, y: Sequence[str], instruments: Optional[Sequence[str]] = None,
            moves: Optional[Sequence[float]] = None) -> "SkHead":
        self.model = self._pipeline().fit(_design(X, self.columns, self.pooled, instruments), np.asarray(y))
        return self

    def predict(self, X: pd.DataFrame) -> np.ndarray:
        raw = self.model.predict_proba(_design(X, self.columns, self.pooled, None))
        out = np.zeros((len(X), len(self.classes)))
        for j, c in enumerate(self.model.classes_):
            out[:, self.classes.index(c)] = raw[:, j]
        return out


FAMILY_CLASSES = {"logit": SkHead, "gbm": SkHead}


def make(family: str, params: Dict[str, Any], target: str, arm: str):
    cols = mb.head_features(target, arm)
    return FAMILY_CLASSES[family](family, params, cols, mb.CLASSES[target], arm == "P")


def configurations(target: str, arm: str) -> List[Tuple[str, Dict[str, Any]]]:
    """The declared selection budget of a head (contracts/nq_ml_bundle.FAMILIES, in FAMILY_ORDER)."""
    kind = mb.HEADS[target]["kind"]
    out = []
    for fam in mb.FAMILY_ORDER:
        spec = mb.FAMILIES.get(fam)
        if spec is None or kind not in spec["kinds"] or fam not in FAMILY_CLASSES:
            continue
        out += [(fam, dict(p)) for p in spec["grid"]]
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


def finish(target: str, P: np.ndarray, X: pd.DataFrame, n: int) -> np.ndarray:
    """Structural zeros, renormalisation and the Jeffreys smoothing (contracts/nq_ml_bundle.SMOOTHING)."""
    allowed = possible_mask(target, X)
    P = np.where(allowed, np.clip(P, 0, None), 0.0)
    s = P.sum(axis=1, keepdims=True)
    k = allowed.sum(axis=1, keepdims=True)
    P = np.where(s > 0, P / np.where(s > 0, s, 1), allowed / k)
    a = mb.SMOOTHING["pseudo_count"]
    return np.where(allowed, (n * P + a) / (n + a * k), 0.0)


def brier(P: np.ndarray, y: Sequence[str], classes: Sequence[str]) -> np.ndarray:
    onehot = np.array([[1.0 if c == lab else 0.0 for c in classes] for lab in y])
    return ((P - onehot) ** 2).sum(axis=1)


def exact(probs: Sequence[float], classes: Sequence[str]) -> Dict[str, str]:
    """``{class: "n/1000000"}`` summing exactly to 1; the largest class takes the rounding remainder, an exact zero stays
    zero."""
    d = mb.PROBABILITY_DENOMINATOR
    p = np.clip(np.asarray(probs, dtype=float), 0, None)
    p = p / p.sum()
    units = [int(round(x * d)) for x in p]
    units[int(np.argmax(p))] += d - sum(units)
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
        return finish(self.target, self.estimator.predict(X), X, self.n)

    def describe(self) -> Dict[str, Any]:
        cols = getattr(self.estimator, "columns", None)
        return {"target": self.target, "status": self.status, "reason": self.reason, "family": self.family,
                "params": self.params, "estimator": None if self.family is None else mb.FAMILIES[self.family][
                    "estimator"], "columns": cols, "classes": list(mb.CLASSES[self.target]),
                "preprocessing": "median imputation (and standard scaling for the logistic families), fitted on the "
                                 "head's training rows inside the estimator",
                "pooled": self.arm == "P", "n": self.n, "rows": self.rows, "class_counts": self.class_counts,
                "unseen": self.unseen, "smoothing": mb.SMOOTHING["rule"], "calibration": self.calibration,
                "selection": self.selection, "folds": self.folds, "training": self.training}


def _rows(data, target: str, arm: str, dates: Sequence[str]) -> np.ndarray:
    pop = mb.ARMS[arm].get("training_symbols", ("NQ",))
    return np.flatnonzero(data.mask(target, instruments=pop, dates=dates))


def _fit(fam, params, target, arm, data, idx):
    est = make(fam, params, target, arm)
    moves = data.moves[target].to_numpy()[idx] if target in data.moves else None
    return est.fit(data.rows.iloc[idx], [data.labels[target].iloc[i] for i in idx],
                   instruments=data.instruments[idx], moves=moves)


def select(data, target: str, arm: str, train_dates: Sequence[str]) -> Tuple[Optional[Tuple[str, Dict]], List, List]:
    """The head's family and parameters by inner folds (forecaster/ml_split.inner_folds by session date, NQ rows
    scored): ``(chosen, scores, folds)``. Out-of-fold predictions are post-processed exactly as at issue time."""
    b = mb.BUDGET["inner_folds"]
    nq_dates = sorted({d for d, s in zip(data.dates, data.instruments) if s == "NQ" and d in set(train_dates)})
    folds = sp.inner_folds(nq_dates, b["blocks"], b["block_sessions"], b["embargo_sessions"], b["min_train_sessions"])
    if not folds:
        folds = [sp.inner_split(nq_dates, 0.25, b["embargo_sessions"])]
    scores = []
    for fam, params in configurations(target, arm):
        losses, ok = [], True
        for f in folds:
            tr = _rows(data, target, arm, f.train)
            va = np.flatnonzero(data.mask(target, instruments=("NQ",), dates=f.test))
            n_tr = int(np.sum(data.instruments[tr] == "NQ"))
            if len(va) == 0 or n_tr < 10 or len({data.labels[target].iloc[i] for i in tr}) < 2:
                continue
            try:
                est = _fit(fam, params, target, arm, data, tr)
                X = data.rows.iloc[va]
                P = finish(target, est.predict(X), X, n_tr)
            except Exception as e:                           # a configuration that cannot fit is out, recorded
                scores.append({"family": fam, "params": params, "error": f"{type(e).__name__}: {e}"})
                ok = False
                break
            losses += list(brier(P, [data.labels[target].iloc[i] for i in va], mb.CLASSES[target]))
        if ok and losses:
            scores.append({"family": fam, "params": params, "brier": float(np.mean(losses)), "n": len(losses)})
    good = [s for s in scores if "brier" in s]
    if not good:
        return None, scores, [f.describe() for f in folds]
    best = min(good, key=lambda s: (round(s["brier"], 12), mb.FAMILY_ORDER.index(s["family"]),
                                    json.dumps(s["params"], sort_keys=True)))
    return (best["family"], best["params"]), scores, [f.describe() for f in folds]


def fit_head(data, target: str, arm: str, train_dates: Sequence[str]) -> Head:
    """One head on ``train_dates`` (see the module docstring); never reads a row of another date."""
    idx = _rows(data, target, arm, train_dates)
    inst = data.instruments[idx]
    labs = [data.labels[target].iloc[i] for i in idx]
    nq = [lab for lab, s in zip(labs, inst) if s == "NQ"]
    head = Head(target, arm, "unavailable", n=len(nq),
                rows={s: int(np.sum(inst == s)) for s in mb.ARMS[arm].get("training_symbols", ("NQ",))},
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
    if arm == "P":
        other = sum(v for s, v in head.rows.items() if s != "NQ")
        if other < mb.POOLING["min_other_rows"]:
            head.reason = (f"{other} usable ES / RTY rows, fewer than {mb.POOLING['min_other_rows']}: no pooled "
                           "training (an NQ-only fit is arm N's, never shown as pooled)")
            return head
    chosen, scores, folds = select(data, target, arm, train_dates)
    head.selection, head.folds = scores, folds
    if chosen is None:
        head.reason = "no configuration could be scored on the inner folds"
        return head
    head.family, head.params = chosen
    head.estimator = _fit(chosen[0], chosen[1], target, arm, data, idx)
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


def fit_bundle(data, arm: str, train_dates: Sequence[str], targets: Sequence[str] = mb.TARGETS) -> Bundle:
    """Every head of ``arm`` on ``train_dates``; a head that fails is unavailable with the reason, the rest stand."""
    heads = {}
    for t in targets:
        try:
            heads[t] = fit_head(data, t, arm, train_dates)
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
