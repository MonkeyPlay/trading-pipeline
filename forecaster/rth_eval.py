# forecaster/rth_eval.py
"""
The RTH analogues' two evaluations, collected side by side from the same issues
(docs/rth_analogues.md):

  rth_continuation_v2   research only (contracts/rth_eval.py): forecast skill from
                        delayed, cutoff-frozen inputs - the 15 minutes after the
                        matching cutoff, which on the delayed feed have mostly passed
                        in the market when the forecast is stored
  rth_operational_v1    operational (contracts/rth_operational.py): the 15 minutes
                        from the second full minute after the forecast is built - a
                        window that starts after the forecast exists

  build_forecasts(...)        v2, at issue: RTH-20, RTH-5, PRE-5 and CLOCK, each member's
                              own move over the 15 minutes after the cutoff
  build_operational(...)      operational, at issue: the same four arms, every member
                              measured over the future window's clock minutes
  cases(conn, now, version)   every session and issue since registration, under the first
                              reason that keeps it out - or scored, with the target's
                              realised move over the forecast's window
  status(conn, now, version)  operational health only, per checkpoint: every scheduled
                              opportunity, scored and each reason with its rate, the issue
                              delay and how much of the window was still ahead - never a
                              score
  score(conn, now, version)   at the endpoint only, once
  fair_crps, brier, p_up      the scores (pure)

Forecasts are stored when issued (journal.rth_eval_forecasts) and never rebuilt; the
scorer adds only the realised move. ``cutoff_at`` on a forecast is the start of its
target window - the matching cutoff for v2, S for the operational one. Whether a
forecast counts is the database's stamp of when it was stored, against the registered
limit; the RTH set's provenance stays on the set.
"""

from __future__ import annotations

import hashlib
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from contracts import nq_preopen as pre
from contracts import nq_prompt_v2 as defs
from contracts import nq_rth as rth
from contracts import rth_eval as ev
from contracts import rth_operational as ops
from database import journal_store as store
from features import calendar as cal
from forecaster.provenance import code_revision
from matching import rth as mr

MINUTE = timedelta(minutes=1)
H = 15                                                   # horizon in minutes

# What each evaluation is, in the words shown with it.
LABELS = {
    ev.VERSION: "research only - forecast skill from delayed, cutoff-frozen inputs: 'eligible' means eligible for "
                "this experiment, not timely for trading",
    ops.VERSION: "operational - a 15-minute window that starts after the forecast is stored",
}
VERSIONS = (ev.VERSION, ops.VERSION)


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


def window_closes(conn, days: Iterable[str], upto: int,
                  symbol: str = defs.SYMBOL) -> Dict[str, Tuple[Dict[int, float], Optional[int]]]:
    """{day: ({i: close of the bar starting i minutes after 09:30 ET, for i < upto}, the index of the session's newest
    stored bar from 09:30 on)} on each day's active contract - the newest confirms the bars before it complete."""
    days = sorted(set(days))
    if not days:
        return {}
    rows = conn.execute(
        "SELECT a.trading_day, (extract(epoch FROM b.timestamp_utc - ((b.trading_day + TIME '09:30') AT TIME ZONE "
        "'America/New_York')) / 60)::int AS i, b.close "
        "FROM bars b JOIN active_contracts a ON a.contract_id = b.contract_id AND a.trading_day = b.trading_day "
        "WHERE a.symbol = %s AND a.trading_day = ANY(%s::date[]) AND b.interval = '1m' AND b.price_type = 'TRADES' "
        "AND b.timestamp_utc >= (b.trading_day + TIME '09:30') AT TIME ZONE 'America/New_York' "
        "AND b.timestamp_utc < (b.trading_day + TIME '09:30') AT TIME ZONE 'America/New_York' "
        "+ make_interval(mins => %s);", (symbol, days, int(upto))).fetchall()
    newest = conn.execute(
        "SELECT a.trading_day, (extract(epoch FROM max(b.timestamp_utc) - ((a.trading_day + TIME '09:30') AT TIME "
        "ZONE 'America/New_York')) / 60)::int "
        "FROM bars b JOIN active_contracts a ON a.contract_id = b.contract_id AND a.trading_day = b.trading_day "
        "WHERE a.symbol = %s AND a.trading_day = ANY(%s::date[]) AND b.interval = '1m' AND b.price_type = 'TRADES' "
        "AND b.timestamp_utc >= (b.trading_day + TIME '09:30') AT TIME ZONE 'America/New_York' "
        "GROUP BY a.trading_day;", (symbol, days)).fetchall()
    out: Dict[str, Tuple[Dict[int, float], Optional[int]]] = {d: ({}, None) for d in days}
    for r in rows:
        out[str(r[0])][0][int(r[1])] = float(r[2])
    for r in newest:
        out[str(r[0])] = (out[str(r[0])][0], int(r[1]))
    return out


