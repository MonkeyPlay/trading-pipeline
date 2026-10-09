# forecaster/rth_eval.py
"""
The RTH analogues' usefulness evaluation (contracts/rth_eval.py, docs/rth_analogues.md):
its forecasts stored when they are issued, its cases, its health, and its one scoring.

  build_forecasts(...)        at issue, for a cutoff window: RTH-20, RTH-5, PRE-5 and
                              CLOCK, each member's own move over the next 15 minutes
                              from the bars as stored then - stored once
                              (journal.rth_eval_forecasts), never rebuilt
  cases(conn, now)            every session and cutoff since registration, under the
                              first reason that keeps it out - or scored, with the
                              target's realised move added
  status(conn, now)           operational health only: cases by reason, issue delays,
                              member counts - never a score
  score(conn, now)            at the endpoint only, once: the paired differences, their
                              block-bootstrap intervals and the decision
  fair_crps, brier, p_up      the scores (pure)

Provenance - who issued a set and when its inputs arrived - stays on the RTH set;
whether a forecast counts is the database's stamp of when the forecast was stored
(``eligible``), against the registered issue-delay limit.
"""

from __future__ import annotations

import hashlib
import math
from collections import Counter
from datetime import date, datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from contracts import nq_preopen as pre
from contracts import nq_prompt_v2 as defs
from contracts import nq_rth as rth
from contracts import rth_eval as ev
from database import journal_store as store
from features import calendar as cal
from forecaster.provenance import code_revision
from matching import rth as mr

MINUTE = ev.HORIZON / 15
H = 15                                                   # horizon in minutes


class NotAtEndpoint(RuntimeError):
    """The evaluation is not at its endpoint yet, or was scored already: nothing is scored."""


# --------------------------------------------------------------------------
# Moves and forecasts (at issue)
# --------------------------------------------------------------------------

def move(opening: mr.Opening, minutes: int) -> Optional[float]:
    """The session's move over the 15 minutes after its ``minutes``-th: (close of bar ``minutes + 15`` - close of bar
    ``minutes``) / its frozen daily ATR, from its confirmed bars - None without them or a valid ATR."""
    ctx = opening.context
    if ctx is None or not ctx.atr or opening.minutes < minutes + H or minutes < 1:
        return None
    return (opening.bars[minutes + H - 1][4] - opening.bars[minutes - 1][4]) / ctx.atr


def _members(openings: Iterable[Tuple[mr.Opening, Optional[float]]], minutes: int) -> Dict[str, Any]:
    """A forecast's members with a move, equal weights, and how many were left out for want of one."""
    rows, left_out = [], 0
    for opening, similarity in openings:
        m = move(opening, minutes)
        if m is None:
            left_out += 1
            continue
        rows.append({"session_date": opening.session_date, "snapshot_id": opening.context.snapshot_id,
                     "contract_id": opening.contract_id, "similarity": mr.show(similarity), "move": mr.show(m)})
    for r in rows:
        r["weight"] = mr.show(1 / len(rows))
    return {"members": rows, "left_out": left_out}


