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
    python scripts/nq_journal.py live --profile candidate_0915 --wait-minutes 13   # the 09:15 candidate
    python scripts/nq_journal.py preview                                    # forecast now: a preview, never stored
    python scripts/nq_journal.py live-report --start 2026-10-05 --end 2026-10-09   # capture timing
    python scripts/nq_journal.py instrument-inventory                       # what each instrument's data holds
    python scripts/nq_journal.py ml-dev-eval                                # A, B and the ML models (development)
    python scripts/nq_journal.py ml-dev-eval --legacy-row-split             # ... the v1 row-position tuning, reproduced
    python scripts/nq_journal.py ml-train                                   # the model artifacts, offline
    python scripts/nq_journal.py summary --date 2026-10-12                  # a session's forecast summary
    python scripts/nq_journal.py experiment-register --name p1_ml_forward_v2 --design ml-forward --start ... --end ...
    python scripts/nq_journal.py rth-issue                                  # RTH analogues of the session in progress
    python scripts/nq_journal.py rth-backfill --start 2025-09-02 --end 2026-10-08  # reconstructions at 15/30/60 min
    python scripts/nq_journal.py rth-backfill --date 2026-10-07 --all-minutes      # every window of the first hour
    python scripts/nq_journal.py rth-show --date 2026-10-07 --minute 15     # reconstructed view at 09:45
    python scripts/nq_journal.py rth-show --date 2026-10-12 --view issued --at 10:05   # as issued by 10:05 ET
    python scripts/nq_journal.py rth-calibrate --end 2026-10-07              # reproduce the tolerances' calibration
    python scripts/nq_journal.py timeliness --start 2026-10-05 --end 2026-10-16   # estimated issuance times per cutoff
    python scripts/nq_journal.py rth-eval-status                            # both evaluations' health - never a score
    python scripts/nq_journal.py rth-eval-score --version rth_operational_v1   # one scoring, at the endpoint only
    python scripts/nq_journal.py review-set --name stage1_review_v1          # choose the 25-session review set
    python scripts/nq_journal.py review-report --name stage1_review_v1       # the reviewer's verdicts, per field

``--profile`` picks the cutoff profile (research_0929, the default, or
operational_0927); each has its own snapshot version. Snapshots are historical
reconstructions; catch-up takes a session in progress once its bars past the cutoff
are stored (forecaster/journal.py snapshot_pending). Outcomes are recorded only once
a session is final (two hours after its scheduled close) and become a new revision
only when they change.