def window_move(closes: Dict[int, float], newest: Optional[int], k: int,
                atr: Optional[float]) -> Tuple[Optional[float], str]:
    """``(move, state)`` over the window of minutes [k, k + 15) after the open: (close of bar k + 14 - close of bar
    k - 1) / ``atr``, needing every bar k - 1 .. k + 14 stored and the last confirmed by a later one. ``state`` is
    'ok', 'pending' (not all stored or confirmed yet, nothing missing behind the newest bar) or 'missing' (a bar
    of the window absent while later bars are stored)."""
    if not atr or k < 1:
        return None, "missing"
    needed = range(k - 1, k + H)
    absent = [i for i in needed if i not in closes]
    if newest is None or newest <= k + H - 1:
        return None, "pending" if all(i > (newest if newest is not None else -1) for i in absent) else "missing"
    if absent:
        return None, "missing"
    return (closes[k + H - 1] - closes[k - 1]) / atr, "ok"


def _members(rows: Iterable[Tuple[mr.Opening, Optional[float], Optional[float]]]) -> Dict[str, Any]:
    """A forecast's members with a move, equal weights, and how many were left out for want of one."""
    out, left_out = [], 0
    for opening, similarity, m in rows:
        if m is None:
            left_out += 1
            continue
        out.append({"session_date": opening.session_date, "snapshot_id": opening.context.snapshot_id,
                    "contract_id": opening.contract_id, "similarity": mr.show(similarity), "move": mr.show(m)})
    for r in out:
        r["weight"] = mr.show(1 / len(out))
    return {"members": out, "left_out": left_out}


def _arms(conn, target: mr.Opening, ranked: Dict[str, Any], openings: Dict[str, mr.Opening],
          moved) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """The four arms with each member's move by ``moved(opening)``, and the pre-open set used."""
    ordered = ranked["ordered"]
    pre_set = store.latest_analogue_set(conn, target.context.snapshot_id, pre.MATCHER_VERSION, defs.LABEL_VERSION,
                                        pre.RULES_PROTOCOL_VERSION)
    pre_members = [(openings[m["session_date"]], float(m["similarity"]))
                   for m in (pre_set or {}).get("members", []) if m["session_date"] in openings]
    clock = sorted((o for o, _ in ranked["scored"]), key=lambda o: o.session_date)
    forecasts = {
        "RTH-20": _members((x["opening"], x["similarity"], moved(x["opening"])) for x in ordered[:20]),
        "RTH-5": _members((x["opening"], x["similarity"], moved(x["opening"]))
                          for x in ordered[:rth.TOP_ANALOGUES]),
        "PRE-5": {**_members((o, s, moved(o)) for o, s in pre_members),
                  "missing_sessions": len((pre_set or {}).get("members", [])) - len(pre_members)},
        "CLOCK": _members((o, None, moved(o)) for o in clock),
    }
    return forecasts, pre_set


def _record(version: str, target: mr.Opening, minutes: int, window_start: datetime, forecasts, sources,
            set_id: str) -> Dict[str, Any]:
    digest = hashlib.sha256(defs.canonical_json({"forecasts": forecasts, "sources": sources}).encode()).hexdigest()
    return {"evaluation_version": version, "set_id": set_id, "symbol": target.symbol,
            "session_date": target.session_date, "elapsed_minutes": minutes, "cutoff_at": window_start,
            "horizon_minutes": H, "target_atr": mr.show(target.context.atr), "forecasts": forecasts,
            "sources": sources, "digest": digest, "code_revision": code_revision()}


