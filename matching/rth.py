# matching/rth.py
"""
RTH analogue selection (matcher nq_match_rth_v1, contracts/nq_rth.py): the earlier
NQ sessions whose first n minutes of regular trading most resemble the target's
first n, for an expanding window from the 09:30 ET open.

  completed_window(bars, open)   the contiguous run of confirmed 1-minute bars from
                                 the open, and why it stops: awaiting confirmation,
                                 not stored yet, or a confirmed gap
  features(opening, n)           the eleven inputs of one session's first n minutes:
                                 the path from the open and its swings in the
                                 session's own frozen daily ATR, location against
                                 the day's VWAP and the frozen pre-open levels,
                                 relative volume, the pre-open context
  compare / score                per feature a score 0..1 and the weighted similarity
  rank(target, pool, n)          every eligible earlier session scored at the same n,
                                 the five best by similarity, comparable weight and
                                 recency - nothing after the cutoff takes part
  volume_baselines(...)          per session and window, the mean volume of the same
                                 window over the sessions before it
  calibrate(openings, n)         the spread of each feature over a set of sessions -
                                 how the tolerances were set (contracts/nq_rth.CALIBRATION)

Pure functions on plain records; forecaster/rth_analogues.py loads them from the
store, issues and stores the sets (journal.rth_analogue_sets), the dashboard shows
them. Values are floats; stored values are 6-decimal strings.
"""

from __future__ import annotations

import hashlib
import math
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Dict, List, Optional, Sequence, Tuple

from contracts import nq_prompt_v2 as defs
from contracts import nq_rth as rth

MINUTE = timedelta(minutes=1)
_SHOW = Decimal("0.000001")

# (start UTC, open, high, low, close, volume)
Bar = Tuple[datetime, float, float, float, float, float]


def show(x: Optional[float]) -> Optional[str]:
    """A value as a 6-decimal string (None stays None)."""
    return None if x is None else str(Decimal(repr(float(x))).quantize(_SHOW))


@dataclass(frozen=True)
class Context:
    """A session's frozen pre-open context: its stored snapshot's daily ATR and levels, and its overnight volume
    and hlc3 x volume (the Globex day's VWAP before the open). A level not valid in the snapshot is None."""
    snapshot_id: str
    atr: Optional[float]
    prev_rth_close: Optional[float]
    on_high: Optional[float]
    on_low: Optional[float]
    overnight_pv: float = 0.0
    overnight_volume: float = 0.0


@dataclass(frozen=True)
class Opening:
    """One session's regular-hours opening: its completed bars from 09:30 ET, in order and contiguous, and the
    context frozen before it. ``volume_baseline[n]``: the mean volume of the first n minutes over the sessions
    before it (volume_baselines), absent when too few of them hold the window."""
    session_date: str
    symbol: str
    contract_id: int
    rth_open_at: datetime
    context: Optional[Context]
    bars: Tuple[Bar, ...]
    volume_baseline: Dict[int, float] = field(default_factory=dict, hash=False)

    @property
    def minutes(self) -> int:
        return len(self.bars)


def completed_window(bars: Sequence[Bar], rth_open_at: datetime, limit: int = rth.MAX_MINUTES,
                     newest_start: Optional[datetime] = None) -> Tuple[List[Bar], Dict[str, Any]]:
    """
    ``(window, stop)``: the bars of ``bars`` (time-ordered, from the open) that make an unbroken run of minutes from
    ``rth_open_at`` and are confirmed complete - a later bar of the session is stored (the newest stored bar may
    still be forming) - at most ``limit``; ``newest_start`` is the start of the session's newest stored bar when it
    lies beyond ``bars`` (default: the newest of ``bars``). ``stop`` says why the window ends:

      complete               ``limit`` minutes confirmed
      awaiting_confirmation  the next minute's bar is stored, but nothing after it yet - it may still be forming
      not_stored             the next minute's bar is not stored and nothing after it is (the feed is behind)
      gap                    the next minute's bar is missing while later bars are stored - a confirmed hole in
                             the observed session; the window stops there and nothing is filled in

    with ``minute`` the start of that next minute (None when complete).
    """
    by_start = {b[0]: b for b in bars}
    newest = max([*by_start, *([newest_start] if newest_start else [])], default=None)
    window: List[Bar] = []
    t = rth_open_at
    while len(window) < limit and t in by_start and newest is not None and newest > t:
        window.append(by_start[t])
        t += MINUTE
    if len(window) >= limit:
        return window, {"state": "complete", "minute": None}
    if t in by_start:
        state = "awaiting_confirmation"
    elif newest is not None and newest > t:
        state = "gap"
    else:
        state = "not_stored"
    return window, {"state": state, "minute": t}