Every command registers the label, convention and snapshot definitions first; a
changed definition under an existing version name stops the run. The IB collector
runs ``catch-up`` itself after every full collection (forecaster/journal.py).
"""

import argparse
import logging
import os
import sys
from datetime import datetime, timezone

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
PROTOCOLS = {"rules": preopen.RULES_PROTOCOL_VERSION}
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
        if args.design == "ml-forward":
            from contracts import nq_ml
            if args.name != nq_ml.FORWARD["name"]:
                raise ValueError(f"the ml-forward design is named {nq_ml.FORWARD['name']}")
            manifest = nq_ml.forward_manifest(conn, args.start, args.end)
        else:
            manifest = ex.experiment_manifest(args.name, args.start, args.end, args.profile, args.purpose,
                                              args.official_run)
        new = ex.register_experiment(conn, manifest)
    except (ValueError, store.VersionConflict) as e:
        print(f"Not registered: {e}")
        return 1
    print(f"{args.name}: {'registered' if new else 'already registered, identical'} - sessions "
          f"{args.start} to {args.end}, {manifest['profile']}, {manifest['purpose']}, official run "
          f"'{manifest['official_run']['rule']}', arms " + ", ".join(f"{k} {v['algorithm']}" for k, v in
                                                                  manifest["arms"].items())
          + f"; primary {manifest['primary']['target']}, {manifest['primary']['metric'].split(',')[0]}.")
    if manifest.get("versions"):
        v = manifest["versions"]
        print(f"  code {v['code_revision']}, features {v['feature_version']}; artifacts "
              + ", ".join(f"{a} {m['sha256'][:12]}" for a, m in v["models"].items()))
        print(f"  endpoint: {manifest['endpoint']['rule']}")
    print("No score has been computed.")
    return 0


def cmd_experiment_score(conn, args):
    """Freezes an experiment's cases (once), scores them, stores the result and writes the report."""
    from forecaster import experiments as ex
    try:
        manifest = ex.load_manifest(conn, args.name)
    except ValueError as e:
        print(e)
        return 1
    from contracts import nq_ml
    if args.name in nq_ml.SUPERSEDED:
        print(f"Not scored: {args.name} is superseded - {nq_ml.SUPERSEDED[args.name]}.")
        return 1
    ok, why = ex.at_endpoint(conn, manifest, datetime.now(cal.NY_TZ).date().isoformat())
    if not ok:
        print(f"Not scored: {args.name} is scored once, at its endpoint - {why}.")
        return 1
    results = ex.score_experiment(conn, args.name)
    result_id = ex.store_results(conn, args.name, results)
    path = ex.write_report(manifest, results, args.report_dir, result_id)
    for key, p in results["primary"]["paired"].items():
        ll = p["log_loss"]
        print(f"{args.name} ({manifest['purpose']}): {results['primary']['target']} {key} on {p['common']} common "
              f"sessions - log loss diff {ex._f(ll['diff'])} {ex._ci(ll['interval'])} over {ll['both_finite']} "
              f"finite pairs, Brier diff {ex._f(p['brier']['diff'])} {ex._ci(p['brier']['interval'])}")
    if results.get("promotion"):
        pr = results["promotion"]
        print(f"promotion: " + "; ".join(pr["steps"]) + (f" - selected {pr['selected']}" if pr["selected"] else ""))
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
    """The live capture of one session (forecaster/live_capture.py): bars at the cutoff, live snapshot, the
    forecasts."""
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
    wait_s = fc.LIVE_DEFAULT_WAIT_S if args.wait_minutes is None else args.wait_minutes * 60
    try:
        live.check_wait(day, args.profile, wait_s)
    except live.LiveCaptureError as e:
        print(f"Not started: {e}.")
        return 1
    try:
        app = live.connect_ib(Config.IB_HOST, Config.IB_PORT, Config.IB_CLIENT_ID + 1)
    except live.LiveCaptureError as e:
        print(e)
        return 1
    try:
        result = live.capture(conn, app, day, args.profile, wait_s=wait_s)
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
    if result.get("delivered"):
        d = result["delivered"]
        print(f"  in force at the deadline: {'arm ' + d['arm'] if d['arm'] else 'nothing'} - {d['reason']}")
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
    """The RTH analogue sets of the session in progress (forecaster/rth_analogues.py) - what Auto runs after each
    collection: the first-hour matcher's (v2: its newest completed window and any checkpoint not stored yet, until
    11:00 ET) and the full-session matcher's (v3: its newest window and the evaluation's cutoffs, until 30 minutes
    after the RTH close). --date issues both for that session."""
    from contracts import nq_rth as rth
    from forecaster import rth_analogues as ra
    ra.register(conn)
    now = datetime.now(timezone.utc)
    runs = ([ra.issue(conn, day=args.date, issued_by=args.by)] if args.date or ra.first_hour_due(now) else []) + \
           ([ra.issue_session(conn, day=args.date, issued_by=args.by)] if args.date or ra.session_due(now) else [])
    if not runs:
        print("No RTH matcher is due now: the first hour's runs from the open to 11:00 ET, the full session's to 30 "
              "minutes after the close.")
        return 0
    for result in runs:
        version = result.get("version", rth.RTH_MATCHER_VERSION)
        if result["status"] != "issued":
            print(f"RTH analogues ({version}) of {result['session_date']}: {result['status']} - {result['reason']}")
            continue                               # waiting, closed or busy (another issue is storing): not a failure
        for m, set_id, new in result["stored"]:
            aset = store.get_rth_set(conn, set_id)
            print(("new: " if new else "already stored: ") + ra.describe(aset))
            print("  " + (", ".join(f"#{x['rank']} {x['session_date']} {float(x['similarity']):.1f}%"
                                    for x in aset["members"]) or "no analogue"))
        for m, version_, forecast_id, new, *h in result["forecasts"]:
            print(f"  {version_} forecasts of the {m}-minute window" + (f", {h[0]} min ahead" if h else "")
                  + f": {'stored' if new else 'already stored'} ({forecast_id[:8]})")
        for miss in result.get("misses") or []:
            span = (f"{miss['first_minutes']}" if miss["first_minutes"] == miss["last_minutes"]
                    else f"{miss['first_minutes']}-{miss['last_minutes']}")
            print(f"  not stored: window(s) {span} - {miss['reason'].replace('_', ' ')}: {miss['detail']}")
        if result.get("closed"):
            print(f"  RTH closed: the session's last window ({result['session_minutes']} minutes) is stored")
        elif result["stop"]["state"] != "complete":
            print(f"  the window stops at {result['minutes']} minute(s): {ra.stop_text(result['stop'])}")
    return 0


