#!/usr/bin/env python3
"""
The benchmark price fan (contracts/fan.py, docs/fan.md): from any minute, the
distribution of the price at every later minute of the trading day.

    python scripts/fan.py register                                        # the fan_rw_v1 definition
    python scripts/fan.py now --symbol NQ                                 # the fan from the latest closed bar
    python scripts/fan.py now --symbol NQ --as-of "2026-10-02 10:15"      # ... as it stood at a past minute (ET)
    python scripts/fan.py now --symbol NQ --json logs/fan_nq.json         # ... and as JSON (the chart's input)
    python scripts/fan.py score --symbol NQ --start 2025-09-02 --end 2026-07-10    # every origin, walk-forward
    python scripts/fan.py score --symbol NQ --symbol ES --start ... --end ... --no-report
    python scripts/fan.py experiment-register --dry-run                   # the intermarket experiment, resolved
    python scripts/fan.py experiment-register                             # ... and registered (fixed in advance)
    python scripts/fan.py experiment-show                                 # what is registered, sealed or open
    python scripts/fan.py panel-audit                                     # every instrument on the minute grid
    python scripts/fan.py baseline-gate                                   # fan_rw_v2 against fan_rw_v1 on the checks

Nothing is stored but the definitions: a fan is recomputed from the bars on demand, and the score reads the stored
sessions walk-forward (each fitted on the sessions before it). ``score`` writes
docs/reports/<version>_<symbol>_<start>_<end>.md and .csv - and refuses a range that reaches into a registered
experiment's sealed holdout (forecaster/fan_experiment.py, docs/fan_experiment.md).
"""

import argparse
import json
import logging
import os
import sys
import time
from datetime import date, datetime, timezone

import numpy as np

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from config import Config
from contracts import fan as F
from database import journal_store as store
from database.connection import get_db_connection, init_database
from features import calendar as cal
from features.session_windows import get_trading_day_date
from forecaster import fan_experiment as fx
from forecaster import fan_harness as fh
from forecaster import fan_panel as fp
from forecaster.fan_benchmark import InsufficientHistory, fan_from, fit, slot_instant
from forecaster.fan_data import history_start, load_days
from forecaster.fan_scoring import score_sessions, summarise, write_report
from forecaster.provenance import code_revision

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("fan")
REPORT_DIR = os.path.join(_PROJECT_ROOT, "docs", "reports")
SHOWN_QUANTILES = (0.05, 0.25, 0.5, 0.75, 0.95)


def _et(t: datetime) -> str:
    return t.astimezone(cal.NY_TZ).strftime("%Y-%m-%d %H:%M ET")


def _as_of(text):
    if not text:
        return datetime.now(timezone.utc)
    return cal.NY_TZ.localize(datetime.strptime(text, "%Y-%m-%d %H:%M")).astimezone(timezone.utc)


def build_fan(conn, symbol: str, as_of: datetime, minutes=None):
    """``(day, model, fan)`` from the last bar of ``symbol`` closed by ``as_of``; raises ValueError without one."""
    session_date = date.fromisoformat(get_trading_day_date(as_of.astimezone(cal.NY_TZ)))
    if not cal.session(session_date).is_open:
        raise ValueError(f"{_et(as_of)} falls in no scheduled session ({session_date} is closed)")
    days = load_days(conn, symbol, history_start(session_date, F.EVENT_SESSIONS), session_date, as_of=as_of)
    if not days or days[-1].session_date != session_date:
        raise ValueError(f"no {symbol} bars stored for the {session_date} session by {_et(as_of)}")
    day = days[-1]
    seen = np.flatnonzero(np.isfinite(day.closes))
    t = int(seen[-1])
    model = fit(day, days[:-1])
    return day, model, fan_from(model, day.returns, t, float(day.closes[t]), minutes)


