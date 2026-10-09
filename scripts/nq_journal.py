#!/usr/bin/env python3
"""
NQ prompt-v2 journal records (docs/nq_prompt_v2.md): pre-open evidence snapshots
and their realised NQ-v2 outcome labels.

    python scripts/nq_journal.py register                                   # definitions only
    python scripts/nq_journal.py snapshot --date 2026-09-24                 # one session
    python scripts/nq_journal.py snapshot --start 2026-06-01 --end 2026-09-25
    python scripts/nq_journal.py outcomes --start 2026-06-01 --end 2026-09-25
    python scripts/nq_journal.py backfill --start 2026-06-01 --end 2026-09-25  # snapshot + outcome, in order
    python scripts/nq_journal.py catch-up                                   # every session past its cutoff not yet stored
    python scripts/nq_journal.py annotate --start 2025-09-01 --end 2026-10-02  # rule-based structure annotations
    python scripts/nq_journal.py match                                      # analogue sets (P1 section 7 rubric)
    python scripts/nq_journal.py annotate-llm --start 2025-09-01 --end 2026-10-02 --estimate   # Claude: size and cost
    python scripts/nq_journal.py annotate-llm --start 2025-09-01 --end 2026-10-02 --batch      # Claude backfill
                                     (Claude requests are manual: only from a terminal, after typing "send")
    python scripts/nq_journal.py annotate-llm --date 2026-10-02 --close-unresolved   # close requests a crash lost
    python scripts/nq_journal.py match --protocol llm                       # analogues over Claude's annotations
    python scripts/nq_journal.py analogues --date 2026-10-02 [--outcomes]   # one session's analogues
    python scripts/nq_journal.py annotation-review-set --name preopen_review_v1   # outcome-blind review set
    python scripts/nq_journal.py annotation-review-report --name preopen_review_v1
    python scripts/nq_journal.py show --date 2026-09-24                     # snapshot + P1 / P2 records
    python scripts/nq_journal.py forecast --start 2025-09-01 --end 2026-10-02   # baseline forecasts (replay)
    python scripts/nq_journal.py forecast --snapshot-id S --annotation-id A --set-id X   # explicit evidence
    python scripts/nq_journal.py show-forecast --run-id R                   # one stored forecast run
    python scripts/nq_journal.py experiment-register --name hist_dev_v1 --start 2025-09-02 --end 2026-10-02
    python scripts/nq_journal.py experiment-score --name hist_dev_v1        # freeze cases, score, report
    python scripts/nq_journal.py experiment-list
    python scripts/nq_journal.py live                                       # the pre-open live capture (3D)
    python scripts/nq_journal.py preview                                    # forecast now: a preview, never stored
    python scripts/nq_journal.py llm-forecast --sessions 1 --estimate      # arms C and D: the plan and its cost
    python scripts/nq_journal.py llm-forecast --sessions 1                 # ... sent after typing "send"
    python scripts/nq_journal.py llm-forecast --date 2026-10-01 --date 2026-10-02   # chosen days
    python scripts/nq_journal.py llm-forecast --start 2026-09-21 --end 2026-10-02 --arms C
    python scripts/nq_journal.py annotate-llm --restricted --start 2025-09-01 --end 2026-10-02 --batch  # C's pool
    python scripts/nq_journal.py live-report --start 2026-10-05 --end 2026-10-09   # capture timing
    python scripts/nq_journal.py rth-issue                                  # RTH analogues of the session in progress
    python scripts/nq_journal.py rth-backfill --start 2025-09-02 --end 2026-10-08  # reconstructions at 15/30/60 min
    python scripts/nq_journal.py rth-backfill --date 2026-10-07 --all-minutes      # every window of the first hour
    python scripts/nq_journal.py rth-show --date 2026-10-07 --minute 15     # reconstructed view at 09:45
    python scripts/nq_journal.py rth-show --date 2026-10-12 --view issued --at 10:05   # as issued by 10:05 ET
    python scripts/nq_journal.py rth-calibrate --end 2026-10-07              # reproduce the tolerances' calibration
    python scripts/nq_journal.py timeliness --start 2026-10-05 --end 2026-10-16   # pre-open cutoffs vs the open
    python scripts/nq_journal.py rth-eval-status                            # both evaluations' health - never a score
    python scripts/nq_journal.py rth-eval-score --version rth_operational_v1   # one scoring, at the endpoint only
    python scripts/nq_journal.py review-set --name stage1_review_v1          # choose the 25-session review set
    python scripts/nq_journal.py review-report --name stage1_review_v1       # the reviewer's verdicts, per field

``--profile`` picks the cutoff profile (research_0929, the default, or
operational_0927); each has its own snapshot version. Snapshots are historical
reconstructions; catch-up takes a session in progress once its bars past the cutoff
are stored (forecaster/journal.py snapshot_pending). Outcomes are recorded only once
a session is final (two hours after its scheduled close) and become a new revision
only when they change. Claude API requests (annotate-llm) are manual only, for now:
from a terminal, after typing "send".

Every command registers the label, convention and snapshot definitions first; a
changed definition under an existing version name stops the run. The IB collector
runs ``catch-up`` itself after every full collection (forecaster/journal.py).
"""

import argparse
import logging
import os
import sys
from datetime import datetime

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from config import Config
from contracts import nq_prompt_v2 as defs
from database import journal_store as store
from database.connection import get_db_connection, init_database
from features import calendar as cal
from features.nq_evidence import SnapshotError
from forecaster import review_set
from contracts import nq_forecast as fc
from contracts import nq_preopen as preopen
from forecaster.journal import annotate, catch_up, match, record_outcome, register, take_snapshot
from forecaster.outcome_display import p2_record
from forecaster.preopen_display import p1_record

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
PROTOCOLS = {"rules": preopen.RULES_PROTOCOL_VERSION, "llm": preopen.LLM_PROTOCOL_VERSION}
logger = logging.getLogger("nq_journal")


def _sessions(args):
    if args.date:
        return [cal.session(args.date)]
    return cal.sessions_between(args.start, args.end)


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

def cmd_snapshot(conn, args):
    status = 0
    for s in _sessions(args):
        try:
            take_snapshot(conn, s.session_date, args.profile)
        except SnapshotError as e:
            logger.error(f"{s.session_date}: {e}")
            status = 1
    return status


def cmd_outcomes(conn, args):
    version = defs.PROFILES[args.profile].snapshot_version
    n = sum(record_outcome(conn, snap) is not None
            for snap in store.list_snapshots(conn, args.start, args.end, version))
    logger.info(f"Outcomes recorded or confirmed for {n} snapshot(s).")
    return 0


def cmd_backfill(conn, args):
    """Snapshot, then outcome, session by session in order."""
    status = 0
    for s in _sessions(args):
        try:
            snapshot_id = take_snapshot(conn, s.session_date, args.profile)
        except SnapshotError as e:
            logger.error(f"{s.session_date}: {e}")
            status = 1
            continue
        record_outcome(conn, store.get_snapshot(conn, snapshot_id))
    return status


def cmd_annotate(conn, args):
    """The rule-based structure annotation of every stored snapshot in the range (idempotent)."""
    version = defs.PROFILES[args.profile].snapshot_version
    snaps = store.list_snapshots(conn, args.start, args.end, version)
    for snap in snaps:
        annotate(conn, snap)
    logger.info(f"Annotations recorded or confirmed for {len(snaps)} {version} snapshot(s).")
    return 0


def cmd_match(conn, args):
    """The analogue sets of every annotated snapshot (stored when new)."""
    match(conn, args.profile, PROTOCOLS[args.protocol])
    return 0