# --------------------------------------------------------------------------
# Features
# --------------------------------------------------------------------------

def tolerance(feature: str, minutes: int) -> float:
    """The difference at which ``feature`` scores 0 for a window of ``minutes``."""
    tol = float(rth.TOLERANCES[feature])
    return tol * math.sqrt(minutes / rth.SCALE_MINUTES) if feature in rth.SCALED else tol


def path(opening: Opening, minutes: int) -> Optional[List[float]]:
    """(close_i - RTH open) / ATR for the first ``minutes`` bars; None without a valid ATR or the bars."""
    ctx = opening.context
    if ctx is None or not ctx.atr or opening.minutes < minutes:
        return None
    o = opening.bars[0][1]
    return [(b[4] - o) / ctx.atr for b in opening.bars[:minutes]]


def features(opening: Opening, minutes: int) -> Dict[str, Optional[float]]:
    """The session's features over its first ``minutes`` (contracts/nq_rth.DEFINITION); None when not available.
    Only the window's bars and the frozen context are read - nothing after the cutoff, nothing of the session's
    eventual range."""
    out: Dict[str, Optional[float]] = {f: None for f in rth.WEIGHTS}
    ctx = opening.context
    if ctx is None or not ctx.atr or ctx.atr <= 0 or opening.minutes < minutes or minutes < 1:
        return out
    atr, w = ctx.atr, opening.bars[:minutes]
    o, c = w[0][1], w[-1][4]
    hi, lo = max(b[2] for b in w), min(b[3] for b in w)
    out["path"] = (c - o) / atr                     # the end of the path; compare() uses the whole of it
    out["net_move"] = (c - o) / atr
    out["range"] = (hi - lo) / atr
    pullback = recovery = 0.0
    run_hi, run_lo = w[0][2], w[0][3]
    for b in w:
        run_hi, run_lo = max(run_hi, b[2]), min(run_lo, b[3])
        pullback = max(pullback, run_hi - b[3])
        recovery = max(recovery, b[2] - run_lo)
    out["deepest_pullback"] = pullback / atr
    out["largest_recovery"] = recovery / atr
    pv = ctx.overnight_pv + sum((b[2] + b[3] + b[4]) / 3 * b[5] for b in w)
    vol = ctx.overnight_volume + sum(b[5] for b in w)
    if vol > 0:
        out["vs_vwap"] = (c - pv / vol) / atr
    if ctx.on_high is not None and ctx.on_low is not None and ctx.on_high > ctx.on_low:
        out["in_overnight_range"] = min(2.0, max(-1.0, (c - ctx.on_low) / (ctx.on_high - ctx.on_low)))
    if ctx.on_high is not None and ctx.on_low is not None:
        out["overnight_range"] = (ctx.on_high - ctx.on_low) / atr
    if ctx.prev_rth_close is not None:
        out["vs_prev_close"] = (c - ctx.prev_rth_close) / atr
        out["gap"] = (o - ctx.prev_rth_close) / atr
    window_volume = sum(b[5] for b in w)
    base = opening.volume_baseline.get(minutes)
    if base and window_volume > 0:
        out["relative_volume"] = math.log(window_volume / base)
    return out