def cmd_rth_backfill(conn, args):
    """Historical reconstructions of RTH analogue sets, stored once and labelled as reconstructions: the first-hour
    matcher's (v2, default) checkpoints - 15, 30, 60 minutes - or with --all-minutes every window of the first hour;
    the full-session matcher's (--version nq_match_rth_v3) evaluation cutoffs and last window, or with --all-minutes
    every window to the close."""
    from contracts import nq_rth as rth
    from forecaster import rth_analogues as ra
    ra.register(conn)
    days = [s.session_date.isoformat() for s in _sessions(args)]
    if args.version == rth.SESSION_VERSION:
        result = ra.reconstruct_session(conn, days, range(1, rth.SESSION_MAX_MINUTES + 1) if args.all_minutes
                                        else None)
    else:
        result = ra.reconstruct(conn, days, range(1, rth.MAX_MINUTES + 1) if args.all_minutes else rth.CHECKPOINTS)
    print(f"RTH analogues ({args.version}): {result['new']} new set(s), {result['already']} already stored, over "
          f"{len(days)} session(s)")
    for day, why in sorted(result["skipped"].items()):
        print(f"  {day}: {why}")
    return 0


def cmd_rth_show(conn, args):
    """One RTH set of a session, in one of two views: reconstructed (default) - the newest calculation of the
    longest stored window not past --minute (default: the whole window), live or not; issued - what had been issued
    live by --at (an ET time of the session's day; default: the end of the day), never a later correction or a
    backfill. --version: the full-session matcher by default when the session has its sets, else the first hour's."""
    from datetime import time as dtime
    from contracts import nq_rth as rth
    from forecaster import rth_analogues as ra
    version = args.version or (rth.SESSION_VERSION if store.rth_windows(conn, defs.SYMBOL, args.date,
                                                                        rth.SESSION_VERSION)
                               else rth.RTH_MATCHER_VERSION)
    minute = args.minute or (rth.SESSION_MAX_MINUTES if version == rth.SESSION_VERSION else rth.MAX_MINUTES)
    windows = store.rth_windows(conn, defs.SYMBOL, args.date, version)
    if not windows:
        print(f"No {version} RTH analogue set of {args.date} is stored.")
        return 1
    print(f"{version} - stored windows: " + ", ".join(
        f"{w['elapsed_minutes']}" + ("*" if w["checkpoint"] else "") + (f" ({w['sets']} sets)" if w["sets"] > 1 else "")
        + (" live" if w["live"] else "") for w in windows) + "   (* checkpoint)")
    if args.view == "issued":
        at = (cal.ny_instant(cal.session(args.date).session_date, dtime.fromisoformat(args.at)) if args.at
              else None)
        aset = store.rth_set_issued(conn, defs.SYMBOL, args.date, version, at=at)
        if aset is None:
            print(f"As issued: nothing was issued live for {args.date}" + (f" by {args.at} ET." if args.at else "."))
            return 1
        print("As issued" + (f" by {args.at} ET" if args.at else "") + ":")
    else:
        aset = store.rth_set_at(conn, defs.SYMBOL, args.date, version, minute)
        if aset is None:
            print(f"No window of {minute} minutes or less is stored.")
            return 1
        print("Reconstructed (the newest calculation, live or not):")
    print(ra.describe(aset))
    print(f"  pool {aset['pool_size']} earlier session(s); excluded: "
          + (", ".join(f"{k.replace('_', ' ')} {v}" for k, v in aset["excluded"].items()) or "none")
          + "; similarity is resemblance of the observed sessions, not a probability")
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
    misses = store.rth_misses(conn, defs.SYMBOL, args.date, version) if version == rth.SESSION_VERSION else []
    for miss in misses:
        print(f"  not stored live: {miss['first_minutes']}-{miss['last_minutes']} ({miss['reason']}): {miss['detail']}")
    return 0