def cmd_annotate_llm(conn, args):
    """The Claude structure annotation (forecaster/structure_llm.py): estimate, live requests or a batch - sent only
    after a person confirmed at a terminal."""
    from forecaster import structure_llm as llm
    protocol = llm.FULL
    if args.restricted:
        from forecaster.llm_arms import RESTRICTED as protocol
    version = defs.PROFILES[args.profile].snapshot_version
    unresolved = store.unresolved_inference_requests(conn, protocol.version)
    batches = sorted({r["batch_id"] for r in unresolved if r["batch_id"]})
    stranded = [r for r in unresolved if not r["batch_id"]]
    if stranded and args.close_unresolved:
        print(f"Closed {llm.close_unresolved(conn, stranded, protocol)} unresolved request(s) as error attempts.")
        stranded, unresolved = [], [r for r in unresolved if r["batch_id"]]
    for r in stranded:
        print(f"  unresolved {r['mode']} request {r['request_id']} ({r['session_date']}, sent {r['created_at']}): its "
              f"answer cannot be fetched; --close-unresolved records it as an error so the session can be sent again")
    waiting = {r["snapshot_id"] for r in unresolved}
    snaps = store.list_snapshots(conn, args.date or args.start, args.date or args.end, version)
    todo = [s for s in snaps if store.latest_annotation(conn, s["snapshot_id"], protocol.version) is None
            and s["snapshot_id"] not in waiting]
    print(f"{len(todo)} of {len(snaps)} {version} snapshot(s) to send for {protocol.version}"
          + (f"; {len(waiting)} with an unresolved request" if waiting else "")
          + (f"; {len(batches)} recorded batch(es) to collect first" if batches else "") + ".")
    if todo:
        if args.restricted:
            from contracts.nq_prompt_v2 import canonical_json
            from forecaster.llm_arms import _cost
            chars = [len(llm._system_prompt(protocol)) + 24 + len(canonical_json(protocol.bundle(x)[0])) for x in todo]
            live = _cost(conn, "C", chars, preopen.RESTRICTED_MAX_TOKENS, False)
            print(f"  ~${live['usd']:.2f} live, ~${live['usd'] / 2:.2f} with --batch ({protocol.model}, effort "
                  f"{protocol.effort}); {live['basis']}")
        else:
            e = llm.estimate(todo, protocol)
            print(f"  about {e['input_tokens']:,} input and {e['output_tokens']:,} output tokens: ~${e['usd_live']} "
                  f"live, ~${e['usd_batch']} with --batch ({protocol.model}, effort {protocol.effort})")
    if args.estimate or not (todo or batches):
        return 0
    what = ([f"{len(todo)} {'batch' if args.batch else 'live'} request(s)"] if todo else []) + \
           ([f"collect {len(batches)} recorded batch(es)"] if batches else [])
    if not confirmed_by_hand(f"Claude API ({preopen.LLM_MODEL}, effort {preopen.LLM_EFFORT}): {' and '.join(what)}."):
        return 1
    with llm.manual_requests():
        return _send_llm(conn, args, llm, todo, batches, protocol)


def confirmed_by_hand(summary: str) -> bool:
    """
    Claude API requests are started by hand only, for now: a person at a terminal reads ``summary`` and types
    "send". Without a terminal (cron, the dashboard's jobs, a pipe) nothing is sent.
    """
    if not sys.stdin.isatty():
        print("Claude API requests are started by hand only, for now: run this in a terminal and confirm there "
              "(or confirm in the dashboard). Nothing was sent.")
        return False
    print(summary)
    try:
        answer = input('Type "send" to go ahead, anything else to stop: ')
    except EOFError:
        answer = ""
    if answer.strip().lower() != "send":
        print("Stopped. Nothing was sent.")
        return False
    return True


def _send_llm(conn, args, llm, todo, batches, protocol):
    """The requests of a confirmed annotate-llm run (inside structure_llm.manual_requests)."""
    import time
    from datetime import datetime, timezone
    try:
        import anthropic
        client = anthropic.Anthropic()
        client.models.retrieve(preopen.LLM_MODEL)
    except Exception as e:
        print(f"Claude API unavailable ({type(e).__name__}: {e}). Set ANTHROPIC_API_KEY in .env.")
        return 1
    status = 0
    for batch_id in batches:                       # an earlier run's batch: collect it, never send it again
        print(f"  collecting recorded batch {batch_id} ...")
        started = datetime.now(timezone.utc)
        while client.messages.batches.retrieve(batch_id).processing_status != "ended":
            time.sleep(llm.batch_poll_delay(started))
        counts = llm.collect_batch(conn, client, batch_id, protocol)
        print(f"  results: {counts}")
        status |= not set(counts) <= {"ok"}
    if not todo:
        return status
    if not args.batch:
        for snap in todo:
            attempt = llm.annotate_live(conn, client, snap, protocol)
            print(f"  {snap['session_date']}: {attempt['status']}" + (f" - {attempt['error']}" if attempt.get("error") else ""))
            status |= attempt["status"] not in ("ok", "contaminated")
        return status
    submitted = datetime.now(timezone.utc)
    batch_id = llm.submit_batch(conn, client, todo, protocol)
    if batch_id is None:
        print("  no batch was created: every snapshot contaminated, or the API refused it (see the error attempts)")
        return 1
    print(f"  batch {batch_id} submitted and recorded ({len(todo)} requests); waiting for it to end - an "
          f"interrupted run collects it next time ...")
    while client.messages.batches.retrieve(batch_id).processing_status != "ended":
        time.sleep(llm.batch_poll_delay(submitted))
    counts = llm.collect_batch(conn, client, batch_id, protocol)
    print(f"  results: {counts}")
    return status | (0 if set(counts) <= {"ok"} else 1)


def cmd_llm_forecast(conn, args):
    """Arms C and D (forecaster/llm_arms.py) over the last --sessions sessions: the plan, then - confirmed by hand
    in a terminal, or by a dashboard approval (--approval) - the Claude requests."""
    from forecaster import approvals
    from forecaster import llm_arms as la
    from forecaster import structure_llm as llm
    arms = tuple(a for a in "CD" if a in args.arms.upper())
    if not arms:
        print("--arms names no arm (C, D, or CD).")
        return 1
    chosen = None
    if args.date or args.start or args.end:
        if bool(args.start) != bool(args.end):
            print("give --start and --end together")
            return 1
        chosen = list(args.date or []) + ([s.session_date.isoformat() for s in cal.sessions_between(args.start,
                                                                                                    args.end)]
                                          if args.start else [])
    days = la.target_sessions(args.sessions, days=chosen)
    if not days:
        print("No scheduled session among the chosen days.")
        return 1
    plan = la.plan(conn, args.sessions, arms, args.profile, days=days, batch=args.batch)
    print(f"Arms {' and '.join(arms)} over {len(days)} session(s), {days[0]} to {days[-1]} ({plan['model']}, "
          f"effort {plan['effort']}{', Batch API' if args.batch else ''}):")
    for row in plan["sessions"]:
        print(f"  {row['session_date']}: " + (f"skipped - {row['skip']}" if row.get("skip") else
                                             ", ".join(f"{a} {row[a]}" for a in arms if a in row)))
    if "C" in arms:
        print(f"  arm C's pool: {plan['restricted_pool']} session(s) annotated by {preopen.RESTRICTED_PROTOCOL_VERSION}"
              f" - analogues come only from earlier ones; with none, arm C is the prior alone")
    print(f"  {plan['requests']} Claude request(s), roughly ${plan['usd']} at list prices"
          + (" (Batch API: half price)" if args.batch else "")
          + f" - a rough estimate; at most ${plan['usd_max']} if every request used its whole token cap")
    for arm, basis in plan["estimate_basis"].items():
        print(f"  arm {arm}: {basis}")
    for arm, ids in plan["pending_batches"].items():
        print(f"  arm {arm}: {len(ids)} recorded batch(es) to collect first: {', '.join(ids)}")
    if args.estimate:
        return 0
    if plan["requests"] == 0 and not plan["pending_batches"]:   # nothing to send or collect: C's runs only
        la.run(conn, None, args.sessions, arms, args.profile, max_requests=0, days=days)
        return 0
    scope = {"command": "llm-forecast", "sessions": days, "arms": "".join(arms), "profile": args.profile,
             "batch": bool(args.batch)}
    if args.approval:
        approved, why = approvals.redeem(args.approval, scope)
        print(why + ("" if approved else ". Nothing was sent."))
        if approved is None:
            return 1
        cap = int(approved.get("max_requests") or 0)
    else:
        what = (f"Send {plan['requests']} Claude request(s) for arms {' and '.join(arms)}"
                + (" through the Batch API" if args.batch else "") if plan["requests"] else
                "Collect the recorded batches (nothing new is sent)")
        if not confirmed_by_hand(f"{what}?"):
            return 1
        cap = plan["requests"]
    with llm.manual_requests():
        try:
            import anthropic
            client = anthropic.Anthropic()
            client.models.retrieve(preopen.LLM_MODEL)
        except Exception as e:
            print(f"Claude API unavailable ({type(e).__name__}: {e}). Set ANTHROPIC_API_KEY in .env.")
            return 1
        summary = la.run(conn, client, args.sessions, arms, args.profile, max_requests=cap, days=days,
                         connect=lambda: get_db_connection(args.db),
                         batch=args.batch, wait_minutes=args.wait_minutes)
    print(f"{summary['requests_sent']} request(s) sent; " + ", ".join(f"{k}: {v}" for k, v in summary["counts"].items()))
    bad = [k for k in summary["counts"] if any(w in k for w in ("failed", "invalid", "error", "refused"))]
    return 1 if bad else 0


