# forecaster/fan_forward.py
"""
The forward record of the intermarket fan experiment (docs/fan_experiment.md, chunk 8):
the frozen fan_model and its baseline issued live - every 15 minutes of the trading day
and at the 09:29 ET cutoff - from what the store holds at that moment, recorded
append-only with everything the forecast read, and scored once each session is final.
It is the untouched evidence that comes after the holdout, and the only check on what a
historical replay cannot see: a bar read live may be revised later.

  mark_at(now)                 the issue time at or just before ``now``: every MARK_MINUTES from
                               the 18:00 ET open, and 09:29 ET (which adds the pre-open slice's
                               16, 31 and 61 minutes); its session and origin - the bar that
                               closes at the mark
  v2_shape(conn, target, day)  the baseline's standardised shape for a session: the symmetric
                               quantiles of its errors over the SHAPE_SESSIONS sessions before,
                               each session's errors cached once it is complete
  build_issue(...)             one forecast from the inputs as read (pure): every horizon's
                               sigma, the frozen model's multiplier and both fans' quantiles
  pending_marks(now)           every mark of the last GRACE, oldest first
  issue(conn, model, now)      for each pending mark not yet issued: reads the store as of the
                               mark, builds and records the issue - or a run row saying why not:
                               stale (its origin bar is not stored yet - once per mark), failed.
                               The feed here is delayed, so a mark is usually issued one run
                               after it; recorded_at shows each issue's latency
  score_issue(...)             one issue against the session's final prices (pure)
  score(conn, model)           every issue of a final session not yet scored
  summary(conn, model)         per horizon, the paired comparison over the sessions scored
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

from contracts import fan as F
from features import calendar as cal
from features.session_windows import get_trading_day_date
from forecaster import fan_benchmark as fb
from forecaster import fan_features as ff
from forecaster import fan_harness as fh
from forecaster import fan_model as fm
from forecaster import fan_panel as fp
from forecaster import fan_v2
from forecaster.fan_benchmark import DAY_SLOTS, Day, InsufficientHistory, day_start, end_slot, slot_instant, slot_of_time
from forecaster.fan_data import history_start, load_days
from forecaster.fan_scoring import PHASE_OF_SLOT

MARK_MINUTES = 15
CUTOFF_ET = time(9, 29)                  # the P1 cutoff: its mark issues the pre-open slice too
# The IB feed this runs on is delayed (2026-10-06: NQ's bars reach the store about 11 minutes late, VXN's about 16):
# a mark is issued once its origin bar is stored, within GRACE of the mark, from the bars closed by the mark only.
GRACE = timedelta(minutes=30)
HISTORY_SESSIONS = 35                    # the sessions before an issue's that its panel holds (the replay's)
CHART_LEVELS = F.QUANTILES               # the quantiles stored for the chart; the CRPS reads the shape's 200


@dataclass(frozen=True)
class Mark:
    at: datetime                         # UTC: the issue time - the origin bar closes then
    session: date
    origin_slot: int
    cutoff: bool                         # the 09:29 ET mark


def mark_at(now: datetime) -> Optional[Mark]:
    """The latest issue time at or before ``now`` inside the trading day in progress (18:00 ET to the futures' day
    end), with the origin bar that closes at it; None between sessions, on a closed day, or before the first mark."""
    now = now.astimezone(timezone.utc)
    day = date.fromisoformat(get_trading_day_date(now.astimezone(cal.NY_TZ)))
    try:
        s = cal.session(day)
    except cal.CalendarCoverageError:
        return None
    if not s.is_open:
        return None
    end = end_slot("FUT", s.schedule)
    minute = int((now - day_start(day)).total_seconds() // 60)
    if minute < MARK_MINUTES or minute >= end:
        return None
    cutoff = slot_of_time(CUTOFF_ET)
    candidates = [minute - minute % MARK_MINUTES] + ([cutoff] if cutoff <= minute else [])
    slot = max(candidates)                                  # below ``end``: the origin bar closes inside the day
    return Mark(slot_instant(day, slot), day, slot - 1, slot == cutoff)


# --------------------------------------------------------------------------
# The baseline's shape, session by session
# --------------------------------------------------------------------------

def _errors_path(cache_dir: str, target: str, d: date) -> str:
    key = F.fan_v2_record()["definition_hash"][:16]
    return os.path.join(cache_dir, "v2_errors", f"{target}_{d.isoformat()}_{key}_{fh.FRAME_FORMAT}.npz")


def shape_from_history(days: Sequence[Day], cache_dir: Optional[str] = None, target: str = "") -> np.ndarray:
    """(FRAME_HORIZONS, K): the shape fan_rw_v2 issues for the session after ``days`` - walk_forward's: the
    symmetric quantiles of the standardised errors of the last SHAPE_SESSIONS complete full sessions, each on its own
    fit from the sessions before it. A complete session's errors are cached under ``cache_dir``."""
    ordered = sorted(days, key=lambda d: d.session_date)
    usable = [i for i, d in enumerate(ordered) if d.complete and d.schedule == "full"]
    errs: List[Dict[int, np.ndarray]] = []
    for i in reversed(usable):
        if len(errs) == F.SHAPE_SESSIONS:
            break
        d = ordered[i]
        path = _errors_path(cache_dir, target, d.session_date) if cache_dir else None
        if path and os.path.exists(path):
            with np.load(path) as z:
                errs.append({int(k[1:]): z[k] for k in z.files})
            continue
        try:
            e = fan_v2.errors(fan_v2.fit(d, ordered[:i]), d)
        except InsufficientHistory:
            break                                           # walk_forward has no error for it, nor for any before
        if path:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            np.savez(path, **{f"h{h}": v for h, v in e.items()})
        errs.append(e)
    return fan_v2.shape_from(list(reversed(errs))).at(np.array(fh.FRAME_HORIZONS))


