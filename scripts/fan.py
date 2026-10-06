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
    python scripts/fan.py checks --candidate phase_scale                  # a candidate against the baseline, 3 checks
    python scripts/fan.py checks --candidate identity --target ES         # ... the harness's self-check, on ES
    python scripts/fan.py features                                        # the features, audited (development)
    python scripts/fan.py checks --candidate gbm_all                      # the learned fan on the checks
    python scripts/fan.py compare gbm_own_ivx lin_pois_ivx                # two stored runs, paired (B minus A)
    python scripts/fan.py search                                          # SPA and StepM over every stored run
    python scripts/fan.py replay                                          # live-style replay of the leading candidates
    python scripts/fan.py freeze --candidate lin_pois_ivx --dry-run       # the frozen definition, registered without --dry-run
    python scripts/fan.py holdout --rehearse --definition data/fan_cache/freeze/<version>.json   # the code path, in sample
    python scripts/fan.py holdout                                         # the frozen model on the holdout - once
    python scripts/fan.py forward issue                                   # the forward record: this mark's forecast
    python scripts/fan.py forward score                                   # ... scored once its session is final
    python scripts/fan.py forward report                                  # ... the record so far

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
from contracts import nq_prompt_v2 as defs
from database import journal_store as store
from database.connection import get_db_connection, init_database
from features import calendar as cal
from features.session_windows import get_trading_day_date
from forecaster import fan_experiment as fx
from forecaster import fan_features as ff
from forecaster import fan_forward as fwd
from forecaster import fan_harness as fh
from forecaster import fan_model as fm
from forecaster import fan_panel as fp
from forecaster import fan_replay as frp
from forecaster import fan_search as fsr
from forecaster.fan_benchmark import InsufficientHistory, fan_from, fit, slot_instant
from forecaster.fan_data import history_start, load_days
from forecaster.fan_scoring import score_sessions, summarise, write_report
from forecaster.provenance import code_revision, source_snapshot

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("fan")
REPORT_DIR = os.path.join(_PROJECT_ROOT, "docs", "reports")
CACHE_DIR = os.path.join(_PROJECT_ROOT, "data", "fan_cache")      # the checks' baseline frames (not in git)
FREEZE_DIR = os.path.join(CACHE_DIR, "freeze")                    # frozen definitions as written (dry runs too)
SNAPSHOT_DIR = os.path.join(CACHE_DIR, "snapshots")               # archived source snapshots of a freeze
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


def cmd_checks(conn, args):
    """Chunk 3's harness (forecaster/fan_harness.py): a candidate against the decided baseline on the experiment's
    three checks - development, never the verdict; nothing is stored in the journal."""
    try:
        m = fx.load_experiment(conn, args.name)["definition"]
    except ValueError as e:
        print(f"{e}: register it first (experiment-register)")
        return 1
    target = args.target or m["targets"]["primary"]
    started = time.time()
    try:
        spec = fm.MODELS.get(args.candidate) or fm.TRIALS.get(args.candidate)
        if spec is not None:
            frames = fh.load_frames(conn, args.name, target, CACHE_DIR)[2] if spec.needs_frames else None
            table = load_features(conn, m, target) if spec.features is not None else None
            make = fm.candidate(args.candidate, table, frames)
        else:
            make = fh.CANDIDATES[args.candidate]
        res = fh.checks(conn, args.name, target, make, CACHE_DIR, args.refresh)
    except ValueError as e:
        print(f"Not run: {e}")
        return 1
    res["code_revision"] = code_revision()
    symbols = [s for s, a in m["instruments"]["availability"].items() if a["status"] == "included"]
    res["provenance"] = {
        "code_revision": res["code_revision"],
        "source": source_snapshot(),
        "data": fp.data_snapshot(conn, symbols, m["split"]["development"]["last"]),
        "feature_format": ff.FEATURE_FORMAT, "frame_format": fh.FRAME_FORMAT,
        "spec": ({"kind": spec.kind, "features": spec.features, "base": spec.base, "note": spec.note,
                  "settings": {**fm.SETTINGS, **spec.settings} if spec.kind in ("gbm", "crps_boost") else
                  ({"alpha": fm.LINEAR_ALPHA} if spec.kind == "linear" else {})}
                 if spec is not None else {"kind": "reference"}),
    }
    print(f"{res['candidate']} against {res['baseline']} on {args.name}'s checks, {target} "
          f"({time.time() - started:.0f} s)" + (f"; features {len(res['features'])}, hash {res['features_hash']}"
                                                 if res.get("features") is not None else "") + ":")
    for c in res["checks"]:
        t, s = c["train"], c["sessions"]
        mu = c["multiplier"]
        print(f"  check {c['check']}: trained on {t['sessions']} sessions ({t['rows']:,} rows), scored "
              f"{len(s['scored'])} of {s['wanted']} ({s['first']} to {s['last']}); multiplier 5/50/95 % "
              + (f"{mu['p5']:.2f} / {mu['p50']:.2f} / {mu['p95']:.2f}" if mu else "-"))
    print(f"{'':16} {'role':12} {'base CRPS':>9} {'cand CRPS':>9} {'difference':>11} {'share':>8}  95 % interval")
    for k in [f"h{h}" for h in fh.REPORT_HORIZONS] + [f"pre_open_{x}" for x in fh.PRE_OPEN_MINUTES]:
        p = res["results"].get(k)
        if p is None:
            continue
        iv = p["interval"]
        print(f"{k:16} {res['roles'][k]:12} {p['base_crps_bps']:>9.4f} {p['other_crps_bps']:>9.4f} "
              f"{p['diff_bps']:>+11.5f} {100 * (p['diff_share'] or 0):>+7.2f}%  "
              + (f"[{iv[0]:+.5f}, {iv[1]:+.5f}]" if iv else "-") + f"  {res['verdicts'][k]}")
    stored = _run_path(args.name, target, res["candidate"])
    os.makedirs(os.path.dirname(stored), exist_ok=True)
    with open(stored, "w") as fh_:
        json.dump(res, fh_)
    print(f"Run stored: {stored}")
    if not args.no_report:
        print(f"Report: {fh.write_checks_report(res, args.report_dir)}")
    return 0