def volume_baselines(openings: Dict[str, Opening], before: Dict[str, Sequence[str]]) -> Dict[str, Dict[int, float]]:
    """
    Per session date, per window n = 1..MAX_MINUTES: the mean volume of the first n minutes over the sessions
    ``before[date]`` (the RELVOL_SESSIONS scheduled sessions before it) whose window is whole - only when at least
    RELVOL_MIN_SESSIONS of them hold it. Every earlier session's window ended before the date's open.
    """
    cumulative: Dict[str, List[float]] = {}
    for d, op in openings.items():
        total, acc = 0.0, []
        for b in op.bars:
            total += b[5]
            acc.append(total)
        cumulative[d] = acc
    out: Dict[str, Dict[int, float]] = {}
    for d in openings:
        base: Dict[int, float] = {}
        prior = [cumulative[p] for p in before.get(d, ()) if p in cumulative]
        for n in range(1, rth.MAX_MINUTES + 1):
            vols = [acc[n - 1] for acc in prior if len(acc) >= n]
            if len(vols) >= rth.RELVOL_MIN_SESSIONS:
                base[n] = sum(vols) / len(vols)
        out[d] = base
    return out


# --------------------------------------------------------------------------
# Scoring
# --------------------------------------------------------------------------

def compare(target: Opening, other: Opening, minutes: int,
            target_features: Optional[Dict[str, Optional[float]]] = None,
            other_features: Optional[Dict[str, Optional[float]]] = None) -> Dict[str, Dict[str, Any]]:
    """Per feature: weight, tolerance, whether both sides have a value, the score (0..1) and the two values - for
    ``path`` the root mean square difference of the two paths minute by minute."""
    a = target_features if target_features is not None else features(target, minutes)
    b = other_features if other_features is not None else features(other, minutes)
    out: Dict[str, Dict[str, Any]] = {}
    for name, weight in rth.WEIGHTS.items():
        tol = tolerance(name, minutes)
        x, y = a.get(name), b.get(name)
        if x is None or y is None:
            out[name] = {"weight": weight, "tolerance": tol, "comparable": False, "score": None,
                         "target": x, "analogue": y, "difference": None}
            continue
        if name == "path":
            pa, pb = path(target, minutes), path(other, minutes)
            diff = math.sqrt(sum((p - q) ** 2 for p, q in zip(pa, pb)) / minutes)
        else:
            diff = abs(x - y)
        out[name] = {"weight": weight, "tolerance": tol, "comparable": True, "score": max(0.0, 1 - diff / tol),
                     "target": x, "analogue": y, "difference": diff}
    return out


def score(components: Dict[str, Dict[str, Any]]) -> Tuple[Optional[float], float]:
    """``(similarity, comparable_weight)`` in percent: 100 x weighted feature scores / comparable weight."""
    comparable = float(sum((c["weight"] for c in components.values() if c["comparable"]), 0))
    matched = sum(float(c["weight"]) * c["score"] for c in components.values() if c["comparable"])
    return (100 * matched / comparable if comparable else None), comparable


