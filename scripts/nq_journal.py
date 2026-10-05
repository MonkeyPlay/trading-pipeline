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
    python scripts/nq_journal.py live-report --start 2026-10-05 --end 2026-10-09   # capture timing
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
    version = defs.PROFILES[args.profile].snapshot_version
    unresolved = store.unresolved_inference_requests(conn, preopen.LLM_PROTOCOL_VERSION)
    batches = sorted({r["batch_id"] for r in unresolved if r["batch_id"]})
    stranded = [r for r in unresolved if not r["batch_id"]]
    if stranded and args.close_unresolved:
        print(f"Closed {llm.close_unresolved(conn, stranded)} unresolved request(s) as error attempts.")
        stranded, unresolved = [], [r for r in unresolved if r["batch_id"]]
    for r in stranded:
        print(f"  unresolved {r['mode']} request {r['request_id']} ({r['session_date']}, sent {r['created_at']}): its "
              f"answer cannot be fetched; --close-unresolved records it as an error so the session can be sent again")
    waiting = {r["snapshot_id"] for r in unresolved}
    snaps = store.list_snapshots(conn, args.date or args.start, args.date or args.end, version)
    todo = [s for s in snaps if store.latest_annotation(conn, s["snapshot_id"], preopen.LLM_PROTOCOL_VERSION) is None
            and s["snapshot_id"] not in waiting]
    print(f"{len(todo)} of {len(snaps)} {version} snapshot(s) to send for {preopen.LLM_PROTOCOL_VERSION}"
          + (f"; {len(waiting)} with an unresolved request" if waiting else "")
          + (f"; {len(batches)} recorded batch(es) to collect first" if batches else "") + ".")
    if todo:
        e = llm.estimate(todo)
        print(f"  about {e['input_tokens']:,} input and {e['output_tokens']:,} output tokens: ~${e['usd_live']} "
              f"live, ~${e['usd_batch']} with --batch ({preopen.LLM_MODEL}, effort {preopen.LLM_EFFORT})")
    if args.estimate or not (todo or batches):
        return 0
    what = ([f"{len(todo)} {'batch' if args.batch else 'live'} request(s)"] if todo else []) + \
           ([f"collect {len(batches)} recorded batch(es)"] if batches else [])
    if not confirmed_by_hand(f"Claude API ({preopen.LLM_MODEL}, effort {preopen.LLM_EFFORT}): {' and '.join(what)}."):
        return 1
    with llm.manual_requests():
        return _send_llm(conn, args, llm, todo, batches)


def confirmed_by_hand(summary: str) -> bool:
    """
    Claude API requests are started by hand only, for now: a person at a terminal reads ``summary`` and types
    "send". Without a terminal (cron, the dashboard's jobs, a pipe) nothing is sent.
    """
    if not sys.stdin.isatty():
        print("Claude API requests are started by hand only, for now: run annotate-llm in a terminal and confirm "
              "there. Nothing was sent.")
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


def _send_llm(conn, args, llm, todo, batches):
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
        counts = llm.collect_batch(conn, client, batch_id)
        print(f"  results: {counts}")
        status |= not set(counts) <= {"ok"}
    if not todo:
        return status
    if not args.batch:
        for snap in todo:
            attempt = llm.annotate_live(conn, client, snap)
            print(f"  {snap['session_date']}: {attempt['status']}" + (f" - {attempt['error']}" if attempt.get("error") else ""))
            status |= attempt["status"] not in ("ok", "contaminated")
        return status
    submitted = datetime.now(timezone.utc)
    batch_id = llm.submit_batch(conn, client, todo)
    if batch_id is None:
        print("  no batch was created: every snapshot contaminated, or the API refused it (see the error attempts)")
        return 1
    print(f"  batch {batch_id} submitted and recorded ({len(todo)} requests); waiting for it to end - an "
          f"interrupted run collects it next time ...")
    while client.messages.batches.retrieve(batch_id).processing_status != "ended":
        time.sleep(llm.batch_poll_delay(submitted))
    counts = llm.collect_batch(conn, client, batch_id)
    print(f"  results: {counts}")
    return status | (0 if set(counts) <= {"ok"} else 1)


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
    p = sub.add_parser("experiment-score", help="Freeze, score and report a registered experiment")
    p.add_argument("--name", required=True)
    p.add_argument("--report-dir", default=os.path.join(_PROJECT_ROOT, "docs", "reports"))
    sub.add_parser("experiment-list", help="Registered experiments and their scorings")
    p = sub.add_parser("live", help="Capture, freeze and issue today's session live (run before the open)")
    p.add_argument("--date", help="Session date (default: today in New York)")
    p.add_argument("--profile", default=defs.DEFAULT_PROFILE, choices=sorted(defs.PROFILES))
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
    if args.command in ("show", "analogues") and not args.date:
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
                   "review-report": cmd_review_report}[args.command]
        return handler(conn, args)
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
