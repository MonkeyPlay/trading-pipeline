#!/usr/bin/env python3
"""
NQ prompt-v2 journal records (docs/nq_prompt_v2.md): pre-open evidence snapshots
and their realised NQ-v2 outcome labels.

    python scripts/nq_journal.py register                                   # definitions only
    python scripts/nq_journal.py snapshot --date 2026-09-24                 # one session
    python scripts/nq_journal.py snapshot --start 2026-06-01 --end 2026-09-25
    python scripts/nq_journal.py outcomes --start 2026-06-01 --end 2026-09-25
    python scripts/nq_journal.py backfill --start 2026-06-01 --end 2026-09-25  # snapshot + outcome, in order
    python scripts/nq_journal.py show --date 2026-09-24                     # snapshot + P2's 40-field record
    python scripts/nq_journal.py review-set --name stage1_review_v1          # choose the 25-session review set
    python scripts/nq_journal.py review-report --name stage1_review_v1       # the reviewer's verdicts, per field

``--profile`` picks the cutoff profile (research_0929, the default, or
operational_0927); each has its own snapshot version. Snapshots are historical
reconstructions. Outcomes are recorded only once a session is final (two hours
after its scheduled close) and become a new revision only when they change.

Every command registers the label, convention and snapshot definitions first; a
changed definition under an existing version name stops the run.
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
from features.nq_evidence import SnapshotError, build_snapshot
from forecaster import labels_prompt_v2 as labels
from forecaster import review_set
from forecaster.outcome_display import p2_record

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("nq_journal")


def register(conn):
    for rec in defs.all_records():
        store.register_version(conn, rec)


def _sessions(args):
    if args.date:
        return [cal.session(args.date)]
    return cal.sessions_between(args.start, args.end)


def take_snapshot(conn, day, profile):
    snap = build_snapshot(conn, day, profile)
    snapshot_id, created = store.save_snapshot(conn, snap)
    th, refs = snap.payload["thresholds"], snap.payload["references"]
    not_valid = [k for k, v in refs.items() if v["status"] != "valid"]
    logger.info(f"{day} [{profile}]: snapshot {snapshot_id[:8]} ({'new' if created else 'already stored'}) "
                f"on {snap.payload['identity']['local_symbol'] or snap.contract_id}, T={th['T']} B={th['B']}; "
                f"unavailable references: {', '.join(not_valid) or 'none'}")
    return snapshot_id


def record_outcome(conn, snapshot, now=None):
    """Labels one stored snapshot once its session is final. Returns the revision, or None if not final."""
    if not labels.session_finalised(snapshot["session_date"], now or datetime.now(timezone.utc)):
        return None
    out = labels.compute_outcome(snapshot, labels.load_realised_bars(conn, snapshot))
    revision, created = store.save_outcome(conn, snapshot["snapshot_id"], defs.LABEL_VERSION, out)
    summary = ", ".join(f"{t}={v['label'] or v['reason'].upper()}" for t, v in out["labels"].items())
    if created and revision > 1:
        logger.warning(f"{snapshot['session_date']}: outcome revised to revision {revision}.")
    logger.info(f"{snapshot['session_date']}: outcome r{revision} {summary}")
    return revision


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
    if args.command == "outcomes" and not (args.start and args.end):
        parser.error("give --start and --end")
    if args.command == "show" and not args.date:
        parser.error("give --date")

    init_database(args.db)
    conn = get_db_connection(args.db)
    try:
        register(conn)
        handler = {"register": lambda c, a: 0, "snapshot": cmd_snapshot, "outcomes": cmd_outcomes,
                   "backfill": cmd_backfill, "show": cmd_show, "review-set": cmd_review_set,
                   "review-report": cmd_review_report}[args.command]
        return handler(conn, args)
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