def cmd_rth_calibrate(conn, args):
    """Reproduces the RTH matcher's tolerance calibration (contracts/nq_rth.CALIBRATION) over the sessions to --end:
    the median pairwise difference of each feature at 30 minutes and twice it - beside the registered values. With
    --minutes (the full-session matcher's sessions) the same spread at other windows beside what the sqrt(n / 30)
    rule implies - how far the descriptive extension past 60 minutes is from the data. Stores nothing; a different
    calibration needs a new matcher version."""
    import math
    from contracts import nq_rth as rth
    from forecaster import rth_analogues as ra
    from matching import rth as mr
    windows = [int(x) for x in args.minutes.split(",")] if args.minutes else [rth.SCALE_MINUTES]
    openings, _ = (ra.load_session_openings(conn, args.end, cache=False) if args.minutes else
                   ra.load_openings(conn, args.end))
    pick = [o for d, o in openings.items() if not args.start or d >= args.start]
    for n in windows:
        result = mr.calibrate(pick, n)
        print(f"{n} minutes: {result['sessions']} session(s) {result['first']}..{result['last']} (registered at "
              f"{rth.SCALE_MINUTES}: {rth.CALIBRATION['sessions']} {rth.CALIBRATION['first_session']}.."
              f"{rth.CALIBRATION['last_session']})")
        for f, med in result["medians"].items():
            implied = float(rth.TOLERANCES[f]) * (math.sqrt(n / rth.SCALE_MINUTES) if f in rth.SCALED else 1)
            print(f"  {f:20s} median {med:.4f}  2 x median {2 * med:.3f}  in use at {n} min {implied:.3f}  "
                  f"(ratio {2 * med / implied:.2f})")
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
    from contracts import rth_session as rs
    for version in ([args.version] if args.version else (*rth_eval.VERSIONS, rs.VERSION)):
        if version == rs.VERSION:
            st = rth_eval.session_status(conn)
            print(f"{version} - {st['label']}")
            if not st["registered"]:
                print("  not registered yet: the first full-session RTH issue registers it.")
                continue
            print(f"  {st['counted_sessions']} of {st['endpoint_sessions']} sessions counted at the 15-minute horizon "
                  f"(end date {st['end_date']})")
            for at, a in st["availability"].items():
                rates = ", ".join(f"{k.replace('_', ' ')} {v} ({a['rates'][k]:.0%})" for k, v in sorted(a["counts"].items()))
                print(f"  {at}: {a['opportunities']} decided - {rates or 'none'}"
                      + (f"; lead to the window's start {q(a['lead_minutes'])}" if a["lead_minutes"] else ""))
            continue
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
    from contracts import rth_session as rs
    version = args.version or rth_eval.VERSIONS[0]
    if args.show:
        stored = store.rth_eval_result(conn, version)
        print(json.dumps(stored["results"], indent=2) if stored else f"{version} has not been scored.")
        return 0 if stored else 1
    try:
        results = rth_eval.session_score(conn) if version == rs.VERSION else rth_eval.score(conn, version=version)
    except rth_eval.NotAtEndpoint as e:
        print(f"Not scored: {e}")
        return 1
    print(json.dumps(results, indent=2))
    return 0