def build_forecasts(conn, target: mr.Opening, minutes: int, ranked: Dict[str, Any],
                    openings: Dict[str, mr.Opening], set_id: str) -> Dict[str, Any]:
    """
    The evaluation forecasts of ``target`` at its ``minutes`` window, from the matcher's ranking ``ranked`` of the
    live set ``set_id`` (the same run, the same inputs) and the session's pre-open analogue set as stored now - the
    record journal.rth_eval_forecasts stores (the database adds when, and whether in time).
    """
    ordered = ranked["ordered"]
    by_date = {o.session_date: o for o, _ in ranked["scored"]}
    pre_set = store.latest_analogue_set(conn, target.context.snapshot_id, pre.MATCHER_VERSION, defs.LABEL_VERSION,
                                        pre.RULES_PROTOCOL_VERSION)
    pre_members = []
    for m in (pre_set or {}).get("members", []):
        o = openings.get(m["session_date"])
        if o is not None:
            pre_members.append((o, float(m["similarity"])))
    forecasts = {
        "RTH-20": _members(((x["opening"], x["similarity"]) for x in ordered[:20]), minutes),
        "RTH-5": _members(((x["opening"], x["similarity"]) for x in ordered[:rth.TOP_ANALOGUES]), minutes),
        "PRE-5": {**_members(pre_members, minutes),
                  "missing_sessions": len((pre_set or {}).get("members", [])) - len(pre_members)},
        "CLOCK": _members(((o, None) for o in sorted(by_date.values(), key=lambda o: o.session_date)), minutes),
    }
    sources = {"matcher_version": rth.RTH_MATCHER_VERSION, "rth_set_id": set_id, "pool_hash": ranked["pool_hash"],
               "preopen_matcher_version": pre.MATCHER_VERSION, "label_version": defs.LABEL_VERSION,
               "preopen_protocol": pre.RULES_PROTOCOL_VERSION,
               "preopen_set_id": None if pre_set is None else pre_set["set_id"]}
    digest = hashlib.sha256(defs.canonical_json({"forecasts": forecasts, "sources": sources}).encode()).hexdigest()
    return {"evaluation_version": ev.VERSION, "set_id": set_id, "symbol": target.symbol,
            "session_date": target.session_date, "elapsed_minutes": minutes,
            "cutoff_at": target.rth_open_at + minutes * MINUTE, "horizon_minutes": H,
            "target_atr": mr.show(target.context.atr), "forecasts": forecasts, "sources": sources,
            "digest": digest, "code_revision": code_revision()}


def issue_forecasts(conn, target: mr.Opening, minutes: int, ranked: Dict[str, Any],
                    openings: Dict[str, mr.Opening], set_id: str) -> Optional[Tuple[str, bool]]:
    """Stores the evaluation forecasts of a cutoff window issued live - the first per session and cutoff; None for
    a window that is not a cutoff."""
    if minutes not in ev.CUTOFFS:
        return None
    return store.save_rth_eval_forecast(conn, build_forecasts(conn, target, minutes, ranked, openings, set_id))


# --------------------------------------------------------------------------
# Scores (pure)
# --------------------------------------------------------------------------

def fair_crps(members: Sequence[float], y: float) -> float:
    """The fair ensemble CRPS (Ferro 2014) of ``members`` against ``y``: mean |x - y| - sum |x_i - x_j| / (2 n (n-1))
    - an ensemble's size is not penalised. Needs two members."""
    n = len(members)
    if n < 2:
        raise ValueError("the fair CRPS needs at least two members")
    spread = sum(abs(a - b) for a in members for b in members)
    return sum(abs(x - y) for x in members) / n - spread / (2 * n * (n - 1))


def p_up(members: Sequence[float]) -> float:
    """(members up + 1) / (members + 2)."""
    return (sum(1 for x in members if x > 0) + 1) / (len(members) + 2)


def brier(p: float, outcome: bool) -> float:
    return (p - (1.0 if outcome else 0.0)) ** 2


def case_scores(forecasts: Dict[str, Any], y: float) -> Dict[str, Dict[str, float]]:
    """Per forecast: size, direction and signed scores against the realised move ``y`` (lower is better)."""
    out = {}
    for name in ev.FORECASTS:
        moves = [float(m["move"]) for m in forecasts[name]["members"]]
        out[name] = {"size": fair_crps([abs(x) for x in moves], abs(y)), "direction": brier(p_up(moves), y > 0),
                     "signed": fair_crps(moves, y)}
    return out


# --------------------------------------------------------------------------
# Cases
# --------------------------------------------------------------------------

def _registered(conn) -> Optional[Dict[str, Any]]:
    return store.get_version(conn, ev.VERSION)


def _outcome(conn, day: str, minutes: int, atr: float, openings: Dict[str, mr.Opening],
             meta: Dict[str, Any], now: datetime) -> Tuple[Optional[float], Optional[str]]:
    """The target's realised move, or why there is none yet (outcome_pending) or none at all (outcome_missing)."""
    o = openings.get(day)
    s = cal.session(day)
    window_end = s.rth_open_at + (minutes + H) * MINUTE
    if o is not None and o.minutes >= minutes + H:
        return (o.bars[minutes + H - 1][4] - o.bars[minutes - 1][4]) / atr, None
    if now < window_end or o is None or meta[day]["stop"]["state"] in ("awaiting_confirmation", "not_stored"):
        return None, "outcome_pending"
    return None, "outcome_missing"