def _components_line(members, feature):
    marks = {True: "=", False: "x", None: "."}
    out = []
    for m in members:
        c = m["components"][feature]
        out.append(marks[None if not c["comparable"] else c["score"] == "1.000000"] if feature != "Chop Score"
                   else ("." if not c["comparable"] else f"{float(c['score']):.2f}"))
    return " ".join(f"{x:>4}" for x in out)


def cmd_analogues(conn, args):
    """One session's analogues: the features side by side; outcomes only with --outcomes."""
    version = defs.PROFILES[args.profile].snapshot_version
    snaps = store.list_snapshots(conn, args.date, args.date, version)
    if not snaps:
        print(f"No {version} snapshot for {args.date}.")
        return 1
    aset = store.latest_analogue_set(conn, snaps[0]["snapshot_id"], preopen.MATCHER_VERSION, defs.LABEL_VERSION,
                                     PROTOCOLS[args.protocol])
    if aset is None:
        print(f"No {preopen.MATCHER_VERSION} analogue set over {PROTOCOLS[args.protocol]} for {args.date}; "
              f"run 'match' first.")
        return 1
    members = aset["members"]
    print(f"{args.date}: {len(members)} analogue(s) from {aset['pool_size']} earlier session(s) "
          f"({preopen.MATCHER_VERSION}, {aset['data_mode']}); excluded {aset['excluded'] or 'none'}")
    print(f"  {'':24} {'target':>14}  " + " ".join(f"{m['session_date'][5:]:>5}" for m in members))
    print(f"  {'similarity / coverage':24} {'':>14}  " + " ".join(
        f"{float(m['similarity']):5.1f}" for m in members))
    print(f"  {'':24} {'':>14}  " + " ".join(f"{float(m['comparable_weight']):5.0f}" for m in members))
    for feature in preopen.MATCH_WEIGHTS:
        target = members[0]["components"][feature]["target"] if members else None
        print(f"  {feature:24} {str(target):>14}  {_components_line(members, feature)}")
    print("  (= match, x mismatch, . not comparable; Chop Score shows its similarity)")
    print(f"  mean similarity {aset['mean_similarity']}")
    if not args.outcomes:
        print("  outcomes hidden (outcome-blind); add --outcomes to show them")
        return 0
    for target, t in aset["outcome_summary"]["targets"].items():
        counts = ", ".join(f"{k} {v}" for k, v in t["counts"].items() if v)
        smoothed = ", ".join(f"{k} {float(v):.2f}" for k, v in (t["smoothed"] or {}).items())
        print(f"  {target:20} {t['status']:10} n={t['eligible']} ({counts or '-'}) smoothed: {smoothed or '-'}")
    return 0


def cmd_annotation_review_set(conn, args):
    """Chooses and stores an outcome-blind review set of pre-open annotations (forecaster/review_set.py)."""
    version = defs.PROFILES[args.profile].snapshot_version
    candidates = []
    for snap in store.list_snapshots(conn, "2000-01-01", "2100-01-01", version):
        a = store.latest_annotation(conn, snap["snapshot_id"], preopen.RULES_PROTOCOL_VERSION)
        if a is None or a["integrity_status"] != "ok":
            continue
        candidates.append({"snapshot_id": snap["snapshot_id"], "session_date": str(snap["session_date"]),
                           "annotation_id": a["annotation_id"],
                           "items": review_set.annotation_items(a, str(snap["session_date"]))})
    if not candidates:
        print(f"No {preopen.RULES_PROTOCOL_VERSION} annotations; run 'annotate' first.")
        return 1
    chosen, coverage = review_set.select_sessions(candidates, args.size)
    by_id = {c["snapshot_id"]: c for c in candidates}
    members = [{**c, "annotation_id": by_id[c["snapshot_id"]]["annotation_id"]} for c in chosen]
    selection = {"rule": review_set.ANNOTATION_RULE, "size": args.size, "candidates": len(candidates), **coverage}
    if not store.create_annotation_review_set(conn, args.name, preopen.RULES_PROTOCOL_VERSION, version, selection,
                                              members):
        print(f"Annotation review set {args.name} already exists; choose another name.")
        return 1
    print(f"{args.name}: {len(chosen)} of {len(candidates)} sessions, covering {coverage['covered']} of "
          f"{coverage['items']} classes")
    return 0


def cmd_annotation_review_report(conn, args):
    members = store.annotation_review_members(conn, args.name)
    if not members:
        print(f"No annotation review set {args.name}.")
        return 1
    verdicts = store.latest_annotation_verdicts(conn, args.name)
    print(f"{args.name}: {len({v['snapshot_id'] for v in verdicts})} of {len(members)} sessions reviewed")
    counts = {}
    for v in verdicts:
        counts.setdefault(v["field"], {"agree": 0, "disagree": 0, "unsure": 0})[v["verdict"]] += 1
    for field, c in sorted(counts.items(), key=lambda kv: (-kv[1]["disagree"], kv[0])):
        print(f"  {field:24} agree {c['agree']:3}  disagree {c['disagree']:3}  unsure {c['unsure']:3}")
    for v in verdicts:
        if v["verdict"] != "agree":
            print(f"  {v['session_date']} {v['field']}: {v['verdict']} (shown {v['shown_value']!r})"
                  + (f" - {v['note']}" if v["note"] else ""))
    return 0


def cmd_catch_up(conn, args):
    """Every session since the journal's first past its cutoff with no snapshot yet, then outcomes (catch_up)."""
    return 1 if catch_up(conn, args.profile)["failed"] else 0