def v2_shape(conn, target: str, session: date, cache_dir: Optional[str] = None) -> np.ndarray:
    """The shape fan_rw_v2 issues for ``session``, from the sessions the store holds before it."""
    before = cal.previous_session(session).session_date
    days = load_days(conn, target, history_start(session, F.EVENT_SESSIONS + F.SHAPE_SESSIONS), before)
    return shape_from_history(days, cache_dir, target)


def shape_id(baseline_hash: str, target: str, session: date, Q: np.ndarray) -> str:
    h = hashlib.sha256(f"{baseline_hash}|{target}|{session.isoformat()}|".encode())
    h.update(np.ascontiguousarray(Q, dtype=np.float64).tobytes())
    return h.hexdigest()[:24]


# --------------------------------------------------------------------------
# One issue (pure)
# --------------------------------------------------------------------------

def horizons_for(mark: Mark) -> List[int]:
    return list(fh.REPORT_HORIZONS) + (list(fh.PRE_OPEN_MINUTES) if mark.cutoff else [])


def build_issue(mark: Mark, day: Day, V: np.ndarray, Q: np.ndarray, table: ff.FeatureTable,
                definition: Dict[str, Any], release_slots: Sequence[int]) -> Dict[str, Any]:
    """
    The forecast from ``mark``'s origin: ``day`` the session as read (bars to the origin), ``V`` fan_rw_v2's variance
    from the origin to each of FRAME_HORIZONS, ``Q`` its shape, ``table`` the features as read, ``definition`` the
    frozen model's. Every horizon ending inside the trading day with a positive variance: the baseline's sigma, the
    model's multiplier, both fans' quantiles at CHART_LEVELS. Raises ValueError when the origin bar was not read.
    """
    t = mark.origin_slot
    if not np.isfinite(day.closes[t]):
        raise ValueError(f"no {mark.session} bar at the origin slot {t}: the store is behind")
    price = float(day.closes[t])
    hs = [h for h in horizons_for(mark) if t + h < day.end]
    idx = np.array([fh.FRAME_HORIZONS.index(h) for h in hs], dtype=int)
    ok = np.isfinite(V[idx]) & (V[idx] > fan_v2.MIN_VARIANCE)
    hs, idx = [h for h, o in zip(hs, ok) if o], idx[ok]
    if not hs:
        raise ValueError("no horizon left to forecast")
    n = len(hs)
    ahead = np.array([bool(fh._ahead(release_slots, h)[t]) for h in hs])
    q75 = np.array([float(np.interp(0.75, fh.TAU, Q[i])) for i in idx])
    rows = fh.Rows(np.full(n, np.datetime64(mark.session.isoformat(), "D")), np.full(n, t), np.array(hs),
                   np.full(n, PHASE_OF_SLOT[t]), ahead, V[idx], np.full(n, np.nan), q75)
    model = fm.frozen_predictor(definition, table)
    mult = model.predict(rows)
    fm.check_columns(model, definition)
    X, feats = table.matrix(rows, lambda f: f.name in set(definition["features"]))
    inst = {f.name: (None if not np.isfinite(X[0, j]) else float(X[0, j]))
            for j, f in enumerate(feats) if f.instrument is not None}
    levels = np.array(CHART_LEVELS)
    forecast = {}
    for k, (h, i) in enumerate(zip(hs, idx)):
        sigma = float(np.sqrt(V[i]))
        z = np.interp(levels, fh.TAU, Q[i])
        forecast[str(h)] = {"sigma": sigma, "multiplier": float(mult[k]), "release_ahead": bool(ahead[k]),
                            "base": [float(price * np.exp(sigma * x)) for x in z],
                            "model": [float(price * np.exp(sigma * mult[k] * x)) for x in z]}
    return {"origin_price": price, "horizons": hs, "levels": list(CHART_LEVELS), "forecast": forecast,
            "features": inst}