def cases(conn, now: Optional[datetime] = None) -> List[Dict[str, Any]]:
    """
    Every scheduled NQ session from the evaluation's registration day to ``now``, at each cutoff: ``{'session_date',
    'minutes', 'reason' (None when scored), 'forecast', 'y', 'scores'}`` - the reason the first of ev.REASONS that
    applies. The forecasts are the stored ones; only the realised move is read now.
    """
    from forecaster import rth_analogues as ra
    now = now or datetime.now(timezone.utc)
    reg = _registered(conn)
    if reg is None:
        return []
    first = ra.utc(reg["registered_at"]).astimezone(cal.NY_TZ).date()
    today = now.astimezone(cal.NY_TZ).date()
    if today < first:
        return []
    stored = {(f["session_date"], f["elapsed_minutes"]): f for f in store.rth_eval_forecasts(conn, ev.VERSION)}
    sets = {}
    openings, meta = ra.load_openings(conn, today.isoformat())
    out = []
    for s in cal.sessions_between(first, today):
        day = s.session_date.isoformat()
        for minutes in ev.CUTOFFS:
            f = stored.get((day, minutes))
            case = {"session_date": day, "minutes": minutes, "forecast": f, "y": None, "scores": None,
                    "reason": None}
            if f is None:
                case["reason"] = ("outcome_pending" if now < s.rth_open_at + (minutes + H) * MINUTE
                                  else "not_issued")
            elif not f["eligible"]:
                case["reason"] = "late"
            else:
                if f["set_id"] not in sets:
                    sets[f["set_id"]] = store.get_rth_set(conn, f["set_id"])
                if sets[f["set_id"]]["pit_status"] != "verified":
                    case["reason"] = "unverified_inputs"
                elif any(len(f["forecasts"][k]["members"]) < n for k, n in ev.MIN_MEMBERS.items()):
                    case["reason"] = "forecast_incomplete"
                else:
                    y, why = _outcome(conn, day, minutes, float(f["target_atr"]), openings, meta, now)
                    if why:
                        case["reason"] = why
                    else:
                        case.update(y=y, scores=case_scores(f["forecasts"], y))
            out.append(case)
    return out


def status(conn, now: Optional[datetime] = None) -> Dict[str, Any]:
    """Operational health - never a score: cases by reason and cutoff, counted sessions against the endpoint, issue
    delays and member counts of the stored forecasts."""
    all_cases = cases(conn, now)
    by_reason = Counter((c["reason"] or "scored") for c in all_cases)
    by_cutoff = {m: Counter((c["reason"] or "scored") for c in all_cases if c["minutes"] == m) for m in ev.CUTOFFS}
    counted = sorted({c["session_date"] for c in all_cases if c["reason"] is None})
    delays = sorted(float(c["forecast"]["issue_delay_s"]) / 60 for c in all_cases if c["forecast"] is not None)
    members = {k: sorted(len(c["forecast"]["forecasts"][k]["members"]) for c in all_cases if c["forecast"])
               for k in ev.FORECASTS}
    return {"registered": _registered(conn) is not None, "cases": len(all_cases), "by_reason": dict(by_reason),
            "by_cutoff": {m: dict(c) for m, c in by_cutoff.items()}, "counted_sessions": len(counted),
            "endpoint_sessions": ev.ENDPOINT_SESSIONS, "end_date": ev.END_DATE,
            "issue_delay_minutes": _quantiles(delays), "members": {k: _quantiles(v) for k, v in members.items()}}


def _quantiles(values: Sequence[float]) -> Optional[Dict[str, float]]:
    if not values:
        return None
    pick = lambda q: values[min(len(values) - 1, int(q * (len(values) - 1) + 0.5))]
    return {"n": len(values), "min": values[0], "median": pick(0.5), "p90": pick(0.9), "max": values[-1]}


# --------------------------------------------------------------------------
# Scoring (once, at the endpoint)
# --------------------------------------------------------------------------