def cmd_show(conn, args):
    version = defs.PROFILES[args.profile].snapshot_version
    snaps = store.list_snapshots(conn, args.date, args.date, version)
    if not snaps:
        print(f"No {version} snapshot for {args.date}; run 'snapshot' first.")
        return 1
    snap = snaps[0]
    p = snap["payload"]
    print(f"{p['identity']['session_date']} ({p['identity']['weekday']}) {p['identity']['local_symbol']} "
          f"- {version}, cutoff {p['cutoff']['cutoff_et']} ET, {snap['data_mode']}, {snap['pit_availability_status']}")
    for name, ref in p["references"].items():
        print(f"  {name:16} {ref['value'] if ref['value'] is not None else 'Unavailable':>12}  {ref['status']}"
              + (f" - {ref['detail']}" if ref.get("detail") else ""))
    atr = p["atr"]
    print(f"  daily ATR {atr['daily'].get('value')} ({atr['daily']['status']}), 2-min ATR "
          f"{atr['two_minute'].get('value')} ({atr['two_minute']['status']}); T={p['thresholds']['T']}, "
          f"B={p['thresholds']['B']}")
    events = p["events"]
    print(f"  events: {', '.join(e['time_et'] + ' ' + e['name'] for e in events['events']) or 'none'}"
          + ("" if events["covered_sources"] else " (no calendar coverage)"))
    annotation = store.latest_annotation(conn, snap["snapshot_id"], PROTOCOLS[args.protocol])
    aset = store.latest_analogue_set(conn, snap["snapshot_id"], preopen.MATCHER_VERSION, defs.LABEL_VERSION,
                                     PROTOCOLS[args.protocol])
    run = store.current_forecast_run(conn, args.date, args.profile, "historical_replay", fc.BASELINE_VERSION)
    if run is not None:                                # the run shows with the evidence it was issued on
        run = store.get_forecast_run(conn, run["run_id"])
        annotation = store.get_annotation(conn, run["annotation_id"]) if run["annotation_id"] else None
        aset = store.get_analogue_set(conn, run["analogue_set_id"]) if run["analogue_set_id"] else None
    provenance, rows = p1_record(snap, annotation, aset, run)
    print(f"  P1 pre-open record - {provenance}:")
    for i, (prop, value, basis) in enumerate(rows, 1):
        print(f"    {i:2}. {prop:36} {value}" + (f"  [{basis}]" if value == "Unavailable" else ""))
    outcome = store.latest_outcome(conn, snap["snapshot_id"], defs.LABEL_VERSION)
    if outcome is None:
        print("  no outcome recorded yet")
        return 0
    print(f"  outcome r{outcome['outcome_revision']} ({defs.LABEL_VERSION}) - P2 realised-outcome record:")
    for prop, value in p2_record(snap, outcome):
        print(f"    {prop:42} {value}")
    return 0


def cmd_forecast(conn, args):
    """Baseline forecasts (forecaster/forecast_service.py): explicit evidence ids, or the sessions of a date range
    resolved to their own snapshot, annotation and analogue set."""
    from forecaster.forecast_service import ForecastInputError, forecast_session, run_forecast
    if args.snapshot_id:
        try:
            run, created = run_forecast(conn, args.snapshot_id, args.annotation_id, args.set_id, args.profile)
        except ForecastInputError as e:
            print(f"Rejected: {e}")
            return 1
        _print_run_line(run, created)
        return 0 if run["lifecycle_status"] == "issued" else 1
    history = store.outcome_history(conn, defs.LABEL_VERSION)
    status = 0
    for s in cal.sessions_between(args.date or args.start, args.date or args.end):
        result = forecast_session(conn, s.session_date.isoformat(), args.profile, PROTOCOLS[args.protocol],
                                  history=history)
        if result is None:
            print(f"  {s.session_date}: no snapshot or annotation")
            continue
        _print_run_line(*result)
        status |= result[0]["lifecycle_status"] not in ("issued", "unavailable")
    return status


def _print_run_line(run, created):
    fifteen = run["predictions"].get("direction_15m")
    detail = (f"15m direction {fifteen['predicted_label'] or fifteen['status']}" if fifteen
              else run["failure_reason"])
    print(f"  {run['session_date']}: run {run['run_id']} {run['lifecycle_status']}"
          f"{' (new)' if created else ' (already stored)'} - {detail}")


def cmd_show_forecast(conn, args):
    """One stored forecast run, by its id: provenance, P1's forecast fields, the per-target detail and evidence."""
    from forecaster.forecast_display import forecast_rows, target_rows
    run = store.get_forecast_run(conn, args.run_id)
    if run is None:
        print(f"No forecast run {args.run_id!r}.")
        return 1
    print(f"Run {run['run_id']} - {run['session_date']} {run['profile']}, {run['mode'].replace('_', ' ')}, "
          f"{run['lifecycle_status']}" + (f" at {run['issued_at']} (database clock)" if run["issued_at"] else
                                          f": {run['failure_reason']}"))
    print(f"  {run['algorithm_version']}, {run['schema_version']}, {run['issue_policy']}, labels "
          f"{run['label_version']}, code {run['code_revision']}")
    print(f"  evidence: snapshot {run['snapshot_id']}, annotation {run['annotation_id']}, analogue set "
          f"{run['analogue_set_id']}; digest {run['evidence_digest'][:16]}"
          + (f"; supersedes {run['supersedes_run_id']}" if run["supersedes_run_id"] else ""))
    ev = run["evidence"] or {}
    if ev.get("members") is not None:
        print("  analogues: " + ", ".join(f"{m['session_date']} ({float(m['similarity']):.0f}%)"
                                            for m in ev["members"]) + f"; prior {ev['prior']['sessions']} "
              f"session(s) - {ev['prior']['known_as_of']}")
    for prop, (value, basis) in forecast_rows(run).items():
        print(f"    {prop:38} {value:16} [{basis}]")
    for r in target_rows(run):
        dist = ", ".join(f"{k} {v}" for k, v in r["distribution"].items()) or "-"
        print(f"  {r['target']:20} {r['status']:21} {r['class']:24} n={r['eligible']} (+{r['without_label']} "
              f"unlabelled), prior {r['prior_sessions']} (+{r['prior_without_label']}): {dist}"
              + (f" - {r['reason']}" if r["reason"] else ""))
    return 0


def cmd_experiment_register(conn, args):
    """Registers an experiment's manifest (forecaster/experiments.py) - before any score exists."""
    from forecaster import experiments as ex
    try:
        if args.design == "d-research":
            from contracts import p1_d_research
            if args.name != p1_d_research.NAME:
                raise ValueError(f"the d-research design is named {p1_d_research.NAME}")
            manifest = p1_d_research.manifest(args.start, args.end)
        else:
            manifest = ex.experiment_manifest(args.name, args.start, args.end, args.profile, args.purpose,
                                              args.official_run)
        new = ex.register_experiment(conn, manifest)
    except (ValueError, store.VersionConflict) as e:
        print(f"Not registered: {e}")
        return 1
    print(f"{args.name}: {'registered' if new else 'already registered, identical'} - sessions "
          f"{args.start} to {args.end}, {args.profile}, {args.purpose}, official run '{args.official_run}', arms "
          + ", ".join(f"{k} {v['algorithm']}" for k, v in manifest["arms"].items())
          + f"; primary {manifest['primary']['target']} log loss. No score has been computed.")
    return 0


def cmd_experiment_score(conn, args):
    """Freezes an experiment's cases (once), scores them, stores the result and writes the report."""
    from forecaster import experiments as ex
    try:
        manifest = ex.load_manifest(conn, args.name)
    except ValueError as e:
        print(e)
        return 1
    results = ex.score_experiment(conn, args.name)
    result_id = ex.store_results(conn, args.name, results)
    path = ex.write_report(manifest, results, args.report_dir, result_id)
    for key, p in results["primary"]["paired"].items():
        ll = p["log_loss"]
        print(f"{args.name} ({manifest['purpose']}): {results['primary']['target']} {key} on {p['common']} common "
              f"sessions - log loss diff {ex._f(ll['diff'])} {ex._ci(ll['interval'])} over {ll['both_finite']} "
              f"finite pairs, Brier diff {ex._f(p['brier']['diff'])} {ex._ci(p['brier']['interval'])}")
    print(f"result {result_id}; report {path}")
    return 0


