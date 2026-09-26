# forecaster/scoring_v2.py
"""
Scores for stored v2 forecasts against their realised outcomes.

Rows are ``database.forecast_store.get_prediction_outcomes`` records. Two views:

  ``score_groups``        per model, target and data mode: how many predictions were
                          issued / abstained / unavailable, the accuracy of issued
                          labels, and the log loss and Brier score of every
                          distribution with an eligible outcome.
  ``paired_comparison``   each model against a baseline on exactly the sessions both
                          have a distribution for - the only fair comparison when
                          the two start issuing at different history lengths - with
                          skill scores (1 - model / baseline) and the mean per-session
                          log-loss gain with its standard error.
"""

import math
from typing import Any, Dict, Iterable, List, Optional, Tuple


def log_loss(probabilities: Dict[str, float], actual: str) -> float:
    return -math.log(max(probabilities.get(actual, 0.0), 1e-15))


def brier(probabilities: Dict[str, float], actual: str) -> float:
    return sum((p - (label == actual)) ** 2 for label, p in probabilities.items())


def _scorable(row) -> bool:
    return row["probabilities"] is not None and row["eligible"]


def score_groups(rows: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    groups: Dict[Tuple, List[Dict[str, Any]]] = {}
    for r in rows:
        groups.setdefault((r["model_version"], r["target_id"], r["data_mode"]), []).append(r)
    out = []
    for (model, target, mode), rs in sorted(groups.items()):
        status = [r["prediction_status"] for r in rs]
        issued = [r for r in rs if r["prediction_status"] == "issued" and r["eligible"]]
        probs = [r for r in rs if _scorable(r)]
        out.append({
            "model": model, "target": target, "mode": mode, "n": len(rs),
            "issued": status.count("issued"), "abstained": status.count("abstained"),
            "unavailable": status.count("unavailable"), "ineligible": sum(not r["eligible"] for r in rs),
            "accuracy": (sum(r["predicted_label"] == r["actual_label"] for r in issued) / len(issued)
                         if issued else math.nan),
            "log_loss": (sum(log_loss(r["probabilities"], r["actual_label"]) for r in probs) / len(probs)
                         if probs else math.nan),
            "brier": (sum(brier(r["probabilities"], r["actual_label"]) for r in probs) / len(probs)
                      if probs else math.nan),
        })
    return out


def _latest_per_session(rows, model) -> Dict[Tuple, Dict[str, Any]]:
    """The newest scorable prediction of ``model`` per (target, data mode, session)."""
    out: Dict[Tuple, Dict[str, Any]] = {}
    for r in rows:
        if r["model_version"] != model or not _scorable(r):
            continue
        key = (r["target_id"], r["data_mode"], str(r["session_date"]))
        if key not in out or str(r["generated_at"]) > str(out[key]["generated_at"]):
            out[key] = r
    return out


def paired_comparison(rows: Iterable[Dict[str, Any]], baseline: str,
                      models: Optional[Iterable[str]] = None) -> List[Dict[str, Any]]:
    """
    Per model (other than ``baseline``), target and data mode, over the sessions
    where both have a distribution and the outcome is eligible:
    ``n``, both log losses and Brier scores, ``log_loss_skill`` and
    ``brier_skill`` (1 - model / baseline; > 0 means better than the baseline),
    and ``gain`` / ``gain_se`` - the mean per-session log-loss improvement over the
    baseline and its standard error.
    """
    rows = list(rows)
    models = sorted(set(models or (r["model_version"] for r in rows)) - {baseline})
    base = _latest_per_session(rows, baseline)
    out = []
    for model in models:
        mine = _latest_per_session(rows, model)
        groups: Dict[Tuple[str, str], List[Tuple[Dict, Dict]]] = {}
        for key, r in mine.items():
            if key in base:
                groups.setdefault(key[:2], []).append((r, base[key]))
        for (target, mode), pairs in sorted(groups.items()):
            ll_m = [log_loss(m["probabilities"], m["actual_label"]) for m, _ in pairs]
            ll_b = [log_loss(b["probabilities"], b["actual_label"]) for _, b in pairs]
            br_m = [brier(m["probabilities"], m["actual_label"]) for m, _ in pairs]
            br_b = [brier(b["probabilities"], b["actual_label"]) for _, b in pairs]
            n = len(pairs)
            gains = [b - m for m, b in zip(ll_m, ll_b)]
            mean_gain = sum(gains) / n
            se = (math.sqrt(sum((g - mean_gain) ** 2 for g in gains) / (n - 1) / n) if n > 1 else math.nan)
            llm, llb, bm, bb = (sum(x) / n for x in (ll_m, ll_b, br_m, br_b))
            out.append({
                "model": model, "baseline": baseline, "target": target, "mode": mode, "n": n,
                "log_loss": llm, "baseline_log_loss": llb,
                "log_loss_skill": 1 - llm / llb if llb > 0 else math.nan,
                "brier": bm, "baseline_brier": bb,
                "brier_skill": 1 - bm / bb if bb > 0 else math.nan,
                "gain": mean_gain, "gain_se": se,
            })
    return out