def fan_json(symbol, as_of, day, model, fan) -> dict:
    """The fan as the chart reads it: per forecast minute its closing instant, sigma and every issued quantile."""
    return {
        "version": F.FAN_VERSION, "definition_hash": F.fan_record()["definition_hash"], "symbol": symbol,
        "contract_id": day.contract_id, "session_date": day.session_date.isoformat(),
        "as_of_utc": as_of.isoformat(),
        "origin": {"slot": fan.origin_slot, "bar_start_utc": slot_instant(day.session_date, fan.origin_slot).isoformat(),
                   "price": fan.price},
        "levels": {"long": fan.level_long, "short": fan.level_short},
        "releases_known": day.releases_known,
        "releases": [{"at_utc": r.at.isoformat(), "name": r.name, "group": r.group,
                      "multipliers": [round(float(m), 4) for m in model.multipliers[r.group]]}
                     for r in day.releases if r.slot > fan.origin_slot],
        "quantiles": list(F.QUANTILES),
        "minutes": fan.minutes.tolist(),
        "at_utc": [t.isoformat() for t in fan.instants()],
        "sigma_bps": [round(float(s) * 1e4, 4) for s in fan.sigma],
        "prices": [[round(float(p), 4) for p in row] for row in fan.prices],
    }


def cmd_register(conn, args):
    created = store.register_version(conn, F.fan_record())
    print(f"{F.FAN_VERSION}: {'registered' if created else 'already registered'} "
          f"(hash {F.fan_record()['definition_hash'][:16]})")
    return 0


def cmd_now(conn, args):
    as_of = _as_of(args.as_of)
    try:
        day, model, fan = build_fan(conn, args.symbol, as_of, args.minutes)
    except (ValueError, InsufficientHistory) as e:
        print(f"No fan: {e}")
        return 1
    origin = slot_instant(day.session_date, fan.origin_slot + 1)
    print(f"{Config.describe_instrument(args.symbol)} - session {day.session_date}, contract {day.contract_id}")
    print(f"Origin: the bar closing {_et(origin)} at {fan.price:.2f}; level long {fan.level_long:.2f}, "
          f"short {fan.level_short:.2f}; releases {'known' if day.releases_known else 'UNKNOWN (outside the calendar)'}")
    for r in day.releases:
        if r.slot > fan.origin_slot:
            print(f"  ahead: {_et(r.at)} {r.name} ({r.group}, x{model.multipliers[r.group][0]:.1f} in its minute)")
    if not len(fan.minutes):
        print("The trading day has ended: no minute left to forecast.")
        return 0
    idx = [F.QUANTILES.index(q) for q in SHOWN_QUANTILES]
    marks = sorted({m for m in (1, 5, 15, 30, 60, 120, 240) if m <= len(fan.minutes)} | {len(fan.minutes)})
    times = fan.instants()
    print(f"{'minutes':>7}  {'at (ET)':<19} {'sigma bps':>9}  " + "  ".join(f"{int(q * 100):>3}%".rjust(10)
                                                                         for q in SHOWN_QUANTILES))
    for m in marks:
        row = fan.prices[m - 1]
        print(f"{m:>7}  {_et(times[m - 1])[:16]:<19} {fan.sigma[m - 1] * 1e4:>9.1f}  "
              + "  ".join(f"{row[i]:>10.2f}" for i in idx))
    if args.json:
        os.makedirs(os.path.dirname(os.path.abspath(args.json)), exist_ok=True)
        with open(args.json, "w") as fh:
            json.dump(fan_json(args.symbol, as_of, day, model, fan), fh)
        print(f"Wrote {args.json}")
    return 0


def cmd_score(conn, args):
    start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)
    try:
        fx.guard(conn, start, end)
    except fx.HoldoutSealed as e:
        print(f"Not scored: {e}")
        return 1
    rec = F.fan_record()
    status = 0
    for symbol in args.symbol:
        days = load_days(conn, symbol, history_start(start, F.EVENT_SESSIONS), end)
        result = score_sessions(days, start, end)
        scores = result["scores"]
        print(f"\n{symbol}: {len(scores)} session(s) scored, skipped {result['skipped'] or 'none'}")
        if not scores:
            status = 1
            continue
        summary = summarise(scores)
        print(f"{'h':>4} {'origins':>9} {'CRPS bps':>9} {'z RMS':>6} {'cover90':>8} {'skill vs flat':>14}")
        for e in summary:
            s, p = e["variants"]["full"], e["paired"]["full-flat"]
            if s.get("n"):
                print(f"{e['horizon']:>4} {s['n']:>9} {s['crps_bps']:>9.3f} {s['z_rms']:>6.3f} "
                      f"{100 * s['coverage']['0.90']:>7.1f}% {100 * (p['skill'] or 0):>13.1f}%")
        if not args.no_report:
            meta = {"version": F.FAN_VERSION, "definition_hash": rec["definition_hash"], "code_revision": code_revision(),
                    "symbol": symbol, "start": scores[0].session_date.isoformat(),
                    "end": scores[-1].session_date.isoformat(), "sessions_scored": len(scores),
                    "skipped": result["skipped"]}
            print(f"Report: {write_report(meta, summary, result['last_model'], args.report_dir)}")
    return status


