#!/usr/bin/env python3
"""
NQ prompt-v2 journal records (docs/nq_prompt_v2.md): pre-open evidence snapshots
and their realised NQ-v2 outcome labels.

    python scripts/nq_journal.py register                                   # definitions only
    python scripts/nq_journal.py snapshot --date 2026-09-24                 # one session
    python scripts/nq_journal.py snapshot --start 2026-06-01 --end 2026-09-25
    python scripts/nq_journal.py outcomes --start 2026-06-01 --end 2026-09-25
    python scripts/nq_journal.py backfill --start 2026-06-01 --end 2026-09-25  # snapshot + outcome, in order
    python scripts/nq_journal.py catch-up                                   # every final session not yet stored
    python scripts/nq_journal.py annotate --start 2025-09-01 --end 2026-10-02  # rule-based structure annotations
    python scripts/nq_journal.py match                                      # analogue sets (P1 section 7 rubric)
    python scripts/nq_journal.py annotate-llm --start 2025-09-01 --end 2026-10-02 --estimate   # Claude: size and cost
    python scripts/nq_journal.py annotate-llm --start 2025-09-01 --end 2026-10-02 --batch      # Claude backfill
    python scripts/nq_journal.py match --protocol llm                       # analogues over Claude's annotations
    python scripts/nq_journal.py analogues --date 2026-10-02 [--outcomes]   # one session's analogues
    python scripts/nq_journal.py annotation-review-set --name preopen_review_v1   # outcome-blind review set
    python scripts/nq_journal.py annotation-review-report --name preopen_review_v1
    python scripts/nq_journal.py show --date 2026-09-24                     # snapshot + P2's 40-field record
    python scripts/nq_journal.py review-set --name stage1_review_v1          # choose the 25-session review set
    python scripts/nq_journal.py review-report --name stage1_review_v1       # the reviewer's verdicts, per field

``--profile`` picks the cutoff profile (research_0929, the default, or
operational_0927); each has its own snapshot version. Snapshots are historical
reconstructions. Outcomes are recorded only once a session is final (two hours
after its scheduled close) and become a new revision only when they change.

Every command registers the label, convention and snapshot definitions first; a
changed definition under an existing version name stops the run. The IB collector
runs ``catch-up`` itself after every full collection (forecaster/journal.py).
"""

