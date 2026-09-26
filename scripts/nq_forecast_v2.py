#!/usr/bin/env python3
"""
The v2 NQ opening-forecast pipeline (docs/forecast_contract_v2.md).

    python scripts/nq_forecast_v2.py live                         # 09:29 ET: capture + forecast
    python scripts/nq_forecast_v2.py snapshot --date 2026-09-24   # historical reconstruction
    python scripts/nq_forecast_v2.py forecast --date 2026-09-24
    python scripts/nq_forecast_v2.py outcomes --start 2026-09-01 --end 2026-09-24
    python scripts/nq_forecast_v2.py backfill --start 2026-06-01 --end 2026-09-24
    python scripts/nq_forecast_v2.py train --date 2026-09-25      # fit + CV report + saved artifact
    python scripts/nq_forecast_v2.py evaluate --outcome-revision 1

Forecasts come from the trained scikit-learn model (nq_sklearn_v1, the default)
or the climatology baseline (nq_climatology_v2); ``--model`` takes one or a
comma-separated list. No language model or external API is called.

Every command registers the feature, label and model definitions first; a
changed definition under an existing version name stops the run.

``live`` is the only path that produces ``live_capture`` snapshots. It trains
the model(s) first, on every outcome already recorded, then waits
(until 09:29:45 ET by default) for the NQ and ES bars starting 09:28 - as
confirmed by the real-time streamer, collector/live_stream.py - freezes the
snapshot, and forecasts; both must be complete before 09:30:00 ET or nothing is
recorded as live. Only bars with a real-time receipt make a live capture
``verified``.
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
from database.queries import get_bar_receipts
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


def _models(arg):
    names = [m.strip() for m in arg.split(",") if m.strip()]
    unknown = [m for m in names if m not in models_v2.MODELS]
    if unknown or not names:
        raise SystemExit(f"unknown model(s) {', '.join(unknown) or arg!r}; choose from "
                         f"{', '.join(sorted(models_v2.MODELS))}")
    return names


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


def run_forecast(conn, snapshot_id, model_version, fitted=None):
    snapshot = store.get_feature_snapshot(conn, snapshot_id)
    run, predictions = models_v2.predict(conn, snapshot, models_v2.MODELS[model_version],
                                         code_revision=_code_revision(), fitted=fitted)
    run_id = store.save_forecast_run(conn, run, predictions)
    summary = ", ".join(
        f"{p['target_id']}=" + (p["predicted_label"] if p["prediction_status"] == "issued"
                                else f"{p['prediction_status'].upper()}({p['decision_reason']})")
        for p in predictions)
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
    for target, o in out["labels"].items():
        rev, created = store.save_realised_outcome(
            conn, snapshot["snapshot_id"], labels_v2.LABEL_VERSION, target, o["label"],
            None if o["label"] is not None else o["status"], o["available_at"], out["digest"],
            labels_v2.METRIC_VERSION, mrev, o["window_start_at"], o["window_end_at"])
        if created and rev > 1:
            logger.warning(f"{snapshot['session_date']} {target}: outcome revised to revision {rev} "
                           f"({o['label'] or o['status']}).")
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
    fitted = None
    if args.artifact:
        if len(_models(args.model)) != 1:
            raise SystemExit("--artifact belongs to exactly one --model")
        fitted = models_v2.load_artifact(args.artifact)   # refused for sessions it could not have forecast
    status = 0
    for s in _sessions(args):
        snaps = store.find_snapshots(conn, s.session_date.isoformat(), catv2.FEATURE_VERSION)
        if not snaps:
            logger.error(f"{s.session_date}: no {catv2.FEATURE_VERSION} snapshot; run 'snapshot' first.")
            status = 1
            continue
        for model in _models(args.model):
            try:
                run_forecast(conn, snaps[0]["snapshot_id"], model, fitted=fitted)
            except ValueError as e:   # an artifact that is not point-in-time for this session
                logger.error(f"{s.session_date}: {e}")
                status = 1
    return status


def cmd_outcomes(conn, md, args):
    n = 0
    for snap in store.list_snapshots(conn, args.start, args.end, catv2.FEATURE_VERSION):
        n += record_outcomes(conn, md, snap)
    logger.info(f"Outcomes recorded or confirmed for {n} snapshot(s).")
    return 0


def cmd_backfill(conn, md, args):
    """
    In session order: reconstruct, forecast, then score - a walk-forward
    backtest. Each model is retrained every ``--retrain-every`` sessions on the
    outcomes of earlier sessions only; in between, the last fit is reused, which
    has seen strictly less, so the backtest stays point-in-time.
    """
    models = _models(args.model)
    fits = {}   # model -> (fitted, sessions since it was fitted)
    for s in _sessions(args):
        try:
            snapshot_id = take_snapshot(conn, md, s.session_date)
        except SnapshotError as e:
            logger.error(f"{s.session_date}: {e}")
            continue
        for model in models:
            fitted, age = fits.get(model, (None, None))
            if fitted is None or age >= args.retrain_every:
                fitted, age = models_v2.fit(conn, models_v2.MODELS[model], s.session_date, s.cutoff_at), 0
            run_forecast(conn, snapshot_id, model, fitted=fitted)
            fits[model] = (fitted, age + 1)
        record_outcomes(conn, md, store.get_feature_snapshot(conn, snapshot_id))
    return 0


def cmd_train(conn, md, args):
    """Fits the model(s) for one session, prints the selection report, saves the artifact."""
    day = cal.session(args.date) if args.date else cal.session(datetime.now(cal.NY_TZ).date())
    while not day.is_open:
        day = cal.session(day.session_date + timedelta(days=1))
    for name in _models(args.model):
        fitted = models_v2.fit(conn, models_v2.MODELS[name], day.session_date, day.cutoff_at)
        report = models_v2.training_report(fitted)
        print(f"\n{name} for {day.session_date} (outcomes knowable by "
              f"{day.cutoff_at.astimezone(cal.NY_TZ):%Y-%m-%d %H:%M} ET)")
        for target, t in report["targets"].items():
            head = f"  {target:18} n={t['n']:<5}"
            if t["status"] != "ok":
                print(f"{head} {t['status']}")
                continue
            print(f"{head} selected={t['selected']}  ({t['first_session']} .. {t['last_session']}, "
                  f"{t.get('cv_folds', 0)} CV folds)")
            for cand, sc in (t.get("cv") or {}).items():
                mark = "*" if cand == t["selected"] else " "
                print(f"      {mark} {cand:24} logloss {sc['log_loss']:.4f}  acc {sc['accuracy']:.3f}  "
                      f"(n={sc['n_validation']})")
        if args.out:
            os.makedirs(os.path.join(args.out, name), exist_ok=True)
            path = os.path.join(args.out, name, f"{day.session_date}.joblib")
            models_v2.save_artifact(fitted, path)
            print(f"  saved {path} (+ .json report)")
    return 0


def _last_bar_ready(conn, contract_id, bar_start, accept_unconfirmed) -> bool:
    """
    The minute is in the store and, when the real-time streamer has received it,
    the streamer's confirmation re-read (IB's historical value of the finished
    minute) has arrived too - unless unconfirmed streamed values are accepted.
    """
    stored = conn.execute("SELECT 1 FROM bars WHERE contract_id = %s AND interval = '1m' "
                          "AND timestamp_utc = %s;", (contract_id, bar_start)).fetchone()
    if not stored:
        return False
    if accept_unconfirmed:
        return True
    receipts = [r["finalised_by"] for r in get_bar_receipts(conn, contract_id, bar_start)]
    return not receipts or "confirm_fetch" in receipts


def cmd_live(conn, md, args):
    today = datetime.now(cal.NY_TZ).date()
    s = cal.session(today)
    if not s.is_open:
        logger.info(f"{today} is not a scheduled session; nothing to do.")
        return 0
    # Train before T so only prediction is left for the 09:29-09:30 window. Only
    # outcomes already recorded now (and knowable by T) are used.
    fitted = {}
    for name in _models(args.model):
        fitted[name] = models_v2.fit(conn, models_v2.MODELS[name], today, s.cutoff_at,
                                     computed_by=datetime.now(timezone.utc))
        logger.info(f"Trained {name}: " + ", ".join(
            f"{t}={f.get('selected') if f['status'] == 'ok' else f['status']} (n={f['n']})"
            for t, f in fitted[name]["targets"].items()))
    now = datetime.now(timezone.utc)
    if now < s.cutoff_at:
        logger.info(f"Waiting for T ({s.cutoff_at.astimezone(cal.NY_TZ):%H:%M:%S} ET).")
        time.sleep((s.cutoff_at - now).total_seconds())
    deadline = cal.ny_instant(today, datetime.strptime(args.wait_until, "%H:%M:%S").time())
    last_bar = s.cutoff_at - timedelta(minutes=1)
    pending = {}
    for symbol in [x.strip().upper() for x in args.wait_for.split(",") if x.strip()]:
        a = md.active_contract(symbol, today)
        cid = a["contract_id"] if a else md.fallback_contract(symbol)
        if cid is None:
            logger.warning(f"{symbol}: no contract known for {today}; not waiting for it.")
        else:
            pending[symbol] = cid
    while pending and datetime.now(timezone.utc) < deadline:
        for symbol, cid in list(pending.items()):
            if _last_bar_ready(conn, cid, last_bar, args.accept_unconfirmed):
                del pending[symbol]
        if pending:
            time.sleep(0.5)
    if pending:
        logger.warning(f"The 09:28 bar of {', '.join(pending)} is not ready by {args.wait_until} ET; "
                       f"freezing with what the store holds.")
    md = DbMarketData(conn)   # no cache from before the wait
    try:
        snapshot_id = take_snapshot(conn, md, today, "live_capture")
        for name, f in fitted.items():
            run_forecast(conn, snapshot_id, name, fitted=f)
    except SnapshotError as e:
        logger.error(str(e))
        return 1
    except Exception as e:   # the database refuses a live run generated at/after 09:30
        logger.error(f"Live forecast not recorded: {e}")
        return 1
    return 0


def cmd_evaluate(conn, md, args):
    """
    Per model, target and data mode: how many predictions were issued, abstained
    or unavailable; accuracy of the issued labels; and log loss and Brier score of
    every probability distribution (issued or abstained) with an eligible outcome.
    """
    rows = store.get_prediction_outcomes(conn, outcome_revision=args.outcome_revision,
                                         outcomes_as_of=args.outcomes_as_of, model_version=args.model)
    groups = {}
    for r in rows:
        groups.setdefault((r["model_version"], r["target_id"], r["data_mode"]), []).append(r)
    if not groups:
        print("No predictions with realised outcomes for that revision.")
        return 0
    print(f"{'model':20} {'target':18} {'mode':25} {'n':>5} {'issued':>6} {'abst':>5} {'unav':>5} "
          f"{'inel':>5} {'acc':>6} {'logloss':>8} {'brier':>6}")
    for (model, target, mode), rs in sorted(groups.items()):
        status = [r["prediction_status"] for r in rs]
        issued = [r for r in rs if r["prediction_status"] == "issued" and r["eligible"]]
        probs = [r for r in rs if r["probabilities"] is not None and r["eligible"]]
        acc = sum(r["predicted_label"] == r["actual_label"] for r in issued) / len(issued) if issued else math.nan
        ll = (sum(-math.log(max(r["probabilities"][r["actual_label"]], 1e-15)) for r in probs) / len(probs)
              if probs else math.nan)
        br = (sum(sum((p - (lab == r["actual_label"])) ** 2 for lab, p in r["probabilities"].items())
                  for r in probs) / len(probs) if probs else math.nan)
        print(f"{model:20} {target:18} {mode:25} {len(rs):5d} {status.count('issued'):6d} "
              f"{status.count('abstained'):5d} {status.count('unavailable'):5d} "
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
    model_help = f"model version(s), comma-separated: {', '.join(sorted(models_v2.MODELS))}"
    p = sub.add_parser("forecast", help="Run model(s) on stored snapshot(s)")
    dates(p)
    p.add_argument("--model", default=models_v2.DEFAULT_MODEL, help=model_help)
    p.add_argument("--artifact", help="Use a model saved by 'train' instead of training now (only for its "
                                      "session or later ones)")
    p = sub.add_parser("outcomes", help="Record outcome metrics and labels for closed sessions")
    dates(p, single=False)
    p = sub.add_parser("backfill", help="Snapshot + forecast + outcomes, session by session")
    dates(p, single=False)
    p.add_argument("--model", default=",".join(models_v2.MODELS), help=model_help)
    p.add_argument("--retrain-every", type=int, default=5,
                   help="Sessions between refits (1 = refit every session); reuse is point-in-time safe")
    p = sub.add_parser("train", help="Fit model(s) for a session and print the cross-validation report")
    p.add_argument("--date", help="Session to train for (default: today, or the next session)")
    p.add_argument("--model", default=models_v2.DEFAULT_MODEL, help=model_help)
    p.add_argument("--out", default=os.path.join(_PROJECT_ROOT, "data", "models"),
                   help="Directory for the fitted artifact and its JSON report ('' to skip saving)")
    p = sub.add_parser("live", help="Live capture and forecast before 09:30 ET")
    p.add_argument("--model", default=models_v2.DEFAULT_MODEL, help=model_help)
    p.add_argument("--wait-until", default="09:29:45", help="ET time to stop waiting for the 09:28 bars")
    p.add_argument("--wait-for", default="NQ,ES",
                   help="Symbols whose 09:28 bar must be stored before freezing (P and the ES leg)")
    p.add_argument("--accept-unconfirmed", action="store_true",
                   help="Do not wait for the streamer's confirm_fetch of a streamed 09:28 bar")
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
        if args.retrain_every < 1:
            parser.error("--retrain-every must be >= 1")

    init_database(args.db)
    conn = get_db_connection(args.db)
    try:
        register(conn)
        md = DbMarketData(conn)
        handler = {"snapshot": cmd_snapshot, "forecast": cmd_forecast, "outcomes": cmd_outcomes,
                   "backfill": cmd_backfill, "train": cmd_train, "live": cmd_live, "evaluate": cmd_evaluate,
                   "register": lambda *a: 0}[args.command]
        return handler(conn, md, args)
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