def _describe_experiment(m, hash_=None):
    """The lines that summarise a fan experiment manifest."""
    sp, inst = m["split"], m["instruments"]["availability"]
    h = m["horizons"]
    lines = [f"{m['name']}" + (f" (hash {hash_[:16]})" if hash_ else "")
             + (f" - supersedes {m['supersedes']['version']}" if m.get("supersedes") else ""),
             f"  primary {h['primary']['target']} at {h['primary']['minutes']} min, every origin - CRPS against "
             f"{m['baselines']['v1']} (or fan_rw_v2 if it passes its gate first)",
             f"  secondary {', '.join(str(x) for x in h['secondary']['minutes'])} min; "
             f"{', '.join(h['secondary']['targets_at_primary_horizon'])} at {h['primary']['minutes']} min; pre-open "
             f"09:29 -> {', '.join(h['secondary']['pre_open']['endpoints_et'])}",
             f"  window from {sp['window']['start']} (set by {', '.join(sp['window']['set_by'])}), "
             f"{sp['window']['warm_up_sessions']} warm-up sessions",
             f"  development {sp['development']['first']} to {sp['development']['last']} "
             f"({sp['development']['count']} sessions)"]
    for b in sp["checks"]["blocks"]:
        lines.append(f"    check {b['check']}: train {b['train']['first']} to {b['train']['last']} "
                     f"({b['train']['sessions']}), check {b['sessions']['first']} to {b['sessions']['last']} "
                     f"({b['sessions']['count']})")
    lines.append(f"  holdout {sp['holdout']['first']} to {sp['holdout']['last']} ({sp['holdout']['count']} sessions)")
    for status in ("included", "deferred"):
        syms = [s for s, a in inst.items() if a["status"] == status]
        if syms:
            lines.append(f"  {status}: " + ", ".join(f"{s} (from {inst[s]['first_complete']})" for s in syms))
    late = [s for s, a in inst.items() if a["status"] == "included" and a.get("missing_before")]
    if late:
        lines.append("  missing before their start: " + ", ".join(
            f"{s} until {inst[s]['missing_before']} ({100 * inst[s]['development_coverage']:.0f}% of development)"
            for s in late))
    for s, a in inst.items():
        if a.get("deferred_because"):
            lines.append(f"    {s}: {a['deferred_because']}")
    excluded = {t: d for t, d in m["data"]["excluded"].items() if d}
    lines.append("  excluded sessions: " + ("; ".join(f"{t} {len(d)} ({', '.join(d[:3])}"
                                                      + (" ..." if len(d) > 3 else "") + ")"
                                                      for t, d in excluded.items()) or "none"))
    return lines


def cmd_experiment_register(conn, args):
    """The intermarket fan experiment's manifest, resolved from the store and registered before any model exists."""
    try:
        manifest = fx.build_manifest(conn, args.name, args.holdout_end, args.holdout_sessions, args.warm_up,
                                     args.min_development, args.min_coverage, args.supersedes or None)
    except ValueError as e:
        print(f"Not registered: {e}")
        return 1
    print("\n".join(_describe_experiment(manifest)))
    if args.dry_run:
        print("Dry run: nothing registered.")
        return 0
    try:
        new = fx.register_experiment(conn, manifest)
    except (ValueError, store.VersionConflict) as e:
        print(f"Not registered: {e}")
        return 1
    rec = fx.load_experiment(conn, args.name)
    print(f"{args.name}: {'registered' if new else 'already registered, identical'} - hash "
          f"{rec['definition_hash'][:16]}. The holdout is sealed until a model frozen against it opens it; no model "
          f"or score exists.")
    return 0


