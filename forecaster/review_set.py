# forecaster/review_set.py
"""
Choosing the stage-1 review set (guideline 1E): 20-30 diverse sessions whose
realised labels a person checks against P1 / P2. The set tests whether the labels
mean what the prompts say - correctness and semantics, not predictive accuracy.

Every candidate session contributes *items*: each target's label, or the reason it
is unavailable (``uncovered``, ``ambiguous_intrabar``, ... are worth checking too),
its schedule when not a full session, a contract roll, and its calendar quarter.
``select_sessions`` picks greedily: each pick is the session adding the most
uncovered items, an item weighted by 1 / the number of sessions that have it, so
rare classes come first. Once everything is covered, picks spread out in time.
Ties: the session furthest from those already picked, then the earlier date.
The result is deterministic for the same outcomes.
"""

from collections import Counter
from datetime import date
from fractions import Fraction
from typing import Any, Dict, Iterable, List, Set, Tuple

RULE = ("greedy coverage of (target, label or unavailable reason), early closes, contract rolls and calendar "
        "quarters, each item weighted 1 / its session count; then the largest gap in time; then the earlier date")


def session_items(labels: Dict[str, Dict[str, Any]], schedule: str, rolled: bool, day: str) -> Set[str]:
    """The items one session covers (see the module docstring)."""
    items = {f"{t}={v['label']}" if v["label"] is not None else f"{t}:{v['reason']}" for t, v in labels.items()}
    if schedule != "full":
        items.add(f"schedule={schedule}")
    if rolled:
        items.add("contract_roll")
    d = date.fromisoformat(day)
    items.add(f"quarter={d.year}Q{(d.month - 1) // 3 + 1}")
    return items


def select_sessions(candidates: Iterable[Dict[str, Any]], size: int = 25) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """
    ``candidates``: ``{'snapshot_id', 'session_date', 'items'}``. Returns the picks in
    order - ``{'snapshot_id', 'session_date', 'reasons'}``, reasons being the items
    the pick covered first - and ``{'items', 'covered', 'uncovered'}``.
    """
    pool = sorted(candidates, key=lambda c: c["session_date"])
    freq = Counter(i for c in pool for i in c["items"])
    chosen: List[Dict[str, Any]] = []
    covered: Set[str] = set()
    picked_days: List[date] = []
    while pool and len(chosen) < size:
        def key(c):
            gain = sum((Fraction(1, freq[i]) for i in c["items"] - covered), Fraction(0))
            d = date.fromisoformat(c["session_date"])
            gap = min((abs((d - p).days) for p in picked_days), default=0)
            return gain, gap, -d.toordinal()
        best = max(pool, key=key)
        pool.remove(best)
        chosen.append({"snapshot_id": best["snapshot_id"], "session_date": best["session_date"],
                       "reasons": sorted(best["items"] - covered)})
        covered |= best["items"]
        picked_days.append(date.fromisoformat(best["session_date"]))
    return chosen, {"items": len(freq), "covered": len(covered), "uncovered": sorted(set(freq) - covered)}