def cmd_instrument_inventory(conn, args):
    """The instrument inventory (forecaster/instrument_inventory.py) with each instrument's inclusion decision
    (contracts/nq_ml.py); writes docs/reports/instrument_inventory.md."""
    from contracts import nq_ml as ml
    from forecaster import instrument_inventory as inv
    rows = [inv.inventory(conn, s, args.since) for s in inv.symbols(conn)]
    decisions = {"NQ": {"decision": "target", "reason": "the instrument forecast (direction_15m)"}}
    decisions.update({s: {"decision": "included" + (" (required)" if i.required else " (optional)"),
                          "reason": f"{i.role}. {i.reason[0].upper()}{i.reason[1:]}."}
                      for s, i in ml.INSTRUMENTS.items()})
    decisions.update({s: {"decision": "excluded", "reason": f"{r[0].upper()}{r[1:]}."} for s, r in ml.EXCLUDED.items()})
    text = inv.report(rows, decisions)
    path = args.out or os.path.join(_PROJECT_ROOT, "docs", "reports", "instrument_inventory.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    print(text + f"\nWritten to {path}")
    return 0


def cmd_ml_train(conn, args):
    """Trains the NQ direction models offline (forecaster/ml_train.py) into data/models/nq_ml/ - a version with an
    artifact already is never overwritten."""
    from forecaster import ml_model, ml_train
    try:
        manifests = ml_train.train(conn, until=args.until)
    except ml_model.ArtifactError as e:
        print(f"Not trained: {e}")
        return 1
    for v, m in manifests.items():
        t = m["training"]
        print(f"{v}: {m['family']} {m['params']}, trained on {t['sessions']} sessions {t['from']} to {t['to']} "
              f"(rows {t['rows']}), sha256 {m['sha256'][:16]} -> {m['path']}")
    return 0


def cmd_ml_dev_eval(conn, args):
    """The development comparison of A, B and the ML models (forecaster/ml_eval.py) under the session-date tuning
    split; writes docs/reports/ml_development_dates.md - development data, not a test. --legacy-row-split reproduces
    the v1 row-position tuning and writes docs/reports/ml_development.md (the 2026-10-09 report's name)."""
    import json
    from contracts import nq_ml as ml
    from forecaster import ml_eval, ml_model
    split = "legacy_rows" if args.legacy_row_split else "dates"
    res = ml_eval.run(conn, split=split)
    chosen = {cfg: ml.FAMILY[v] for v, cfg in ml.CONFIG_OF.items()}
    have = [v for v in ml.ALGORITHMS if ml_model.manifest(v) is not None]
    lat = None
    if have:                                     # the inference latency of the stored artifacts, on recent sessions
        from forecaster.ml_train import pool
        lat = ml_eval.latency(conn, have, [str(s["session_date"]) for s in pool(conn)][-(1 if args.quick else 5):])
    text = ml_eval.report(res, chosen, lat)
    os.makedirs(args.report_dir, exist_ok=True)
    name = "ml_development" if split == "legacy_rows" else "ml_development_dates"
    path = os.path.join(args.report_dir, f"{name}.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    with open(os.path.join(args.report_dir, f"{name}.json"), "w", encoding="utf-8") as f:
        json.dump({k: v for k, v in res.items() if k != "folds"} | {"folds": res["folds"], "latency": lat}, f,
                  indent=1, default=str)
    print(text + f"\nWritten to {path}")
    return 0


def cmd_summary(conn, args):
    """The session's forecast summary (forecaster/forecast_summary.py): validated numbers only."""
    from forecaster import forecast_summary as fsum
    for line in fsum.lines(fsum.build(conn, args.date, args.profile, args.mode)):
        print(line)
    return 0


def cmd_timeliness(conn, args):
    """Estimated issuance times: when a pre-open forecast at each candidate cutoff could have been issued on this
    feed, reconstructed from the bars' receipt times and the Auto runs' recorded ends (forecaster/timeliness.py) -
    nothing forecast, scored or sent. Writes docs/reports/preopen_timeliness.md."""
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
    p.add_argument("--design", choices=("ab", "ml-forward"), default="ab",
                   help="ml-forward: p1_ml_forward_v2 (arms A, B and the ML models; contracts/nq_ml.FORWARD)")
    p = sub.add_parser("instrument-inventory", help="What the database holds per instrument, and the ML's choice")
    p.add_argument("--since", default="2025-09-01", help="Sessions from (hours and cutoff checks)")
    p.add_argument("--out", default=None, help="Report path (default docs/reports/instrument_inventory.md)")
    p = sub.add_parser("ml-train", help="Train the NQ direction models offline into data/models/nq_ml/")
    p.add_argument("--until", default=None, help="Last training session (default: every labelled one)")
    p = sub.add_parser("ml-dev-eval", help="Development comparison of A, B and the ML models (not a test)")
    p.add_argument("--report-dir", default=os.path.join(_PROJECT_ROOT, "docs", "reports"))
    p.add_argument("--quick", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--legacy-row-split", action="store_true",
                   help="Reproduce the v1 row-position tuning (writes ml_development.md); default: the date split")
    p = sub.add_parser("summary", help="A session's forecast summary from validated numbers")
    p.add_argument("--date", required=True)
    p.add_argument("--profile", default=defs.DEFAULT_PROFILE, choices=sorted(defs.PROFILES))
    p.add_argument("--mode", default="historical_replay", choices=("historical_replay", "live"))
    p = sub.add_parser("experiment-score", help="Freeze, score and report a registered experiment")
    p.add_argument("--name", required=True)
    p.add_argument("--report-dir", default=os.path.join(_PROJECT_ROOT, "docs", "reports"))
    sub.add_parser("experiment-list", help="Registered experiments and their scorings")
    p = sub.add_parser("live", help="Capture, freeze and issue today's session live (run before the open)")
    p.add_argument("--date", help="Session date (default: today in New York)")
    p.add_argument("--profile", default=defs.DEFAULT_PROFILE, choices=sorted(defs.PROFILES))
    p.add_argument("--wait-minutes", type=float, default=None,
                   help=f"How long after the cutoff to wait for its bar (default {fc.LIVE_DEFAULT_WAIT_S} s); with "
                        f"the time kept for issuing it must end by the {fc.LIVE_DEADLINE_ET:%H:%M:%S} ET deadline")
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
    p.add_argument("--all-minutes", action="store_true",
                   help="Every window (of the first hour; of the session with --version nq_match_rth_v3)")
    p.add_argument("--version", choices=["nq_match_rth_v2", "nq_match_rth_v3"], default="nq_match_rth_v2",
                   help="The first-hour matcher (default) or the full-session one")
    p = sub.add_parser("rth-show", help="The RTH set a review of a session at a minute sees")
    p.add_argument("--date", help="Session date YYYY-MM-DD")
    p.add_argument("--minute", type=int, default=None,
                   help="Reconstructed view: minutes after the 09:30 ET open (default: the whole window)")
    p.add_argument("--version", choices=["nq_match_rth_v2", "nq_match_rth_v3"], default=None,
                   help="Default: the full-session matcher when the session has its sets, else the first hour's")
    p.add_argument("--view", choices=["reconstructed", "issued"], default="reconstructed")
    p.add_argument("--at", help="Issued view: HH:MM ET on the session's day (default: everything issued)")
    p = sub.add_parser("rth-eval-status", help="The RTH evaluations' operational health (never a score)")
    p.add_argument("--version", choices=["rth_continuation_v2", "rth_operational_v1", "rth_session_v1"])
    p = sub.add_parser("rth-eval-score", help="An RTH evaluation's one scoring, at its endpoint only")
    p.add_argument("--version", choices=["rth_continuation_v2", "rth_operational_v1", "rth_session_v1"],
                   help="Default rth_continuation_v2 (research only)")
    p.add_argument("--show", action="store_true", help="Print the stored result")
    p = sub.add_parser("rth-calibrate", help="Reproduce the RTH matcher's tolerance calibration (stores nothing)")
    p.add_argument("--start", help="First session date (default: all)")
    p.add_argument("--end", required=True, help="Last session date")
    p.add_argument("--minutes", default=None,
                   help="Windows to measure the spread at, e.g. 30,60,120,240,390 (the full-session sessions)")
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
                   "match": cmd_match, "analogues": cmd_analogues,
                   "annotation-review-set": cmd_annotation_review_set,
                   "annotation-review-report": cmd_annotation_review_report, "show": cmd_show, "review-set": cmd_review_set,
                   "forecast": cmd_forecast, "show-forecast": cmd_show_forecast,
                   "experiment-register": cmd_experiment_register, "experiment-score": cmd_experiment_score,
                   "experiment-list": cmd_experiment_list, "live": cmd_live, "live-report": cmd_live_report,
                   "preview": cmd_preview, "rth-issue": cmd_rth_issue, "instrument-inventory": cmd_instrument_inventory,
                   "ml-train": cmd_ml_train, "ml-dev-eval": cmd_ml_dev_eval, "summary": cmd_summary,
                   "rth-backfill": cmd_rth_backfill, "rth-show": cmd_rth_show, "rth-calibrate": cmd_rth_calibrate, "timeliness": cmd_timeliness,
                   "rth-eval-status": cmd_rth_eval_status, "rth-eval-score": cmd_rth_eval_score,
                   "review-report": cmd_review_report}[args.command]
        return handler(conn, args)
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