def score_issue(forecast: Dict[str, Any], origin_slot: int, origin_price: float, final: Day,
                shapes: Dict[int, np.ndarray]) -> List[Dict[str, Any]]:
    """Each horizon of one issue against the session's final prices: the realised log move from the origin price as
    read, the revision of the origin bar, both CRPS (the manifest's, K = 200, basis points) and both PITs."""
    lp = np.log(final.last_price)
    revision = float(lp[origin_slot] - np.log(origin_price)) if np.isfinite(lp[origin_slot]) else float("nan")
    out = []
    for h_text, f in forecast.items():
        h = int(h_text)
        y = float(lp[origin_slot + h] - np.log(origin_price))
        if not np.isfinite(y):
            continue
        Q = shapes[h]
        sb, sm = f["sigma"], f["sigma"] * f["multiplier"]
        out.append({"horizon": h, "outcome_bar_at": slot_instant(final.session_date, origin_slot + h),
                    "realised": y, "revision": revision,
                    "crps_base_bps": float(sb * fh.crps(np.array([y / sb]), Q)[0] * 1e4),
                    "crps_model_bps": float(sm * fh.crps(np.array([y / sm]), Q)[0] * 1e4),
                    "pit_base": float(np.interp(y / sb, Q, fh.TAU, left=0.0, right=1.0)),
                    "pit_model": float(np.interp(y / sm, Q, fh.TAU, left=0.0, right=1.0))})
    return out


# --------------------------------------------------------------------------
# The store
# --------------------------------------------------------------------------

def _instruments(definition: Dict[str, Any]) -> List[str]:
    return sorted({n.split(".")[0] for n in definition["features"] if not n.startswith("base.")})


def _run(conn, model: str, mark_at_: datetime, status: str, code_revision: str, issue_id: Optional[str] = None,
         detail: Optional[Dict[str, Any]] = None) -> None:
    conn.execute("INSERT INTO journal.fan_forward_runs (run_id, model, mark_at, status, issue_id, detail, "
                 "code_revision) VALUES (%s, %s, %s, %s, %s, %s, %s);",
                 (str(uuid.uuid4()), model, mark_at_, status, issue_id, json.dumps(detail or {}), code_revision))


def _freshness(conn, panel: fp.Panel, mark: Mark) -> Dict[str, Any]:
    """Per instrument read: its last bar's close and age at the origin, and when the store fetched that day."""
    out = {}
    for j, sym in enumerate(panel.symbols):
        cid = int(panel.contract[-1, j])
        age = panel.age[-1, j, mark.origin_slot]
        row = conn.execute("SELECT fetched_at, status FROM session_days WHERE contract_id = %s AND trading_day = %s "
                           "AND interval = '1m' ORDER BY fetched_at DESC LIMIT 1;", (cid, mark.session)).fetchone()
        out[sym] = {"contract_id": cid, "close": float(panel.close[-1, j, mark.origin_slot])
                    if np.isfinite(panel.close[-1, j, mark.origin_slot]) else None,
                    "age_minutes": None if not np.isfinite(age) else float(age),
                    "store_fetched_at": None if row is None or row[0] is None else str(row[0]),
                    "store_status": None if row is None else row[1]}
    return out


