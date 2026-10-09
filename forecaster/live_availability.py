# forecaster/live_availability.py
"""
Arm D's on-time availability over every scheduled opportunity (the live A/B/D
comparison, contracts/p1_live_abd.py). A practical acceptance threshold, not evidence
of forecasting skill.

An opportunity succeeds only when a valid D forecast was stored by the frozen deadline:
issued and acknowledged by it on the database clock (forecast_service.timely). Every
other outcome stays in the denominator, by category:

  late              D answered, stored after the deadline
  not acknowledged  issued in time, acknowledged after the deadline
  invalid           D answered, the answer failed validation
  failed            the provider failed, refused, cut off, or gave no answer
  no stored answer  requested, nothing stored (the process stopped mid-request)
  no request        a capture without a D request - and why when it says (no
                    approval, the deadline had passed, no analogue set)
  missing data      the capture went stale: the cutoff bar did not arrive within its
                    data wait
  capture failed    the capture stopped before a snapshot (and why)
  no capture        nothing ran for the session

The report gives the observed rate, its numerator and denominator, and an exact
(Clopper-Pearson) 95 % interval. Ten sessions can help choose the setup; they cannot
establish reliable availability.
"""

from __future__ import annotations

from collections import Counter
from typing import Any, Dict, List, Optional, Sequence, Tuple

from contracts import nq_forecast as fc
from contracts import nq_prompt_v2 as defs
from database import journal_store as store
from features import calendar as cal
from forecaster.forecast_service import timely


def exact_interval(k: int, n: int, level: float = 0.95) -> Optional[Tuple[float, float]]:
    """The Clopper-Pearson interval of ``k`` successes in ``n`` (None for n = 0)."""
    if n == 0:
        return None
    from scipy.stats import beta
    a = (1 - level) / 2
    lo = 0.0 if k == 0 else float(beta.ppf(a, k, n - k + 1))
    hi = 1.0 if k == n else float(beta.ppf(1 - a, k + 1, n - k))
    return lo, hi


def outcome(conn, day: str, profile: str, algorithm: str = fc.SYNTHESIS_VERSION) -> Tuple[bool, str, Optional[str]]:
    """``(success, category, detail)`` of one scheduled opportunity (see the module docstring)."""
    runs = [store.get_forecast_run(conn, r["run_id"]) for r in store.list_forecast_runs(conn, day, day, profile, "live")
            if r["algorithm_version"] == algorithm]
    if any(timely(r) for r in runs):
        return True, "timely", None
    if runs:
        run = runs[-1]                                     # the first attempt (one request per snapshot)
        status = run["lifecycle_status"]
        if status == "issued":
            return False, "not acknowledged", "issued in time, acknowledged after the deadline"
        return False, status, run["failure_reason"]
    captures = [c for c in store.live_captures(conn, day, day) if c["profile"] == profile]
    if not captures:
        return False, "no capture", None
    events = [e for c in captures for e in c["events"]]
    named = {e["event"]: (e["detail"] or {}) for e in events}
    if "synthesis_requested" in named:
        return False, "no stored answer", named["synthesis_requested"].get("request_id")
    if "synthesis_skipped" in named:
        return False, "no request", named["synthesis_skipped"].get("reason")
    if "stale" in named:
        return False, "missing data", f"the cutoff bar had not arrived {named['stale'].get('wait_limit_s')} s after " \
                                      f"the cutoff (newest bar {named['stale'].get('newest')})"
    if "failed" in named:
        return False, "capture failed", named["failed"].get("reason")
    return False, "no request", "a capture without arm D"


def availability(conn, start: str, end: str, profile: str, requirement: float = 0.90,
                 excluded: Sequence[str] = ()) -> Dict[str, Any]:
    """Every scheduled session in [start, end] (bar ``excluded``) as an opportunity: the observed rate, numerator,
    denominator, exact 95 % interval, the categories, and whether the observed rate meets ``requirement``."""
    sessions: List[Dict[str, Any]] = []
    for s in cal.sessions_between(start, end):
        day = s.session_date.isoformat()
        if day in excluded:
            continue
        ok, category, detail = outcome(conn, day, profile)
        sessions.append({"session_date": day, "success": ok, "category": category, "detail": detail})
    n, k = len(sessions), sum(1 for x in sessions if x["success"])
    return {"profile": profile, "from": start, "to": end, "numerator": k, "denominator": n,
            "rate": None if n == 0 else k / n, "interval": exact_interval(k, n), "requirement": requirement,
            "met": None if n == 0 else k / n >= requirement,
            "categories": dict(Counter(x["category"] for x in sessions)), "sessions": sessions}


def report(res: Dict[str, Any]) -> str:
    """Markdown: the rate with its numerator, denominator and interval, then every opportunity."""
    lines = [f"## Arm D on-time availability ({res['profile']}, {res['from']} to {res['to']})", ""]
    if not res["denominator"]:
        return "\n".join(lines + ["No scheduled opportunity in the range.", ""])
    lo, hi = res["interval"]
    lines += [f"**{res['numerator']} of {res['denominator']} scheduled opportunities ({res['rate']:.0%})** had a "
              f"valid D forecast stored by the deadline; exact 95 % interval {lo:.0%} to {hi:.0%}. The requirement "
              f"({res['requirement']:.0%} observed) is " + ("met" if res["met"] else "not met") + ". A practical "
              "acceptance threshold, not evidence of skill; a few sessions can help choose the setup, not establish "
              "reliable availability.", "",
              "| category | sessions |", "|---|---:|"]
    lines += [f"| {k} | {v} |" for k, v in sorted(res["categories"].items(), key=lambda kv: -kv[1])]
    lines += ["", "| session | outcome | detail |", "|---|---|---|"]
    lines += [f"| {x['session_date']} | {x['category']} | {x['detail'] or ''} |" for x in res["sessions"]]
    return "\n".join(lines) + "\n"