def session_differences(scored: Sequence[Dict[str, Any]], a: str, b: str, measure: str,
                        minutes: Optional[int] = None) -> List[Tuple[str, float]]:
    """Per session (date order), the score of ``a`` minus that of ``b`` averaged over its scored cutoffs (or one
    cutoff)."""
    per: Dict[str, List[float]] = {}
    for c in scored:
        if minutes is not None and c["minutes"] != minutes:
            continue
        per.setdefault(c["session_date"], []).append(c["scores"][a][measure] - c["scores"][b][measure])
    return [(d, sum(v) / len(v)) for d, v in sorted(per.items())]


def block_bootstrap(values: Sequence[float], block: int, resamples: int, seed: int, level: float) -> Dict[str, Any]:
    """The mean of ``values`` (in date order) and its percentile interval from a circular moving-block bootstrap."""
    import numpy as np
    x = np.asarray(values, dtype=float)
    n = len(x)
    if n == 0:
        return {"n": 0, "mean": None, "low": None, "high": None}
    rng = np.random.default_rng(seed)
    blocks = -(-n // block)
    starts = rng.integers(0, n, size=(resamples, blocks))
    idx = (starts[:, :, None] + np.arange(block)[None, None, :]).reshape(resamples, -1)[:, :n] % n
    means = x[idx].mean(axis=1)
    tail = (1 - level) / 2
    return {"n": n, "mean": float(x.mean()), "low": float(np.quantile(means, tail)),
            "high": float(np.quantile(means, 1 - tail))}


def decide(interval: Dict[str, Any]) -> str:
    if interval["high"] is not None and interval["high"] < 0:
        return "RTH-20 better: the whole interval lies below zero"
    return "no sufficiently reliable improvement was established"


def analyse(all_cases: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """The evaluation's analysis of scored cases (pure): primary and secondary comparisons, pooled and per cutoff."""
    scored = [c for c in all_cases if c["reason"] is None]
    b = ev.BOOTSTRAP
    out: Dict[str, Any] = {"counted_sessions": len({c["session_date"] for c in scored}),
                           "by_reason": dict(Counter((c["reason"] or "scored") for c in all_cases)),
                           "primary": {}, "secondary": {}, "per_cutoff": {}}
    for kind, comparisons in (("primary", ev.PRIMARY), ("secondary", ev.SECONDARY)):
        for a, base, measure in comparisons:
            diffs = [d for _, d in session_differences(scored, a, base, measure)]
            iv = block_bootstrap(diffs, b["block"], b["resamples"], b["seed"], b["level"])
            out[kind][f"{a} vs {base}: {measure}"] = {**iv, "decision": decide(iv) if kind == "primary" else None}
    for minutes in ev.CUTOFFS:
        for a, base, measure in ev.PRIMARY:
            diffs = [d for _, d in session_differences(scored, a, base, measure, minutes)]
            out["per_cutoff"].setdefault(ev.CUTOFFS[minutes], {})[f"{a} vs {base}: {measure}"] = block_bootstrap(
                diffs, b["block"], b["resamples"], b["seed"], b["level"])
    return out


def score(conn, now: Optional[datetime] = None) -> Dict[str, Any]:
    """
    The evaluation's one scoring, stored (journal.rth_eval_results): only at the endpoint - 60 counted sessions, or
    the end date with at least 30 - and only once. Raises NotAtEndpoint otherwise, having computed no score.
    """
    now = now or datetime.now(timezone.utc)
    if store.rth_eval_result(conn, ev.VERSION) is not None:
        raise NotAtEndpoint(f"{ev.VERSION} was scored already; the stored result stands (rth-eval-score --show)")
    st = status(conn, now)
    counted, past_end = st["counted_sessions"], now.astimezone(cal.NY_TZ).date() >= date.fromisoformat(ev.END_DATE)
    if counted < ev.ENDPOINT_SESSIONS and not past_end:
        raise NotAtEndpoint(f"{counted} of {ev.ENDPOINT_SESSIONS} sessions counted; the end date is {ev.END_DATE}")
    if counted < ev.ENDPOINT_SESSIONS and counted < ev.MIN_SESSIONS_AT_END_DATE:
        results = {"insufficient": True, "counted_sessions": counted, "by_reason": st["by_reason"]}
    else:
        results = analyse(cases(conn, now))
    results.update(definition_hash=ev.definition_hash(), version=ev.VERSION)
    store.save_rth_eval_result(conn, ev.VERSION, results, code_revision())
    return results
