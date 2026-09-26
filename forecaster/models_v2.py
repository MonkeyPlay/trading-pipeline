# forecaster/models_v2.py
"""
Model versions for the v2 records: a trained scikit-learn classifier and the
climatology baseline it has to beat. No language model and no network call is
involved anywhere: a predictor receives a stored snapshot's typed feature
payload and returns, per target, a full probability distribution over the
target's vocabulary (nq_schema_v2, section 7).

A model version names the feature version it reads, the label version it
predicts, its targets, its *required features* (which decide the run's
``input_quality_status``) and its parameters - for the trained model that is the
explicit feature allowlist, the preprocessing, the candidate estimators and the
selection rule, so training and inference are identical by construction.

``nq_sklearn_v1``
    Trained walk-forward: to forecast session D it fits only on earlier sessions
    whose realised label was knowable before D's cutoff (and, live, already
    computed). Per target, every candidate - the class prior, L2 logistic
    regressions and a shallow gradient-boosted tree ensemble - is scored by
    chronological cross-validation (TimeSeriesSplit, log loss); the best one is
    refitted on the whole training window. A feature-based candidate is used
    only when it beats the prior out of sample, so the model never does worse
    than climatology on its own validation.

``nq_climatology_v2``
    Laplace-smoothed label frequencies over the same training sessions.

Prediction status (section 7):
    issued       a label (the arg-max, ties by vocabulary order) + probabilities
    abstained    probabilities kept, no actionable label: out_of_distribution,
                 shortened_session (full-RTH targets on an early close),
                 event_policy or uncertainty, when those policies are enabled
    unavailable  no probabilities: data_quality (a required input invalid) or
                 uncertainty (too few training sessions)
"""

import hashlib
import json
import logging
import warnings
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from database import forecast_store as store
from features import calendar as cal
from features import catalogue as catv2
from features.indicators import finite
from forecaster import labels_v2

logger = logging.getLogger(__name__)

PREDICTION_STATUSES = ("issued", "abstained", "unavailable")
DECISION_REASONS = ("none", "data_quality", "uncertainty", "out_of_distribution", "event_policy",
                    "late_generation", "shortened_session")
CALIBRATION_STATUSES = ("unvalidated", "validated_raw", "calibrated")
RTH_TARGETS = ("direction_rth", "session_type_rth")

# Inputs of the trained model: an explicit, compact allowlist (section 12). Left
# out on purpose: fields that are always null with the configured sources
# (us2y_change_bps, the 10y-2y spot curve, cash DXY), the optional GC/CL
# extensions, and near-duplicates of listed fields (distance_pdh/pdl_atr and
# overnight_range_position are linear in the range-position and ON distances;
# nq_preopen_return is gap_signed_atr * daily_atr_fraction; atr_1m_14_fraction
# is carried by its 30-day relative).
SKLEARN_FEATURES = {
    "numeric": [
        "daily_atr_fraction", "daily_volatility_ratio", "atr_1m_14_relative_30d",
        "gap_signed_atr", "prior_range_position", "distance_onh_atr", "distance_onl_atr",
        "overnight_range_atr", "distance_on_vwap_hlc3_atr",
        "return_15m_atr", "return_60m_atr", "range_60m_atr", "efficiency_60m",
        "ema200_distance_5m_atr", "ema9_21_spread_5m_atr", "ema20_slope_15m_atr",
        "rvol_overnight_30d", "rvol_60m_30d",
        "prior_rth_return_atr", "prior_rth_range_atr", "prior_rth_close_location",
        "es_preopen_return", "rty_preopen_return", "nq_es_relative_return",
        "nq_es_standardized_divergence", "nq_es_relative_return_60m",
        "vix_level", "vix_change_points", "vxn_level", "vxn_change_points",
        "us10y_change_bps", "smh_preopen_return", "dx_fut_preopen_return",
        "us10y_yield_fut_change_bps", "yield_fut_curve_10y_2y_change_bps",
        "days_to_nq_expiry", "minutes_to_high_event",
    ],
    "boolean": ["monthly_opex_week", "roll_transition", "is_early_close", "has_future_high_event"],
    "categorical": ["weekday", "remaining_event_risk"],
}
MISSING_CATEGORY = "missing"

_REQUIRED = ["daily_atr_fraction", "gap_signed_atr"]   # the labels are measured in units of A

