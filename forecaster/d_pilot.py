# forecaster/d_pilot.py
"""
The v5 latency pilot (contracts/p1_d_latency_pilot.py): its plan against the cap, the
requests once approved, and the report - durations, validation and token costs, never
a score.

    python scripts/nq_journal.py d-pilot --estimate     # what would be sent, against the cap
    python scripts/nq_journal.py d-pilot                # sent after typing "send" (or --approval)
    python scripts/nq_journal.py d-pilot --report       # durations, validation, tokens, cost
"""

from __future__ import annotations

import statistics
from typing import Any, Dict, List, Optional

from contracts import nq_prompt_v2 as defs
from contracts import p1_d_latency_pilot as pilot
from database import journal_store as store
from forecaster import llm_arms as la


def plan(conn) -> Dict[str, Any]:
    """The pilot's plan (llm_arms.plan over its sessions, arm D) against the cap: ``worst`` is the most one request
    can cost (its whole token cap), ``spent`` what the pilot's answered requests cost, and ``fits`` whether the
    next request can be sent without the cap being exceeded even at its worst."""
    p = la.plan(conn, len(pilot.SESSIONS), ("D",), pilot.PROFILE, days=list(pilot.SESSIONS))
    worst = p["usd_max"] / p["requests"] if p["requests"] else 0.0
    spent = sum(r["usd"] or 0.0 for r in rows(conn))
    room = min(p["requests"], pilot.MAX_REQUESTS - len(rows(conn)))
    fits = room > 0 and spent + worst <= pilot.MAX_USD
    return {**p, "cap_usd": pilot.MAX_USD, "cap_requests": pilot.MAX_REQUESTS, "worst": round(worst, 2),
            "spent": round(spent, 4), "fits": fits, "allowed": room if fits else 0}


def run(conn, client) -> Dict[str, Any]:
    """Sends the pilot's requests one at a time (inside structure_llm.manual_requests, after an approval): before
    each, what has been spent plus that request at its worst must stay within the cap - so the cap holds even if
    every request used its whole token cap. Stops at the first that would not fit."""
    sent, counts, stopped = 0, {}, None
    for day in pilot.SESSIONS:
        p = plan(conn)
        if p["requests"] == 0:
            break
        if not p["fits"]:
            stopped = (f"stopped before {day}: ${p['spent']:.2f} spent + ${p['worst']:.2f} at worst is above the "
                       f"${pilot.MAX_USD:.2f} cap" if len(rows(conn)) < pilot.MAX_REQUESTS
                       else f"stopped: {pilot.MAX_REQUESTS} request(s) sent")
            break
        out = la.synthesize(conn, client, day, pilot.PROFILE)
        if out["status"] != "sent":
            continue
        sent += 1
        key = f"D {out['run']['lifecycle_status']}"
        counts[key] = counts.get(key, 0) + 1
    return {"requests_sent": sent, "counts": counts, "stopped": stopped}


def _usd(usage: Optional[Dict[str, Any]]) -> Optional[float]:
    if not usage:
        return None
    return sum((usage.get(k) or 0) * price for k, price in pilot.USD_PER_MTOK.items()) / 1e6


def rows(conn) -> List[Dict[str, Any]]:
    """Every pilot request's run: duration, status, reason, tokens and cost - never its predictions."""
    from forecaster.forecast_service import utc
    version = defs.PROFILES[pilot.PROFILE].snapshot_version
    out = []
    for day in pilot.SESSIONS:
        for r in store.list_forecast_runs(conn, day, day, pilot.PROFILE, la.MODE):
            if r["algorithm_version"] != pilot.ALGORITHM or not r.get("request_id"):
                continue
            run = store.get_forecast_run(conn, r["run_id"])
            if store.get_snapshot(conn, run["snapshot_id"])["snapshot_version"] != version:
                continue
            attempt = (run["evidence"] or {}).get("attempt") or {}
            usage = attempt.get("usage")
            started, done = utc(run["generation_started_at"]), utc(run["generation_completed_at"])
            out.append({"session_date": day, "run_id": run["run_id"], "status": run["lifecycle_status"],
                        "reason": run["failure_reason"], "seconds": (done - started).total_seconds(),
                        "usage": usage, "usd": _usd(usage), "stop_reason": attempt.get("stop_reason")})
    return sorted(out, key=lambda x: x["session_date"])


def report(conn) -> str:
    """The pilot's report as markdown (no score)."""
    rs = rows(conn)
    secs = sorted(r["seconds"] for r in rs)
    valid = sum(1 for r in rs if r["status"] == "issued")
    costs = [r["usd"] for r in rs if r["usd"] is not None]
    lines = [f"# {pilot.NAME}: arm D ({pilot.ALGORITHM}) latency pilot", "",
             f"{len(rs)} of {pilot.MAX_REQUESTS} request(s) answered or failed, profile {pilot.PROFILE}, sessions "
             f"{', '.join(pilot.SESSIONS)}. No predictive score is computed or read. Five requests are first "
             "observations of v5's timing, not an estimate of its tail latency.", ""]
    if secs:
        lines += [f"- **Duration:** fastest {secs[0]:.0f} s, median {statistics.median(secs):.0f} s, slowest "
                  f"{secs[-1]:.0f} s.",
                  f"- **Valid:** {valid} of {len(rs)} passed validation and were issued.",
                  f"- **Cost:** ${sum(costs):.2f} in all at list prices"
                  + (f", ${statistics.median(costs):.3f} median a request." if costs else ".")]
    lines += ["", "| session | status | seconds | input | cache write | cache read | output | USD | reason |",
              "|---|---|---:|---:|---:|---:|---:|---:|---|"]
    for r in rs:
        u = r["usage"] or {}
        usd = "-" if r["usd"] is None else f"{r['usd']:.3f}"
        lines.append(f"| {r['session_date']} | {r['status']} | {r['seconds']:.1f} | {u.get('input_tokens', '-')} | "
                     f"{u.get('cache_creation_input_tokens', '-')} | {u.get('cache_read_input_tokens', '-')} | "
                     f"{u.get('output_tokens', '-')} | {usd} | {r['reason'] or ''} |")
    return "\n".join(lines) + "\n"