def cmd_search(conn, args):
    """The multiple-comparison review of the development search (forecaster/fan_search.py): Hansen's SPA and Romano
    and Wolf's StepM over every stored checks run, against v2 and against gbm_own."""
    m = fx.load_experiment(conn, args.name)["definition"]
    target = args.target or m["targets"]["primary"]
    folder = os.path.join(CACHE_DIR, "checks")
    prefix = f"{args.name}_{target}_"
    runs = {}
    for f in sorted(os.listdir(folder)) if os.path.isdir(folder) else []:
        if f.startswith(prefix) and f.endswith(".json"):
            with open(os.path.join(folder, f)) as fh_:
                r = json.load(fh_)
            if r["candidate"] not in fsr.EXCLUDED:
                runs[r["candidate"]] = r
    if len(runs) < 2:
        print("Fewer than two stored runs: run scripts/fan.py checks first")
        return 1
    key = f"h{args.horizon}"
    res = fsr.review(runs, key)
    print(f"{len(runs)} stored runs of {args.name} ({target}), {key}, {res['v2']['sessions']} sessions")
    for part in ("v2", "own"):
        r = res[part]
        if r is None:
            continue
        print(f"against {r['benchmark']}: SPA p lower {r['spa']['p']['lower']:.3f}, consistent "
              f"{r['spa']['p']['consistent']:.3f}, upper {r['spa']['p']['upper']:.3f}; StepM (5 %) names "
              + (", ".join(r["better"]) or "none"))
    if not args.no_report:
        print(f"Report: {fsr.write_report(res, runs, args.name, target, key, args.report_dir)}")
    return 0


def cmd_replay(conn, args):
    """A live-style replay of the leading candidates on predefined check sessions (forecaster/fan_replay.py):
    every input rebuilt as the store served it at issuance, against the research path."""
    m = fx.load_experiment(conn, args.name)["definition"]
    target = args.target or m["targets"]["primary"]
    started = time.time()
    _, _, frames = fh.load_frames(conn, args.name, target, CACHE_DIR)
    table = load_features(conn, m, target)
    avail = m["instruments"]["availability"]
    symbols = [s for s, a in avail.items() if a["status"] == "included"]
    dev = m["split"]["development"]["sessions"]
    blocks = m["split"]["checks"]["blocks"]
    check_of = {d: b["check"] for b in blocks for d in dev
                if b["sessions"]["first"] <= d <= b["sessions"]["last"] and d in frames}
    col = [f.name for f in table.features].index(f"{target}.day_rv")
    day_rv = {d: float(table.X[table.sessions.index(d), 0, col]) for d in check_of}
    first, last = min(check_of), max(check_of)
    releases = [d.session_date.isoformat() for d in load_days(conn, target, date.fromisoformat(first),
                                                                date.fromisoformat(last))
                if d.releases_known and any(r.group in ("high", "fomc") for r in d.releases)]
    picked = frp.pick_sessions(sorted(check_of), day_rv, releases)
    candidates = {}
    for name in args.candidate:
        candidates[name] = {}
        for b in blocks:
            if not any(check_of[d] == b["check"] for days in picked.values() for d in days):
                continue
            train = fh.Rows.concat([fh.frame_rows(frames[d], every=fh.TRAIN_EVERY) for d in dev
                                    if d < b["sessions"]["first"] and d in frames])
            c = fm.candidate(name, table, frames)()
            c.fit(train)
            candidates[name][b["check"]] = c
    res = frp.replay(conn, target, picked, table, frames, symbols,
                     {s: avail[s]["first_complete"] for s in symbols}, candidates, check_of)
    print(f"Replay of {', '.join(args.candidate)} on {sum(len(v) for v in picked.values())} sessions "
          f"({time.time() - started:.0f} s): {'passed' if res['passed'] else 'FAILED'}")
    for kind, days in picked.items():
        print(f"  {kind:9} {', '.join(days)}")
    for k, v in res["checks"].items():
        print(f"  {k:12} compared {v['compared']:>6,}  failed {v['failed']:>4}  largest relative difference "
              f"{v['max_rel']:.2e}" + (f"  e.g. {v['examples'][0]}" if v["examples"] else ""))
    if not args.no_report:
        meta = {"experiment": args.name, "target": target, "code_revision": code_revision(),
                "candidates": args.candidate, "source": source_snapshot(),
                "data_fingerprint": fp.data_snapshot(conn, symbols, dev[-1])["fingerprint"]}
        print(f"Report: {frp.write_report(res, meta, args.report_dir)}")
    return 0 if res["passed"] else 1


