# forecaster/ml_model.py
"""
The NQ direction model's estimators, tuning and artifacts (contracts/nq_ml.py).

  pipeline(family, params)   imputation (and scaling for the logistic model) and the
                             estimator in one scikit-learn Pipeline, so preprocessing is
                             always fitted on the training rows only and saved with it
  tune(X, y, family, dates, instruments)
                             the predefined grid: every point fitted on the window's
                             earlier session dates, scored by the mean unhalved multiclass
                             Brier score of NQ's rows on its last 25 % of dates, the
                             one-session embargo between (contracts/nq_ml.SPLIT,
                             forecaster/ml_split.py); the best refitted on the whole window.
                             split='legacy_rows' is the row-position cut the registered v1
                             artifacts were tuned with (tune_rows_legacy) - reproduction only
  probabilities(model, X)    class probabilities in contracts/nq_ml.CLASSES order (a class
                             the training window lacked gets 0)
  exact(probs)               the probabilities as exact fractions over 1,000,000 summing
                             to 1 (the largest class takes the rounding remainder)
  save / load                data/models/nq_ml/<version>/model.joblib with manifest.json:
                             training dates and sessions, the instrument manifest, feature
                             definitions, preprocessing, parameters, software versions and
                             the artifact's sha256; load refuses an artifact whose bytes do
                             not match its manifest
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
from fractions import Fraction
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

from contracts import nq_ml as ml

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODELS_DIR = os.path.join(_ROOT, "data", "models", "nq_ml")


class ArtifactError(RuntimeError):
    pass


def pipeline(family: str, params: Dict[str, Any]):
    from sklearn.ensemble import HistGradientBoostingClassifier
    from sklearn.impute import SimpleImputer
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import StandardScaler
    if family == "logit":
        return Pipeline([("impute", SimpleImputer(strategy="median", keep_empty_features=True)),
                         ("scale", StandardScaler()),
                         ("model", LogisticRegression(C=params["C"], max_iter=2000))])
    if family == "gbm":
        return Pipeline([("impute", SimpleImputer(strategy="median", keep_empty_features=True)),
                         ("model", HistGradientBoostingClassifier(
                             max_depth=2, min_samples_leaf=20, l2_regularization=1.0, early_stopping=False,
                             learning_rate=params["learning_rate"], max_iter=params["max_iter"], random_state=0))])
    raise ValueError(f"unknown model family {family!r}")


def grid(family: str) -> List[Dict[str, Any]]:
    g = ml.MODELS[family]["grid"]
    keys = sorted(g)
    out = [{}]
    for k in keys:
        out = [dict(p, **{k: v}) for p in out for v in g[k]]
    return out


def probabilities(model, X) -> np.ndarray:
    """Class probabilities in ml.CLASSES order; a class the model never saw in training gets 0."""
    raw = model.predict_proba(X)
    out = np.zeros((raw.shape[0], len(ml.CLASSES)))
    for j, c in enumerate(model.classes_):
        out[:, ml.CLASSES.index(c)] = raw[:, j]
    return out


def brier(p: np.ndarray, y: Sequence[str]) -> np.ndarray:
    """The unhalved multiclass Brier score per row: the sum over the classes of (p - outcome)^2, 0 to 2."""
    onehot = np.array([[1.0 if c == label else 0.0 for c in ml.CLASSES] for label in y])
    return ((p - onehot) ** 2).sum(axis=1)


SPLITS = ("dates", "legacy_rows")


def tune(X, y: Sequence[str], family: str, dates: Optional[Sequence[str]] = None,
         instruments: Optional[Sequence[str]] = None, split: str = "dates", objective: str = "NQ"
         ) -> Tuple[Any, Dict[str, Any], List[Dict[str, Any]]]:
    """``(fitted model, chosen params, the grid's validation scores)`` - see the module docstring. ``dates`` (each
    row's session date) is required for the date split; ``instruments`` (each row's instrument) makes the validation
    score ``objective``'s rows only."""
    if split == "legacy_rows":
        return tune_rows_legacy(X, y, family)
    if split != "dates":
        raise ValueError(f"unknown split {split!r}")
    from forecaster import ml_split as sp
    if dates is None or len(dates) != len(y):
        raise ValueError("the date split needs every row's session date")
    y = np.asarray(y)
    fold = sp.inner_split(dates, ml.TUNING["validation_share"], ml.SPLIT["embargo_sessions"])
    train = sp.mask(dates, fold.train)
    val = sp.objective(dates, instruments, fold, objective)
    if not val.any():
        raise ValueError(f"no {objective} row on the validation dates {fold.test[0]}..{fold.test[-1]}")
    scores = []
    for params in grid(family):
        m = pipeline(family, params).fit(X[train], y[train])
        scores.append({"params": params, "brier": float(brier(probabilities(m, X[val]), y[val]).mean())})
    best = min(scores, key=lambda s: (s["brier"], json.dumps(s["params"], sort_keys=True)))
    return pipeline(family, best["params"]).fit(X, y), best["params"], scores


