# matching/structural.py
"""
Structural analogue selection (guideline stage 2B / 2C, matcher nq_match_p1_v2 in
contracts/nq_preopen.py): P1 section 7's rubric over the pre-open structure
annotations, then the selected sessions' realised outcomes.

  features(annotation)        the rubric's 14 inputs of one annotation: the five
                              price locations and nine P1 fields (None when not
                              classified)
  rank(target, pool)          every pool session scored against the target with
                              P1's weights, the 75% comparable-weight floor, and
                              the five best by similarity, comparable weight,
                              recency and snapshot id - outcomes play no part; a
                              session with more than one record is left out
  prior_manifest(...)         the earlier sessions (once each) and outcome revisions
                              the prior is built from - every snapshot of the profile,
                              annotated or not - each excluded one counted under its
                              reason, and their digest
  outcome_summary(...)        after selection: per P1 target the analogues' raw
                              class counts and frequencies, the denominator, the
                              smoothed baseline and the prior it shrinks towards

Pure functions on plain records; the journal (forecaster/journal.py) loads them,
stores the result (journal.analogue_sets / analogue_members) and the dashboard
shows it. Arithmetic is exact (fractions); stored values are decimal strings.
"""

from __future__ import annotations

import hashlib
import math
from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal
from fractions import Fraction
from typing import Any, Callable, Dict, Optional, Sequence

from contracts import nq_preopen as pre
from contracts import nq_prompt_v2 as defs

_SHOW = Decimal("0.000001")


def show(x: Optional[Fraction]) -> Optional[str]:
    """An exact fraction as a 6-decimal string (None stays None)."""
    return None if x is None else str((Decimal(x.numerator) / Decimal(x.denominator)).quantize(_SHOW))


@dataclass(frozen=True)
class Record:
    """One session's pre-open record: its snapshot and one structure annotation of it."""
    snapshot_id: str
    session_date: str
    symbol: str
    snapshot_version: str
    annotation_id: str
    protocol_version: str
    integrity_status: str
    features: Dict[str, Any] = field(hash=False)


@dataclass(frozen=True)
class SessionRef:
    """One session of the prior pool: a snapshot of the profile, whether or not it has a structure annotation."""
    snapshot_id: str
    session_date: str
    symbol: str


def features(annotation: Dict[str, Any]) -> Dict[str, Any]:
    """The rubric inputs of one stored annotation; a field counts only when classified."""
    out: Dict[str, Any] = {}
    for name in pre.MATCH_WEIGHTS:
        if name.startswith("price:"):
            out[name] = (annotation.get("price_location") or {}).get(name[len("price:"):])
        else:
            f = (annotation.get("fields") or {}).get(name) or {}
            out[name] = f.get("value") if f.get("status") == "classified" else None
    return out


def compare(target: Record, other: Record) -> Dict[str, Dict[str, Any]]:
    """Per feature: its weight, whether both sides are comparable, and the score (0..1) when they are."""
    out = {}
    for name, weight in pre.MATCH_WEIGHTS.items():
        a, b = target.features.get(name), other.features.get(name)
        if a is None or b is None:
            out[name] = {"weight": weight, "comparable": False, "score": None, "target": a, "analogue": b}
            continue
        if name == "Chop Score":
            score = max(Fraction(0), 1 - Fraction(abs(int(a) - int(b)), 3))
        else:
            score = Fraction(int(a == b))
        out[name] = {"weight": weight, "comparable": True, "score": score, "target": a, "analogue": b}
    return out


def score(components: Dict[str, Dict[str, Any]]):
    """``(similarity, comparable_weight)``: 100 x weighted matches / comparable weight, both in percent."""
    comparable = sum((c["weight"] for c in components.values() if c["comparable"]), Fraction(0))
    matched = sum((c["weight"] * c["score"] for c in components.values() if c["comparable"]), Fraction(0))
    return (100 * matched / comparable if comparable else None), comparable


# The weights as integers over their common denominator and every score in thirds (the Chop Score's steps), so that
# rank() scores each pair in integer arithmetic: score(compare(...)) is (100 M / 3C, C / D) for the integer sums M of
# weight x 3 x score and C of the comparable weights - the same exact fractions, built once per pair.
_DEN = math.lcm(*(w.denominator for w in pre.MATCH_WEIGHTS.values()))
_INT_WEIGHTS = [(name, int(w * _DEN), name == "Chop Score") for name, w in pre.MATCH_WEIGHTS.items()]


def _pair_score(target: Record, other: Record):
    """score(compare(target, other)), computed in integers - see _INT_WEIGHTS."""
    comparable = matched = 0
    tf, of = target.features, other.features
    for name, weight, chop in _INT_WEIGHTS:
        a, b = tf.get(name), of.get(name)
        if a is None or b is None:
            continue
        comparable += weight
        matched += weight * (max(0, 3 - abs(int(a) - int(b))) if chop else 3 * int(a == b))
    return (Fraction(100 * matched, 3 * comparable) if comparable else None), Fraction(comparable, _DEN)


