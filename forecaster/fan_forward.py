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
  issue(conn, model, now)      for each pending mark not yet issued (or the marks given): reads
                               the store as of the mark, builds and records the issue - or a run
                               row saying why not: stale (its origin bar is not stored yet - once
                               per mark), failed. Every row says which trigger ran it and when
                               (TRIGGERS) and when its computation started; recorded_at shows
                               each issue's latency
  mark_due(now), collect_until the mark trigger (scripts/fan.py forward mark): at a mark, collect
                               until its origin bar is stored or the live deadline nears, then
                               issue that mark at once - the live attempt, apart from the 2-minute
                               catch-up runs that make the delayed-feed evaluation
  timing(...)                  per trigger, how fast the forecasts got out against the rules'
                               live deadline, and the parts: the trigger's start, the collection,
                               the computation - and when the origin bar reached the store (the
                               feed's part)
  score_issue(...)             one issue against the session's final prices (pure)
  score(conn, model)           every issue of a final session not yet scored
  summarise(...)               the record per rule version (pure): the marks the calendar expected
                               from the version's activation, attempted, issued, stale, failed and
                               missed; per horizon and class the paired comparison over sessions
  summary(conn, model)         summarise() on the stored record
  RULES, define(...)           the forward record's definition (kind 'fan_forward'): the classes an
                               issue falls in and the evaluation each feeds, registered before the
                               sample it governs; every issue names the version it was made under
  rules_params, classify       a horizon's class under its own version's stored parameters

On this account the IB feed is delayed (no real-time subscription; IB answers live requests
with error 354, scripts/ib_feed_check.py): a bar reaches IB's history about 10 minutes after
it closes for NQ and 15 for VXN. Only the horizons of which enough remains after that can be
on time (RULES). Getting the data on time and getting the forecast out on time are separate
requirements: the mark trigger's timing measures the second on any feed, and its stale rows
record the first.
"""

from __future__ import annotations

import hashlib
import re
import json
import os
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np

from contracts import fan as F
from features import calendar as cal
from features.session_windows import get_trading_day_date
from contracts import nq_prompt_v2 as defs
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
LIVE_SECONDS = 60                        # rules v2: issued within this of its mark, an issue is live
REMAINING_SHARE = 0.75                   # rules v2: a delayed-origin horizon keeps at least this share of itself
RULES = {
    "params": {"live_seconds": LIVE_SECONDS, "remaining_share": REMAINING_SHARE,
               "grace_minutes": int(GRACE.total_seconds() // 60), "mark_minutes": MARK_MINUTES},
    "schedule": f"every {MARK_MINUTES} minutes of the trading day from 18:15 ET to the halt, and 09:29 ET (with the "
                "pre-open slice's 16, 31 and 61 minutes); each issue centred on the bar that closes at its mark",
    "issuance": f"a mark is issued by the first run that finds its origin bar stored, within {int(GRACE.total_seconds() // 60)} "
                "minutes of the mark, from the bars closed by the mark only; a later mark is never issued",
    "issued_at": "the database's time the issue was recorded (fan_forward_issues.recorded_at)",
    "classes": {"live": f"issued within {LIVE_SECONDS} s of its mark and before the horizon's target - the intended "
                        "live forecast",
                "delayed_origin": f"issued later, with at least {REMAINING_SHARE:.0%} of the horizon left: a forecast "
                                  "anchored to an older price - the delayed-feed evaluation, a different experiment "
                                  "from the live one",
                "late": "issued later, with less left", "expired": "issued at or after the horizon's target"},
    "receipts": "per instrument, the bar at the origin - when it first reached the store and when the values read "
                "did (bars.first_stored_at, version_stored_at) - and every bar the forecast could have read: their "
                "count, the latest version time, how many have no known time, and a hash of the values",
    "active_from": "the database's time this definition was registered: from then on every mark of the trading "
                   "calendar is expected",
    "evaluation": {"live": "the live evaluation: live horizons only, the model against fan_rw_v2 on identical issues, "
                           "NQ, per session - the manifest's CRPS and the 90 % bands' coverage. It needs inputs on "
                           "time: on this account none is, so it stays empty",
                   "delayed_feed": "delayed-origin horizons, the same measures, reported apart and labelled as the "
                                   "delayed-feed evaluation - never as evidence about the live 5- and 15-minute "
                                   "forecasts",
                   "research": "late and expired horizons, and issues made before any rules",
                   "review": "once 20 sessions hold live or delayed-origin issues at a horizon, or on 2027-01-29, "
                             "whichever comes first: an initial operational and calibration check - not enough "
                             "sessions to confirm a difference the size of the holdout's -0.14 %; a recalibration is a "
                             "new model version with its own later evaluation"},
    "operations": "every report shows, per rule version, the marks the trading calendar expected from its activation "
                  "(sessions with no attempt included), those attempted, issued, stale (their origin bar never "
                  "stored in time), failed and missed, and the same counted from each session's first attempt",
    "feed": "IB without a real-time subscription (error 354): a bar reaches IB's history about 10 minutes after it "
            "closes for NQ and 15 for VXN (scripts/ib_feed_check.py, 2026-10-06) - so no issue is live, and only the "
            "60, 120 and 240-minute horizons can be delayed-origin",
}


# Operations, not rules: what ran an attempt and how fast - recorded per run row, judged against the rules' deadline.
TRIGGERS = ("mark", "catchup", "auto", "manual", "unrecorded")
MARK_WINDOW = timedelta(seconds=60)      # the mark trigger acts on a mark this recent, else on none
ISSUE_RESERVE = timedelta(seconds=20)    # it stops collecting this long before the live deadline (an issue takes ~8 s)
COLLECT_PAUSE_S = 2.0                    # between its collections while the origin bar is missing


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
                            "target_at": (mark.at + timedelta(minutes=h)).isoformat(),
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
# Timeliness (pure)
# --------------------------------------------------------------------------

def rules_params(definition: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """The classification parameters a registered rule version stored - read from the version itself, never from
    this module's constants: v2 stores them; v1 stored its rule as text ('issued_at <= mark + 0.25 x h'), parsed here.
    None for an issue made under no rules."""
    if definition is None:
        return None
    if "params" in definition:
        return {"labels": "v2", **definition["params"]}
    m = re.search(r"issued_at <= mark \+ ([0-9.]+) x h", definition["timeliness"]["on_time"])
    return {"labels": "v1", "live_seconds": None, "remaining_share": 1.0 - float(m.group(1))}


def classify(params: Dict[str, Any], mark_at: datetime, issued_at: datetime, h: int) -> str:
    """A horizon's class under a rule version's ``params`` (rules_params): v2 live | delayed_origin | late |
    expired; v1 on_time | delayed | expired (its own labels - its 'on time' is a delayed-origin class)."""
    target = mark_at + timedelta(minutes=h)
    if issued_at >= target:
        return "expired"
    v1 = params["labels"] == "v1"
    if not v1 and issued_at - mark_at <= timedelta(seconds=params["live_seconds"]):
        return "live"
    if target - issued_at >= timedelta(minutes=h) * params["remaining_share"]:
        return "on_time" if v1 else "delayed_origin"
    return "delayed" if v1 else "late"


def session_marks(session: date, until: Optional[datetime] = None) -> List[datetime]:
    """Every mark of ``session`` (UTC instants) up to ``until``: the marks a record running through it expects."""
    s = cal.session(session)
    end = end_slot("FUT", s.schedule)
    slots = sorted(set(range(MARK_MINUTES, end, MARK_MINUTES)) | {slot_of_time(CUTOFF_ET)})
    out = [slot_instant(session, k) for k in slots]
    return [t for t in out if until is None or t <= until]


def define(conn, model: Dict[str, Any], suffix: str = "v2") -> Tuple[str, bool]:
    """Registers the forward record's current RULES for ``model`` (kind 'fan_forward'); returns (version, new)."""
    from database import journal_store as store
    version = f"{model['version']}_forward_{suffix}"
    body = {"model": {"version": model["version"], "definition_hash": model["definition_hash"]}, **RULES}
    return version, store.register_version(conn, defs._record(version, "fan_forward", body))


def record_version(conn, model: Dict[str, Any]) -> Optional[str]:
    """The forward-record definition registered for ``model``, or None."""
    row = conn.execute("SELECT version FROM journal.definition_versions WHERE kind = 'fan_forward' AND "
                       "definition->'model'->>'version' = %s ORDER BY registered_at DESC LIMIT 1;",
                       (model["version"],)).fetchone()
    return None if row is None else row[0]


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


def _availability(conn, symbol: str, first: datetime, last: datetime) -> Dict[str, Any]:
    """Every bar of ``symbol``'s active contracts from ``first`` to ``last`` (the reach of what an issue could read):
    how many, the latest time the values read reached the store, how many have no known time (held from before the
    receipt times), and an md5 of their values - so a later revision of any of them shows."""
    row = conn.execute(
        "SELECT count(*), to_char(max(b.version_stored_at) AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS.US\"+00:00\"'), "
        "count(*) FILTER (WHERE b.version_stored_at IS NULL), md5(string_agg(concat_ws('|', b.contract_id, "
        "extract(epoch FROM b.timestamp_utc)::bigint, b.close, b.volume), ';' ORDER BY b.contract_id, b.timestamp_utc)) "
        "FROM bars b WHERE b.interval = '1m' AND b.timestamp_utc >= %s AND b.timestamp_utc <= %s AND b.contract_id IN "
        "(SELECT DISTINCT contract_id FROM active_contracts WHERE symbol = %s AND trading_day >= %s::date - 10 "
        "AND trading_day <= %s::date + 1);", (first, last, symbol, first, last)).fetchone()
    return {"from": first.isoformat(), "to": last.isoformat(), "bars": int(row[0]),
            "latest_version_stored_at": row[1], "unknown_times": int(row[2]), "values_md5": row[3]}


def _freshness(conn, panel: fp.Panel, mark: Mark) -> Dict[str, Any]:
    """Per instrument read: its last bar at the origin - when it started, its close, its age, and when it first
    reached the store (its receipt time) - and when the store last fetched that day."""
    out = {}
    for j, sym in enumerate(panel.symbols):
        cid = int(panel.contract[-1, j])
        age = panel.age[-1, j, mark.origin_slot]
        bar_at = (mark.at - timedelta(minutes=1 + float(age))) if np.isfinite(age) else None
        receipt = None
        if bar_at is not None:
            fmt = "'YYYY-MM-DD\"T\"HH24:MI:SS.US\"+00:00\"'"
            r = conn.execute(f"SELECT to_char(first_stored_at AT TIME ZONE 'UTC', {fmt}), "
                             f"to_char(version_stored_at AT TIME ZONE 'UTC', {fmt}) FROM bars WHERE contract_id = %s "
                             "AND interval = '1m' AND timestamp_utc = %s;", (cid, bar_at)).fetchone()
            receipt = None if r is None else {"first_stored_at": r[0], "version_stored_at": r[1]}
        row = conn.execute("SELECT fetched_at, status FROM session_days WHERE contract_id = %s AND trading_day = %s "
                           "AND interval = '1m' ORDER BY fetched_at DESC LIMIT 1;", (cid, mark.session)).fetchone()
        out[sym] = {"contract_id": cid, "close": float(panel.close[-1, j, mark.origin_slot])
                    if np.isfinite(panel.close[-1, j, mark.origin_slot]) else None,
                    "age_minutes": None if not np.isfinite(age) else float(age),
                    "bar_at": None if bar_at is None else bar_at.isoformat(),
                    "first_stored_at": None if receipt is None else receipt["first_stored_at"],
                    "version_stored_at": None if receipt is None else receipt["version_stored_at"],
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


def mark_due(now: datetime, window: timedelta = MARK_WINDOW) -> Optional[Mark]:
    """The mark the mark trigger acts on at ``now``: the latest at or before it, if at most ``window`` old."""
    m = mark_at(now)
    return m if m is not None and now.astimezone(timezone.utc) - m.at <= window else None


def origin_stored(conn, symbol: str, mark: Mark) -> bool:
    """Whether the bar at ``mark``'s origin is in the store for ``symbol``'s active contract of the session (read in
    its own transaction, so a collector's later commit is seen)."""
    with conn:
        return conn.execute(
            "SELECT 1 FROM bars b WHERE b.interval = '1m' AND b.timestamp_utc = %s AND b.contract_id IN (SELECT "
            "contract_id FROM active_contracts WHERE symbol = %s AND trading_day = %s) LIMIT 1;",
            (slot_instant(mark.session, mark.origin_slot), symbol, mark.session)).fetchone() is not None


def collect_until(collect: Callable[[], int], stored: Callable[[], bool], deadline: datetime,
                  clock: Callable[[], datetime], pause: Callable[[], None]) -> List[Dict[str, Any]]:
    """The mark trigger's collection: ``collect`` (returns its exit code) until ``stored`` says the origin bar is in
    the store or ``clock`` reaches ``deadline`` - at least once, with ``pause`` between, and never starting one at or
    after the deadline. One entry per collection: {'start', 'end' (ISO), 'rc', 'stored'}."""
    out: List[Dict[str, Any]] = []
    while True:
        start = clock()
        rc = collect()
        ok = stored()
        out.append({"start": start.isoformat(), "end": clock().isoformat(), "rc": rc, "stored": ok})
        if ok or clock() >= deadline:
            return out
        pause()
        if clock() >= deadline:
            return out


def issue(conn, model: Dict[str, Any], experiment: Dict[str, Any], now: datetime, code_revision: str,
          cache_dir: Optional[str] = None, dry_run: bool = False, trigger: Optional[Dict[str, Any]] = None,
          marks: Optional[Sequence[Mark]] = None) -> List[Dict[str, Any]]:
    """Issues the frozen ``model``'s forecast for every pending mark not yet issued - or for ``marks`` - (see the
    module docstring); one result per mark tried - {'status', 'mark', 'issue' (the record, when built)}. ``trigger``
    ({'kind': one of TRIGGERS, 'triggered_at', 'collected_at', ...}) is recorded on every run row written. Nothing
    is written with ``dry_run``."""
    return [_issue_mark(conn, model, experiment, mark, code_revision, cache_dir, dry_run, trigger)
            for mark in (pending_marks(now) if marks is None else marks)]


def _issue_mark(conn, model: Dict[str, Any], experiment: Dict[str, Any], mark: Mark, code_revision: str,
                cache_dir: Optional[str], dry_run: bool, trigger: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    definition, version = model["definition"], model["version"]
    m = experiment["definition"]
    target = m["targets"]["primary"]
    result: Dict[str, Any] = {"mark": mark}
    started = {"started_at": datetime.now(timezone.utc).isoformat(), **({"trigger": trigger} if trigger else {})}

    def record(status: str, issue_id: Optional[str] = None, detail: Optional[Dict[str, Any]] = None):
        result["status"] = status
        if not dry_run:
            with conn:
                _run(conn, version, mark.at, status, code_revision, issue_id, {**started, **(detail or {})})
        return result

    exists = conn.execute("SELECT issue_id FROM journal.fan_forward_issues WHERE model = %s AND target = %s AND "
                          "session_date = %s AND origin_slot = %s;",
                          (version, target, mark.session, mark.origin_slot)).fetchone()
    if exists:
        result.update(status="issued before", issue_id=str(exists[0]))
        return result
    rules = record_version(conn, model)
    if rules is None:
        result["status"] = "no rules"                       # define() first: an issue is made under fixed rules
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
    origin_bar = slot_instant(mark.session, mark.origin_slot)
    reach = {s_: day_start(date.fromisoformat(window[0])) - timedelta(days=fp.CARRY_DAYS) for s_ in symbols}
    reach[target] = min(reach[target], day_start(history_start(mark.session, F.EVENT_SESSIONS)))
    inputs = {"features": built["features"], "variance": {str(h): float(V[fh.FRAME_HORIZONS.index(h)])
                                                          for h in built["horizons"]},
              "instruments": _freshness(conn, panel, mark),
              "availability": {s_: _availability(conn, s_, reach[s_], origin_bar) for s_ in symbols},
              "releases_known": day.releases_known, "feature_format": ff.FEATURE_FORMAT,
              "frame_format": fh.FRAME_FORMAT}
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
                     "origin_slot, origin_bar_at, mark_at, origin_price, shape_id, inputs, forecast, code_revision, "
                     "record) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s);",
                     (issue_id, version, definition["baseline"]["version"], target, mark.session, mark.origin_slot,
                      slot_instant(mark.session, mark.origin_slot), mark.at, built["origin_price"], sid,
                      json.dumps(inputs), json.dumps(built["forecast"]), code_revision, rules))
        _run(conn, version, mark.at, "issued", code_revision, issue_id, started)
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


CLASS_ORDER = ("live", "delayed_origin", "on_time", "late", "delayed", "expired", "legacy")


def _when(text: Optional[str]) -> Optional[datetime]:
    return datetime.fromisoformat(text) if text else None


def _stats(xs: Sequence[float]) -> Optional[Dict[str, float]]:
    xs = sorted(xs)
    if not xs:
        return None
    return {"n": len(xs), "median": float(np.median(xs)), "p90": float(np.quantile(xs, 0.9)), "max": float(xs[-1])}


def timing(params: Optional[Dict[str, Any]], runs: Sequence[Dict[str, Any]], issues: Sequence[Dict[str, Any]],
           issued_rows: Optional[Dict[str, Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
    """
    How fast each trigger got the forecasts out (pure), in TRIGGERS order: the marks it tried, issued, found without
    their origin bar ('stale': the first run to find it so says so, even when a later run issued the mark - unlike
    the operations' stale, a mark never issued) or failed; how many issues were out within the rules' live deadline
    of their mark (None under rules
    without one); and in seconds - median, 90th percentile, largest - the latency from the mark to the issue and its
    parts: the trigger's start after the mark, the collection, the computation; and when the origin bar reached the
    store after the mark (its receipt time - the feed's part, not the pipeline's). ``runs``: the attempts counted,
    {mark_at, status, issue_id, detail} - a row without a trigger is 'unrecorded' (before triggers were kept);
    ``issues``: {issue_id, mark_at, issued_at, arrival (the origin bar's first_stored_at or None)}, each timed by the
    run row that issued it (``issued_rows`` by issue id; from ``runs`` when not given).
    """
    live = (params or {}).get("live_seconds")
    groups: Dict[str, Dict[str, Any]] = {}

    def group(kind: str) -> Dict[str, Any]:
        return groups.setdefault(kind, {"marks": set(), "issued": 0, "stale": 0, "failed": 0, "within": 0,
                                        "latency": [], "schedule": [], "collect": [], "compute": [], "arrival": []})

    def kind_of(detail: Optional[Dict[str, Any]]) -> str:
        return ((detail or {}).get("trigger") or {}).get("kind") or "unrecorded"

    issued_by = issued_rows if issued_rows is not None else {
        r["issue_id"]: r for r in runs if r["status"] == "issued" and r.get("issue_id")}
    for r in runs:
        g = group(kind_of(r.get("detail")))
        g["marks"].add(r["mark_at"])
        if r["status"] in ("stale", "failed"):
            g[r["status"]] += 1
    for i in issues:
        detail = (issued_by.get(i["issue_id"]) or {}).get("detail") or {}
        trig = detail.get("trigger") or {}
        g = group(kind_of(detail))
        g["marks"].add(i["mark_at"])
        g["issued"] += 1
        latency = (i["issued_at"] - i["mark_at"]).total_seconds()
        g["latency"].append(latency)
        g["within"] += live is not None and latency <= live
        start, collected, computing = (_when(trig.get("triggered_at")), _when(trig.get("collected_at")),
                                       _when(detail.get("started_at")))
        if start:
            g["schedule"].append((start - i["mark_at"]).total_seconds())
        if start and collected:
            g["collect"].append((collected - start).total_seconds())
        if computing:
            g["compute"].append((i["issued_at"] - computing).total_seconds())
        if i.get("arrival"):
            g["arrival"].append((i["arrival"] - i["mark_at"]).total_seconds())
    out = []
    for kind in list(TRIGGERS) + sorted(set(groups) - set(TRIGGERS)):
        g = groups.get(kind)
        if g is None:
            continue
        out.append({"trigger": kind, "attempted": len(g["marks"]), "issued": g["issued"], "stale": g["stale"],
                    "failed": g["failed"], "deadline_s": live, "within_deadline": g["within"] if live is not None
                    else None, **{f"{k}_s": _stats(g[k]) for k in ("latency", "schedule", "collect", "compute",
                                                                     "arrival")}})
    return out


def summarise(versions: Sequence[Tuple[str, Dict[str, Any], datetime]], attempts: Sequence[Tuple[datetime, str]],
              issues: Sequence[Dict[str, Any]], scores: Sequence[Dict[str, Any]], now: datetime,
              runs: Sequence[Dict[str, Any]] = ()) -> List[Dict[str, Any]]:
    """
    The forward record per rule version (pure). ``versions``: (name, stored definition, registered at), oldest first;
    ``attempts``: (mark, status) of every run row; ``issues``: {issue_id, record, mark_at, issued_at, horizons, and
    for timing arrival}; ``scores``: {issue_id, horizon, session, crps_base, crps_model, pit_base, pit_model,
    revision}; ``runs``: the run rows with their details, for timing(). A version is active from its registration to
    the next one's (or ``now``): every mark of the trading calendar in that span is expected - a session with no
    attempt and an outage included - and its attempts are the marks in the span. Its issues are classified under its
    own stored parameters, and timed against its own deadline; issues made under no rules form a 'legacy' group.
    """
    out = []
    issued_rows = {r["issue_id"]: r for r in runs if r["status"] == "issued" and r.get("issue_id")}
    spans = [(name, d, start, versions[i + 1][2] if i + 1 < len(versions) else now)
             for i, (name, d, start) in enumerate(versions)]
    for name, definition, start, end in spans + [(None, None, None, None)]:
        params = rules_params(definition)
        mine = [i for i in issues if i["record"] == name]
        if name is None and not mine:
            continue
        g: Dict[str, Any] = {"version": name, "labels": params["labels"] if params else "legacy", "params": params,
                             "active_from": start, "active_to": end, "operations": None, "horizons": [],
                             "timing": timing(params, [r for r in runs if name is not None and start <= r["mark_at"]
                                                       <= end and r["status"] in ("issued", "stale", "failed")], mine,
                                              issued_rows)}
        if name is not None:
            expected = [t for d in cal.sessions_between(start.astimezone(cal.NY_TZ).date(),
                                                         end.astimezone(cal.NY_TZ).date() + timedelta(days=1))
                        if d.is_open for t in session_marks(d.session_date) if start <= t <= end]
            in_span = {}
            for t, st in attempts:
                if start <= t <= end:
                    in_span.setdefault(t, set()).add(st)
            issued = {i["mark_at"] for i in mine}
            first_by_session: Dict[date, datetime] = {}
            for t in in_span:
                d = date.fromisoformat(get_trading_day_date(t.astimezone(cal.NY_TZ)))
                first_by_session[d] = min(first_by_session.get(d, t), t)
            since_first = [t for t in expected if date.fromisoformat(get_trading_day_date(t.astimezone(cal.NY_TZ)))
                           in first_by_session and t >= first_by_session[date.fromisoformat(
                               get_trading_day_date(t.astimezone(cal.NY_TZ)))]]
            g["operations"] = {
                "expected": len(expected), "attempted": len([t for t in expected if t in in_span]),
                "issued": len(issued), "issued_expected": len([t for t in expected if t in issued]),
                "stale": len([t for t, st in in_span.items() if t not in issued and "stale" in st]),
                "failed": len([t for t, st in in_span.items() if t not in issued and "failed" in st]),
                "missed": len([t for t in expected if t not in in_span and t not in issued]),
                "sessions_expected": len({get_trading_day_date(t.astimezone(cal.NY_TZ)) for t in expected}),
                "sessions_attempted": len(first_by_session),
                "since_first_attempt": {"expected": len(since_first),
                                        "missed": len([t for t in since_first if t not in in_span and t not in issued])}}
        by_issue = {i["issue_id"]: i for i in mine}
        counts: Dict[Tuple[int, str], int] = {}
        for i in mine:
            for h in i["horizons"]:
                k = (int(h), classify(params, i["mark_at"], i["issued_at"], int(h)) if params else "legacy")
                counts[k] = counts.get(k, 0) + 1
        cells: Dict[Tuple[int, str], Dict[str, Any]] = {}
        for sc in scores:
            i = by_issue.get(sc["issue_id"])
            if i is None:
                continue
            h = int(sc["horizon"])
            k = (h, classify(params, i["mark_at"], i["issued_at"], h) if params else "legacy")
            e = cells.setdefault(k, {"sessions": {}, "pit_b": [], "pit_m": [], "rev": []})
            e["sessions"].setdefault(sc["session"], []).append((sc["crps_base"], sc["crps_model"]))
            e["pit_b"].append(sc["pit_base"])
            e["pit_m"].append(sc["pit_model"])
            e["rev"].append(sc["revision"])
        for (h, cls) in sorted(set(counts) | set(cells), key=lambda k: (k[0], CLASS_ORDER.index(k[1]))):
            e = cells.get((h, cls))
            row = {"horizon": h, "class": cls, "issued": counts.get((h, cls), 0), "scored": 0, "sessions": 0,
                   "paired": None, "cover90_base": None, "cover90_model": None, "revised_share": None,
                   "revision_max_bps": None}
            if e:
                sess = [{"x": (float(np.mean([a for a, _ in v])), float(np.mean([b for _, b in v])), len(v))}
                        for _, v in sorted(e["sessions"].items())]
                pb, pm, rev = np.array(e["pit_b"]), np.array(e["pit_m"]), np.array(e["rev"])
                row.update(scored=len(pb), sessions=len(sess), paired=fh.paired(sess, "x"),
                           cover90_base=float(np.mean(np.abs(pb - 0.5) <= 0.45)),
                           cover90_model=float(np.mean(np.abs(pm - 0.5) <= 0.45)),
                           revised_share=float(np.mean(np.abs(rev) > 0)),
                           revision_max_bps=float(np.max(np.abs(rev)) * 1e4))
            g["horizons"].append(row)
        out.append(g)
    return out


def summary(conn, model: Dict[str, Any], now: Optional[datetime] = None) -> Dict[str, Any]:
    """summarise() on the stored record of ``model``."""
    version = model["version"]
    now = now or datetime.now(timezone.utc)
    utc = lambda x: datetime.fromtimestamp(float(x), timezone.utc)
    versions = [(v, json.loads(d) if isinstance(d, str) else d, utc(t)) for v, d, t in conn.execute(
        "SELECT version, definition, extract(epoch FROM registered_at) FROM journal.definition_versions WHERE kind = "
        "'fan_forward' AND definition->'model'->>'version' = %s ORDER BY registered_at;", (version,)).fetchall()]
    runs = [{"mark_at": utc(t), "status": st, "issue_id": None if i is None else str(i),
             "detail": (json.loads(d) if isinstance(d, str) else d) or {}} for t, st, i, d in conn.execute(
        "SELECT extract(epoch FROM mark_at), status, issue_id, detail FROM journal.fan_forward_runs WHERE model = %s;",
        (version,)).fetchall()]
    attempts = [(r["mark_at"], r["status"]) for r in runs]
    issues = [{"issue_id": str(i), "record": r, "mark_at": utc(m), "issued_at": utc(t),
               "horizons": list((json.loads(f) if isinstance(f, str) else f).keys()), "arrival": _when(a)}
              for i, r, m, t, f, a in conn.execute(
                  "SELECT issue_id, record, extract(epoch FROM mark_at), extract(epoch FROM recorded_at), forecast, "
                  "inputs->'instruments'->target->>'first_stored_at' FROM journal.fan_forward_issues WHERE model = %s;",
                  (version,)).fetchall()]
    scores = [{"issue_id": str(i), "horizon": h, "session": str(d)[:10], "crps_base": cb, "crps_model": cm,
               "pit_base": pb, "pit_model": pm, "revision": rv} for i, d, h, cb, cm, pb, pm, rv in conn.execute(
        "SELECT i.issue_id, i.session_date, s.horizon, s.crps_base_bps, s.crps_model_bps, s.pit_base, s.pit_model, "
        "s.revision FROM journal.fan_forward_scores s JOIN journal.fan_forward_issues i ON i.issue_id = s.issue_id "
        "WHERE i.model = %s;", (version,)).fetchall()]
    counts = dict(conn.execute("SELECT status, count(*) FROM journal.fan_forward_runs WHERE model = %s GROUP BY "
                               "status;", (version,)).fetchall())
    return {"model": version, "runs": counts, "versions": summarise(versions, attempts, issues, scores, now, runs)}


_CLASS_TEXT = {"live": "live", "delayed_origin": "delayed origin (the delayed-feed evaluation)", "late": "late",
               "expired": "expired", "on_time": "v1 'on time' (a delayed-origin class)", "delayed": "v1 delayed",
               "legacy": "made under no rules"}
TRIGGER_TEXT = {"mark": "mark trigger (the live attempt)", "catchup": "2-minute catch-up (delayed feed)",
                "auto": "dashboard Auto mode", "manual": "by hand", "unrecorded": "not recorded (runs before triggers were)"}


def spread(s: Optional[Dict[str, float]]) -> str:
    """A timing() statistic as 'median / 90th percentile / largest' seconds."""
    return "-" if not s else f"{s['median']:.0f} / {s['p90']:.0f} / {s['max']:.0f}"


def _timing_lines(rows: Sequence[Dict[str, Any]]) -> List[str]:
    dl = rows[0]["deadline_s"]
    L = ["**Timing**, in seconds after the mark (median / 90th percentile / largest); the live deadline is "
         + (f"{dl:.0f} s" if dl is not None else "none under these rules")
         + ". When the origin bar reached the store is the feed's part; the trigger's start, the collection and the "
           "computation are the pipeline's.", "",
         "| Trigger | Marks tried | Issued | Found the origin bar missing | Failed | Within the deadline | Issued after | "
         "Trigger started after | Collection | Computation | Origin bar stored after |",
         "|---|---|---|---|---|---|---|---|---|---|---|"]
    for t in rows:
        L.append(f"| {TRIGGER_TEXT.get(t['trigger'], t['trigger'])} | {t['attempted']} | {t['issued']} | {t['stale']} | "
                 f"{t['failed']} | {'-' if t['within_deadline'] is None else t['within_deadline']} | "
                 f"{spread(t['latency_s'])} | {spread(t['schedule_s'])} | {spread(t['collect_s'])} | "
                 f"{spread(t['compute_s'])} | {spread(t['arrival_s'])} |")
    return L + [""]


def write_report(s: Dict[str, Any], meta: Dict[str, Any], path: str) -> str:
    """The forward record so far as markdown at ``path``, rule version by rule version."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    L = [f"# Forward record: {s['model']}", "",
         f"Code {meta['code_revision'][:12]}; as of {meta['as_of']}. Every issue is classified under the rule version "
         "it was made under (its stored parameters). Live horizons are the live evaluation; delayed-origin ones are the "
         "delayed-feed evaluation, a different experiment - never evidence about live forecasts; late, expired and "
         "issues made under no rules are research.", ""]
    for g in s["versions"]:
        title = f"Rules `{g['version']}`" if g["version"] else "Issues made under no rules (legacy)"
        L += [f"## {title}", ""]
        if g["version"]:
            o = g["operations"]
            L += [f"Active {g['active_from']:%Y-%m-%d %H:%M} UTC to {g['active_to']:%Y-%m-%d %H:%M} UTC. **Operations:** "
                  f"{o['sessions_expected']} session(s) expected, {o['sessions_attempted']} attempted; {o['expected']} "
                  f"marks expected by the calendar, {o['attempted']} attempted, {o['issued_expected']} issued "
                  f"({o['issued']} issues in all), {o['stale']} stale, {o['failed']} failed, **{o['missed']} missed**; "
                  f"from each session's first attempt: {o['since_first_attempt']['expected']} expected, "
                  f"{o['since_first_attempt']['missed']} missed.", ""]
        L += ["| Horizon | Class | Issued | Scored | Sessions | v2 CRPS | Model CRPS | Model minus v2 | 95 % interval | "
              "90 % band covers (v2 / model) | Origin bar revised |", "|---|---|---|---|---|---|---|---|---|---|---|"]
        for r in g["horizons"]:
            p = r["paired"]
            iv = p["interval"] if p else None
            L.append(f"| {r['horizon']} min | {_CLASS_TEXT[r['class']]} | {r['issued']} | {r['scored']} | {r['sessions']} | "
                     + (f"{p['base_crps_bps']:.4f} | {p['other_crps_bps']:.4f} | {100 * (p['diff_share'] or 0):+.2f} % | "
                        if p else "- | - | - | ")
                     + (f"[{iv[0]:+.5f}, {iv[1]:+.5f}]" if iv else ("too few sessions" if p else "-")) + " | "
                     + (f"{100 * r['cover90_base']:.1f} % / {100 * r['cover90_model']:.1f} % | "
                        f"{100 * r['revised_share']:.1f} % (largest {r['revision_max_bps']:.2f} bps) |"
                        if r["cover90_base"] is not None else "- | - |"))
        if not g["horizons"]:
            L.append("| - | - | 0 | 0 | 0 | - | - | - | - | - | - |")
        L.append("")
        if g.get("timing"):
            L += _timing_lines(g["timing"])
    with open(path, "w") as fh_:
        fh_.write("\n".join(L))
    return path
