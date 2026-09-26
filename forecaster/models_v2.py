# forecaster/models_v2.py
"""
Model versions for the v2 records, and the deterministic baseline.

A model version names the feature version it reads, the label version it
predicts, its targets and its *required features* - the documented subset of
the catalogue that decides its ``input_quality_status``. A predictor consumes a
stored snapshot only; it never reads bars or computes indicators.

``nq_climatology_v1`` is the reference any real model has to beat: per target,
the Laplace-smoothed frequency of each label over earlier sessions whose
outcome was knowable before the forecast's cutoff. It abstains when its
required inputs are invalid (the labels are measured in units of A from P, so a
snapshot without them has no well-defined target) or when it has too few
training sessions.
"""

import hashlib
import json
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Dict, List, Tuple

from database import forecast_store as store
from features import catalogue as catv2
from forecaster import labels_v2

CLIMATOLOGY = {
    "model_version": "nq_climatology_v1",
    "feature_version": catv2.FEATURE_VERSION,
    "label_version": labels_v2.LABEL_VERSION,
    "target_ids": list(labels_v2.TARGETS),
    "required_features": ["daily_atr_fraction", "gap_signed_atr"],
    "description": "Laplace-smoothed label frequencies over earlier sessions (no features used "
                   "beyond the input-quality gate). The baseline to beat.",
    "parameters": {"alpha": 1.0, "min_training_sessions": 20, "max_training_sessions": 250},
}

MODELS = {CLIMATOLOGY["model_version"]: CLIMATOLOGY}


def registry_record(model: Dict[str, Any]) -> Dict[str, Any]:
    body = {k: model[k] for k in ("feature_version", "label_version", "target_ids",
                                  "required_features", "parameters")}
    return {**model, "definition_hash": hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()}


def predict(conn, snapshot: Dict[str, Any], model: Dict[str, Any] = CLIMATOLOGY,
            code_revision: str = None) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """(run record, prediction records) for one stored snapshot."""
    params = model["parameters"]
    vocab = store.get_label_vocabulary(conn, model["label_version"])
    quality = catv2.quality_status(snapshot["feature_status"], model["required_features"])
    live = snapshot["data_mode"] == "live_capture"
    # Outcomes must have been knowable before the cutoff; a live run additionally
    # uses only outcome rows that already existed when it ran.
    available_by = snapshot["cutoff_at"]
    computed_by = datetime.now(timezone.utc) if live else None

    predictions, fits = [], {}
    for target in model["target_ids"]:
        labels = vocab[target]
        if quality == "invalid":
            predictions.append({"target_id": target, "abstained": True,
                                "abstention_reason": "input_quality_invalid"})
            continue
        rows = store.training_outcomes(
            conn, model["label_version"], target, model["feature_version"],
            before_session=snapshot["session_date"], available_by=available_by,
            computed_by=computed_by, limit=params["max_training_sessions"])
        counts = Counter(r["actual_label"] for r in rows)
        fits[target] = {"n": len(rows), "counts": {lab: counts.get(lab, 0) for lab in labels},
                        "first_session": rows[-1]["session_date"] if rows else None,
                        "last_session": rows[0]["session_date"] if rows else None}
        if len(rows) < params["min_training_sessions"]:
            predictions.append({"target_id": target, "abstained": True,
                                "abstention_reason": f"insufficient_training_sessions ({len(rows)} < "
                                                     f"{params['min_training_sessions']})"})
            continue
        a, n, k = params["alpha"], len(rows), len(labels)
        probs = {lab: (counts.get(lab, 0) + a) / (n + k * a) for lab in labels}
        best = max(labels, key=lambda lab: (probs[lab], -labels.index(lab)))
        predictions.append({"target_id": target, "predicted_label": best, "probabilities": probs,
                            "abstained": False})

    generated_at = datetime.now(timezone.utc)
    run = {
        "snapshot_id": snapshot["snapshot_id"],
        "model_version": model["model_version"],
        "label_version": model["label_version"],
        "generated_at": generated_at,
        "input_quality_status": quality,
        "code_revision": code_revision,
        "calibration": {
            "method": "laplace_smoothed_empirical_frequency",
            "alpha": params["alpha"],
            "fitted_at": generated_at.isoformat(),
            "outcome_selection": {
                "available_by": str(available_by),
                "computed_by": computed_by.isoformat() if computed_by else None,
                "revision": "latest revision per session satisfying both bounds",
                "one_per_session": "live capture preferred, else newest snapshot",
            },
            "training": fits,
        },
    }
    return run, predictions