def cmd_experiment_list(conn, args):
    for rec in store.list_versions(conn, "experiment"):
        m, results = rec["definition"], store.experiment_results(conn, rec["version"])
        print(f"{rec['version']}: {m['sessions']['from']} to {m['sessions']['to']}, {m['purpose']}, registered "
              f"{str(rec['registered_at'])[:16]}; {len(results)} scoring(s)"
              + (f", latest {str(results[0]['computed_at'])[:16]}" if results else ""))
    return 0


def cmd_live(conn, args):
    """The live capture of one session (forecaster/live_capture.py): bars at the cutoff, live snapshot, both arms."""
    from forecaster import live_capture as live
    day = args.date or datetime.now(cal.NY_TZ).date().isoformat()
    try:
        session = cal.session(day)
    except Exception as e:
        print(f"{day}: {e}")
        return 1
    if not session.is_open:
        print(f"{day} is not a scheduled session; nothing to capture.")
        return 0
    try:
        app = live.connect_ib(Config.IB_HOST, Config.IB_PORT, Config.IB_CLIENT_ID + 1)
    except live.LiveCaptureError as e:
        print(e)
        return 1
    try:
        result = live.capture(conn, app, day, args.profile)
    except live.LiveCaptureError as e:
        print(e)
        return 1
    finally:
        app.disconnect()
    for c in store.live_captures(conn, day, day):
        if c["capture_id"] == result["capture_id"]:
            print(f"{day} {args.profile}: capture {c['capture_id']} - {result['status']}")
            for line in live.timing(conn, c):
                print(f"  {line}")
    return 0 if result["status"] == "issued" else 1


def cmd_preview(conn, args):
    """Forecast now (forecaster/preview.py): the next session's forecast from the data stored so far, written to the
    preview file the dashboard shows - nothing goes into the journal."""
    from forecaster import preview as pv
    from forecaster.forecast_display import target_rows
    result = pv.preview(conn)
    print(f"Preview file: {pv.save(result, args.out or pv.PREVIEW_FILE)}")
    if result["status"] != "ok":
        print(f"No preview: {result['reason']}")
        return 0
    print(f"{result['session_date']} as of {result['as_of_et']} (data through {result['data_through'][11:16]} UTC; "
          f"{'the whole pre-open' if result['complete'] else 'the pre-open so far, to the ' + result['cutoff_et'] + ' ET cutoff'})")
    aset = result["analogue_set"]
    print("  analogues: " + (", ".join(f"{m['session_date']} ({float(m['similarity']):.0f}%)" for m in aset["members"])
                             if aset and aset["members"] else "none"))
    for run in result["runs"]:
        print(f"  {run['algorithm_version']}: {run['lifecycle_status']}"
              + (f" - {run['failure_reason']}" if run["failure_reason"] else ""))
        for r in target_rows(run):
            print(f"    {r['property']}: {r['class']}" + (f" ({', '.join(f'{k} {v}' for k, v in r['distribution'].items())})"
                                                         if r["distribution"] else ""))
    return 0


def cmd_rth_issue(conn, args):
    """The RTH analogue sets of the session in progress (forecaster/rth_analogues.issue): its newest completed
    window from the open and any checkpoint not stored yet - what Auto runs after each collection."""
    from forecaster import rth_analogues as ra
    ra.register(conn)
    result = ra.issue(conn, day=args.date, issued_by=args.by)
    if result["status"] != "issued":
        print(f"RTH analogues of {result['session_date']}: {result['status']} - {result['reason']}")
        return 0                                  # waiting, closed or busy (another issue is storing): not a failure
    for m, set_id, new in result["stored"]:
        aset = store.get_rth_set(conn, set_id)
        print(("new: " if new else "already stored: ") + ra.describe(aset))
        print("  " + (", ".join(f"#{x['rank']} {x['session_date']} {float(x['similarity']):.1f}%"
                                for x in aset["members"]) or "no analogue"))
    for m, version, forecast_id, new in result["forecasts"]:
        print(f"  {version} forecasts of the {m}-minute window: {'stored' if new else 'already stored'} "
              f"({forecast_id[:8]})")
    if result["stop"]["state"] != "complete":
        print(f"  the window stops at {result['minutes']} minute(s): {ra.stop_text(result['stop'])}")
    return 0


def cmd_rth_backfill(conn, args):
    """Historical reconstructions of RTH analogue sets: the checkpoints (15, 30, 60 minutes) or, with
    --all-minutes, every window of the first hour - stored once, labelled as reconstructions."""
    from contracts import nq_rth as rth
    from forecaster import rth_analogues as ra
    ra.register(conn)
    days = [s.session_date.isoformat() for s in _sessions(args)]
    minutes = range(1, rth.MAX_MINUTES + 1) if args.all_minutes else rth.CHECKPOINTS
    result = ra.reconstruct(conn, days, minutes)
    print(f"RTH analogues ({rth.RTH_MATCHER_VERSION}): {result['new']} new set(s), {result['already']} already "
          f"stored, over {len(days)} session(s)")
    for day, why in sorted(result["skipped"].items()):
        print(f"  {day}: {why}")
    return 0


def cmd_rth_show(conn, args):
    """One RTH set of a session, in one of two views: reconstructed (default) - the newest calculation of the
    longest stored window not past --minute (default 60), live or not; issued - what had been issued live by --at
    (an ET time of the session's day; default: the end of the day), never a later correction or a backfill."""
    from datetime import time as dtime
    from contracts import nq_rth as rth
    from forecaster import rth_analogues as ra
    windows = store.rth_windows(conn, defs.SYMBOL, args.date, rth.RTH_MATCHER_VERSION)
    if not windows:
        print(f"No {rth.RTH_MATCHER_VERSION} RTH analogue set of {args.date} is stored.")
        return 1
    print("Stored windows: " + ", ".join(f"{w['elapsed_minutes']}" + ("*" if w["checkpoint"] else "")
                                         + (f" ({w['sets']} sets)" if w["sets"] > 1 else "")
                                         + (" live" if w["live"] else "") for w in windows)
          + "   (* checkpoint)")
    if args.view == "issued":
        at = (cal.ny_instant(cal.session(args.date).session_date, dtime.fromisoformat(args.at)) if args.at
              else None)
        aset = store.rth_set_issued(conn, defs.SYMBOL, args.date, rth.RTH_MATCHER_VERSION, at=at)
        if aset is None:
            print(f"As issued: nothing was issued live for {args.date}" + (f" by {args.at} ET." if args.at else "."))
            return 1
        print("As issued" + (f" by {args.at} ET" if args.at else "") + ":")
    else:
        aset = store.rth_set_at(conn, defs.SYMBOL, args.date, rth.RTH_MATCHER_VERSION, args.minute)
        if aset is None:
            print(f"No window of {args.minute} minutes or less is stored.")
            return 1
        print("Reconstructed (the newest calculation, live or not):")
    print(ra.describe(aset))
    print(f"  pool {aset['pool_size']} earlier session(s); excluded: "
          + (", ".join(f"{k.replace('_', ' ')} {v}" for k, v in aset["excluded"].items()) or "none")
          + "; similarity is resemblance of the observed openings, not a probability")
    feats = list(rth.WEIGHTS)
    print(f"  {'':22s}{'target':>10s}" + "".join(f"{m['session_date']:>13s}" for m in aset["members"]))
    print(f"  {'similarity':22s}{'':>10s}" + "".join(f"{float(m['similarity']):12.1f}%" for m in aset["members"]))
    for f in feats:
        t = aset["target_features"].get(f)
        row = f"  {rth.LABELS[f] + ' (' + str(rth.WEIGHTS[f]) + ')':22s}" + (f"{'—':>10s}" if t is None
                                                                           else f"{float(t):10.3f}")
        for m in aset["members"]:
            c = m["components"][f]
            row += (f"{'—':>13s}" if not c["comparable"] else
                    f"{float(c['analogue']):7.3f} {float(c['score']):.2f}")
        print(row)
    return 0