def _included(m):
    avail = m["instruments"]["availability"]
    symbols = [s for s, a in avail.items() if a["status"] == "included"]
    return symbols, {s: avail[s]["first_complete"] for s in symbols}


def cmd_freeze(conn, args):
    """Chunk 7, part one: train the chosen candidate once on every development session and write its 'fan_model'
    definition - registered (opening the holdout for it) only without --dry-run. Scoring the holdout is a separate
    command (holdout)."""
    exp = fx.load_experiment(conn, args.name)
    m = exp["definition"]
    target = m["targets"]["primary"]
    if fx.frozen_model(conn, exp) is not None:
        print(f"Not frozen: {args.name} already has a frozen model ({fx.frozen_model(conn, exp)['version']})")
        return 1
    src = source_snapshot(archive_dir=None if args.dry_run else SNAPSHOT_DIR)
    if src["dirty"] and not args.dry_run and not args.allow_dirty:
        print("Not frozen: the working tree has uncommitted changes - commit them first, or pass --allow-dirty (the "
              "source is then archived under data/fan_cache/snapshots and its snapshot recorded)")
        return 1
    _, baseline, frames = fh.load_frames(conn, args.name, target, CACHE_DIR)
    table = load_features(conn, m, target)
    excluded = set(m["data"]["excluded"].get(target, []))
    dev = [d for d in m["split"]["development"]["sessions"] if d not in excluded and d in frames]
    rows = fh.Rows.concat([fh.frame_rows(frames[d], every=fh.TRAIN_EVERY) for d in dev])
    try:
        cand = fm.candidate(args.candidate, table, frames)()
        cand.fit(rows)
        symbols, _ = _included(m)
        provenance = {"code_revision": code_revision(), "source": src,
                      "data": fp.data_snapshot(conn, symbols, m["split"]["development"]["last"]),
                      "feature_format": ff.FEATURE_FORMAT, "frame_format": fh.FRAME_FORMAT}
        base_rec = F.fan_v2_record() if baseline == F.FAN_V2_VERSION else F.fan_record()
        definition = fm.frozen_definition(args.candidate, cand, exp, {"version": baseline,
                                          "definition_hash": base_rec["definition_hash"]}, dev, provenance)
        frozen = fm.frozen_predictor(definition, table)
        worst = 0.0
        for d in dev[-5:]:
            r = fh.frame_rows(frames[d])
            worst = max(worst, float(np.max(np.abs(cand.predict(r) - frozen.predict(r)))))
        fm.check_columns(frozen, definition)
    except ValueError as e:
        print(f"Not frozen: {e}")
        return 1
    if worst != 0.0:
        print(f"Not frozen: the definition does not reproduce the fitted model (largest difference {worst:.3g})")
        return 1
    version = f"{args.name}_{args.candidate}_frozen"
    record = defs._record(version, "fan_model", definition)
    why = fx.why_sealed(exp, record)
    if why:
        print(f"Not frozen: {why}")
        return 1
    os.makedirs(FREEZE_DIR, exist_ok=True)
    path = os.path.join(FREEZE_DIR, f"{version}.json")
    with open(path, "w") as fh_:
        json.dump(record, fh_, indent=1)
    h15 = definition["fitted"][str(fx.PRIMARY_HORIZON)]
    top = sorted(zip(h15["columns"], h15["coef"]), key=lambda x: -abs(x[1]))[:8]
    print(f"{version} - definition hash {record['definition_hash'][:16]}")
    print(f"  {args.candidate}: trained once on {len(dev)} development sessions ({dev[0]} to {dev[-1]}), "
          f"{len(rows):,} rows; features {len(definition['features'])} (hash {definition['features_hash']})")
    print(f"  baseline {baseline}; source {src['commit'][:12]}{' +dirty' if src['dirty'] else ''} (snapshot "
          f"{src['snapshot']}); data {provenance['data']['fingerprint']}")
    print(f"  the definition alone reproduces the fitted model on the last 5 development sessions (difference 0)")
    print(f"  {fx.PRIMARY_HORIZON}-minute coefficients on standardised columns, largest first: "
          + ", ".join(f"{c} {v:+.3f}" for c, v in top))
    print(f"  written to {os.path.relpath(path, _PROJECT_ROOT)}")
    if args.dry_run:
        print("Dry run: nothing registered - the holdout stays sealed.")
        return 0
    store.register_version(conn, record)
    print(f"Registered {version}: {args.name}'s holdout is open for this model alone. Score it once with "
          f"scripts/fan.py holdout.")
    return 0


