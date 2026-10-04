# forecaster/forecast_display.py
"""
The forecast's part of P1's 47-field record (guideline revision 2, 3F and Appendix
B): fields 25-36 and 42-45 from one stored forecast run, read by its run id - never
recomputed, never the "latest" run. forecaster/preopen_display.p1_record joins them
with the snapshot, annotation and analogue-set fields.

  forecast_rows(run)     {property: (value, basis)} for the forecast's properties
  target_rows(run)       per target: status, class, distribution and denominators -
                         the detailed view beside P1's single classes

Probabilities are stored as exact fractions and shown as percentages with one
decimal; P1's Bullish / Bearish / Choppy Probability are the 15-minute direction
distribution (Choppy = the neutral band). An ambiguous prediction (an exact tie) or
an unavailable target shows "Unavailable" with its reason, never a neutral class.
"""

from __future__ import annotations

from fractions import Fraction
from typing import Any, Dict, List, Tuple

from contracts import nq_forecast as fc
from contracts import nq_prompt_v2 as defs

UNAVAILABLE = "Unavailable"
FORECAST_PROPERTIES = ([name for name, _ in fc.FORECAST_TARGETS[:6]] + list(fc.PROBABILITY_PROPERTIES)
                       + ["Expected First Level Tested", "Expected First Level Price", "Forecast Confidence"]
                       + list(fc.REFERENCE_TARGETS))


def percent(value: str) -> str:
    return f"{float(Fraction(value)) * 100:.1f}%"


def _price(value) -> str:
    return f"{float(value):.2f}"


def _basis(target: str, p: Dict[str, Any], algorithm: str) -> str:
    source = ("the earlier-session prior alone" if algorithm == fc.PRIOR_VERSION else
              {"analogues": f"{p['eligible']} analogue label(s) ({p['without_label']} without)",
               "prior_only": f"no analogue label ({p['without_label']} without): the prior only",
               "none": "no estimate"}[p["estimation_status"]])
    share = f"; p = {percent(p['distribution'][p['predicted_label']])}" if p["status"] == "predicted" else ""
    return (f"{algorithm}: {source}, prior {p['prior_sessions']} session(s) "
            f"({p['prior_without_label']} without a label){share}")


def forecast_rows(run: Dict[str, Any]) -> Dict[str, Tuple[str, str]]:
    """P1 fields 25-36 and 42-45 of a stored run as (value, basis)."""
    if run["lifecycle_status"] != "issued":
        why = f"run {run['run_id'][:8]} {run['lifecycle_status']}: {run['failure_reason']}"
        return {prop: (UNAVAILABLE, why) for prop in FORECAST_PROPERTIES}
    preds, outputs = run["predictions"], run["outputs"]
    out: Dict[str, Tuple[str, str]] = {}
    missing = {"status": "unavailable", "reason": "no prediction stored for this target", "distribution": None}
    for name, target in fc.FORECAST_TARGETS:
        p = preds.get(target) or missing
        if p["status"] == "predicted":
            out[name] = (defs.display(target, p["predicted_label"], "predicted"),
                         _basis(target, p, run["algorithm_version"]))
        else:
            out[name] = (UNAVAILABLE, f"{p['status'].replace('_', ' ')}: {p['reason']}")
    fifteen = preds.get(fc.PROBABILITY_TARGET) or missing
    dist = fifteen["distribution"]
    for name, cls in fc.PROBABILITY_PROPERTIES.items():
        out[name] = ((percent(dist[cls]), f"{fc.PROBABILITY_TARGET} distribution, {dist[cls]} (fraction stored)")
                     if dist else (UNAVAILABLE, fifteen["reason"]))
    price = outputs.get("first_level_price")
    out["Expected First Level Price"] = ((_price(price), "the predicted level's frozen price")
                                         if price is not None else out["Expected First Level Tested"])
    out["Forecast Confidence"] = (UNAVAILABLE, outputs.get("forecast_confidence_reason") or "not produced")
    targets = outputs.get("reference_targets") or {}
    for name, (side, rank) in fc.REFERENCE_TARGETS.items():
        levels = targets.get(side) or []
        if rank < len(levels):
            level = levels[rank]
            also = f" (also {', '.join(level['coincident'])})" if level["coincident"] else ""
            out[name] = (_price(level["price"]),
                         f"{defs.FIRST_LEVEL_CANDIDATES[level['id']]}{also}, "
                         f"{float(Fraction(level['distance'])):.2f} {'above' if side == 'upside' else 'below'} the "
                         f"cutoff price - a reference target, not a forecast that it is reached")
        else:
            out[name] = (UNAVAILABLE, targets.get("reason") or f"no further supported reference {side}")
    return out


def target_rows(run: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The detailed per-target view: P1's predicted property, status, class, distribution in percent and
    denominators."""
    rows = []
    for name, target in fc.FORECAST_TARGETS:
        p = run["predictions"].get(target)
        if p is None:
            continue
        rows.append({
            "property": name, "target": target, "status": p["status"], "estimation": p["estimation_status"],
            "class": defs.display(target, p["predicted_label"], "predicted") if p["predicted_label"] else UNAVAILABLE,
            "distribution": {defs.display(target, c, "predicted"): percent(v)
                             for c, v in (p["distribution"] or {}).items()},
            "eligible": p["eligible"], "without_label": p["without_label"], "prior_sessions": p["prior_sessions"],
            "prior_without_label": p["prior_without_label"], "reason": p["reason"]})
    return rows