def cmd_rth_calibrate(conn, args):
    """Reproduces the RTH matcher's tolerance calibration (contracts/nq_rth.CALIBRATION) over the sessions to --end:
    the median pairwise difference of each feature at 30 minutes and twice it - beside the registered values. Stores
    nothing; a different calibration needs a new matcher version."""
    from contracts import nq_rth as rth
    from forecaster import rth_analogues as ra
    from matching import rth as mr
    openings, _ = ra.load_openings(conn, args.end)
    result = mr.calibrate([o for d, o in openings.items() if not args.start or d >= args.start])
    print(f"{result['sessions']} session(s) {result['first']}..{result['last']} (registered: "
          f"{rth.CALIBRATION['sessions']} {rth.CALIBRATION['first_session']}..{rth.CALIBRATION['last_session']})")
    for f, med in result["medians"].items():
        print(f"  {f:20s} median {med:.4f}  tolerance {result['tolerances'][f]:>5s}  registered {rth.TOLERANCES[f]}")
    return 0


def cmd_rth_eval_status(conn, args):
    """The RTH evaluations' operational health (forecaster/rth_eval.status), each under what it measures: per
    checkpoint every scheduled opportunity - scored and each reason with its rate - the issue delay, how much of the
    window was still ahead when stored, the lead to the window's start; counted sessions, member counts. Never a
    score."""
    from contracts import rth_eval as ev
    from forecaster import rth_eval

    def q(d, unit="min"):
        return "-" if not d else f"median {d['median']:.1f}, p90 {d['p90']:.1f}, max {d['max']:.1f} {unit}"
    for version in ([args.version] if args.version else rth_eval.VERSIONS):
        st = rth_eval.status(conn, version=version)
        print(f"{version} - {st['label']}")
        if not st["registered"]:
            print("  not registered yet: the first RTH issue (Auto, or rth-issue) registers it.")
            continue
        print(f"  {st['counted_sessions']} of {st['endpoint_sessions']} sessions counted (end date {st['end_date']}); "
              "a count reached says nothing about any one checkpoint's evidence")
        for at, a in st["availability"].items():
            n = a["opportunities"]
            rates = ", ".join(f"{k.replace('_', ' ')} {v} ({a['rates'][k]:.0%})" for k, v in sorted(a["counts"].items()))
            print(f"  {at}: {n} scheduled opportunit{'y' if n == 1 else 'ies'} decided - {rates or 'none'}")
            if a["delay_minutes"]:
                print(f"     stored after the matching cutoff: {q(a['delay_minutes'])}; window still ahead when "
                      f"stored: {q(a['window_ahead_minutes'])}; lead to the window's start: {q(a['lead_minutes'])}")
        for k, m in st["members"].items():
            if m:
                print(f"  {k} members: min {m['min']}, median {m['median']} (needs {ev.MIN_MEMBERS[k]})")
    print("No score is shown before the endpoint.")
    return 0


def cmd_rth_eval_score(conn, args):
    """An RTH evaluation's one scoring (forecaster/rth_eval.score): refused before the endpoint and after it was
    done; --show prints the stored result."""
    import json
    from forecaster import rth_eval
    version = args.version or rth_eval.VERSIONS[0]
    if args.show:
        stored = store.rth_eval_result(conn, version)
        print(json.dumps(stored["results"], indent=2) if stored else f"{version} has not been scored.")
        return 0 if stored else 1
    try:
        results = rth_eval.score(conn, version=version)
    except rth_eval.NotAtEndpoint as e:
        print(f"Not scored: {e}")
        return 1
    print(json.dumps(results, indent=2))
    return 0


def cmd_rth_calibrate(conn, args):
    """Reproduces the RTH matcher's tolerance calibration (contracts/nq_rth.CALIBRATION) over the sessions to --end:
    the median pairwise difference of each feature at 30 minutes and twice it - beside the registered values. Stores
    nothing; a different calibration needs a new matcher version."""
    from contracts import nq_rth as rth
    from forecaster import rth_analogues as ra
    from matching import rth as mr
    openings, _ = ra.load_openings(conn, args.end)
    result = mr.calibrate([o for d, o in openings.items() if not args.start or d >= args.start])
    print(f"{result['sessions']} session(s) {result['first']}..{result['last']} (registered: "
          f"{rth.CALIBRATION['sessions']} {rth.CALIBRATION['first_session']}..{rth.CALIBRATION['last_session']})")
    for f, med in result["medians"].items():
        print(f"  {f:20s} median {med:.4f}  tolerance {result['tolerances'][f]:>5s}  registered {rth.TOLERANCES[f]}")
    return 0


def cmd_rth_eval_status(conn, args):
    """The RTH evaluation's operational health (forecaster/rth_eval.status): cases by reason and cutoff, counted
    sessions against the endpoint, issue delays, member counts - never a score."""
    from contracts import rth_eval as ev
    from forecaster import rth_eval
    st = rth_eval.status(conn)
    if not st["registered"]:
        print(f"{ev.VERSION} is not registered yet: the first RTH issue (Auto, or rth-issue) registers it.")
        return 0
    print(f"{ev.VERSION}: {st['counted_sessions']} of {st['endpoint_sessions']} sessions counted (end date "
          f"{st['end_date']}); {st['cases']} case(s): "
          + ", ".join(f"{k.replace('_', ' ')} {v}" for k, v in sorted(st["by_reason"].items())))
    for m, counts in st["by_cutoff"].items():
        print(f"  {ev.CUTOFFS[m]}: " + (", ".join(f"{k.replace('_', ' ')} {v}" for k, v in sorted(counts.items()))
                                         or "none"))
    d = st["issue_delay_minutes"]
    if d:
        print(f"  issue delay after the cutoff: median {d['median']:.1f} min, p90 {d['p90']:.1f}, max {d['max']:.1f} "
              f"(limit {ev.MAX_ISSUE_DELAY.total_seconds() / 60:.0f}) over {d['n']} forecast(s)")
    for k, q in st["members"].items():
        if q:
            print(f"  {k} members: min {q['min']}, median {q['median']} (needs {ev.MIN_MEMBERS[k]})")
    print("  No score is shown before the endpoint.")
    return 0


def cmd_rth_eval_score(conn, args):
    """The RTH evaluation's one scoring (forecaster/rth_eval.score): refused before the endpoint and after it was
    done; --show prints the stored result."""
    import json
    from contracts import rth_eval as ev
    from forecaster import rth_eval
    if args.show:
        stored = store.rth_eval_result(conn, ev.VERSION)
        print(json.dumps(stored["results"], indent=2) if stored else f"{ev.VERSION} has not been scored.")
        return 0 if stored else 1
    try:
        results = rth_eval.score(conn)
    except rth_eval.NotAtEndpoint as e:
        print(f"Not scored: {e}")
        return 1
    print(json.dumps(results, indent=2))
    return 0