def cmd_holdout(conn, args):
    """Chunk 7, part two: the frozen model against the baseline on the holdout's sessions - once, from its stored
    definition, never refitted; the result stored in the journal. With --rehearse (and a freeze dry run's
    --definition) the identical pipeline runs on the last development sessions instead, storing nothing: a check of
    the code path before the one real run (in sample, so no evidence about the model)."""
    exp = fx.load_experiment(conn, args.name)
    m = exp["definition"]
    target = m["targets"]["primary"]
    if args.rehearse:
        if not args.definition:
            print("--rehearse needs --definition: the JSON a freeze dry run wrote")
            return 1
        with open(args.definition) as fh_:
            record = json.load(fh_)
        why = fx.why_sealed(exp, record)
        if why:
            print(f"Not rehearsed: {why}")
            return 1
        days = m["split"]["development"]["sessions"][-m["split"]["holdout"]["count"]:]
    else:
        record = fx.frozen_model(conn, exp)
        if record is None:
            print(f"{args.name}'s holdout is sealed: freeze a model first (scripts/fan.py freeze)")
            return 1
        done = [r for r in store.experiment_results(conn, args.name) if r["results"].get("kind") == "holdout"]
        if done:
            print(f"Already scored {str(done[-1]['computed_at'])[:19]} UTC (result {done[-1]['result_id'][:8]}): "
                  f"{done[-1]['results']['verdict']}. The holdout is scored once.")
            return 0
        days = fx.holdout_sessions(conn, args.name, record["version"])
    definition = record["definition"]
    if definition["feature_format"] != ff.FEATURE_FORMAT or definition["frame_format"] != fh.FRAME_FORMAT:
        print(f"Not scored: the model was frozen with feature format {definition['feature_format']} and frame format "
              f"{definition['frame_format']}; the code computes {ff.FEATURE_FORMAT} and {fh.FRAME_FORMAT}")
        return 1
    started = time.time()
    baseline = definition["baseline"]["version"]
    d0, d1 = date.fromisoformat(days[0]), date.fromisoformat(days[-1])
    loaded = load_days(conn, target, history_start(d0, F.EVENT_SESSIONS + F.SHAPE_SESSIONS), d1)
    frames = fh.baseline_frames(loaded, d0, d1, baseline)
    symbols, first_complete = _included(m)
    sessions = [s.session_date.isoformat() for s in cal.sessions_between(
        date.fromisoformat(m["split"]["window"]["start"]), d1)]
    table = ff.build(fp.load_panel(conn, sessions, symbols), target, first_complete)
    predictor = fm.frozen_predictor(definition, table)
    res = fh.score_fixed(frames, days, predictor, m, target)
    fm.check_columns(predictor, definition)
    res.update(kind="holdout_rehearsal" if args.rehearse else "holdout", experiment=args.name,
               experiment_hash=exp["definition_hash"], model=record["version"],
               model_hash=record["definition_hash"], baseline=baseline, code_revision=code_revision(),
               source=source_snapshot(), data=fp.data_snapshot(conn, symbols, days[-1]))
    p = res["results"][res["primary"]]
    print(f"{record['version']} against {baseline} on {len(res['sessions']['scored'])} "
          f"{'development (rehearsal, in sample)' if args.rehearse else 'holdout'} sessions ({time.time() - started:.0f} s):"
          f" {res['primary']} {100 * p['diff_share']:+.2f} %, interval [{p['interval'][0]:+.5f}, "
          f"{p['interval'][1]:+.5f}] bps - {res['verdict']}")
    title = (f"Holdout rehearsal (in sample - not evidence): {record['version']}" if args.rehearse else
             f"Holdout: {record['version']} against {baseline} ({args.name}, {target})")
    preamble = [f"Model `{record['version']}` (definition `{record['definition_hash'][:16]}`), frozen on "
                f"{len(definition['training_sessions'])} development sessions; experiment `{exp['definition_hash'][:16]}`; "
                f"code {res['code_revision'][:12]} (source snapshot `{res['source']['snapshot']}`); data "
                f"`{res['data']['fingerprint']}`.", "",
                f"**Rule** (fixed before any result): {definition['acceptance']['pass']}; "
                f"{definition['acceptance']['inconclusive']}; {definition['acceptance']['fail']}."]
    if args.rehearse:
        path = os.path.join(FREEZE_DIR, f"{record['version']}_rehearsal.md")
        print(f"Rehearsal report: {fh.write_fixed_report(res, path, title, preamble)} - nothing stored")
        return 0
    result_id = store.save_experiment_result(conn, args.name, res, res["code_revision"])
    path = os.path.join(args.report_dir, f"fan_holdout_{args.name}_{target}.md")
    print(f"Stored as result {result_id[:8]}. Report: {fh.write_fixed_report(res, path, title, preamble)}")
    return 0