def cmd_experiment_show(conn, args):
    """A registered fan experiment: its manifest and whether its holdout is sealed."""
    try:
        rec = fx.load_experiment(conn, args.name)
    except ValueError as e:
        print(e)
        return 1
    print("\n".join(_describe_experiment(rec["definition"], rec["definition_hash"])))
    model = fx.frozen_model(conn, rec)
    newer = fx.superseded_by(conn, args.name)
    print(f"  registered {str(rec['registered_at'])[:19]} UTC; "
          + (f"superseded by {newer}" if newer else
             "holdout " + (f"open - frozen model {model['version']}" if model else "sealed: no frozen model")))
    return 0


def cmd_panel_audit(conn, args):
    """The point-in-time panel of an experiment's development sessions, audited: what each instrument holds, the
    hours it trades and how old its value is (forecaster/fan_panel.py) - never the holdout."""
    try:
        rec = fx.load_experiment(conn, args.name)
    except ValueError as e:
        print(f"{e}: register it first (experiment-register)")
        return 1
    m = rec["definition"]
    dev = m["split"]["development"]["sessions"]
    fx.guard(conn, dev[0], dev[-1])
    symbols = [s for s, a in m["instruments"]["availability"].items() if a["status"] == "included"]
    started = time.time()
    panel = fp.load_panel(conn, dev, symbols)
    seconds = time.time() - started
    rows = fp.audit(panel, fx.availability(conn, symbols, dev[-1]))
    print(f"{args.name}: {len(dev)} development sessions ({dev[0]} to {dev[-1]}) x {len(symbols)} instruments, "
          f"loaded in {seconds:.1f} s")
    print(f"{'':5} {'bars':>5}  {'trades (ET)':34} {'no bar':>6} {'incomplete':>10}  age at 18:00 / 09:29 (min)")
    for r in rows:
        a = r["median_age"]
        print(f"{r['symbol']:5} {r['bars_per_session']:>5.0f}  {r['hours']:34} {len(r['no_bar']):>6} "
              f"{len(r['not_complete'] or []):>10}  {a['18:00']} / {a['09:29']}")
    if not args.no_report:
        meta = {"experiment": args.name, "first": dev[0], "last": dev[-1], "sessions": len(dev),
                "role": "development", "code_revision": code_revision(), "seconds": seconds}
        print(f"Report: {fp.write_audit(rows, meta, args.report_dir)}")
    return 0