CLIMATOLOGY = {
    "model_version": "nq_climatology_v2",
    "kind": "climatology",
    "feature_version": catv2.FEATURE_VERSION,
    "label_version": labels_v2.LABEL_VERSION,
    "target_ids": list(labels_v2.TARGETS),
    "required_features": _REQUIRED,
    "description": "Laplace-smoothed label frequencies over earlier sessions (no features used beyond the "
                   "input-quality gate). The baseline to beat.",
    "parameters": {"alpha": 1.0, "min_training_sessions": 20, "max_training_sessions": 250},
}

SKLEARN = {
    "model_version": "nq_sklearn_v1",
    "kind": "sklearn",
    "feature_version": catv2.FEATURE_VERSION,
    "label_version": labels_v2.LABEL_VERSION,
    "target_ids": list(labels_v2.TARGETS),
    "required_features": _REQUIRED,
    "description": "scikit-learn classifier per target, trained walk-forward on earlier sessions' snapshots "
                   "and realised labels; the estimator is chosen by chronological cross-validated log loss "
                   "among the class prior, logistic regression and gradient boosting.",
    "parameters": {
        "features": SKLEARN_FEATURES,
        "preprocessing": {
            "numeric_and_boolean": "float (booleans 0/1); median imputation fitted on the training window "
                                   "plus a missing-indicator column per feature with missing training "
                                   "values; standard scaling",
            "categorical": "null -> 'missing'; one-hot over the catalogue's allowed values + 'missing'",
        },
        "candidates": [
            {"name": "prior", "estimator": "DummyClassifier", "params": {"strategy": "prior"}},
            {"name": "logistic_c0.05", "estimator": "LogisticRegression", "params": {"C": 0.05, "max_iter": 2000}},
            {"name": "logistic_c0.5", "estimator": "LogisticRegression", "params": {"C": 0.5, "max_iter": 2000}},
            {"name": "hist_gradient_boosting", "estimator": "HistGradientBoostingClassifier",
             "params": {"max_depth": 3, "learning_rate": 0.05, "max_iter": 150, "min_samples_leaf": 20,
                        "l2_regularization": 1.0, "early_stopping": False}},
        ],
        "selection": {"metric": "log_loss", "cv": "TimeSeriesSplit", "n_splits": 5,
                      "min_fold_train_sessions": 40, "ties": "earlier candidate wins"},
        "probability_smoothing": "p = (1 - lam) * p_model + lam * laplace_prior, lam = k*alpha / (n + k*alpha); "
                                 "gives unseen classes a non-zero probability",
        "alpha": 1.0,
        "min_training_sessions": 60,
        "max_training_sessions": 1500,
        "random_state": 0,
        "out_of_distribution": {"abs_z": 8.0, "min_features": 3,
                                "rule": "abstain when at least min_features numeric inputs lie more than "
                                        "abs_z training standard deviations from the training mean"},
        "abstain_on_high_event": False,
        "min_top_probability": None,
    },
}

MODELS = {m["model_version"]: m for m in (SKLEARN, CLIMATOLOGY)}
DEFAULT_MODEL = SKLEARN["model_version"]


def _utc(ts) -> pd.Timestamp:
    """A timestamp as tz-aware UTC; the store returns naive UTC strings."""
    t = pd.Timestamp(ts)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


def registry_record(model: Dict[str, Any]) -> Dict[str, Any]:
    body = {k: model[k] for k in ("feature_version", "label_version", "target_ids",
                                  "required_features", "parameters")}
    rec = {k: v for k, v in model.items() if k != "kind"}
    return {**rec, "definition_hash": hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()}


# --------------------------------------------------------------------------
# Design matrix
# --------------------------------------------------------------------------

def design_matrix(payloads: List[Dict[str, Any]], feats=SKLEARN_FEATURES) -> pd.DataFrame:
    """The allowlisted features of each payload, typed: floats (NaN for null),
    booleans as 0/1, categoricals as strings with nulls as 'missing'."""
    cols: Dict[str, list] = {}
    for n in feats["numeric"]:
        cols[n] = [np.nan if (v := finite(p.get(n))) is None else v for p in payloads]
    for n in feats["boolean"]:
        cols[n] = [float(p[n]) if isinstance(p.get(n), bool) else np.nan for p in payloads]
    for n in feats["categorical"]:
        allowed = catv2.BY_NAME[n].allowed
        cols[n] = [p.get(n) if p.get(n) in allowed else MISSING_CATEGORY for p in payloads]
    return pd.DataFrame(cols, columns=feats["numeric"] + feats["boolean"] + feats["categorical"])