def _sources(ranked, set_id, pre_set, **extra) -> Dict[str, Any]:
    return {"matcher_version": rth.RTH_MATCHER_VERSION, "rth_set_id": set_id, "pool_hash": ranked["pool_hash"],
            "preopen_matcher_version": pre.MATCHER_VERSION, "label_version": defs.LABEL_VERSION,
            "preopen_protocol": pre.RULES_PROTOCOL_VERSION,
            "preopen_set_id": None if pre_set is None else pre_set["set_id"], **extra}


def build_forecasts(conn, target: mr.Opening, minutes: int, ranked: Dict[str, Any],
                    openings: Dict[str, mr.Opening], set_id: str) -> Dict[str, Any]:
    """
    rth_continuation_v2's forecasts of ``target`` at its ``minutes`` window: from the matcher's ranking ``ranked`` of
    the live set ``set_id`` (the same run, the same inputs), each member's move over the 15 minutes after the cutoff
    - the record journal.rth_eval_forecasts stores (the database adds when, and whether in time).
    """
    forecasts, pre_set = _arms(conn, target, ranked, openings, lambda o: move(o, minutes))
    return _record(ev.VERSION, target, minutes, target.rth_open_at + minutes * MINUTE, forecasts,
                   _sources(ranked, set_id, pre_set), set_id)