def tune_rows_legacy(X, y: Sequence[str], family: str) -> Tuple[Any, Dict[str, Any], List[Dict[str, Any]]]:
    """The v1 tuner, unchanged: every grid point fitted on the first 75 % of the ROWS and scored on the last 25 %. Kept
    only to reproduce the registered v1 artifacts and ml_study_v1. For rows that are not one per session in date
    order - the pooled rows are instrument first, date second - the cut is not chronological and validation shares
    dates with training (forecaster/ml_split.py)."""
    y = np.asarray(y)
    n = len(y)
    cut = int(round(n * (1 - ml.TUNING["validation_share"])))
    scores = []
    for params in grid(family):
        m = pipeline(family, params).fit(X[:cut], y[:cut])
        scores.append({"params": params, "brier": float(brier(probabilities(m, X[cut:]), y[cut:]).mean())})
    best = min(scores, key=lambda s: (s["brier"], json.dumps(s["params"], sort_keys=True)))
    return pipeline(family, best["params"]).fit(X, y), best["params"], scores


def exact(probs: Sequence[float]) -> Dict[str, str]:
    """``{class: "n/1000000"}`` summing exactly to 1 (contracts/nq_ml.PROBABILITY_DENOMINATOR)."""
    d = ml.PROBABILITY_DENOMINATOR
    p = np.clip(np.asarray(probs, dtype=float), 0, None)
    p = p / p.sum()
    units = [int(round(x * d)) for x in p]
    units[int(np.argmax(p))] += d - sum(units)
    return {c: f"{u}/{d}" for c, u in zip(ml.CLASSES, units)}


def fraction(text: str) -> Fraction:
    n, d = text.split("/")
    return Fraction(int(n), int(d))


def software() -> Dict[str, str]:
    import joblib
    import pandas
    import scipy
    import sklearn
    return {"python": platform.python_version(), "scikit-learn": sklearn.__version__, "numpy": np.__version__,
            "pandas": pandas.__version__, "scipy": scipy.__version__, "joblib": joblib.__version__}


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def artifact_dir(version: str, root: Optional[str] = None) -> str:
    return os.path.join(root or MODELS_DIR, version)


def save(version: str, model, family: str, params: Dict[str, Any], training: Dict[str, Any],
         root: Optional[str] = None, tuning: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """Writes the artifact and its manifest; returns the manifest. An existing artifact of the version is never
    overwritten (a new model is a new version)."""
    import joblib
    root = root or MODELS_DIR
    d = artifact_dir(version, root)
    if os.path.exists(os.path.join(d, "model.joblib")):
        raise ArtifactError(f"{version} already has an artifact in {d}: a retrained model needs a new version name")
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, "model.joblib")
    joblib.dump({"model": model, "features": ml.CONFIGS[ml.CONFIG_OF[version]], "classes": list(ml.CLASSES),
                 "version": version}, path)
    manifest = {
        "version": version, "config": ml.CONFIG_OF[version], "family": family, "params": params,
        "estimator": ml.MODELS[family]["estimator"], "preprocessing": ml.MODELS[family]["preprocessing"],
        "feature_version": ml.FEATURE_VERSION, "features": ml.CONFIGS[ml.CONFIG_OF[version]],
        "instruments": (["NQ"] if ml.CONFIG_OF[version] == "nq_only" else ["NQ", *ml.INSTRUMENTS]),
        "classes": list(ml.CLASSES), "training": training, "tuning": tuning or [], "software": software(),
        "path": os.path.join("data", "models", "nq_ml", version, "model.joblib"),     # where production keeps it
        "sha256": _sha256(path)}
    with open(os.path.join(d, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, sort_keys=True)
    return manifest


def manifest(version: str, root: Optional[str] = None) -> Optional[Dict[str, Any]]:
    path = os.path.join(artifact_dir(version, root), "manifest.json")
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load(version: str, root: Optional[str] = None, expected_sha256: Optional[str] = None) -> Dict[str, Any]:
    """The artifact (model, features, classes) and its manifest, checked byte for byte against the manifest's sha256
    (and ``expected_sha256``, the registered one, when given)."""
    import joblib
    m = manifest(version, root)
    if m is None:
        raise ArtifactError(f"no artifact for {version} in {artifact_dir(version, root)}")
    path = os.path.join(artifact_dir(version, root), "model.joblib")
    sha = _sha256(path)
    if sha != m["sha256"] or (expected_sha256 is not None and sha != expected_sha256):
        raise ArtifactError(f"{version}: the artifact's sha256 {sha[:12]} differs from its manifest's "
                            f"{m['sha256'][:12]}" + (f" or the registered {expected_sha256[:12]}" if expected_sha256
                                                     else ""))
    art = joblib.load(path)
    if art["features"] != ml.CONFIGS[ml.CONFIG_OF[version]] or art["classes"] != list(ml.CLASSES):
        raise ArtifactError(f"{version}: the artifact's features or classes are not the contract's")
    return {**art, "manifest": m}


def records(root: Optional[str] = None) -> List[Dict[str, Any]]:
    """The registered definitions of the ML forecasts whose artifact exists (contracts/nq_ml.algorithm_definition),
    with the feature definitions and the ML forecast schema."""
    from contracts import nq_prompt_v2 as defs
    out = [ml.features_record(), ml.schema_record()]
    for version in ml.ALGORITHMS:
        m = manifest(version, root)
        if m is not None:
            out.append(defs._record(version, "forecast_algorithm", ml.algorithm_definition(version, m)))
    return out