def _preprocessor(feats=SKLEARN_FEATURES):
    from sklearn.compose import ColumnTransformer
    from sklearn.impute import SimpleImputer
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import OneHotEncoder, StandardScaler

    numeric = Pipeline([
        ("impute", SimpleImputer(strategy="median", add_indicator=True, keep_empty_features=True)),
        ("scale", StandardScaler()),
    ])
    categories = [list(catv2.BY_NAME[n].allowed) + [MISSING_CATEGORY] for n in feats["categorical"]]
    onehot = OneHotEncoder(categories=categories, handle_unknown="ignore", sparse_output=False)
    return ColumnTransformer([("num", numeric, feats["numeric"] + feats["boolean"]),
                              ("cat", onehot, feats["categorical"])])


def _estimator(spec: Dict[str, Any], seed: int):
    from sklearn.dummy import DummyClassifier
    from sklearn.ensemble import HistGradientBoostingClassifier
    from sklearn.linear_model import LogisticRegression

    cls = {"DummyClassifier": DummyClassifier, "LogisticRegression": LogisticRegression,
           "HistGradientBoostingClassifier": HistGradientBoostingClassifier}[spec["estimator"]]
    params = dict(spec["params"])
    if "random_state" in cls().get_params():
        params.setdefault("random_state", seed)
    return cls(**params)


def _pipeline(spec, seed, feats=SKLEARN_FEATURES):
    from sklearn.pipeline import Pipeline
    return Pipeline([("features", _preprocessor(feats)), ("model", _estimator(spec, seed))])


def _laplace(y: List[str], labels: List[str], alpha: float) -> np.ndarray:
    counts = Counter(y)
    n, k = len(y), len(labels)
    return np.array([(counts.get(lab, 0) + alpha) / (n + k * alpha) for lab in labels])


def _probabilities(pipe, X: pd.DataFrame, y_train: List[str], labels: List[str], alpha: float) -> np.ndarray:
    """Rows of probabilities over ``labels`` (vocabulary order), shrunk toward the
    Laplace prior of the training labels so that unseen classes stay possible."""
    raw = pipe.predict_proba(X)
    p = np.zeros((len(X), len(labels)))
    for j, c in enumerate(pipe.classes_):
        p[:, labels.index(c)] = raw[:, j]
    k, n = len(labels), len(y_train)
    lam = k * alpha / (n + k * alpha)
    p = (1 - lam) * p + lam * _laplace(y_train, labels, alpha)
    return p / p.sum(axis=1, keepdims=True)


def _fit_candidate(spec, X, y, seed):
    """Fits ``spec``, or the class prior when the training labels hold a single class."""
    if len(set(y)) < 2:
        spec = {"name": "prior", "estimator": "DummyClassifier", "params": {"strategy": "prior"}}
    pipe = _pipeline(spec, seed)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        pipe.fit(X, np.asarray(y, dtype=object))
    return pipe


# --------------------------------------------------------------------------
# Training
# --------------------------------------------------------------------------

def _training_rows(conn, model, target, session_date, available_by, computed_by):
    rows = store.training_outcomes(
        conn, model["label_version"], target, model["feature_version"], before_session=session_date,
        available_by=available_by, computed_by=computed_by,
        limit=model["parameters"]["max_training_sessions"])
    return list(reversed(rows))   # chronological


def fit_target(rows: List[Dict[str, Any]], labels: List[str], params: Dict[str, Any]) -> Dict[str, Any]:
    """
    Model selection and the final fit for one target from chronologically
    ordered training rows ({'session_date', 'features', 'actual_label'}).
    """
    from sklearn.model_selection import TimeSeriesSplit

    y = [r["actual_label"] for r in rows]
    fit: Dict[str, Any] = {
        "n": len(rows), "counts": {lab: y.count(lab) for lab in labels},
        "first_session": str(rows[0]["session_date"]) if rows else None,
        "last_session": str(rows[-1]["session_date"]) if rows else None,
    }
    if len(rows) < params["min_training_sessions"]:
        fit["status"] = "insufficient_training_sessions"
        return fit

    X = design_matrix([r["features"] for r in rows], params["features"])
    sel, seed, alpha = params["selection"], params["random_state"], params["alpha"]
    n_splits = min(sel["n_splits"], len(rows) - 1)
    splits = ([(tr, te) for tr, te in TimeSeriesSplit(n_splits=n_splits).split(X)
               if len(tr) >= sel["min_fold_train_sessions"]] if n_splits >= 2 else [])
    scores = {}
    for spec in params["candidates"]:
        loss = hits = count = 0.0
        for tr, te in splits:
            y_tr = [y[i] for i in tr]
            pipe = _fit_candidate(spec, X.iloc[tr], y_tr, seed)
            p = _probabilities(pipe, X.iloc[te], y_tr, labels, alpha)
            idx = np.array([labels.index(y[i]) for i in te])
            loss += float(-np.log(np.clip(p[np.arange(len(te)), idx], 1e-15, 1.0)).sum())
            hits += float((p.argmax(axis=1) == idx).sum())
            count += len(te)
        if count:
            scores[spec["name"]] = {"log_loss": loss / count, "accuracy": hits / count, "n_validation": int(count)}
    best = (min(params["candidates"], key=lambda c: scores[c["name"]]["log_loss"])
            if scores else params["candidates"][0])   # min() keeps the earliest on ties

    pipe = _fit_candidate(best, X, y, seed)
    numeric = X[params["features"]["numeric"]]
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")   # all-null columns
        mean, std = numeric.mean(), numeric.std(ddof=1)
    fit.update({
        "status": "ok", "selected": best["name"], "cv": scores, "cv_folds": len(splits),
        "calibration_status": "validated_raw" if scores else "unvalidated",
        "pipeline": pipe, "y": y,
        "numeric_mean": {k: finite(v) for k, v in mean.items()},
        "numeric_std": {k: finite(v) for k, v in std.items()},
    })
    return fit


