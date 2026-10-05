# forecaster/forecast_baseline.py
"""
The deterministic P1 baseline forecast (contracts/nq_forecast.BASELINE,
nq_baseline_p1_v1): pure functions on a run's frozen evidence
(forecaster/forecast_service.freeze_forecast_evidence) - no database, no LLM.

  distribution(target, members, prior)   the smoothed analogue distribution of one
                                          target, exactly: (count + k x prior) / (n + k)
  choose(distribution)                    the most probable class, or the classes of
                                          an exact tie (never resolved to a neutral one)
  reference_targets(evidence)             the frozen candidates above / below the
                                          cutoff price, nearest first
  baseline_forecast(evidence, algorithm)  every P1 target's prediction and the
                                          run-level outputs, under the baseline
                                          (nq_baseline_p1_v1, arm B) or the prior
                                          alone (nq_prior_p1_v1, arm A)

Every estimate is conditional on classifiable outcomes: the analogues and prior
sessions without a label for a target are counted beside its distribution, not
turned into a class.
"""

from __future__ import annotations

from decimal import Decimal
from fractions import Fraction
from typing import Any, Dict, List, Optional, Sequence

from contracts import nq_forecast as fc
from contracts import nq_preopen as pre
from contracts import nq_prompt_v2 as defs


def exact(x: Fraction) -> str:
    """A fraction as stored: "numerator/denominator"."""
    return f"{x.numerator}/{x.denominator}"


def parse(value: str) -> Fraction:
    return Fraction(value)


def _price(level: Dict[str, Any]) -> Fraction:
    return Fraction(level["exact"]) if level.get("exact") else Fraction(Decimal(str(level["value"])))


def distribution(target: str, members: Sequence[Optional[str]], prior_counts: Dict[str, int],
                 prior_without_label: int, k: int = pre.SMOOTHING_PSEUDO_COUNT) -> Dict[str, Any]:
    """
    One target's baseline estimate. ``members`` are the selected analogues' labels for the target (None: no label),
    ``prior_counts`` the earlier sessions' class counts. Returns the estimation status (analogues | prior_only |
    none), the exact distribution (None without a prior label: the smoothed baseline needs one) and its
    denominators.
    """
    vocab = list(defs.TARGETS[target]["labels"])
    labels = [x for x in members if x is not None]
    n, P = len(labels), sum(prior_counts.get(c, 0) for c in vocab)
    out = {"eligible": n, "without_label": len(members) - n, "prior_sessions": P,
           "prior_without_label": prior_without_label, "distribution": None}
    if P == 0:
        return {**out, "estimation_status": "none"}
    dist = {c: (labels.count(c) + k * Fraction(prior_counts.get(c, 0), P)) / (n + k) for c in vocab}
    assert sum(dist.values()) == 1
    return {**out, "estimation_status": "analogues" if n else "prior_only", "distribution": dist}


def choose(dist: Dict[str, Fraction]) -> List[str]:
    """The classes with the highest probability, in vocabulary order: one, or the classes of an exact tie."""
    top = max(dist.values())
    return [c for c, p in dist.items() if p == top]


def reference_targets(evidence: Dict[str, Any], per_side: int = 2) -> Dict[str, Any]:
    """
    The frozen candidates with a valid price on each side of the cutoff price, nearest first (BASELINE
    reference_targets): ``{'cutoff_price', 'upside': [...], 'downside': [...], 'reason'}``, each entry
    ``{'id', 'price', 'distance', 'coincident'}``. Candidates at one price are one level, named by precedence.
    """
    cp = evidence.get("cutoff_price")
    if not cp:
        return {"cutoff_price": None, "upside": [], "downside": [], "reason": "no valid cutoff price"}
    close = _price(cp)
    by_price: Dict[Fraction, List[str]] = {}
    for name in defs.FIRST_LEVEL_PRECEDENCE:
        level = (evidence.get("candidates") or {}).get(name) or {}
        if level.get("status") == "valid" and level.get("value") is not None:
            by_price.setdefault(_price(level), []).append(name)
    out: Dict[str, Any] = {"cutoff_price": cp["value"], "upside": [], "downside": [], "reason": None}
    for price in sorted(by_price, key=lambda p: (abs(p - close), p)):
        if price == close:
            continue
        side = "upside" if price > close else "downside"
        if len(out[side]) < per_side:
            names = by_price[price]
            out[side].append({"id": names[0], "price": evidence["candidates"][names[0]]["value"],
                              "distance": exact(abs(price - close)), "coincident": names[1:]})
    return out


def _eligibility(target: str, evidence: Dict[str, Any]) -> Optional[str]:
    """Why the target cannot be predicted for this session, or None."""
    if defs.TARGETS[target]["standard_session_only"] and evidence["schedule"] != "full":
        return (f"a standard-session target on a {evidence['schedule'].replace('_', ' ')} session (its realised "
                f"label would be shortened_session)")
    if target == "first_level_tested":
        missing = [n for n in defs.FIRST_LEVEL_CANDIDATES
                   if ((evidence.get("candidates") or {}).get(n) or {}).get("status") != "valid"]
        if missing:
            return (f"frozen candidate(s) unavailable at the cutoff: {', '.join(missing)} (the realised first "
                    f"level would be missing_reference)")
    return None


def baseline_forecast(evidence: Dict[str, Any], algorithm: str = fc.BASELINE_VERSION) -> Dict[str, Any]:
    """Every P1 target's prediction from the frozen evidence under ``algorithm`` (the smoothed analogue baseline,
    or the earlier-session prior alone, which ignores the analogues), and the run-level outputs."""
    if algorithm not in fc.ALGORITHMS:
        raise ValueError(f"unknown forecast algorithm {algorithm!r}")
    members = [] if algorithm == fc.PRIOR_VERSION else evidence["members"]     # arm C smooths like arm B
    predictions = {}
    for _, target in fc.FORECAST_TARGETS:
        est = distribution(target, [m["labels"].get(target) for m in members],
                           evidence["prior"]["counts"][target], evidence["prior"]["without_label"][target])
        dist = est.pop("distribution")
        why = _eligibility(target, evidence)
        if why is not None:
            predictions[target] = {**est, "status": "unavailable", "predicted_label": None, "distribution": None,
                                   "reason": why}
            continue
        if dist is None:
            predictions[target] = {**est, "status": "unavailable", "predicted_label": None, "distribution": None,
                                   "reason": "no earlier session with a label: the smoothed baseline needs a prior"}
            continue
        best = choose(dist)
        stored = {c: exact(p) for c, p in dist.items()}
        if len(best) > 1:
            predictions[target] = {**est, "status": "ambiguous_prediction", "predicted_label": None,
                                   "distribution": stored,
                                   "reason": f"exact tie at {exact(dist[best[0]])}: {', '.join(best)}"}
        else:
            predictions[target] = {**est, "status": "predicted", "predicted_label": best[0], "distribution": stored,
                                   "reason": None}
    first = predictions["first_level_tested"]
    level = (evidence.get("candidates") or {}).get(first["predicted_label"] or "") or {}
    outputs = {
        "first_level_price": level.get("value") if first["status"] == "predicted" else None,
        "reference_targets": reference_targets(evidence),
        "forecast_confidence": None,
        "forecast_confidence_reason": "no evidence-quality convention is registered (P1 field 36)",
        "probabilities": predictions[fc.PROBABILITY_TARGET]["distribution"],
    }
    return {"predictions": predictions, "outputs": outputs}