def rank(target: Record, pool: Sequence[Record]) -> Dict[str, Any]:
    """
    The target's analogues from ``pool``: ``{'selected': [...], 'excluded': {reason: n},
    'pool_size', 'pool_hash'}``. ``pool_size`` counts the earlier compatible sessions
    scored; every other candidate is counted under its exclusion reason.
    """
    excluded: Counter = Counter()
    eligible = []
    for other in pool:
        if other.snapshot_id == target.snapshot_id or other.session_date >= target.session_date:
            excluded["not_earlier"] += 1
        elif other.symbol != target.symbol:
            excluded["other_symbol"] += 1
        elif (other.snapshot_version, other.protocol_version) != (target.snapshot_version, target.protocol_version):
            excluded["incompatible_version"] += 1
        elif other.integrity_status != "ok":
            excluded["contaminated"] += 1
        else:
            eligible.append(other)
    sessions = Counter((r.symbol, r.session_date) for r in eligible)
    scored = [r for r in eligible if sessions[(r.symbol, r.session_date)] == 1]
    if len(scored) < len(eligible):
        excluded["duplicate_session"] += len(eligible) - len(scored)
    pool_hash = hashlib.sha256("\n".join(sorted(f"{r.snapshot_id}:{r.annotation_id}" for r in scored))
                               .encode()).hexdigest()
    candidates = []
    for other in scored:
        similarity, comparable = _pair_score(target, other)
        if comparable < pre.MIN_COMPARABLE:
            excluded["low_coverage"] += 1
            continue
        candidates.append((similarity, comparable, other))
    # highest similarity, then higher coverage, then the more recent session, then the snapshot id
    candidates.sort(key=lambda c: (-c[0], -c[1], _neg_date(c[2].session_date), c[2].snapshot_id))
    selected = [{"rank": i, "record": other, "similarity": sim, "comparable_weight": comp,
                 "components": compare(target, other)}
                for i, (sim, comp, other) in enumerate(candidates[:pre.TOP_ANALOGUES], 1)]
    return {"selected": selected, "excluded": dict(sorted(excluded.items())), "pool_size": len(scored),
            "pool_hash": pool_hash}


def prior_manifest(target: Record, sessions: Sequence[Any],
                   known: Callable[[str], Optional[Dict[str, Any]]]) -> Dict[str, Any]:
    """
    The prior's sessions (nq_match_p1_v2, guideline revision 2 2C). ``sessions`` are every snapshot of the profile
    (anything with ``snapshot_id``, ``session_date`` and ``symbol``) - eligibility does not depend on a structure
    annotation existing; ``known(snapshot_id)`` is the outcome revision usable for this target ({'labels',
    'outcome_revision'}) or None. An earlier session of the target's symbol, held once, with a known outcome is in;
    every other session is counted under its reason - other_symbol, not_earlier (the target and later),
    duplicate_session, no_outcome_known. Per-target labels stay with the caller: a null label shrinks that target's
    prior only. Returns ``{'labels': [...], 'manifest': [[snapshot_id, session_date, revision], ...] in session
    order, 'excluded': {reason: n}, 'digest'}``.
    """
    excluded: Counter = Counter()
    earlier = []
    for s in sessions:
        if s.symbol != target.symbol:
            excluded["other_symbol"] += 1
        elif s.session_date >= target.session_date:
            excluded["not_earlier"] += 1
        else:
            earlier.append(s)
    dates = Counter(s.session_date for s in earlier)
    used = []
    for s in sorted(earlier, key=lambda s: (s.session_date, s.snapshot_id)):
        outcome = known(s.snapshot_id) if dates[s.session_date] == 1 else None
        if dates[s.session_date] > 1:
            excluded["duplicate_session"] += 1
        elif outcome is None:
            excluded["no_outcome_known"] += 1
        else:
            used.append((s, outcome))
    manifest = [[s.snapshot_id, s.session_date, int(o["outcome_revision"])] for s, o in used]
    return {"labels": [o["labels"] for _, o in used], "manifest": manifest,
            "excluded": dict(sorted(excluded.items())),
            "digest": hashlib.sha256(defs.canonical_json(manifest).encode()).hexdigest()}


def _neg_date(d: str) -> int:
    """A sort key that puts later dates first."""
    return -int(d.replace("-", ""))


def outcome_summary(selected: Sequence[Dict[str, Any]], outcomes: Dict[str, Optional[Dict[str, Any]]],
                    prior: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """
    After selection: per P1 target the analogues' class counts over those with a label,
    the denominator, raw frequencies, the prior from ``prior`` (every earlier session's
    labels) and the smoothed baseline. ``outcomes`` maps snapshot id -> outcome labels
    (None: no outcome). Analogues are never replaced; a missing label shrinks the
    denominator.
    """
    k = pre.SMOOTHING_PSEUDO_COUNT
    sims = [m["similarity"] for m in selected]
    out: Dict[str, Any] = {"analogues": len(selected),
                           "mean_similarity": show(sum(sims, Fraction(0)) / len(sims)) if sims else None,
                           "targets": {}}
    for target in pre.OUTCOME_TARGETS:
        vocab = list(defs.TARGETS[target]["labels"])
        labels = []
        for m in selected:
            labs = outcomes.get(m["record"].snapshot_id)
            lab = (labs or {}).get(target, {}).get("label")
            if lab is not None:
                labels.append(lab)
        n = len(labels)
        counts = {c: labels.count(c) for c in vocab}
        prior_labels = [p[target]["label"] for p in prior if p.get(target, {}).get("label") is not None]
        P = len(prior_labels)
        prior_without = len(prior) - P
        prior_p = {c: Fraction(prior_labels.count(c), P) for c in vocab} if P else None
        raw = {c: Fraction(counts[c], n) for c in vocab} if n else None
        smoothed = {c: (counts[c] + k * prior_p[c]) / (n + k) for c in vocab} if prior_p else None
        status = "analogues" if n else ("prior_only" if prior_p else "none")
        out["targets"][target] = {
            "status": status, "eligible": n, "without_label": len(selected) - n, "counts": counts,
            "raw": None if raw is None else {c: show(v) for c, v in raw.items()},
            "prior_sessions": P, "prior_without_label": prior_without,
            "prior": None if prior_p is None else {c: show(v) for c, v in prior_p.items()},
            "smoothed": None if smoothed is None else {c: show(v) for c, v in smoothed.items()},
        }
    return out