def cmd_timeliness(conn, args):
    """When a pre-open forecast at each candidate cutoff could actually have been issued on this feed, measured from
    the bars' receipt times and the Auto runs' recorded ends (forecaster/timeliness.py) - nothing forecast, scored
    or sent. Writes docs/reports/preopen_timeliness.md."""
    from forecaster import timeliness as tl
    days = [s.session_date.isoformat() for s in _sessions(args)]
    text = tl.report(tl.measure(conn, days))
    path = os.path.join(_PROJECT_ROOT, "docs", "reports", "preopen_timeliness.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    print(text + f"\nWritten to {path}")
    return 0


def cmd_live_report(conn, args):
    """Every live capture of a date range with its steps as seconds after the cutoff (database clock)."""
    from forecaster import live_capture as live
    captures = store.live_captures(conn, args.start, args.end)
    if not captures:
        print("No live captures in the range.")
    for c in captures:
        print(f"{c['session_date']} {c['profile']}: capture {c['capture_id'][:8]} (code {c['code_revision'][:12]})")
        for line in live.timing(conn, c):
            print(f"  {line}")
    return 0


def cmd_review_set(conn, args):
    """Chooses and stores a review set from the current label version's outcomes (forecaster/review_set.py)."""
    version = defs.PROFILES[args.profile].snapshot_version
    candidates, previous_contract = [], None
    for snap in store.list_snapshots(conn, "2000-01-01", "2100-01-01", version):
        outcome = store.latest_outcome(conn, snap["snapshot_id"], defs.LABEL_VERSION)
        rolled = previous_contract is not None and snap["contract_id"] != previous_contract
        previous_contract = snap["contract_id"]
        if outcome is None:
            continue
        schedule = cal.session(snap["session_date"]).schedule
        candidates.append({"snapshot_id": snap["snapshot_id"], "session_date": snap["session_date"],
                           "items": review_set.session_items(outcome["labels"], schedule, rolled,
                                                             snap["session_date"])})
    if not candidates:
        print(f"No {defs.LABEL_VERSION} outcomes on {version} snapshots; run 'outcomes' first.")
        return 1
    chosen, coverage = review_set.select_sessions(candidates, args.size)
    selection = {"rule": review_set.RULE, "size": args.size, "candidates": len(candidates),
                 "first_session": candidates[0]["session_date"], "last_session": candidates[-1]["session_date"],
                 **coverage}
    if not store.create_review_set(conn, args.name, defs.LABEL_VERSION, version, selection, chosen):
        print(f"Review set {args.name} already exists; review sets are immutable - choose another name.")
        return 1
    print(f"{args.name}: {len(chosen)} of {len(candidates)} sessions, covering {coverage['covered']} of "
          f"{coverage['items']} label classes" + (f" (not covered: {', '.join(coverage['uncovered'])})"
                                                  if coverage["uncovered"] else ""))
    for c in chosen:
        print(f"  {c['session_date']}  {', '.join(c['reasons'][:6])}{' ...' if len(c['reasons']) > 6 else ''}")
    return 0


def cmd_review_report(conn, args):
    """Per P2 field: agree / disagree / unsure over the reviewed sessions, then every disagreement."""
    members = store.review_members(conn, args.name)
    if not members:
        print(f"No review set {args.name}.")
        return 1
    verdicts = store.latest_verdicts(conn, args.name)
    reviewed = {v["snapshot_id"] for v in verdicts}
    print(f"{args.name}: {len(reviewed)} of {len(members)} sessions reviewed")
    counts = {}
    for v in verdicts:
        counts.setdefault(v["field"], {"agree": 0, "disagree": 0, "unsure": 0})[v["verdict"]] += 1
    for field, c in sorted(counts.items(), key=lambda kv: (-kv[1]["disagree"], kv[0])):
        print(f"  {field:42} agree {c['agree']:3}  disagree {c['disagree']:3}  unsure {c['unsure']:3}")
    for v in verdicts:
        if v["verdict"] != "agree":
            print(f"  {v['session_date']} {v['field']}: {v['verdict']} (shown {v['shown_value']!r})"
                  + (f" - {v['note']}" if v["note"] else ""))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="NQ prompt-v2 journal records")
    parser.add_argument("--db", default=Config.DATABASE_URL, help="PostgreSQL connection URL")
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p, single=True, ranged=True):
        p.add_argument("--profile", default=defs.DEFAULT_PROFILE, choices=sorted(defs.PROFILES))
        if single:
            p.add_argument("--date", help="Session date YYYY-MM-DD")
        if ranged:
            p.add_argument("--start", help="First session date (inclusive)")
            p.add_argument("--end", help="Last session date (inclusive)")

    sub.add_parser("register", help="Register the definitions only")
    common(sub.add_parser("snapshot", help="Build and store evidence snapshot(s)"))
    common(sub.add_parser("outcomes", help="Label the stored snapshots of final sessions"), single=False)
    common(sub.add_parser("backfill", help="Snapshot + outcome, session by session"))
    common(sub.add_parser("catch-up", help="Snapshot for every session past its cutoff not yet stored, outcome once final"),
           single=False, ranged=False)
    common(sub.add_parser("annotate", help="Rule-based structure annotations of the stored snapshots"),
           single=False)
    p = sub.add_parser("match", help="Analogue sets of every annotated snapshot")
    common(p, single=False, ranged=False)
    p.add_argument("--protocol", choices=sorted(PROTOCOLS), default="rules", help="Annotation protocol to match on")
    p = sub.add_parser("annotate-llm", help="Claude structure annotations (needs ANTHROPIC_API_KEY)")
    common(p)
    p.add_argument("--batch", action="store_true", help="Use the Batch API (half price, results within 24 h)")
    p.add_argument("--estimate", action="store_true", help="Only estimate the tokens and the cost")
    p.add_argument("--close-unresolved", action="store_true",
                   help="Record requests whose answer can never be fetched (a run ended mid-request) as errors")
    p.add_argument("--restricted", action="store_true",
                   help="Arm C's restricted protocol (Overnight Structure and Premarket Pattern only, date-blinded) - "
                        "e.g. to annotate its pool with --batch")
    p = sub.add_parser("llm-forecast", help="Arms C (restricted LLM) and D (synthesis) over the last sessions "
                                            "(Claude requests, confirmed by hand)")
    p.add_argument("--sessions", type=int, default=1, help="The last N scheduled sessions up to today (default 1)")
    p.add_argument("--date", action="append", help="A chosen session (repeatable); with --start/--end instead of "
                                                   "--sessions")
    p.add_argument("--start", help="First chosen session (with --end)")
    p.add_argument("--end", help="Last chosen session (with --start)")
    p.add_argument("--arms", default="CD", help="C, D or CD (default)")
    p.add_argument("--estimate", action="store_true", help="Only show the plan and its rough cost")
    p.add_argument("--approval", default=None, help="A dashboard approval token (forecaster/approvals.py)")
    p.add_argument("--batch", action="store_true",
                   help="Send through the Batch API: half price, answers within 24 h (usually far sooner)")
    p.add_argument("--wait-minutes", type=float, default=30,
                   help="How long to wait for a batch (default 30); one still processing is collected next time")
    p.add_argument("--profile", default=defs.DEFAULT_PROFILE, choices=sorted(defs.PROFILES))
    p = sub.add_parser("analogues", help="One session's analogues (outcome-blind unless --outcomes)")
    common(p, ranged=False)
    p.add_argument("--outcomes", action="store_true", help="Also show the analogues' outcomes")
    p.add_argument("--protocol", choices=sorted(PROTOCOLS), default="rules", help="Annotation protocol of the set")
    p = sub.add_parser("annotation-review-set", help="Choose an outcome-blind review set of pre-open annotations")
    p.add_argument("--name", required=True)
    p.add_argument("--size", type=int, default=25)
    p.add_argument("--profile", default=defs.DEFAULT_PROFILE, choices=sorted(defs.PROFILES))
    p = sub.add_parser("annotation-review-report", help="Summarise an annotation review set's verdicts")
    p.add_argument("--name", required=True)
    p = sub.add_parser("forecast", help="Baseline forecasts (historical replay), stored once per evidence")
    common(p)
    p.add_argument("--protocol", choices=sorted(PROTOCOLS), default="rules", help="Annotation protocol")
    p.add_argument("--snapshot-id", help="Explicit evidence: snapshot id (with --annotation-id, --set-id)")
    p.add_argument("--annotation-id", help="Explicit evidence: annotation id")
    p.add_argument("--set-id", help="Explicit evidence: analogue set id (omit: no analogue set)")
    p = sub.add_parser("experiment-register", help="Register a forecast experiment's manifest (stage 4)")
    p.add_argument("--name", required=True)
    p.add_argument("--start", required=True)
    p.add_argument("--end", required=True)
    p.add_argument("--profile", default=defs.DEFAULT_PROFILE, choices=sorted(defs.PROFILES))
    p.add_argument("--purpose", default="development", choices=("development", "test"))
    p.add_argument("--official-run", default="first", choices=("first", "latest", "first_timely"))
    p.add_argument("--design", choices=("ab", "d-research"), default="ab",
                   help="d-research: p1_d_research_v1 (arms A, B, D; contracts/p1_d_research.py) - registering sends "
                        "no request and schedules nothing")
    p = sub.add_parser("experiment-score", help="Freeze, score and report a registered experiment")
    p.add_argument("--name", required=True)
    p.add_argument("--report-dir", default=os.path.join(_PROJECT_ROOT, "docs", "reports"))
    sub.add_parser("experiment-list", help="Registered experiments and their scorings")
    p = sub.add_parser("live", help="Capture, freeze and issue today's session live (run before the open)")
    p.add_argument("--date", help="Session date (default: today in New York)")
    p.add_argument("--profile", default=defs.DEFAULT_PROFILE, choices=sorted(defs.PROFILES))
    p = sub.add_parser("preview", help="Forecast now: the next session's forecast from the data stored so far "
                                       "(a preview, never stored in the journal)")
    p.add_argument("--out", default=None, help="Preview file (default: the one the dashboard reads)")
    p = sub.add_parser("rth-issue", help="RTH analogue sets of the session in progress (newest window, checkpoints)")
    p.add_argument("--date", help="Session date YYYY-MM-DD (default: today in New York)")
    p.add_argument("--by", choices=["manual", "auto"], default="manual",
                   help="Who issues: a person (default) or Auto mode (the dashboard passes it)")
    p = sub.add_parser("rth-backfill", help="Historical reconstructions of RTH analogue sets")
    p.add_argument("--date", help="Session date YYYY-MM-DD")
    p.add_argument("--start", help="First session date (inclusive)")
    p.add_argument("--end", help="Last session date (inclusive)")
    p.add_argument("--all-minutes", action="store_true", help="Every window of the first hour, not only 15/30/60")
    p = sub.add_parser("rth-show", help="The RTH set a review of a session at a minute sees")
    p.add_argument("--date", help="Session date YYYY-MM-DD")
    p.add_argument("--minute", type=int, default=60, help="Reconstructed view: minutes after the 09:30 ET open")
    p.add_argument("--view", choices=["reconstructed", "issued"], default="reconstructed")
    p.add_argument("--at", help="Issued view: HH:MM ET on the session's day (default: everything issued)")
    p = sub.add_parser("rth-eval-status", help="The RTH evaluations' operational health (never a score)")
    p.add_argument("--version", choices=["rth_continuation_v2", "rth_operational_v1"])
    p = sub.add_parser("rth-eval-score", help="An RTH evaluation's one scoring, at its endpoint only")
    p.add_argument("--version", choices=["rth_continuation_v2", "rth_operational_v1"],
                   help="Default rth_continuation_v2 (research only)")
    p.add_argument("--show", action="store_true", help="Print the stored result")
    p = sub.add_parser("rth-calibrate", help="Reproduce the RTH matcher's tolerance calibration (stores nothing)")
    p.add_argument("--start", help="First session date (default: all)")
    p.add_argument("--end", required=True, help="Last session date")
    p = sub.add_parser("timeliness", help="When pre-open forecasts at candidate cutoffs could have been issued")
    p.add_argument("--date", help="Session date YYYY-MM-DD")
    p.add_argument("--start", help="First session date (inclusive)")
    p.add_argument("--end", help="Last session date (inclusive)")
    p = sub.add_parser("live-report", help="Live captures and their timing")
    p.add_argument("--start", required=True)
    p.add_argument("--end", required=True)
    p = sub.add_parser("show-forecast", help="Print one stored forecast run")
    p.add_argument("--run-id", required=True)
    p = sub.add_parser("show", help="Print one session's snapshot and latest outcome")
    common(p, ranged=False)
    p.add_argument("--protocol", choices=sorted(PROTOCOLS), default="rules",
                   help="Annotation protocol of the pre-open record")
    p = sub.add_parser("review-set", help="Choose and store a review set of diverse sessions")
    p.add_argument("--name", required=True, help="Review set name (immutable once stored)")
    p.add_argument("--size", type=int, default=25, help="Sessions in the set (the guideline asks for 20-30)")
    p.add_argument("--profile", default=defs.DEFAULT_PROFILE, choices=sorted(defs.PROFILES))
    p = sub.add_parser("review-report", help="Summarise a review set's verdicts")
    p.add_argument("--name", required=True, help="Review set name")

    args = parser.parse_args(argv)
    if args.command in ("snapshot", "backfill") and not args.date and not (args.start and args.end):
        parser.error("give --date, or --start and --end")
    if args.command in ("outcomes", "annotate") and not (args.start and args.end):
        parser.error("give --start and --end")
    if args.command == "annotate-llm" and not args.date and not (args.start and args.end):
        parser.error("give --date, or --start and --end")
    if args.command in ("rth-backfill", "timeliness") and not args.date and not (args.start and args.end):
        parser.error("give --date, or --start and --end")
    if args.command in ("show", "analogues", "rth-show") and not args.date:
        parser.error("give --date")
    if args.command == "forecast" and not (args.date or (args.start and args.end) or
                                           (args.snapshot_id and args.annotation_id)):
        parser.error("give --date, --start and --end, or --snapshot-id and --annotation-id")

    init_database(args.db)
    conn = get_db_connection(args.db)
    try:
        register(conn)
        handler = {"register": lambda c, a: 0, "snapshot": cmd_snapshot, "outcomes": cmd_outcomes,
                   "backfill": cmd_backfill, "catch-up": cmd_catch_up, "annotate": cmd_annotate,
                   "match": cmd_match, "annotate-llm": cmd_annotate_llm, "analogues": cmd_analogues,
                   "annotation-review-set": cmd_annotation_review_set,
                   "annotation-review-report": cmd_annotation_review_report, "show": cmd_show, "review-set": cmd_review_set,
                   "forecast": cmd_forecast, "show-forecast": cmd_show_forecast,
                   "experiment-register": cmd_experiment_register, "experiment-score": cmd_experiment_score,
                   "experiment-list": cmd_experiment_list, "live": cmd_live, "live-report": cmd_live_report,
                   "preview": cmd_preview, "llm-forecast": cmd_llm_forecast, "rth-issue": cmd_rth_issue,
                   "rth-backfill": cmd_rth_backfill, "rth-show": cmd_rth_show, "rth-calibrate": cmd_rth_calibrate, "timeliness": cmd_timeliness,
                   "rth-eval-status": cmd_rth_eval_status, "rth-eval-score": cmd_rth_eval_score,
                   "review-report": cmd_review_report}[args.command]
        return handler(conn, args)
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