def fit(conn, model: Dict[str, Any], session_date, cutoff_at, computed_by=None) -> Dict[str, Any]:
    """
    Trains ``model`` for forecasting ``session_date``: only sessions before it
    whose outcome was knowable by ``cutoff_at`` (and computed by ``computed_by``,
    when given) are used. The result can be passed to ``predict`` for any
    snapshot of that session or a later one.
    """
    import sklearn

    vocab = labels_v2.vocabulary()
    params = model["parameters"]
    targets = {}
    for target in model["target_ids"]:
        rows = _training_rows(conn, model, target, str(session_date), _utc(cutoff_at).to_pydatetime(),
                              None if computed_by is None else _utc(computed_by).to_pydatetime())
        if model["kind"] == "climatology":
            y = [r["actual_label"] for r in rows]
            targets[target] = {
                "n": len(rows), "counts": {lab: y.count(lab) for lab in vocab[target]}, "y": y,
                "first_session": str(rows[0]["session_date"]) if rows else None,
                "last_session": str(rows[-1]["session_date"]) if rows else None,
                "status": "ok" if len(rows) >= params["min_training_sessions"] else "insufficient_training_sessions",
                "selected": "laplace_frequency", "calibration_status": "unvalidated",
            }
        else:
            targets[target] = fit_target(rows, vocab[target], params)
    return {
        "model_version": model["model_version"],
        "session_date": str(session_date),
        "available_by": _utc(cutoff_at).isoformat(),
        "computed_by": _utc(computed_by).isoformat() if computed_by is not None else None,
        "fitted_at": datetime.now(timezone.utc).isoformat(),
        "sklearn_version": sklearn.__version__,
        "targets": targets,
    }


def training_report(fitted: Dict[str, Any]) -> Dict[str, Any]:
    """The JSON-safe part of a fit: windows, class counts, CV scores, selection."""
    keep = ("status", "n", "counts", "first_session", "last_session", "selected", "cv", "cv_folds",
            "calibration_status")
    return {**{k: v for k, v in fitted.items() if k != "targets"},
            "targets": {t: {k: f[k] for k in keep if k in f} for t, f in fitted["targets"].items()}}


def save_artifact(fitted: Dict[str, Any], path: str) -> None:
    import joblib
    joblib.dump(fitted, path)
    with open(path.rsplit(".", 1)[0] + ".json", "w") as f:
        json.dump(training_report(fitted), f, indent=2, sort_keys=True, allow_nan=False)


def load_artifact(path: str) -> Dict[str, Any]:
    import joblib
    return joblib.load(path)


# --------------------------------------------------------------------------
# Prediction
# --------------------------------------------------------------------------

def _out_of_distribution(features: Dict[str, Any], tf: Dict[str, Any], rule: Dict[str, Any]) -> List[str]:
    far = []
    for name, mu in tf.get("numeric_mean", {}).items():
        x, sd = finite(features.get(name)), tf["numeric_std"].get(name)
        if x is not None and mu is not None and sd and abs(x - mu) / sd > rule["abs_z"]:
            far.append(name)
    return far