def operational_start(rth_open_at: datetime, built_at: datetime) -> int:
    """The first minute (after the open) of the operational window for a forecast built at ``built_at``: the second
    full minute after it - so the window starts 60 to 120 seconds after the build, never inside it, and a forecast
    stored late is stamped ineligible rather than given a later window."""
    return int((built_at - rth_open_at) // MINUTE) + ops.LEAD_MINUTES


def build_operational(conn, target: mr.Opening, minutes: int, ranked: Dict[str, Any],
                      openings: Dict[str, mr.Opening], set_id: str,
                      built_at: Optional[datetime] = None) -> Optional[Dict[str, Any]]:
    """
    rth_operational_v1's forecasts of ``target``, issued with its ``minutes`` window's live set: the target window
    starts S = the second full minute after ``built_at`` (now), and every member's move is over the same clock
    minutes [S, S + 15) of its own session, from the bars stored now. None when S + 15 lies past 11:30 ET.
    """
    built_at = built_at or datetime.now(timezone.utc)
    k = operational_start(target.rth_open_at, built_at)
    if k + H > ops.LAST_MINUTE or k < minutes:          # never a window that starts before the matched minutes end
        return None
    start = target.rth_open_at + k * MINUTE
    members = {o.session_date for o, _ in ranked["scored"]} | {x["opening"].session_date for x in ranked["ordered"]}
    pre_set = store.latest_analogue_set(conn, target.context.snapshot_id, pre.MATCHER_VERSION, defs.LABEL_VERSION,
                                        pre.RULES_PROTOCOL_VERSION)
    members |= {m["session_date"] for m in (pre_set or {}).get("members", [])}
    closes = window_closes(conn, members, k + H)

    def moved(o: mr.Opening) -> Optional[float]:
        c, newest = closes.get(o.session_date, ({}, None))
        return window_move(c, newest, k, o.context.atr if o.context else None)[0]
    forecasts, pre_set = _arms(conn, target, ranked, openings, moved)
    sources = _sources(ranked, set_id, pre_set, target_start_minute=k, match_cutoff_minutes=minutes,
                       match_cutoff_at=target.rth_open_at + minutes * MINUTE, built_at=built_at)
    return _record(ops.VERSION, target, minutes, start, forecasts, _jsonable(sources), set_id)


def _jsonable(sources: Dict[str, Any]) -> Dict[str, Any]:
    return {k: (v.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ") if isinstance(v, datetime) else v)
            for k, v in sources.items()}


def issue_forecasts(conn, target: mr.Opening, minutes: int, ranked: Dict[str, Any],
                    openings: Dict[str, mr.Opening], set_id: str,
                    built_at: Optional[datetime] = None) -> List[Tuple[str, str, bool]]:
    """Both evaluations' forecasts of a cutoff window issued live - the first per evaluation, session and cutoff;
    ``[(version, forecast_id, new)]``, empty for a window that is not a cutoff. ``built_at``: the issue's clock (the
    operational window starts from it)."""
    if minutes not in ev.CUTOFFS:
        return []
    out = [(ev.VERSION, *store.save_rth_eval_forecast(conn, build_forecasts(conn, target, minutes, ranked, openings,
                                                                             set_id)))]
    operational = build_operational(conn, target, minutes, ranked, openings, set_id, built_at)
    if operational is not None:
        out.append((ops.VERSION, *store.save_rth_eval_forecast(conn, operational)))
    return out


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

def _registered(conn, version: str = ev.VERSION) -> Optional[Dict[str, Any]]:
    return store.get_version(conn, version)


def window_start(f: Dict[str, Any]) -> int:
    """The first minute (after the open) of a stored forecast's target window, as the forecast itself records it: the
    operational one's S, v2's matching cutoff."""
    return int(f["sources"].get("target_start_minute", f["elapsed_minutes"]))


def _times(f: Dict[str, Any]) -> Dict[str, float]:
    """A stored forecast's timing in minutes: ``delay`` after its matching cutoff, ``ahead`` - how much of its
    target window was still in the future when stored (0 when none), ``lead`` - from storing to its window's start
    (negative: stored after the start), ``age`` - from its matching cutoff to its window's start."""
    from forecaster import rth_analogues as ra
    start, created = ra.utc(f["cutoff_at"]), ra.utc(f["created_at"])
    cutoff = ra.utc(f["sources"].get("match_cutoff_at") or f["cutoff_at"])
    lead = (start - created).total_seconds() / 60
    return {"delay": (created - cutoff).total_seconds() / 60, "lead": lead,
            "ahead": max(0.0, min(float(H), H + lead)), "age": (start - cutoff).total_seconds() / 60}


def cases(conn, now: Optional[datetime] = None, version: str = ev.VERSION) -> List[Dict[str, Any]]:
    """
    Every scheduled NQ session from ``version``'s registration day to ``now``, at each cutoff: ``{'session_date',
    'minutes', 'reason' (None when scored), 'forecast', 'times', 'y', 'scores'}`` - the reason the first of ev.REASONS
    that applies. The forecasts are the stored ones; only the realised move over each one's window is read now.
    """
    from forecaster import rth_analogues as ra
    now = now or datetime.now(timezone.utc)
    reg = _registered(conn, version)
    if reg is None:
        return []
    first = ra.utc(reg["registered_at"]).astimezone(cal.NY_TZ).date()
    today = now.astimezone(cal.NY_TZ).date()
    if today < first:
        return []
    stored = {(f["session_date"], f["elapsed_minutes"]): f for f in store.rth_eval_forecasts(conn, version)
              if first.isoformat() <= f["session_date"] <= today.isoformat()}
    upto = max([window_start(f) + H + 1 for f in stored.values()], default=max(ev.CUTOFFS) + H + 1)
    closes = window_closes(conn, {d for d, _ in stored}, upto)
    sets: Dict[str, Any] = {}
    out = []
    for s in cal.sessions_between(first, today):
        day = s.session_date.isoformat()
        for minutes in ev.CUTOFFS:
            f = stored.get((day, minutes))
            case = {"session_date": day, "minutes": minutes, "forecast": f, "times": None, "y": None,
                    "scores": None, "reason": None}
            if f is None:
                # an issue that could still come; for the operational one up to its window's latest start
                last = s.rth_open_at + (minutes + H + (ops.LEAD_MINUTES if version == ops.VERSION else 0)) * MINUTE
                case["reason"] = "outcome_pending" if now < last else "not_issued"
                out.append(case)
                continue
            case["times"] = _times(f)
            if not f["eligible"]:
                case["reason"] = "late"
            else:
                if f["set_id"] not in sets:
                    sets[f["set_id"]] = store.get_rth_set(conn, f["set_id"])
                if sets[f["set_id"]]["pit_status"] != "verified":
                    case["reason"] = "unverified_inputs"
                elif any(len(f["forecasts"][k]["members"]) < n for k, n in ev.MIN_MEMBERS.items()):
                    case["reason"] = "forecast_incomplete"
                else:
                    c, newest = closes.get(day, ({}, None))
                    y, state = window_move(c, newest, window_start(f), float(f["target_atr"]))
                    if state == "ok":
                        case.update(y=y, scores=case_scores(f["forecasts"], y))
                    else:
                        case["reason"] = "outcome_pending" if state == "pending" else "outcome_missing"
            out.append(case)
    return out


def _quantiles(values: Sequence[float]) -> Optional[Dict[str, float]]:
    values = sorted(values)
    if not values:
        return None
    pick = lambda q: values[min(len(values) - 1, int(q * (len(values) - 1) + 0.5))]
    return {"n": len(values), "min": values[0], "median": pick(0.5), "p90": pick(0.9), "max": values[-1]}


def availability(all_cases: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Per checkpoint, over every scheduled opportunity that is decided (outcome_pending left out): the
    opportunities, scored and each reason with its rate, and the timing of the stored forecasts - issue delay after
    the matching cutoff, minutes of the window still ahead when stored, lead to the window's start and the age of
    the information at it."""
    out = {}
    for minutes, at in ev.CUTOFFS.items():
        cs = [c for c in all_cases if c["minutes"] == minutes and c["reason"] != "outcome_pending"]
        n = len(cs)
        counts = Counter((c["reason"] or "scored") for c in cs)
        timed = [c["times"] for c in cs if c.get("times") is not None]
        out[at] = {"opportunities": n, "counts": dict(counts),
                   "rates": {k: round(v / n, 4) for k, v in counts.items()} if n else {},
                   "delay_minutes": _quantiles([t["delay"] for t in timed]),
                   "window_ahead_minutes": _quantiles([t["ahead"] for t in timed]),
                   "lead_minutes": _quantiles([t["lead"] for t in timed]),
                   "information_age_minutes": _quantiles([t["age"] for t in timed])}
    return out


def status(conn, now: Optional[datetime] = None, version: str = ev.VERSION) -> Dict[str, Any]:
    """Operational health - never a score: per checkpoint every scheduled opportunity with scored and each reason and
    its rate, the timing of the stored forecasts, counted sessions against the endpoint and member counts."""
    all_cases = cases(conn, now, version)
    counted = sorted({c["session_date"] for c in all_cases if c["reason"] is None})
    members = {k: _quantiles([len(c["forecast"]["forecasts"][k]["members"]) for c in all_cases if c["forecast"]])
               for k in ev.FORECASTS}
    return {"version": version, "label": LABELS[version], "registered": _registered(conn, version) is not None,
            "cases": len(all_cases), "by_reason": dict(Counter((c["reason"] or "scored") for c in all_cases)),
            "availability": availability(all_cases), "counted_sessions": len(counted),
            "endpoint_sessions": ev.ENDPOINT_SESSIONS, "end_date": ev.END_DATE, "members": members}


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
    """The analysis of the cases (pure): availability per checkpoint over every opportunity, then the primary and
    secondary comparisons pooled and per cutoff - each per-cutoff result with its own n, which 60 counted sessions
    do not make adequate by themselves."""
    scored = [c for c in all_cases if c["reason"] is None]
    b = ev.BOOTSTRAP
    out: Dict[str, Any] = {"counted_sessions": len({c["session_date"] for c in scored}),
                           "by_reason": dict(Counter((c["reason"] or "scored") for c in all_cases)),
                           "availability": availability(all_cases),
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


def score(conn, now: Optional[datetime] = None, version: str = ev.VERSION) -> Dict[str, Any]:
    """
    An evaluation's one scoring, stored (journal.rth_eval_results): only at the endpoint - 60 counted sessions, or
    the end date with at least 30 - and only once. Raises NotAtEndpoint otherwise, having computed no score.
    """
    now = now or datetime.now(timezone.utc)
    if store.rth_eval_result(conn, version) is not None:
        raise NotAtEndpoint(f"{version} was scored already; the stored result stands (rth-eval-score --show)")
    st = status(conn, now, version)
    counted, past_end = st["counted_sessions"], now.astimezone(cal.NY_TZ).date() >= date.fromisoformat(ev.END_DATE)
    if counted < ev.ENDPOINT_SESSIONS and not past_end:
        raise NotAtEndpoint(f"{counted} of {ev.ENDPOINT_SESSIONS} sessions counted; the end date is {ev.END_DATE}")
    if counted < ev.ENDPOINT_SESSIONS and counted < ev.MIN_SESSIONS_AT_END_DATE:
        results = {"insufficient": True, "counted_sessions": counted, "by_reason": st["by_reason"],
                   "availability": st["availability"]}
    else:
        results = analyse(cases(conn, now, version))
    contract = ev if version == ev.VERSION else ops
    results.update(definition_hash=contract.definition_hash(), version=version, label=LABELS[version])
    store.save_rth_eval_result(conn, version, results, code_revision())
    return results