def rank(target: Opening, pool: Sequence[Opening], minutes: int) -> Dict[str, Any]:
    """
    The target's RTH analogues at ``minutes`` from ``pool``: ``{'selected': [...], 'ordered': every candidate past
    the coverage floor, best first, 'excluded': {reason: n}, 'pool_size', 'pool_hash', 'target_features', 'scored':
    [(opening, features), ...]}``. Every earlier session is
    scored afresh; each excluded one is counted under its reason (not_earlier, other_symbol, no_preopen_context,
    incomplete_window, low_coverage).
    """
    excluded: Counter = Counter()
    target_features = features(target, minutes)
    scored = []
    for other in pool:
        if other.session_date >= target.session_date:
            excluded["not_earlier"] += 1
        elif other.symbol != target.symbol:
            excluded["other_symbol"] += 1
        elif other.context is None or not other.context.atr:
            excluded["no_preopen_context"] += 1
        elif other.minutes < minutes:
            excluded["incomplete_window"] += 1
        else:
            scored.append((other, features(other, minutes)))
    pool_hash = hashlib.sha256("\n".join(sorted(f"{o.session_date}:{o.contract_id}:{o.context.snapshot_id}"
                                                for o, _ in scored)).encode()).hexdigest()
    candidates = []
    for other, feats in scored:
        components = compare(target, other, minutes, target_features, feats)
        similarity, comparable = score(components)
        if comparable < float(rth.MIN_COMPARABLE):
            excluded["low_coverage"] += 1
            continue
        candidates.append((similarity, comparable, other, components))
    # highest similarity, then higher coverage, then the more recent session
    candidates.sort(key=lambda c: (-round(c[0], 9), -c[1], _neg_date(c[2].session_date)))
    selected = [{"rank": i, "opening": other, "similarity": sim, "comparable_weight": comp, "components": comps}
                for i, (sim, comp, other, comps) in enumerate(candidates[:rth.TOP_ANALOGUES], 1)]
    ordered = [{"rank": i, "opening": other, "similarity": sim, "comparable_weight": comp}
               for i, (sim, comp, other, _) in enumerate(candidates, 1)]
    return {"selected": selected, "ordered": ordered, "excluded": dict(sorted(excluded.items())),
            "pool_size": len(scored), "pool_hash": pool_hash, "target_features": target_features, "scored": scored}


def input_digest(target: Opening, minutes: int, ranked: Dict[str, Any]) -> str:
    """
    The identity of a set's inputs: the matcher version, the window, the target's window bars, context and
    features, and every scored candidate's session, contract, context snapshot and features - everything a score
    depends on, nothing after the cutoff. A revised input makes a different digest (a new set beside the old).
    """
    def feats(f):
        return {k: show(v) for k, v in f.items()}
    doc = {
        "version": rth.RTH_MATCHER_VERSION, "minutes": minutes,
        "target": {"session_date": target.session_date, "contract_id": target.contract_id,
                   "snapshot_id": target.context.snapshot_id if target.context else None,
                   "bars": [[b[0].isoformat(), *map(show, b[1:])] for b in target.bars[:minutes]],
                   "features": feats(ranked["target_features"])},
        "pool": sorted([[o.session_date, o.contract_id, o.context.snapshot_id, feats(f)]
                        for o, f in ranked["scored"]], key=lambda r: r[0]),
    }
    return hashlib.sha256(defs.canonical_json(doc).encode()).hexdigest()


def _neg_date(d: str) -> int:
    """A sort key that puts later dates first."""
    return -int(d.replace("-", ""))


def calibrate(openings: Sequence[Opening], minutes: int = rth.SCALE_MINUTES) -> Dict[str, Any]:
    """
    How the tolerances were set (contracts/nq_rth.CALIBRATION): over ``openings`` with a valid ATR and ``minutes``
    of window, the median absolute pairwise difference of each feature (for ``path`` the root mean square path
    difference) - feature values only, nothing after the cutoff, no outcome. Returns ``{'sessions', 'first',
    'last', 'medians': {feature: float}, 'tolerances': {feature: str}}`` - the tolerance being twice the median,
    to two significant figures.
    """
    import statistics
    usable = sorted((o for o in openings if o.context is not None and o.context.atr and o.minutes >= minutes),
                    key=lambda o: o.session_date)
    feats = [features(o, minutes) for o in usable]
    paths = [path(o, minutes) for o in usable]
    medians: Dict[str, float] = {}
    for name in rth.WEIGHTS:
        diffs = []
        for i in range(len(usable)):
            for j in range(i + 1, len(usable)):
                if name == "path":
                    diffs.append(math.sqrt(sum((p - q) ** 2 for p, q in zip(paths[i], paths[j])) / minutes))
                elif feats[i][name] is not None and feats[j][name] is not None:
                    diffs.append(abs(feats[i][name] - feats[j][name]))
        medians[name] = statistics.median(diffs) if diffs else float("nan")
    return {"sessions": len(usable), "first": usable[0].session_date if usable else None,
            "last": usable[-1].session_date if usable else None, "medians": medians,
            "tolerances": {k: f"{2 * v:.2g}" for k, v in medians.items()}}
