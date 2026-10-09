# forecaster/timeliness.py
"""
Estimated issuance times: when could a pre-open forecast with a given cutoff have been
issued on this feed? Reconstructed from what each session recorded - before any cutoff
is chosen for a live pre-open experiment (docs/forecasting_audit.md, section 7). These
are estimates, not demonstrated delivery: they add the ML forecasts' measured
generation time to the A/B times. Delivery is demonstrated only by a live capture's
'delivered' step (forecaster/live_capture.py).

For a session collected live (bars.first_stored_at known) and a candidate cutoff C:

  data ready      when the bar closing at C and a later bar confirming it were both in
                  the store (first_stored_at) - the snapshot's own readiness rule
  A/B issued      the end of the Auto run that stored the confirming bar (its journal
                  step takes the snapshot and issues A and B; logs/pipeline_run.log)
  with ML         A/B issued + the ML forecasts' measured generation time (their runs'
                  generation_completed_at - generation_started_at), at its fastest,
                  median and slowest
  deadline        09:30:00 ET, the open (the live issue policy's own is 09:29:50)

Nothing is forecast or scored; no request is sent. A session without receipt times,
or whose confirming bar was stored by a run with no recorded end, is counted as not
measurable. The hit rate is over the measured sessions and the mornings nothing
collected, and every scheduled session is listed. A handful of sessions can inform the
operational design (cutoff, deadline, late policy); they say nothing about whether a
forecast is any good.
"""

from __future__ import annotations

import os
import re
import statistics
from datetime import datetime, time, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence

from contracts import nq_forecast as fc
from contracts import nq_prompt_v2 as defs
from features import calendar as cal

CANDIDATES = (time(9, 15), time(9, 20), time(9, 25), time(9, 29))
DEADLINE = time(9, 30)
LIVE_WINDOW = timedelta(hours=2)          # data stored later than this after the cutoff: not collected live
_LOG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs", "pipeline_run.log")


def run_ends(path: str = _LOG) -> List[datetime]:
    """The end of every Auto run in the log, in order."""
    out = []
    try:
        with open(path, errors="replace") as f:
            for line in f:
                m = re.match(r"\[(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ)\] Dashboard: Auto update finished", line)
                if m:
                    out.append(datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc))
    except OSError:
        pass
    return out


def generation_seconds(conn, versions: Sequence[str] = ()) -> List[float]:
    """The measured generation times of the runs of ``versions`` (by default the ML forecasts), fastest first."""
    if not versions:
        from contracts import nq_ml
        versions = nq_ml.ALGORITHMS
    rows = conn.execute(
        "SELECT extract(epoch FROM generation_completed_at - generation_started_at) FROM journal.forecast_runs "
        "WHERE algorithm_version = ANY(%s) AND generation_started_at IS NOT NULL "
        "AND lifecycle_status IN ('issued', 'invalid', 'late');", (list(versions),)).fetchall()
    return sorted(float(r[0]) for r in rows)


def spread(values: Sequence[float]) -> Optional[Dict[str, float]]:
    """Fastest, median and slowest of ``values`` (None when there are none)."""
    return {"n": len(values), "min": min(values), "median": statistics.median(values), "max": max(values)} \
        if values else None


def measure(conn, days: Sequence[str], ends: Optional[List[datetime]] = None,
            ml_seconds: Optional[List[float]] = None) -> Dict[str, Any]:
    """Per candidate cutoff and session: data ready, A/B issued, with ML (fastest, median and slowest measured), the
    slack to the deadline - and the hit rates over the opportunities."""
    ends = run_ends() if ends is None else ends
    ml_seconds = generation_seconds(conn) if ml_seconds is None else sorted(ml_seconds)
    d = spread(ml_seconds)
    ml_min, ml_med, ml_max = (None, None, None) if d is None else (d["min"], d["median"], d["max"])
    out: Dict[str, Any] = {"ml_generation_s": {"version": "the ML forecasts", "values": ml_seconds,
                                              "n": len(ml_seconds), "min": ml_min, "median": ml_med, "max": ml_max},
                           "deadline_et": DEADLINE.strftime("%H:%M"), "candidates": {}}
    for c in CANDIDATES:
        rows = []
        for day in days:
            s = cal.session(day)
            if not s.is_open:
                continue
            cutoff = cal.ny_instant(s.session_date, c)
            deadline = cal.ny_instant(s.session_date, DEADLINE)
            row = conn.execute(
                "SELECT max(b.first_stored_at) FILTER (WHERE b.timestamp_utc = %s) AS bar, "
                "min(b.first_stored_at) FILTER (WHERE b.timestamp_utc >= %s) AS later, "
                "bool_or(b.first_stored_at IS NULL AND b.timestamp_utc >= %s - interval '1 minute') AS unknown "
                "FROM bars b JOIN active_contracts a ON a.contract_id = b.contract_id AND a.trading_day = b.trading_day "
                "WHERE a.symbol = %s AND b.trading_day = %s AND b.interval = '1m' AND b.price_type = 'TRADES' "
                "AND b.timestamp_utc >= %s - interval '1 minute' AND b.timestamp_utc < %s + interval '30 minutes';",
                (cutoff - timedelta(minutes=1), cutoff, cutoff, defs.SYMBOL, day, cutoff, cutoff)).fetchone()
            bar, later = row[0], row[1]
            rec: Dict[str, Any] = {"session_date": day}
            if bar is None or later is None:
                rec["status"] = "not measurable (no receipt times)"
                rows.append(rec)
                continue
            ready = max(_utc(bar), _utc(later))
            if ready - cutoff > LIVE_WINDOW:            # stored hours later: nothing was collecting that morning
                rec.update(status="missed (nothing collected live: data stored "
                                  f"{(ready - cutoff).total_seconds() / 3600:.1f} h after the cutoff)", ready=ready)
                rows.append(rec)
                continue
            run_end = next((e for e in ends if e >= ready), None)
            if run_end is None or run_end - ready > timedelta(minutes=5):
                rec.update(status="not measurable (no recorded Auto run end)", ready=ready)
                rows.append(rec)
                continue
            rec.update(status="measured", ready=ready, ab=run_end,
                       ab_slack_s=(deadline - run_end).total_seconds(),
                       ml_slack_fastest_s=None if ml_min is None else (deadline - run_end).total_seconds() - ml_min,
                       ml_slack_median_s=None if ml_med is None else (deadline - run_end).total_seconds() - ml_med,
                       ml_slack_worst_s=None if ml_max is None else (deadline - run_end).total_seconds() - ml_max,
                       ready_after_cutoff_s=(ready - cutoff).total_seconds())
            rows.append(rec)
        measured = [r for r in rows if r["status"] == "measured"]
        missed = [r for r in rows if r["status"].startswith("missed")]
        # every opportunity with data counts: a morning nothing collected is a miss of the delivered system
        hit = lambda key: (sum(1 for r in measured if r[key] is not None and r[key] >= 0), len(measured) + len(missed))
        out["candidates"][c.strftime("%H:%M")] = {
            "sessions": rows, "scheduled": len(rows), "measured": len(measured), "missed": len(missed),
            "ab_hits": hit("ab_slack_s"), "ml_hits_fastest": hit("ml_slack_fastest_s"),
            "ml_hits_median": hit("ml_slack_median_s"), "ml_hits_worst": hit("ml_slack_worst_s"),
            "ready_after_cutoff_s": spread([r["ready_after_cutoff_s"] for r in measured]),
            "ab_slack_s": spread([r["ab_slack_s"] for r in measured])}
    return out


