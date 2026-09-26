#!/usr/bin/env python3
"""
The v2 NQ opening-forecast pipeline (docs/forecast_contract_v2.md).

    python scripts/nq_forecast_v2.py live                         # 09:29 ET: capture + forecast
    python scripts/nq_forecast_v2.py snapshot --date 2026-09-24   # historical reconstruction
    python scripts/nq_forecast_v2.py forecast --date 2026-09-24
    python scripts/nq_forecast_v2.py outcomes --start 2026-09-01 --end 2026-09-24
    python scripts/nq_forecast_v2.py backfill --start 2026-06-01 --end 2026-09-24
    python scripts/nq_forecast_v2.py evaluate --outcome-revision 1

Every command registers the feature, label and model definitions first; a
changed definition under an existing version name stops the run.

``live`` is the only path that produces ``live_capture`` snapshots. It waits
(until 09:29:45 ET by default) for the NQ bar starting 09:28 to reach the store,
freezes the snapshot, and forecasts; both must be complete before 09:30:00 ET or
nothing is recorded as live. Bars must be collected separately between 09:29:00
and the deadline - see "Live capture timing" in the doc.
"""

import argparse
import logging
import math
import os
import subprocess
import sys
import time
from datetime import date, datetime, timedelta, timezone

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from config import Config
from database import forecast_store as store
from database.connection import get_db_connection, init_database
from features import calendar as cal
from features import catalogue as catv2
from features.market_data import DbMarketData
from features.nq_v2 import SnapshotError, build_snapshot
from forecaster import labels_v2, models_v2

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("nq_forecast_v2")


def _code_revision():
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=_PROJECT_ROOT,
                                       stderr=subprocess.DEVNULL, text=True).strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def register(conn):
    store.register_feature_version(conn, catv2.registry_record())
    store.register_label_version(conn, labels_v2.label_registry_record())
    for model in models_v2.MODELS.values():
        store.register_model_version(conn, models_v2.registry_record(model))


def _sessions(args):
    if args.date:
        return [cal.session(args.date)]
    return cal.sessions_between(args.start, args.end)


def take_snapshot(conn, md, day, data_mode="historical_reconstruction"):
    snap = build_snapshot(md, day, data_mode)
    snapshot_id, created = store.save_feature_snapshot(conn, snap)
    bad = {k: v for k, v in snap.feature_status.items() if v != "valid"}
    logger.info(f"{day}: snapshot {snapshot_id} ({'new' if created else 'already stored'}), "
                f"{snap.data_mode}, quality {snap.data_quality_status}, revision "
                f"{snap.source_revision_id[:12]}; {len(bad)} feature(s) not valid")
    return snapshot_id


def run_forecast(conn, snapshot_id, model_version):
    snapshot = store.get_feature_snapshot(conn, snapshot_id)
    run, predictions = models_v2.predict(conn, snapshot, models_v2.MODELS[model_version],
                                         code_revision=_code_revision())
    run_id = store.save_forecast_run(conn, run, predictions)
    summary = ", ".join(f"{p['target_id']}={p.get('predicted_label') or 'ABSTAIN'}" for p in predictions)
    logger.info(f"{snapshot['session_date']}: run {run_id} [{model_version}, input "
                f"{run['input_quality_status']}] {summary}")
    return run_id


def record_outcomes(conn, md, snapshot, now=None):
    now = now or datetime.now(timezone.utc)
    if not labels_v2.session_finalised(snapshot, now):
        return False
    out = labels_v2.compute_outcome(md, snapshot)
    mrev, _ = store.save_outcome_metrics(conn, snapshot["snapshot_id"], labels_v2.METRIC_VERSION,
                                         out["metrics"], out["metric_status"], out["available_at"],
                                         out["digest"])
    for target, (label, reason, available_at) in out["labels"].items():
        rev, created = store.save_realised_outcome(
            conn, snapshot["snapshot_id"], labels_v2.LABEL_VERSION, target, label, reason, available_at,
            out["digest"], labels_v2.METRIC_VERSION, mrev)
        if created and rev > 1:
            logger.warning(f"{snapshot['session_date']} {target}: outcome revised to revision {rev} "
                           f"({label or reason}).")
    return True


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

def cmd_snapshot(conn, md, args):
    status = 0
    for s in _sessions(args):
        try:
            take_snapshot(conn, md, s.session_date)
        except SnapshotError as e:
            logger.error(f"{s.session_date}: {e}")
            status = 1
    return status


def cmd_forecast(conn, md, args):
    status = 0
    for s in _sessions(args):
        snaps = store.find_snapshots(conn, s.session_date.isoformat(), catv2.FEATURE_VERSION)
        if not snaps:
            logger.error(f"{s.session_date}: no {catv2.FEATURE_VERSION} snapshot; run 'snapshot' first.")
            status = 1
            continue
        run_forecast(conn, snaps[0]["snapshot_id"], args.model)
    return status


def cmd_outcomes(conn, md, args):
    n = 0
    for snap in store.list_snapshots(conn, args.start, args.end, catv2.FEATURE_VERSION):
        n += record_outcomes(conn, md, snap)
    logger.info(f"Outcomes recorded or confirmed for {n} snapshot(s).")
    return 0


def cmd_backfill(conn, md, args):
    """In session order: reconstruct, forecast (seeing only earlier outcomes), score."""
    for s in _sessions(args):
        try:
            snapshot_id = take_snapshot(conn, md, s.session_date)
        except SnapshotError as e:
            logger.error(f"{s.session_date}: {e}")
            continue
        run_forecast(conn, snapshot_id, args.model)
        record_outcomes(conn, md, store.get_feature_snapshot(conn, snapshot_id))
    return 0