def pending_marks(now: datetime, grace: timedelta = GRACE) -> List[Mark]:
    """Every mark in (now - grace, now], oldest first."""
    now = now.astimezone(timezone.utc).replace(second=0, microsecond=0)
    out: Dict[datetime, Mark] = {}
    for k in range(int(grace.total_seconds() // 60)):
        m = mark_at(now - timedelta(minutes=k))
        if m is not None and m.at > now - grace:
            out[m.at] = m
    return [out[t] for t in sorted(out)]


def issue(conn, model: Dict[str, Any], experiment: Dict[str, Any], now: datetime, code_revision: str,
          cache_dir: Optional[str] = None, dry_run: bool = False) -> List[Dict[str, Any]]:
    """Issues the frozen ``model``'s forecast for every pending mark not yet issued (see the module docstring); one
    result per mark tried - {'status', 'mark', 'issue' (the record, when built)}. Nothing is written with
    ``dry_run``."""
    return [_issue_mark(conn, model, experiment, mark, code_revision, cache_dir, dry_run)
            for mark in pending_marks(now)]


def _issue_mark(conn, model: Dict[str, Any], experiment: Dict[str, Any], mark: Mark, code_revision: str,
                cache_dir: Optional[str], dry_run: bool) -> Dict[str, Any]:
    definition, version = model["definition"], model["version"]
    m = experiment["definition"]
    target = m["targets"]["primary"]
    result: Dict[str, Any] = {"mark": mark}

    def record(status: str, issue_id: Optional[str] = None, detail: Optional[Dict[str, Any]] = None):
        result["status"] = status
        if not dry_run:
            with conn:
                _run(conn, version, mark.at, status, code_revision, issue_id, detail)
        return result

    exists = conn.execute("SELECT issue_id FROM journal.fan_forward_issues WHERE model = %s AND target = %s AND "
                          "session_date = %s AND origin_slot = %s;",
                          (version, target, mark.session, mark.origin_slot)).fetchone()
    if exists:
        result.update(status="issued before", issue_id=str(exists[0]))
        return result
    try:
        days = load_days(conn, target, history_start(mark.session, F.EVENT_SESSIONS), mark.session, as_of=mark.at)
        if not days or days[-1].session_date != mark.session or not np.isfinite(days[-1].closes[mark.origin_slot]):
            last = None
            if days and days[-1].session_date == mark.session:
                seen = np.flatnonzero(np.isfinite(days[-1].closes))
                last = int(seen[-1]) if len(seen) else None
            said = conn.execute("SELECT 1 FROM journal.fan_forward_runs WHERE model = %s AND mark_at = %s AND "
                                "status = 'stale' LIMIT 1;", (version, mark.at)).fetchone()
            if said:                                         # said once: the next attempt may find it stored
                result["status"] = "stale (again)"
                return result
            return record("stale", detail={"origin_slot": mark.origin_slot, "last_slot_read": last})
        day = days[-1]
        baseline_hash = definition["baseline"]["definition_hash"]
        vmodel = fan_v2.fit(day, days[:-1])
        V = fb.horizon_variances(vmodel, day.returns, fh.FRAME_HORIZONS)["full"][:, mark.origin_slot]
        Q = v2_shape(conn, target, mark.session, cache_dir)
        sid = shape_id(baseline_hash, target, mark.session, Q)
        symbols = _instruments(definition)
        avail = m["instruments"]["availability"]
        window = [s.session_date.isoformat() for s in cal.sessions_before(mark.session, HISTORY_SESSIONS)]
        panel = fp.load_panel(conn, window + [mark.session.isoformat()], symbols, as_of=mark.at)
        table = ff.build(panel, target, {s: avail[s]["first_complete"] for s in symbols})
        built = build_issue(mark, day, V, Q, table, definition, [slot for _, _, slot in fan_v2.placed(day)])
    except Exception as e:                                   # recorded, never silent
        return record("failed", detail={"error": f"{type(e).__name__}: {e}"})
    issue_id = str(uuid.uuid4())
    inputs = {"features": built["features"], "variance": {str(h): float(V[fh.FRAME_HORIZONS.index(h)])
                                                          for h in built["horizons"]},
              "instruments": _freshness(conn, panel, mark), "releases_known": day.releases_known,
              "feature_format": ff.FEATURE_FORMAT, "frame_format": fh.FRAME_FORMAT}
    result["issue"] = {"issue_id": issue_id, "origin_price": built["origin_price"], "inputs": inputs,
                       "forecast": built["forecast"], "shape_id": sid}
    if dry_run:
        result["status"] = "issued (dry run)"
        return result
    with conn:
        conn.execute("INSERT INTO journal.fan_forward_shapes (shape_id, baseline, target, session_date, horizons, "
                     "quantiles) VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (shape_id) DO NOTHING;",
                     (sid, definition["baseline"]["version"], target, mark.session, list(fh.FRAME_HORIZONS),
                      json.dumps({str(h): [float(x) for x in Q[i]] for i, h in enumerate(fh.FRAME_HORIZONS)})))
        conn.execute("INSERT INTO journal.fan_forward_issues (issue_id, model, baseline, target, session_date, "
                     "origin_slot, origin_bar_at, mark_at, origin_price, shape_id, inputs, forecast, code_revision) "
                     "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s);",
                     (issue_id, version, definition["baseline"]["version"], target, mark.session, mark.origin_slot,
                      slot_instant(mark.session, mark.origin_slot), mark.at, built["origin_price"], sid,
                      json.dumps(inputs), json.dumps(built["forecast"]), code_revision))
        _run(conn, version, mark.at, "issued", code_revision, issue_id)
    result["status"] = "issued"
    return result


def score(conn, model: Dict[str, Any], code_revision: str) -> Dict[str, int]:
    """Scores every issue of ``model`` whose session the store now holds complete, horizon by horizon, once."""
    version = model["version"]
    pending = conn.execute(
        "SELECT i.issue_id, i.target, i.session_date, i.origin_slot, i.origin_price, i.forecast, i.shape_id "
        "FROM journal.fan_forward_issues i WHERE i.model = %s AND NOT EXISTS (SELECT 1 FROM "
        "journal.fan_forward_scores s WHERE s.issue_id = i.issue_id) ORDER BY i.session_date, i.origin_slot;",
        (version,)).fetchall()
    done = {"scored": 0, "waiting": 0}
    finals: Dict[Tuple[str, date], Optional[Day]] = {}
    shapes: Dict[str, Dict[int, np.ndarray]] = {}
    for issue_id, target, session, slot, price, forecast, sid in pending:
        session = date.fromisoformat(str(session)[:10])
        key = (target, session)
        if key not in finals:
            days = load_days(conn, target, session, session)
            finals[key] = days[0] if days and days[0].complete else None
        final = finals[key]
        if final is None:
            done["waiting"] += 1
            continue
        if sid not in shapes:
            q = conn.execute("SELECT quantiles FROM journal.fan_forward_shapes WHERE shape_id = %s;", (sid,)).fetchone()[0]
            q = json.loads(q) if isinstance(q, str) else q
            shapes[sid] = {int(h): np.array(v) for h, v in q.items()}
        forecast = json.loads(forecast) if isinstance(forecast, str) else forecast
        rows = score_issue(forecast, int(slot), float(price), final, shapes[sid])
        with conn:
            for r in rows:
                conn.execute("INSERT INTO journal.fan_forward_scores (issue_id, horizon, outcome_bar_at, realised, "
                             "revision, crps_base_bps, crps_model_bps, pit_base, pit_model, code_revision) VALUES "
                             "(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING;",
                             (str(issue_id), r["horizon"], r["outcome_bar_at"], r["realised"], r["revision"],
                              r["crps_base_bps"], r["crps_model_bps"], r["pit_base"], r["pit_model"], code_revision))
        done["scored"] += 1
    return done


def summary(conn, model: Dict[str, Any]) -> Dict[str, Any]:
    """The forward record so far: attempts by status, and per horizon the scored issues, their sessions, both mean
    CRPS, the paired comparison over sessions (the manifest's interval, once there are enough sessions), the 90 %
    bands' coverage and how often and how far the origin bar was revised."""
    version = model["version"]
    runs = dict(conn.execute("SELECT status, count(*) FROM journal.fan_forward_runs WHERE model = %s GROUP BY status;",
                             (version,)).fetchall())
    rows = conn.execute(
        "SELECT i.session_date, s.horizon, s.crps_base_bps, s.crps_model_bps, s.pit_base, s.pit_model, s.revision "
        "FROM journal.fan_forward_scores s JOIN journal.fan_forward_issues i ON i.issue_id = s.issue_id "
        "WHERE i.model = %s ORDER BY i.session_date;", (version,)).fetchall()
    per_h: Dict[int, Dict[str, List]] = {}
    for d, h, cb, cm, pb, pm, rev in rows:
        e = per_h.setdefault(int(h), {"sessions": {}, "pit_b": [], "pit_m": [], "rev": []})
        e["sessions"].setdefault(str(d)[:10], []).append((cb, cm))
        e["pit_b"].append(pb)
        e["pit_m"].append(pm)
        e["rev"].append(rev)
    horizons = {}
    for h, e in sorted(per_h.items()):
        sess = [{"x": (float(np.mean([a for a, _ in v])), float(np.mean([b for _, b in v])), len(v))}
                for _, v in sorted(e["sessions"].items())]
        pb, pm, rev = np.array(e["pit_b"]), np.array(e["pit_m"]), np.array(e["rev"])
        horizons[h] = {"issues": len(pb), "sessions": len(sess), "paired": fh.paired(sess, "x"),
                       "cover90_base": float(np.mean(np.abs(pb - 0.5) <= 0.45)),
                       "cover90_model": float(np.mean(np.abs(pm - 0.5) <= 0.45)),
                       "revised_share": float(np.mean(np.abs(rev) > 0)), "revision_max_bps": float(np.max(np.abs(rev)) * 1e4)}
    return {"model": version, "runs": runs, "horizons": horizons}


def write_report(s: Dict[str, Any], meta: Dict[str, Any], path: str) -> str:
    """The forward record so far as markdown at ``path``."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    L = [f"# Forward record: {s['model']}", "",
         f"Issued live every {MARK_MINUTES} minutes of the trading day and at 09:29 ET from what the store held at "
         "each mark (forecaster/fan_forward.py); scored once each session is final, from the origin price as read. "
         f"Code {meta['code_revision'][:12]}; as of {meta['as_of']}.", "",
         "**Attempts:** " + (", ".join(f"{k} {v}" for k, v in sorted(s["runs"].items())) or "none") + ".", "",
         "| Horizon | Issues scored | Sessions | v2 CRPS | Model CRPS | Model minus v2 | 95 % interval | 90 % band "
         "covers (v2 / model) | Origin bar revised |", "|---|---|---|---|---|---|---|---|---|"]
    for h, e in s["horizons"].items():
        p = e["paired"]
        iv = p["interval"] if p else None
        L.append(f"| {h} min | {e['issues']} | {e['sessions']} | {p['base_crps_bps']:.4f} | {p['other_crps_bps']:.4f} | "
                 f"{100 * (p['diff_share'] or 0):+.2f} % | "
                 + (f"[{iv[0]:+.5f}, {iv[1]:+.5f}]" if iv else "too few sessions") + f" | "
                 f"{100 * e['cover90_base']:.1f} % / {100 * e['cover90_model']:.1f} % | "
                 f"{100 * e['revised_share']:.1f} % (largest {e['revision_max_bps']:.2f} bps) |")
    if not s["horizons"]:
        L.append("| - | 0 | 0 | - | - | - | - | - | - |")
    L += ["", "Development evidence only in the sense that nothing is chosen from it: the frozen model stays as "
          "registered, and an interval appears once 10 sessions are scored.", ""]
    with open(path, "w") as fh_:
        fh_.write("\n".join(L))
    return path