def _utc(value) -> datetime:
    if not isinstance(value, datetime):
        value = datetime.fromisoformat(str(value).replace(" ", "T").replace("Z", "+00:00"))
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def report(res: Dict[str, Any]) -> str:
    """The estimates as markdown."""
    d = res["ml_generation_s"]
    minutes = lambda sp: "-" if sp is None else f"{sp['min'] / 60:+.1f} / {sp['median'] / 60:+.1f} / {sp['max'] / 60:+.1f}"
    lines = ["# Estimated pre-open issuance times (reconstructed)", "",
             "**Estimates, not demonstrated delivery.** The A/B times are the recorded ends of the Auto runs that "
             "stored each cutoff's confirming bar, and the ML times add the ML forecasts' measured generation time "
             "to them. Demonstrated delivery is a live capture's 'delivered' step (nq_journal.py live, "
             "forecaster/live_capture.py). Ten sessions can inform the operational design - cutoff, deadline, late "
             "policy; they cannot establish reliable predictive performance.", "",
             "Measured from the bars' receipt times and the Auto runs' recorded ends (forecaster/timeliness.py); "
             "nothing forecast, scored or sent. Deadline: the open, " + res["deadline_et"] + " ET. A hit needs a "
             "non-negative slack; a morning nothing was collected counts as a miss; sessions without receipt times "
             "are listed, not counted.", "",
             "The ML forecasts' generation time: "
             + (f"{d['n']} measured run(s) - fastest {d['min']:.1f} s, median {d['median']:.1f} s, slowest "
                f"{d['max']:.1f} s." if d["n"] else "no ML run measured yet, so no ML estimate: its columns stay "
                                                   "empty until one is.")]
    lines += ["", "| cutoff | scheduled | measured | missed (not collected) | A/B in time | A/B + ML in time "
              "(fastest / median / slowest ML) | data ready after the cutoff, min (fastest / median / slowest) | "
              "A/B slack to the open, min (least / median / most) |", "|---|---:|---:|---:|---|---|---|---|"]
    for c, r in res["candidates"].items():
        frac = lambda h: f"{h[0]} of {h[1]}"
        with_ml = (f"{frac(r['ml_hits_fastest'])} / {frac(r['ml_hits_median'])} / {frac(r['ml_hits_worst'])}"
                   if d["n"] else "-")
        lines.append(f"| {c} | {r['scheduled']} | {r['measured']} | {r['missed']} | {frac(r['ab_hits'])} | "
                     f"{with_ml} | {minutes(r['ready_after_cutoff_s'])} | {minutes(r['ab_slack_s'])} |")
    lines += ["", "Per session (minutes; slack to the open, negative = after it):", ""]
    for c, r in res["candidates"].items():
        for x in r["sessions"]:
            if x["status"] == "measured":
                lines.append(f"- {c}, {x['session_date']}: data ready {x['ready_after_cutoff_s'] / 60:.1f} after the "
                             f"cutoff, A/B slack {x['ab_slack_s'] / 60:+.1f}"
                             + (f", with ML {x['ml_slack_worst_s'] / 60:+.1f} to {x['ml_slack_fastest_s'] / 60:+.1f}"
                                if x["ml_slack_worst_s"] is not None else ""))
            else:
                lines.append(f"- {c}, {x['session_date']}: {x['status']}")
    return "\n".join(lines) + "\n"