def cmd_baseline_gate(conn, args):
    """Chunk 2's gate (forecaster/fan_harness.py): fan_rw_v2 against fan_rw_v1 for the primary target over the
    experiment's checks - which version is the baseline. Decided once: a stored gate is shown, not run again."""
    try:
        fx.load_experiment(conn, args.name)
    except ValueError as e:
        print(f"{e}: register it first (experiment-register)")
        return 1
    done = [r for r in store.experiment_results(conn, args.name) if r["results"].get("kind") == "baseline_gate"]
    if done:
        r = done[-1]
        print(f"Decided {str(r['computed_at'])[:19]} UTC (result {r['result_id'][:8]}): the baseline of {args.name} "
              f"is {r['results']['baseline']}. A gate is decided once; it is not run again.")
        return 0
    created = store.register_version(conn, F.fan_v2_record())          # the candidate is fixed before it is scored
    print(f"{F.FAN_V2_VERSION}: {'registered' if created else 'already registered'} "
          f"(hash {F.fan_v2_record()['definition_hash'][:16]})")
    started = time.time()
    res = fh.baseline_gate(conn, args.name)
    res["code_revision"] = code_revision()
    result_id = store.save_experiment_result(conn, args.name, res, res["code_revision"])
    print(f"{len(res['sessions']['scored'])} check sessions scored in {time.time() - started:.0f} s "
          f"({res['sessions']['first']} to {res['sessions']['last']})")
    print(f"{'':16} {'v1 CRPS':>8} {'v2 CRPS':>8} {'v2 - v1':>9} {'share':>8}  95 % interval")
    for k, p in res["results"].items():
        if p is None:
            continue
        iv = p["interval"]
        print(f"{k:16} {p['base_crps_bps']:>8.4f} {p['other_crps_bps']:>8.4f} {p['diff_bps']:>+9.5f} "
              f"{100 * (p['diff_share'] or 0):>+7.2f}%  " + (f"[{iv[0]:+.5f}, {iv[1]:+.5f}]" if iv else "-")
              + f"  {res['verdicts'][k]}")
    print(f"Decision (stored as result {result_id[:8]}): the baseline is {res['baseline']}")
    if not args.no_report:
        print(f"Report: {fh.write_gate_report(res, args.report_dir, fh.V2_NOTES)}")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="The benchmark price fan")
    parser.add_argument("--db", default=Config.DATABASE_URL, help="PostgreSQL connection URL")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("register", help=f"Register the {F.FAN_VERSION} definition")
    p = sub.add_parser("now", help="The fan from the latest closed bar, or from --as-of")
    p.add_argument("--symbol", default="NQ")
    p.add_argument("--as-of", help="ET wall clock 'YYYY-MM-DD HH:MM' (default: now)")
    p.add_argument("--minutes", type=int, help="Forecast this many minutes (default: to the day's end)")
    p.add_argument("--json", help="Also write the fan as JSON to this path")
    p = sub.add_parser("score", help="Score the fan at every origin of a range, walk-forward")
    p.add_argument("--symbol", action="append", required=True, help="Repeat for several instruments")
    p.add_argument("--start", required=True, help="First session YYYY-MM-DD")
    p.add_argument("--end", required=True, help="Last session YYYY-MM-DD")
    p.add_argument("--report-dir", default=REPORT_DIR)
    p.add_argument("--no-report", action="store_true", help="Print the summary only")
    p = sub.add_parser("experiment-register", help="Register the intermarket fan experiment before any model exists")
    p.add_argument("--name", default=fx.EXPERIMENT_NAME)
    p.add_argument("--holdout-end", default=fx.HOLDOUT_END, help="The holdout's last session (YYYY-MM-DD)")
    p.add_argument("--holdout-sessions", type=int, default=fx.HOLDOUT_SESSIONS)
    p.add_argument("--warm-up", type=int, default=fx.WARM_UP_SESSIONS, help="Sessions into the window before "
                   "development starts")
    p.add_argument("--min-development", type=int, default=fx.MIN_DEVELOPMENT_SESSIONS,
                   help="The fewest development sessions the primary target's history may give")
    p.add_argument("--min-coverage", type=float, default=fx.MIN_COVERAGE,
                   help="An instrument complete from less of development than this share is deferred")
    p.add_argument("--supersedes", default=fx.SUPERSEDES, help="The registered version this one replaces "
                   "('' for none)")
    p.add_argument("--dry-run", action="store_true", help="Resolve and print the manifest, register nothing")
    p = sub.add_parser("experiment-show", help="A registered fan experiment and whether its holdout is sealed")
    p.add_argument("--name", default=fx.EXPERIMENT_NAME)
    p = sub.add_parser("baseline-gate", help="Decide the baseline: fan_rw_v2 against fan_rw_v1 on the checks")
    p.add_argument("--name", default=fx.EXPERIMENT_NAME)
    p.add_argument("--report-dir", default=REPORT_DIR)
    p.add_argument("--no-report", action="store_true", help="Print the summary only")
    p = sub.add_parser("panel-audit", help="Audit the point-in-time panel of an experiment's development sessions")
    p.add_argument("--name", default=fx.EXPERIMENT_NAME)
    p.add_argument("--report-dir", default=REPORT_DIR)
    p.add_argument("--no-report", action="store_true", help="Print the summary only")
    args = parser.parse_args(argv)

    init_database(args.db)
    conn = get_db_connection(args.db)
    try:
        if args.command != "register":
            store.register_version(conn, F.fan_record())
        return {"register": cmd_register, "now": cmd_now, "score": cmd_score,
                "experiment-register": cmd_experiment_register, "experiment-show": cmd_experiment_show,
                "panel-audit": cmd_panel_audit, "baseline-gate": cmd_baseline_gate}[args.command](conn, args)
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
