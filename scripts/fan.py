#!/usr/bin/env python3
"""
The benchmark price fan (contracts/fan.py, docs/fan.md): from any minute, the
distribution of the price at every later minute of the trading day.

    python scripts/fan.py register                                        # the fan_rw_v1 definition
    python scripts/fan.py now --symbol NQ                                 # the fan from the latest closed bar
    python scripts/fan.py now --symbol NQ --as-of "2026-10-02 10:15"      # ... as it stood at a past minute (ET)
    python scripts/fan.py now --symbol NQ --json logs/fan_nq.json         # ... and as JSON (the chart's input)
    python scripts/fan.py score --symbol NQ --start 2025-09-02 --end 2026-10-02    # every origin, walk-forward
    python scripts/fan.py score --symbol NQ --symbol ES --start ... --end ... --no-report

Nothing is stored but the definition: a fan is recomputed from the bars on demand, and the score reads the stored
sessions walk-forward (each fitted on the sessions before it). ``score`` writes
docs/reports/<version>_<symbol>_<start>_<end>.md and .csv.
"""

import argparse
import json
import logging
import os
import sys
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
    args = parser.parse_args(argv)

    init_database(args.db)
    conn = get_db_connection(args.db)
    try:
        if args.command != "register":
            store.register_version(conn, F.fan_record())
        return {"register": cmd_register, "now": cmd_now, "score": cmd_score}[args.command](conn, args)
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
