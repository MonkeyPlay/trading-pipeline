# forecaster/grading.py
"""
The Forecast page's grading of one session: every arm's current run against the
session's realised outcome (its latest outcome revision).

  per target   p(realised) - the probability the arm's distribution gave the class
               that happened (0-1) - and a hit when its predicted class was it; a
               target without a realised label (ambiguous, missing, an early close)
               is not graded and is listed with its reason
  per arm      targets graded, hits, the mean p(realised), and its difference to
               the benchmark - arm A, the earlier-session prior (guideline 4B: does
               additional structure beat a simple baseline?) - on the targets both
               graded
  chance       1 / the number of classes, per target, for orientation

A view of one session, not a score: stage 4's experiments score registered runs over
many sessions (forecaster/experiments.py), with log loss and Brier.
"""

from __future__ import annotations

from fractions import Fraction
from typing import Any, Dict, List, Optional

from contracts import nq_forecast as fc
from contracts import nq_prompt_v2 as defs

BENCHMARK = "A"


def current_runs(runs: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Per arm letter the newest issued run of ``runs`` (one session's, newest first)."""
    out: Dict[str, Dict[str, Any]] = {}
    for r in runs:
        arm = fc.arm_of(r["algorithm_version"])
        if arm and arm not in out and r["lifecycle_status"] == "issued":
            out[arm] = r
    return out


def _mean(values: List[float]) -> Optional[float]:
    return sum(values) / len(values) if values else None


def grade(runs: Dict[str, Dict[str, Any]], labels: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """The grading of ``runs`` (arm letter -> run with ``predictions``) against an outcome's ``labels``."""
    graded = [(name, t) for name, t in fc.FORECAST_TARGETS if (labels.get(t) or {}).get("label")]
    ungraded = [(name, t, (labels.get(t) or {}).get("reason") or "no realised label")
                for name, t in fc.FORECAST_TARGETS if not (labels.get(t) or {}).get("label")]
    arms: Dict[str, Dict[str, Any]] = {}
    for arm in sorted(runs):
        cells = {}
        for _, t in graded:
            p = (runs[arm].get("predictions") or {}).get(t) or {}
            dist, realised = p.get("distribution"), labels[t]["label"]
            if not dist:
                cells[t] = {"p": None, "hit": None, "predicted": None, "why": p.get("reason") or "no distribution"}
                continue
            cells[t] = {"p": float(Fraction(dist[realised])), "predicted": p.get("predicted_label"),
                        "hit": p.get("status") == "predicted" and p.get("predicted_label") == realised}
        scored = [c for c in cells.values() if c["p"] is not None]
        arms[arm] = {"cells": cells, "graded": len(scored), "hits": sum(bool(c["hit"]) for c in scored),
                     "mean_p": _mean([c["p"] for c in scored]), "vs_benchmark": None}
    bench = arms.get(BENCHMARK)
    if bench is not None:
        for arm, a in arms.items():
            if arm == BENCHMARK:
                continue
            common = [t for _, t in graded if a["cells"][t]["p"] is not None and bench["cells"][t]["p"] is not None]
            a["vs_benchmark"] = _mean([a["cells"][t]["p"] - bench["cells"][t]["p"] for t in common])
    return {"targets": graded, "ungraded": ungraded, "arms": arms,
            "realised": {t: labels[t]["label"] for _, t in graded},
            "chance": {t: 1 / len(defs.TARGETS[t]["labels"]) for _, t in graded}}