def predict(conn, snapshot: Dict[str, Any], model: Dict[str, Any] = SKLEARN, code_revision: str = None,
            fitted: Optional[Dict[str, Any]] = None) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """
    (run record, prediction records) for one stored snapshot. Trains the model
    first unless ``fitted`` - from ``fit`` for this session or an earlier one -
    is given.
    """
    params = model["parameters"]
    vocab = store.get_label_vocabulary(conn, model["label_version"])
    quality = catv2.quality_status(snapshot["feature_status"], model["required_features"])
    live = snapshot["data_mode"] == "live_capture"
    s = cal.session(snapshot["session_date"])
    if fitted is None:
        # Outcomes must have been knowable before the cutoff; a live run additionally
        # uses only outcome rows that already existed when it ran.
        fitted = fit(conn, model, snapshot["session_date"], snapshot["cutoff_at"],
                     datetime.now(timezone.utc) if live else None)
    elif (fitted["model_version"] != model["model_version"]
          or fitted["session_date"] > str(snapshot["session_date"])
          or _utc(fitted["available_by"]) > _utc(snapshot["cutoff_at"])):
        raise ValueError(f"model fitted for {fitted['model_version']} / {fitted['session_date']} cannot "
                         f"forecast {model['model_version']} / {snapshot['session_date']}: it may have seen "
                         f"outcomes that were not knowable at that cutoff.")

    features = snapshot["features"]
    X = design_matrix([features], params["features"]) if model["kind"] == "sklearn" else None
    predictions, ood_by_target = [], {}
    for target in model["target_ids"]:
        labels, tf = vocab[target], fitted["targets"][target]
        pred = {"target_id": target, "calibration_status": tf.get("calibration_status", "unvalidated")}
        if quality == "invalid":
            bad = [f for f in model["required_features"] if snapshot["feature_status"].get(f) != "valid"]
            predictions.append({**pred, "prediction_status": "unavailable", "decision_reason": "data_quality",
                                "abstained": True, "abstention_reason": f"required input invalid: {', '.join(bad)}"})
            continue
        if tf["status"] != "ok":
            predictions.append({**pred, "prediction_status": "unavailable", "decision_reason": "uncertainty",
                                "abstained": True, "abstention_reason": f"insufficient_training_sessions "
                                f"({tf['n']} < {params['min_training_sessions']})"})
            continue

        if model["kind"] == "sklearn":
            p = _probabilities(tf["pipeline"], X, tf["y"], labels, params["alpha"])[0]
        else:
            p = _laplace(tf["y"], labels, params["alpha"])
        probs = {lab: float(v) for lab, v in zip(labels, p)}
        best = labels[int(np.argmax(p))]   # first maximum = vocabulary order on ties

        reason, detail = "none", None
        far = _out_of_distribution(features, tf, params["out_of_distribution"]) if model["kind"] == "sklearn" else []
        if far:
            ood_by_target[target] = far
        if target in RTH_TARGETS and s.is_early_close:
            reason, detail = "shortened_session", "early close: full-RTH targets are not issued"
        elif len(far) >= params.get("out_of_distribution", {}).get("min_features", 1 << 30):
            reason, detail = "out_of_distribution", f"far from the training data: {', '.join(far)}"
        elif params.get("abstain_on_high_event") and features.get("has_future_high_event") is True:
            reason, detail = "event_policy", "high-tier event scheduled before the close"
        elif params.get("min_top_probability") and max(p) < params["min_top_probability"]:
            reason, detail = "uncertainty", f"top probability {max(p):.3f} < {params['min_top_probability']}"
        if reason == "none":
            predictions.append({**pred, "prediction_status": "issued", "decision_reason": "none",
                                "predicted_label": best, "probabilities": probs, "abstained": False})
        else:
            predictions.append({**pred, "prediction_status": "abstained", "decision_reason": reason,
                                "probabilities": probs, "abstained": True, "abstention_reason": detail})

    generated_at = datetime.now(timezone.utc)
    run = {
        "snapshot_id": snapshot["snapshot_id"],
        "model_version": model["model_version"],
        "label_version": model["label_version"],
        "generated_at": generated_at,
        "input_quality_status": quality,
        "code_revision": code_revision,
        "calibration_version": None,   # raw (smoothed) probabilities; no calibration model applied
        "calibration": {
            "method": ("sklearn_walk_forward_model_selection" if model["kind"] == "sklearn"
                       else "laplace_smoothed_empirical_frequency"),
            "alpha": params["alpha"],
            "fitted_at": fitted["fitted_at"],
            "fitted_for_session": fitted["session_date"],
            "sklearn_version": fitted.get("sklearn_version"),
            "outcome_selection": {
                "available_by": fitted["available_by"],
                "computed_by": fitted["computed_by"],
                "revision": "latest revision per session satisfying both bounds",
                "one_per_session": "live capture preferred, else newest snapshot",
            },
            "training": training_report(fitted)["targets"],
            "out_of_distribution_features": ood_by_target,
        },
    }
    return run, predictions