FORWARD_CLIENT_OFFSET = 2         # the forward record's IB client id: IB_CLIENT_ID + 2 (scripts/fan_forward.sh's too)


def _forward_mark(conn, args, exp, model) -> int:
    """The mark trigger (scripts/fan_forward.sh mark, at each mark): collect the model's instruments until the
    origin bar of the mark due now is stored or the live deadline nears (fan_forward.ISSUE_RESERVE before it), then
    issue that mark at once - the live attempt - recording when it was triggered, each collection and the
    computation. Outside a mark's first minute it does nothing."""
    import subprocess
    from datetime import timedelta
    started = datetime.now(timezone.utc)
    triggered = datetime.fromisoformat(args.triggered_at) if args.triggered_at else started
    mark = fwd.mark_due(started)
    if mark is None:
        print(f"No mark due at {started.astimezone(cal.NY_TZ):%H:%M:%S} ET: the mark trigger acts within "
              f"{fwd.MARK_WINDOW.total_seconds():.0f} s of a mark")
        return 0
    target = exp["definition"]["targets"]["primary"]
    symbols = [target] + [s for s in fwd._instruments(model["definition"]) if s != target]
    cmd = [sys.executable, "-m", "collector.ib_collector", "--days", "0", "--no-trailing-refresh",
           "--workers", str(len(symbols)), "--symbol", ",".join(symbols),
           "--host", str(Config.IB_HOST), "--port", str(Config.IB_PORT),
           "--client-id", str(Config.IB_CLIENT_ID + FORWARD_CLIENT_OFFSET)]
    clock = lambda: datetime.now(timezone.utc)
    deadline = mark.at + timedelta(seconds=fwd.LIVE_SECONDS) - fwd.ISSUE_RESERVE
    sys.stdout.flush()
    collections = fwd.collect_until(lambda: subprocess.run(cmd, cwd=_PROJECT_ROOT).returncode,
                                    lambda: fwd.origin_stored(conn, target, mark), deadline, clock,
                                    lambda: time.sleep(fwd.COLLECT_PAUSE_S))
    trigger = {"kind": "mark", "triggered_at": triggered.isoformat(), "collected_at": collections[-1]["end"],
               "collections": collections}
    (res,) = fwd.issue(conn, model, exp, clock(), code_revision(), CACHE_DIR, trigger=trigger, marks=[mark])
    after = lambda t: (t - mark.at).total_seconds()
    line = (f"{res['status']}: the mark {_et(mark.at)}{' (the 09:29 cutoff)' if mark.cutoff else ''} - triggered "
            f"{after(triggered):+.1f} s, {len(collections)} collection(s) to {after(datetime.fromisoformat(collections[-1]['end'])):+.1f} s, "
            f"origin bar {'stored' if collections[-1]['stored'] else 'not stored'}")
    if res["status"] == "issued":
        at = conn.execute("SELECT extract(epoch FROM recorded_at) FROM journal.fan_forward_issues WHERE issue_id = %s;",
                          (res["issue"]["issue_id"],)).fetchone()[0]
        lat = float(at) - mark.at.timestamp()
        line += f", recorded {lat:+.1f} s - {'within' if lat <= fwd.LIVE_SECONDS else 'past'} the {fwd.LIVE_SECONDS} s deadline"
    print(line)
    return 1 if res["status"] == "failed" else 0