def cmd_live(conn, md, args):
    today = datetime.now(cal.NY_TZ).date()
    s = cal.session(today)
    if not s.is_open:
        logger.info(f"{today} is not a scheduled session; nothing to do.")
        return 0
    now = datetime.now(timezone.utc)
    if now < s.cutoff_at:
        logger.info(f"Waiting for T ({s.cutoff_at.astimezone(cal.NY_TZ):%H:%M:%S} ET).")
        time.sleep((s.cutoff_at - now).total_seconds())
    deadline = cal.ny_instant(today, datetime.strptime(args.wait_until, "%H:%M:%S").time())
    nq = md.active_contract("NQ", today) or {"contract_id": md.fallback_contract("NQ")}
    last_bar = s.cutoff_at - timedelta(minutes=1)
    while datetime.now(timezone.utc) < deadline:
        row = conn.execute("SELECT 1 FROM bars WHERE contract_id = %s AND interval = '1m' "
                           "AND timestamp_utc = %s;", (nq["contract_id"], last_bar)).fetchone()
        if row:
            break
        time.sleep(1.0)
    else:
        logger.warning("The NQ 09:28 bar is not in the store; freezing without it (P will be null).")
    md = DbMarketData(conn)   # no cache from before the wait
    try:
        snapshot_id = take_snapshot(conn, md, today, "live_capture")
        run_forecast(conn, snapshot_id, args.model)
    except SnapshotError as e:
        logger.error(str(e))
        return 1
    except Exception as e:   # the database refuses a live run generated at/after 09:30
        logger.error(f"Live forecast not recorded: {e}")
        return 1
    return 0


def cmd_evaluate(conn, md, args):
    rows = store.get_prediction_outcomes(conn, outcome_revision=args.outcome_revision,
                                         outcomes_as_of=args.outcomes_as_of, model_version=args.model)
    groups = {}
    for r in rows:
        groups.setdefault((r["model_version"], r["target_id"], r["data_mode"]), []).append(r)
    if not groups:
        print("No predictions with realised outcomes for that revision.")
        return 0
    print(f"{'model':22} {'target':22} {'mode':26} {'n':>5} {'abst':>5} {'inel':>5} "
          f"{'acc':>6} {'logloss':>8} {'brier':>6}")
    for (model, target, mode), rs in sorted(groups.items()):
        scored = [r for r in rs if not r["abstained"] and r["eligible"]]
        acc = sum(r["predicted_label"] == r["actual_label"] for r in scored) / len(scored) if scored else math.nan
        ll = (sum(-math.log(max(r["probabilities"][r["actual_label"]], 1e-15)) for r in scored) / len(scored)
              if scored else math.nan)
        br = (sum(sum((p - (lab == r["actual_label"])) ** 2 for lab, p in r["probabilities"].items())
                  for r in scored) / len(scored) if scored else math.nan)
        print(f"{model:22} {target:22} {mode:26} {len(rs):5d} {sum(r['abstained'] for r in rs):5d} "
              f"{sum(not r['eligible'] for r in rs):5d} {acc:6.3f} {ll:8.4f} {br:6.4f}")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="v2 NQ opening forecast records")
    parser.add_argument("--db", default=Config.DATABASE_URL, help="PostgreSQL connection URL")
    sub = parser.add_subparsers(dest="command", required=True)

    def dates(p, single=True):
        if single:
            p.add_argument("--date", help="Session date YYYY-MM-DD")
        p.add_argument("--start", help="First session date (inclusive)")
        p.add_argument("--end", help="Last session date (inclusive)")

    p = sub.add_parser("snapshot", help="Reconstruct historical snapshot(s)")
    dates(p)
    p = sub.add_parser("forecast", help="Run a model on stored snapshot(s)")
    dates(p)
    p.add_argument("--model", default="nq_climatology_v1", choices=sorted(models_v2.MODELS))
    p = sub.add_parser("outcomes", help="Record outcome metrics and labels for closed sessions")
    dates(p, single=False)
    p = sub.add_parser("backfill", help="Snapshot + forecast + outcomes, session by session")
    dates(p, single=False)
    p.add_argument("--model", default="nq_climatology_v1", choices=sorted(models_v2.MODELS))
    p = sub.add_parser("live", help="Live capture and forecast before 09:30 ET")
    p.add_argument("--model", default="nq_climatology_v1", choices=sorted(models_v2.MODELS))
    p.add_argument("--wait-until", default="09:29:45", help="ET time to stop waiting for the 09:28 bar")
    p = sub.add_parser("evaluate", help="Score predictions against a chosen outcome revision")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--outcome-revision", type=int)
    g.add_argument("--outcomes-as-of", help="UTC timestamp: latest revision computed by then")
    p.add_argument("--model", default=None)
    sub.add_parser("register", help="Register definitions only")

    args = parser.parse_args(argv)
    if args.command in ("snapshot", "forecast"):
        if not args.date and not (args.start and args.end):
            parser.error("give --date, or --start and --end")
    if args.command in ("outcomes", "backfill") and not (args.start and args.end):
        parser.error("give --start and --end")
    if args.command == "backfill":
        args.date = None

    init_database(args.db)
    conn = get_db_connection(args.db)
    try:
        register(conn)
        md = DbMarketData(conn)
        handler = {"snapshot": cmd_snapshot, "forecast": cmd_forecast, "outcomes": cmd_outcomes,
                   "backfill": cmd_backfill, "live": cmd_live, "evaluate": cmd_evaluate,
                   "register": lambda *a: 0}[args.command]
        return handler(conn, md, args)
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