import argparse
import logging
import os
import sys

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
from contracts import nq_preopen as preopen
from forecaster.journal import annotate, catch_up, match, record_outcome, register, take_snapshot
from forecaster.outcome_display import p2_record

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
    """The Claude structure annotation (forecaster/structure_llm.py): estimate, live requests or a batch."""
    import time
    from datetime import datetime, timezone
    from forecaster import structure_llm as llm
    version = defs.PROFILES[args.profile].snapshot_version
    snaps = store.list_snapshots(conn, args.date or args.start, args.date or args.end, version)
    todo = [s for s in snaps if store.latest_annotation(conn, s["snapshot_id"], preopen.LLM_PROTOCOL_VERSION) is None]
    print(f"{len(todo)} of {len(snaps)} {version} snapshot(s) without a {preopen.LLM_PROTOCOL_VERSION} annotation.")
    if args.estimate or not todo:
        if todo:
            e = llm.estimate(todo)
            print(f"  about {e['input_tokens']:,} input and {e['output_tokens']:,} output tokens: ~${e['usd_live']} "
                  f"live, ~${e['usd_batch']} with --batch ({preopen.LLM_MODEL}, effort {preopen.LLM_EFFORT})")
        return 0
    try:
        import anthropic
        client = anthropic.Anthropic()
        client.models.retrieve(preopen.LLM_MODEL)
    except Exception as e:
        print(f"Claude API unavailable ({type(e).__name__}: {e}). Set ANTHROPIC_API_KEY in .env.")
        return 1
    if not args.batch:
        status = 0
        for snap in todo:
            attempt = llm.annotate_live(conn, client, snap)
            print(f"  {snap['session_date']}: {attempt['status']}" + (f" - {attempt['error']}" if attempt.get("error") else ""))
            status |= attempt["status"] not in ("ok", "contaminated")
        return status
    submitted = datetime.now(timezone.utc)
    batch_id = llm.submit_batch(client, todo)
    print(f"  batch {batch_id} submitted ({len(todo)} requests); waiting for it to end ...")
    while client.messages.batches.retrieve(batch_id).processing_status != "ended":
        time.sleep(llm.batch_poll_delay(submitted))
    counts = llm.collect_batch(conn, client, batch_id, {s["snapshot_id"]: s for s in todo}, submitted)
    print(f"  results: {counts}")
    return 0 if set(counts) <= {"ok"} else 1


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
    aset = store.latest_analogue_set(conn, snaps[0]["snapshot_id"], preopen.MATCHER_VERSION, defs.LABEL_VERSION)
    if aset is None:
        print(f"No {preopen.MATCHER_VERSION} analogue set for {args.date}; run 'match' first.")
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
    """Every final session since the journal's first that has no snapshot yet, then outcomes (catch_up)."""
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
    annotation = store.latest_annotation(conn, snap["snapshot_id"], preopen.RULES_PROTOCOL_VERSION)
    if annotation is None:
        print(f"  no {preopen.RULES_PROTOCOL_VERSION} annotation yet")
    else:
        print(f"  pre-open structure ({preopen.RULES_PROTOCOL_VERSION}, {annotation['integrity_status']}):")
        for prop in (p for p in preopen.FIELDS if p in annotation["fields"]):
            f = annotation["fields"][prop]
            shown = f["value"] if f["value"] is not None else f"Unavailable ({f['reason']})"
            print(f"    {prop:22} {shown}")
        print("    price location: " + ", ".join(f"{k} {v}" for k, v in annotation["price_location"].items()))
    outcome = store.latest_outcome(conn, snap["snapshot_id"], defs.LABEL_VERSION)
    if outcome is None:
        print("  no outcome recorded yet")
        return 0
    print(f"  outcome r{outcome['outcome_revision']} ({defs.LABEL_VERSION}) - P2 realised-outcome record:")
    for prop, value in p2_record(snap, outcome):
        print(f"    {prop:42} {value}")
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
    common(sub.add_parser("catch-up", help="Snapshot + outcome for every final session not yet stored"),
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
    p = sub.add_parser("analogues", help="One session's analogues (outcome-blind unless --outcomes)")
    common(p, ranged=False)
    p.add_argument("--outcomes", action="store_true", help="Also show the analogues' outcomes")
    p = sub.add_parser("annotation-review-set", help="Choose an outcome-blind review set of pre-open annotations")
    p.add_argument("--name", required=True)
    p.add_argument("--size", type=int, default=25)
    p.add_argument("--profile", default=defs.DEFAULT_PROFILE, choices=sorted(defs.PROFILES))
    p = sub.add_parser("annotation-review-report", help="Summarise an annotation review set's verdicts")
    p.add_argument("--name", required=True)
    common(sub.add_parser("show", help="Print one session's snapshot and latest outcome"), ranged=False)
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

    init_database(args.db)
    conn = get_db_connection(args.db)
    try:
        register(conn)
        handler = {"register": lambda c, a: 0, "snapshot": cmd_snapshot, "outcomes": cmd_outcomes,
                   "backfill": cmd_backfill, "catch-up": cmd_catch_up, "annotate": cmd_annotate,
                   "match": cmd_match, "annotate-llm": cmd_annotate_llm, "analogues": cmd_analogues,
                   "annotation-review-set": cmd_annotation_review_set,
                   "annotation-review-report": cmd_annotation_review_report, "show": cmd_show, "review-set": cmd_review_set,
                   "review-report": cmd_review_report}[args.command]
        return handler(conn, args)
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