def cmd_forward(conn, args):
    """Chunk 8, the forward record (forecaster/fan_forward.py): issue the frozen model's forecast for the pending
    marks, the mark trigger's live attempt at the mark due now, score the issues of final sessions, or report the
    record so far."""
    exp = fx.load_experiment(conn, args.name)
    model = fx.frozen_model(conn, exp)
    if model is None:
        print(f"{args.name} has no frozen model: nothing to record")
        return 1
    if args.action == "mark":
        return _forward_mark(conn, args, exp, model)
    if args.action == "issue":
        if args.at and not args.dry_run:
            print("--at reads the store as of a past moment: only with --dry-run (the record is prospective)")
            return 1
        now = (cal.NY_TZ.localize(datetime.strptime(args.at, "%Y-%m-%d %H:%M:%S")).astimezone(timezone.utc)
               if args.at else datetime.now(timezone.utc))
        trigger = {"kind": args.trigger, "triggered_at": args.triggered_at or datetime.now(timezone.utc).isoformat(),
                   **({"collected_at": args.collected_at} if args.collected_at else {})}
        results = fwd.issue(conn, model, exp, now, code_revision(), CACHE_DIR, dry_run=args.dry_run, trigger=trigger)
        if not results:
            print(f"No mark pending: none in the last {fwd.GRACE.total_seconds() / 60:.0f} minutes (a session's first "
                  f"mark is 18:{fwd.MARK_MINUTES:02d} ET; there are none between sessions)")
            return 0
        lo, hi = fwd.CHART_LEVELS.index(0.05), fwd.CHART_LEVELS.index(0.95)
        failed = False
        for res in results:
            mark = res["mark"]
            if res["status"] == "no rules":
                print("No forward-record rules registered: run scripts/fan.py forward define first")
                return 1
            print(f"{res['status']}: the mark {_et(mark.at)} ({mark.session}, origin slot {mark.origin_slot}"
                  + (", the 09:29 cutoff" if mark.cutoff else "") + ")")
            failed |= res["status"] == "failed"
            rec = res.get("issue")
            if rec:
                print(f"  origin price {rec['origin_price']:.2f}; shape {rec['shape_id'][:12]}; inputs at the origin: "
                      + ", ".join(f"{s} {v['age_minutes']:.0f} min old" for s, v in rec["inputs"]["instruments"].items()
                                  if v["age_minutes"] is not None))
                for h, f in rec["forecast"].items():
                    print(f"  {h:>4} min  sigma {f['sigma'] * 1e4:6.2f} bps  multiplier {f['multiplier']:.3f}  90 %: "
                          f"v2 {f['base'][lo]:.2f}-{f['base'][hi]:.2f}, model {f['model'][lo]:.2f}-{f['model'][hi]:.2f}")
        return 1 if failed else 0
    if args.action == "score":
        done = fwd.score(conn, model, code_revision())
        print(f"Scored {done['scored']} issue(s); {done['waiting']} wait for their session to be final")
        return 0
    if args.action == "define":
        version, new = fwd.define(conn, model)
        print(f"{version}: {'registered' if new else 'already registered'} - the rules every issue from now on is made "
              f"and judged under (live: issued within {fwd.LIVE_SECONDS} s of its mark; delayed origin: at least "
              f"{fwd.REMAINING_SHARE:.0%} of a horizon left)")
        return 0
    s = fwd.summary(conn, model)
    for g in s["versions"]:
        print(f"{g['version'] or 'issues made under no rules (legacy)'}:")
        o = g["operations"]
        if o:
            print(f"  marks expected {o['expected']} ({o['sessions_expected']} session(s)), attempted {o['attempted']}, "
                  f"issued {o['issued_expected']}, stale {o['stale']}, failed {o['failed']}, missed {o['missed']}")
        for r in g["horizons"]:
            p = r["paired"]
            print(f"  {r['horizon']:>4} min {r['class']:15} issued {r['issued']:>4} scored {r['scored']:>4} "
                  + (f"v2 {p['base_crps_bps']:.4f} model {p['other_crps_bps']:.4f} ({100 * (p['diff_share'] or 0):+.2f} %)"
                     if p else ""))
        for t in g.get("timing", []):
            print(f"  timing, {fwd.TRIGGER_TEXT.get(t['trigger'], t['trigger'])}: attempted {t['attempted']}, issued "
                  f"{t['issued']}, stale {t['stale']}, failed {t['failed']}"
                  + (f", within {t['deadline_s']:.0f} s of the mark {t['within_deadline']}"
                     if t["deadline_s"] is not None else "")
                  + f"; issued after {fwd.spread(t['latency_s'])} s (median / p90 / max), origin bar stored after "
                    f"{fwd.spread(t['arrival_s'])} s")
    if not args.no_report:
        path = os.path.join(args.report_dir, f"fan_forward_{s['model']}.md")
        meta = {"code_revision": code_revision(), "as_of": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")}
        print(f"Report: {fwd.write_report(s, meta, path)}")
    return 0


def _run_path(name, target, candidate):
    return os.path.join(CACHE_DIR, "checks", f"{name}_{target}_{candidate}.json")


def cmd_compare(conn, args):
    """Two stored checks runs paired on identical sessions and origins: B minus A per horizon (development)."""
    m = fx.load_experiment(conn, args.name)["definition"]
    target = args.target or m["targets"]["primary"]
    runs = []
    for c in (args.a, args.b):
        path = _run_path(args.name, target, c)
        if not os.path.exists(path):
            print(f"No stored run of {c}: run scripts/fan.py checks --candidate {c} first")
            return 1
        with open(path) as fh_:
            runs.append(json.load(fh_))
    try:
        res = fh.compare_runs(*runs)
    except ValueError as e:
        print(f"Not comparable: {e}")
        return 1
    print(f"{args.b} minus {args.a}, {res['sessions']} check sessions ({target}); negative: {args.b} better")
    for k, p in res["results"].items():
        if p is None:
            continue
        iv = p["interval"]
        print(f"{k:16} {p['base_crps_bps']:>9.4f} {p['other_crps_bps']:>9.4f} {p['diff_bps']:>+11.5f} "
              f"{100 * (p['diff_share'] or 0):>+7.2f}%  " + (f"[{iv[0]:+.5f}, {iv[1]:+.5f}]" if iv else "-")
              + f"  {res['verdicts'][k]}")
    return 0


def load_features(conn, m, target):
    """The feature table (forecaster/fan_features.py) of every open session from the experiment's window start to
    the end of development - never the holdout."""
    dev = m["split"]["development"]["sessions"]
    fx.guard(conn, m["split"]["window"]["start"], dev[-1])
    avail = m["instruments"]["availability"]
    symbols = [s for s, a in avail.items() if a["status"] == "included"]
    sessions = [s.session_date.isoformat() for s in cal.sessions_between(
        date.fromisoformat(m["split"]["window"]["start"]), date.fromisoformat(dev[-1]))]
    panel = fp.load_panel(conn, sessions, symbols)
    return ff.build(panel, target, {s: avail[s]["first_complete"] for s in symbols})


def cmd_features(conn, args):
    """Chunk 4: the features on the development sessions, audited - each feature's coverage and its rank correlation
    with the baseline's error size, on the development sessions before the first check."""
    try:
        exp = fx.load_experiment(conn, args.name)
    except ValueError as e:
        print(f"{e}: register it first (experiment-register)")
        return 1
    m = exp["definition"]
    target = args.target or m["targets"]["primary"]
    started = time.time()
    try:
        _, _, frames = fh.load_frames(conn, args.name, target, CACHE_DIR)
    except ValueError as e:
        print(f"Not run: {e}")
        return 1
    table = load_features(conn, m, target)
    first_check = m["split"]["checks"]["blocks"][0]["sessions"]["first"]
    days = [d for d in m["split"]["development"]["sessions"] if d < first_check and d in frames]
    rows = fh.Rows.concat([fh.frame_rows(frames[d], horizons=(args.horizon,), every=fh.TRAIN_EVERY) for d in days])
    rows = rows.take(rows.horizon == args.horizon)
    result = ff.audit(table, rows)
    print(f"{len(result)} features ({len(table.features)} from {len(set(f.instrument for f in table.features))} "
          f"instruments) on {len(table.sessions)} sessions, audited on {len(days)} sessions before {first_check} ({len(rows):,} rows, {args.horizon} min) in "
          f"{time.time() - started:.0f} s")
    print(f"{'feature':22} {'group':14} {'coverage':>8} {'rank corr':>9}")
    for r in sorted(result, key=lambda r: -abs(r["spearman"] or 0))[:args.top]:
        print(f"{r['name']:22} {r['group']:14} {100 * r['coverage']:>7.1f}% "
              + (f"{r['spearman']:>+9.3f}" if r["spearman"] is not None else f"{'-':>9}"))
    if not args.no_report:
        meta = {"experiment": args.name, "target": target, "format": ff.FEATURE_FORMAT,
                "code_revision": code_revision(), "sessions": len(days), "first": days[0], "last": days[-1],
                "every": fh.TRAIN_EVERY, "horizon": args.horizon, "rows": len(rows)}
        print(f"Report: {ff.write_audit(result, meta, args.report_dir)}")
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
    p = sub.add_parser("checks", help="A candidate against the decided baseline on the three checks (development)")
    p.add_argument("--name", default=fx.EXPERIMENT_NAME)
    p.add_argument("--candidate", choices=sorted(fh.CANDIDATES) + sorted(fm.MODELS) + sorted(fm.TRIALS),
                   required=True)
    p.add_argument("--target", help="The primary target by default; a secondary one (ES, RTY) by name")
    p.add_argument("--refresh", action="store_true", help="Recompute the baseline's cached frames "
                   "(after a backfill revised development sessions)")
    p.add_argument("--report-dir", default=REPORT_DIR)
    p.add_argument("--no-report", action="store_true", help="Print the summary only")
    p = sub.add_parser("forward", help="The forward record (chunk 8): issue, mark, score or report")
    p.add_argument("action", choices=("define", "issue", "mark", "score", "report"))
    p.add_argument("--name", default=fx.EXPERIMENT_NAME)
    p.add_argument("--dry-run", action="store_true", help="issue: build and print, write nothing")
    p.add_argument("--at", help="issue --dry-run: as of this ET time 'YYYY-MM-DD HH:MM:SS' instead of now")
    p.add_argument("--trigger", default="manual", choices=[t for t in fwd.TRIGGERS if t not in ("mark", "unrecorded")],
                   help="issue: what ran it, recorded on its run rows (mark runs as the action 'mark')")
    p.add_argument("--triggered-at", help="issue, mark: when the run started, ISO UTC (default: now)")
    p.add_argument("--collected-at", help="issue: when its collection finished, ISO UTC")
    p.add_argument("--report-dir", default=REPORT_DIR)
    p.add_argument("--no-report", action="store_true", help="report: print only")
    p = sub.add_parser("freeze", help="Train the chosen candidate on all of development and freeze it (chunk 7)")
    p.add_argument("--name", default=fx.EXPERIMENT_NAME)
    p.add_argument("--candidate", required=True, choices=sorted(n for n, s in fm.MODELS.items()
                                                                 if s.kind in fm.FROZEN_KINDS))
    p.add_argument("--dry-run", action="store_true", help="Write and check the definition, register nothing")
    p.add_argument("--allow-dirty", action="store_true", help="Freeze from uncommitted code (its source archived)")
    p = sub.add_parser("holdout", help="Score the frozen model on the holdout, once (or rehearse the code path)")
    p.add_argument("--name", default=fx.EXPERIMENT_NAME)
    p.add_argument("--rehearse", action="store_true", help="Run on the last development sessions; store nothing")
    p.add_argument("--definition", help="--rehearse: a freeze dry run's JSON (data/fan_cache/freeze/)")
    p.add_argument("--report-dir", default=REPORT_DIR)
    p = sub.add_parser("replay", help="Live-style replay of the leading candidates on predefined check sessions")
    p.add_argument("--name", default=fx.EXPERIMENT_NAME)
    p.add_argument("--target", help="The primary target by default")
    p.add_argument("--candidate", action="append", default=None, help="Repeat; default lin_pois_ivx and gbm_own_ivx")
    p.add_argument("--report-dir", default=REPORT_DIR)
    p.add_argument("--no-report", action="store_true", help="Print the summary only")
    p = sub.add_parser("search", help="Multiple-comparison review of the stored checks runs (SPA, StepM)")
    p.add_argument("--name", default=fx.EXPERIMENT_NAME)
    p.add_argument("--target", help="The primary target by default")
    p.add_argument("--horizon", type=int, default=fx.PRIMARY_HORIZON)
    p.add_argument("--report-dir", default=REPORT_DIR)
    p.add_argument("--no-report", action="store_true", help="Print the summary only")
    p = sub.add_parser("compare", help="Two stored checks runs paired on identical sessions (B minus A)")
    p.add_argument("--name", default=fx.EXPERIMENT_NAME)
    p.add_argument("--target", help="The primary target by default")
    p.add_argument("a", help="The reference candidate")
    p.add_argument("b", help="The candidate compared with it")
    p = sub.add_parser("features", help="The features on the development sessions, audited")
    p.add_argument("--name", default=fx.EXPERIMENT_NAME)
    p.add_argument("--target", help="The primary target by default")
    p.add_argument("--horizon", type=int, default=fx.PRIMARY_HORIZON, help="Minutes ahead the audit reads")
    p.add_argument("--top", type=int, default=25, help="Print this many features, strongest first")
    p.add_argument("--report-dir", default=REPORT_DIR)
    p.add_argument("--no-report", action="store_true", help="Print the summary only")
    p = sub.add_parser("panel-audit", help="Audit the point-in-time panel of an experiment's development sessions")
    p.add_argument("--name", default=fx.EXPERIMENT_NAME)
    p.add_argument("--report-dir", default=REPORT_DIR)
    p.add_argument("--no-report", action="store_true", help="Print the summary only")
    args = parser.parse_args(argv)
    if args.command == "replay" and not args.candidate:
        args.candidate = ["lin_pois_ivx", "gbm_own_ivx"]

    init_database(args.db)
    conn = get_db_connection(args.db)
    try:
        if args.command != "register":
            store.register_version(conn, F.fan_record())
        return {"register": cmd_register, "now": cmd_now, "score": cmd_score,
                "experiment-register": cmd_experiment_register, "experiment-show": cmd_experiment_show,
                "panel-audit": cmd_panel_audit, "baseline-gate": cmd_baseline_gate, "checks": cmd_checks,
                "features": cmd_features, "compare": cmd_compare, "search": cmd_search, "replay": cmd_replay,
                "freeze": cmd_freeze, "holdout": cmd_holdout, "forward": cmd_forward}[args.command](conn, args)
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
