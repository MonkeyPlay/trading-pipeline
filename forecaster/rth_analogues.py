# forecaster/rth_analogues.py
"""
The RTH analogue sets of NQ sessions (contracts/nq_rth.py, docs/rth_analogues.md):
the earlier sessions whose first n minutes from the 09:30 ET open most resemble a
session's first n, stored once per window in journal.rth_analogue_sets - a record of
what matched, from which inputs, issued how and when. The pre-open analogue sets
(forecaster/journal.py) are separate and unchanged.

  load_openings(conn, last_day)   every NQ session to ``last_day``: its confirmed
                                  RTH bars of the first hour on its active contract,
                                  the context frozen in its pre-open snapshot, its
                                  overnight volume, the relative-volume baselines, and
                                  when each input's values reached the store
  build_set(target, pool, n, ...) one set as stored: ranked, digested, with its data
                                  quality and its inputs' provenance - nothing after
                                  the cutoff takes part
  issue(conn, now, issued_by)     the session in progress: its newest window and any of
                                  15, 30, 45, 60 minutes not stored yet - Auto runs it
                                  after each collection ('auto'), a person by hand
                                  ('manual'); for 15, 30 and 45 also the evaluation's
                                  forecasts (forecaster/rth_eval.py), stored as issued
  reconstruct(conn, days, ...)    historical reconstructions ('backfill': never live)
  describe(set)                   the line naming a stored set: window, data cutoff,
                                  checkpoint, provisional, how and when it was issued
  windows_over(day, through)      the P1 targets whose label window has ended by
                                  ``through``: observed, no longer a forecast
  newest_bar_end(conn, day)       the end of the session's newest stored NQ bar

Live is how a set was produced, not only its age: the database marks a set live only
when it was issued by Auto or by hand within 30 minutes of its cutoff; a backfill is
always a historical reconstruction. Each set also records when its inputs' values
reached the store (bars.version_stored_at - a value without one was stored before
migration 0023, so by its application; snapshots' built_at): the target's window -
how far behind the cutoff the feed was - and every earlier session read. Its inputs
are verified as of the cutoff only when every earlier input was in the store by then
and the target's within 30 minutes of it, as a live issue would have had them.

What an analogue did after the cutoff is never read here: the dashboard draws it,
greyed, beside the session (dashboard/views/candles.py). Repeated runs on unchanged
inputs store nothing new; a revised input (a vendor revision of a bar in a window, a
new candidate session) stores a new set beside the earlier one.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

from contracts import nq_prompt_v2 as defs
from contracts import nq_rth as rth
from database import journal_store as store
from features import calendar as cal
from forecaster import rth_eval
from forecaster.provenance import code_revision
from matching import rth as mr

logger = logging.getLogger("nq_journal")
MINUTE = timedelta(minutes=1)
_LOCK_KEY = 0x52544841          # "RTHA": one RTH issue at a time, across processes (pg_try_advisory_lock)


def iso(t: Optional[datetime]) -> Optional[str]:
    return None if t is None else t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def utc(value) -> Optional[datetime]:
    """A stored timestamp (a datetime, or the store's 'YYYY-MM-DD HH:MM:SS' UTC text) as an aware UTC datetime."""
    if value is None:
        return None
    if not isinstance(value, datetime):
        value = datetime.fromisoformat(str(value).replace(" ", "T").replace("Z", "+00:00"))
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _float(ref: Optional[Dict[str, Any]]) -> Optional[float]:
    """A snapshot reference's value when valid, else None."""
    if not ref or ref.get("status") != "valid" or ref.get("value") is None:
        return None
    return float(ref["value"])


def context_of(snapshot: Dict[str, Any], overnight: Tuple[float, float] = (0.0, 0.0)) -> mr.Context:
    """The frozen pre-open context the matcher reads from a stored snapshot (and the overnight volume sums)."""
    p = snapshot["payload"]
    refs = p.get("references") or {}
    return mr.Context(snapshot_id=str(snapshot["snapshot_id"]), atr=_float((p.get("atr") or {}).get("daily")),
                      prev_rth_close=_float(refs.get("prev_rth_close")), on_high=_float(refs.get("on_high")),
                      on_low=_float(refs.get("on_low")), overnight_pv=overnight[0], overnight_volume=overnight[1])


class _Receipts:
    """When input values reached the store: ``bound(times)`` is the latest of ``times``, where a value without a
    receipt time (None) counts as stored by migration 0023's application - the migration reset every earlier
    value's time, and the collector stamps every value stored since. None when that is unknown too."""

    def __init__(self, conn) -> None:
        row = conn.execute("SELECT applied_at FROM schema_migrations WHERE version = 23;").fetchone()
        self.floor = utc(row[0]) if row is not None and row[0] is not None else None

    def bound(self, times: Iterable[Optional[datetime]]) -> Optional[datetime]:
        known, unknown = [], False
        for t in times:
            if t is None:
                unknown = True
            else:
                known.append(utc(t))
        if unknown:
            if self.floor is None:
                return None
            known.append(self.floor)
        return max(known) if known else None


def _first_hour_bars(conn, symbol: str, first_day: str, last_day: str) -> Dict[str, Dict[str, Any]]:
    """{day: {'contract_id', 'bars', 'stored': {start: version_stored_at}, 'newest_start'}}: each session's 1-minute
    bars in [09:30, 10:31) ET on its active contract, when each one's values reached the store, and the start of
    the session's newest stored bar from 09:30 on (which confirms the bars before it complete)."""
    rows = conn.execute(
        "SELECT a.trading_day, b.contract_id, b.timestamp_utc, b.open, b.high, b.low, b.close, b.volume, "
        "b.version_stored_at "
        "FROM bars b JOIN active_contracts a ON a.contract_id = b.contract_id AND a.trading_day = b.trading_day "
        "WHERE a.symbol = %s AND a.trading_day BETWEEN %s AND %s AND b.interval = '1m' "
        "AND b.price_type = 'TRADES' "
        "AND b.timestamp_utc >= (b.trading_day + TIME '09:30') AT TIME ZONE 'America/New_York' "
        "AND b.timestamp_utc < (b.trading_day + TIME '10:31') AT TIME ZONE 'America/New_York' "
        "ORDER BY a.trading_day, b.timestamp_utc;", (symbol, first_day, last_day)).fetchall()
    out: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        d = out.setdefault(str(r[0]), {"contract_id": int(r[1]), "bars": [], "stored": {}, "newest_start": None})
        start = utc(r[2])
        d["bars"].append((start, float(r[3]), float(r[4]), float(r[5]), float(r[6]), float(r[7])))
        d["stored"][start] = r[8]
    newest = conn.execute(
        "SELECT a.trading_day, max(b.timestamp_utc) "
        "FROM bars b JOIN active_contracts a ON a.contract_id = b.contract_id AND a.trading_day = b.trading_day "
        "WHERE a.symbol = %s AND a.trading_day BETWEEN %s AND %s AND b.interval = '1m' "
        "AND b.price_type = 'TRADES' "
        "AND b.timestamp_utc >= (b.trading_day + TIME '09:30') AT TIME ZONE 'America/New_York' "
        "GROUP BY a.trading_day;", (symbol, first_day, last_day)).fetchall()
    for r in newest:
        if str(r[0]) in out:
            out[str(r[0])]["newest_start"] = utc(r[1])
    return out


def _overnight_sums(conn, symbol: str, first_day: str, last_day: str) -> Dict[str, Dict[str, Any]]:
    """{day: {'pv', 'volume', 'stored', 'unknown'}}: of each session's bars before its 09:30 ET open on its active
    contract, the sum of hlc3 x volume and of volume - the Globex day's VWAP as it stood at the open
    (features.calculations.calculate_vwap's anchor) - and when their values last reached the store (``unknown``:
    some have no receipt time)."""
    rows = conn.execute(
        "SELECT a.trading_day, sum((b.high + b.low + b.close) / 3 * b.volume), sum(b.volume), "
        "max(b.version_stored_at), bool_or(b.version_stored_at IS NULL) "
        "FROM bars b JOIN active_contracts a ON a.contract_id = b.contract_id AND a.trading_day = b.trading_day "
        "WHERE a.symbol = %s AND a.trading_day BETWEEN %s AND %s AND b.interval = '1m' "
        "AND b.price_type = 'TRADES' "
        "AND b.timestamp_utc < (b.trading_day + TIME '09:30') AT TIME ZONE 'America/New_York' "
        "GROUP BY a.trading_day;", (symbol, first_day, last_day)).fetchall()
    return {str(r[0]): {"pv": float(r[1] or 0), "volume": float(r[2] or 0), "stored": r[3], "unknown": bool(r[4])}
            for r in rows}


def load_openings(conn, last_day: str, profile: str = defs.DEFAULT_PROFILE,
                  symbol: str = defs.SYMBOL) -> Tuple[Dict[str, mr.Opening], Dict[str, Any]]:
    """
    ``(openings, meta)``: every scheduled session of ``symbol`` to ``last_day`` with first-hour bars stored, by
    date - its confirmed window (matching/rth.completed_window), the context of its newest pre-open snapshot of the
    profile's version (None without one) and its relative-volume baselines. ``meta[day]``:

      stop             why the window ends (complete, awaiting_confirmation, not_stored, gap) and at which minute
      newest_bar_end   the end of the session's newest stored bar (the one still forming included)
      stored           {bar start: when its values reached the store} of the first-hour bars
      inputs_at        when every input of the session the matcher reads - its first-hour bars, overnight bars and
                       snapshot - was in the store (a bound; None when unknown)
    """
    version = defs.PROFILES[profile].snapshot_version
    snaps = {str(s["session_date"]): s for s in store.list_snapshots(conn, "2000-01-01", last_day, version, symbol)}
    row = conn.execute("SELECT min(trading_day) FROM active_contracts WHERE symbol = %s;", (symbol,)).fetchone()
    if row is None or row[0] is None:
        return {}, {}
    first_day = str(row[0])
    receipts = _Receipts(conn)
    bars = _first_hour_bars(conn, symbol, first_day, last_day)
    overnight = _overnight_sums(conn, symbol, first_day, last_day)
    openings: Dict[str, mr.Opening] = {}
    meta: Dict[str, Any] = {}
    for day, d in bars.items():
        try:
            s = cal.session(day)
        except cal.CalendarCoverageError:
            continue
        if not s.is_open:
            continue
        window, stop = mr.completed_window(d["bars"], s.rth_open_at, newest_start=d["newest_start"])
        snap = snaps.get(day)
        on = overnight.get(day, {"pv": 0.0, "volume": 0.0, "stored": None, "unknown": False})
        openings[day] = mr.Opening(day, symbol, d["contract_id"], s.rth_open_at,
                                   context_of(snap, (on["pv"], on["volume"])) if snap else None, tuple(window))
        on_times = ([on["stored"]] + ([None] if on["unknown"] else [])) if day in overnight else []
        meta[day] = {"stop": stop, "newest_bar_end": d["newest_start"] + MINUTE, "stored": d["stored"],
                     "inputs_at": receipts.bound([*d["stored"].values(), *on_times,
                                                  *([snap["built_at"]] if snap else [])]),
                     "overnight_at": receipts.bound(on_times),
                     "snapshot_built_at": utc(snap["built_at"]) if snap else None, "receipts": receipts}
    before = {d: [p.session_date.isoformat() for p in cal.sessions_before(d, rth.RELVOL_SESSIONS)]
              for d in openings}
    baselines = mr.volume_baselines(openings, before)
    for d, op in openings.items():
        op.volume_baseline.update(baselines[d])
    return openings, meta


def provenance(target: mr.Opening, minutes: int, meta: Dict[str, Any], pool_dates: Iterable[str]) -> Dict[str, Any]:
    """
    When the inputs of ``target``'s set at ``minutes`` reached the store: ``inputs_received_at`` - the target's
    window bars, the bar that confirmed the last of them complete, its overnight bars and its snapshot (minus the
    cutoff: how far behind the feed was); ``pool_received_at`` - every earlier session read; ``pit_status``
    'verified' when every earlier input was in the store by the cutoff and the target's within LIVE_MAX_LAG of it
    (as a live issue would have had them), else 'unverified' - inputs stored or revised later, or receipt times
    unknown.
    """
    m = meta[target.session_date]
    receipts = m["receipts"]
    cutoff = target.rth_open_at + minutes * MINUTE
    confirm = [t for t in sorted(m["stored"]) if t >= cutoff][:1]           # the bar after the window, if loaded
    target_at = receipts.bound([*(m["stored"][b[0]] for b in target.bars[:minutes]),
                                *(m["stored"][t] for t in confirm), m["overnight_at"], m["snapshot_built_at"]])
    pool = [meta[d]["inputs_at"] for d in pool_dates if d in meta]
    pool_at = None if any(p is None for p in pool) else (max(pool) if pool else None)
    verified = (pool_at is not None and pool_at <= cutoff and target_at is not None
                and target_at <= cutoff + rth.LIVE_MAX_LAG)
    return {"inputs_received_at": target_at, "pool_received_at": pool_at,
            "pit_status": "verified" if verified else "unverified"}


def build_set(target: mr.Opening, pool: Iterable[mr.Opening], minutes: int, meta: Dict[str, Any],
              issued_by: str, version: str = rth.RTH_MATCHER_VERSION,
              started: Optional[datetime] = None) -> Tuple[Dict[str, Any], List[Dict[str, Any]], Dict[str, Any]]:
    """``(set, members, ranked)`` of ``target`` at ``minutes``, issued by ``issued_by`` (auto, manual, backfill) -
    what save_rth_set stores; the database adds when, and whether that was live. ``version`` v3 (the full session)
    also records the timing a minute-by-minute issue needs: the session's RTH length, when the computation started
    (``started``) and the bar that confirmed the window's last bar - v2's record is unchanged."""
    if issued_by not in rth.ISSUED_BY:
        raise ValueError(f"issued_by must be one of {rth.ISSUED_BY}, not {issued_by!r}")
    if target.context is None:
        raise ValueError(f"{target.session_date} has no pre-open snapshot: no RTH set")
    if target.minutes < minutes:
        raise ValueError(f"{target.session_date} holds {target.minutes} confirmed RTH minute(s), not {minutes}")
    pool = list(pool)
    ranked = mr.rank(target, pool, minutes)
    sims = [m["similarity"] for m in ranked["selected"]]
    m = meta[target.session_date]
    stop = m["stop"] if target.minutes == minutes else {"state": "complete", "minute": None}
    inputs = provenance(target, minutes, meta, [o.session_date for o in pool])
    rec = {
        "symbol": target.symbol, "session_date": target.session_date, "contract_id": target.contract_id,
        "matcher_version": version, "context_snapshot_id": target.context.snapshot_id,
        "elapsed_minutes": minutes, "cutoff_at": target.rth_open_at + minutes * MINUTE,
        "input_digest": mr.input_digest(target, minutes, ranked, version),
        "pool_size": ranked["pool_size"], "pool_hash": ranked["pool_hash"], "excluded": ranked["excluded"],
        "mean_similarity": mr.show(sum(sims) / len(sims)) if sims else None,
        "target_features": {f: mr.show(v) for f, v in ranked["target_features"].items()},
        "quality": {
            "window_minutes": minutes, "provisional": minutes < rth.PROVISIONAL_MINUTES,
            "confirmed_minutes": target.minutes,
            # why the newest window ends where it does (only for the newest window of the session as stored)
            "stopped": stop["state"], "stopped_at": iso(stop["minute"]),
            "newest_bar_end": iso(m["newest_bar_end"]),
            "in_calibration_sample": rth.CALIBRATION["first_session"] <= target.session_date
                                     <= rth.CALIBRATION["last_session"],
        },
        "code_revision": code_revision(), "issued_by": issued_by, **inputs,
    }
    if version == rth.SESSION_VERSION:
        cutoff = rec["cutoff_at"]
        confirm = next((t for t in sorted(m["stored"]) if t >= cutoff), None)
        if confirm is None and m["newest_bar_end"] - MINUTE >= cutoff:
            confirm = m["newest_bar_end"] - MINUTE          # a later bar past the loaded ones (after the close)
        rec.update(session_minutes=m["session_minutes"], computation_started_at=started, confirmed_by_start=confirm,
                   confirmed_received_at=m["receipts"].bound([m["stored"].get(confirm)]) if confirm in m["stored"]
                   else None)
        rec["quality"]["session_minutes"] = m["session_minutes"]
        rec["quality"]["tolerance_extension"] = minutes > 60     # past the calibrated range: descriptive only
    members = [{
        "rank": x["rank"], "session_date": x["opening"].session_date, "contract_id": x["opening"].contract_id,
        "snapshot_id": x["opening"].context.snapshot_id, "similarity": mr.show(x["similarity"]),
        "comparable_weight": mr.show(x["comparable_weight"]),
        "components": {f: {"weight": str(c["weight"]), "tolerance": mr.show(c["tolerance"]),
                           "comparable": c["comparable"], "score": mr.show(c["score"]),
                           "target": mr.show(c["target"]), "analogue": mr.show(c["analogue"]),
                           "difference": mr.show(c["difference"])} for f, c in x["components"].items()},
    } for x in ranked["selected"]]
    return rec, members, ranked


def register(conn) -> None:
    """Registers the RTH matcher's definition and its evaluation's (contracts/rth_eval.py) - before any set or
    evaluation forecast of the run is stored; a changed definition under a registered version name stops the run."""
    from contracts import rth_eval, rth_operational, rth_session
    store.register_version(conn, rth.matcher_record())
    store.register_version(conn, rth_eval.record())
    store.register_version(conn, rth_operational.record())
    store.register_version(conn, rth.session_record())
    store.register_version(conn, rth_session.record())


def _target_snapshot_ok(openings: Dict[str, mr.Opening], day: str) -> Optional[str]:
    """Why ``day`` cannot have an RTH set yet, or None."""
    op = openings.get(day)
    if op is None:
        return "no RTH bar of the session is stored yet"
    if op.context is None:
        return "its pre-open snapshot is not stored yet (the pre-open set stands)"
    if not op.context.atr:
        return "its pre-open snapshot has no valid daily ATR, the matcher's unit"
    if op.minutes == 0:
        return "waiting for the first completed RTH bar (the 09:30 bar is confirmed complete once a later bar is stored)"
    return None


def issue(conn, now: Optional[datetime] = None, day: Optional[str] = None, issued_by: str = "manual",
          profile: str = defs.DEFAULT_PROFILE) -> Dict[str, Any]:
    """
    The session in progress (``day``, else the one the New York date of ``now`` names): its newest confirmed
    window and any of the always-issued windows (15, 30, 45, 60) not stored yet, issued by ``issued_by`` ('auto':
    Auto mode, 'manual': a person) - and, for the evaluations' cutoff windows (15, 30, 45), the forecasts of both
    (forecaster/rth_eval.py: research and operational), stored once per evaluation, session and cutoff. Returns ``{'status': 'issued' | 'waiting' | 'busy'
    | 'closed', 'session_date', 'minutes', 'stored': [(minutes, set_id, new)], 'forecasts': [(minutes, evaluation,
    forecast_id, new)], 'stop', 'reason'}``. Takes a database-wide lock:
    a second issue running at the same time (another process) returns 'busy' and stores nothing.
    """
    if issued_by not in rth.LIVE_ISSUERS:
        raise ValueError(f"an issue is made by one of {rth.LIVE_ISSUERS}; a backfill is reconstruct()")
    now = now or datetime.now(timezone.utc)
    day = day or now.astimezone(cal.NY_TZ).date().isoformat()
    try:
        s = cal.session(day)
    except cal.CalendarCoverageError as e:
        return {"status": "closed", "session_date": day, "reason": str(e), "stored": []}
    if not s.is_open:
        return {"status": "closed", "session_date": day, "reason": f"no session on {day}", "stored": []}
    if now < s.rth_open_at + MINUTE:
        return {"status": "waiting", "session_date": day, "stored": [],
                "reason": "before the open: the pre-open set stands until the first RTH bar is complete"}
    got = conn.execute("SELECT pg_try_advisory_lock(%s);", (_LOCK_KEY,)).fetchone()[0]
    if not got:
        return {"status": "busy", "session_date": day, "stored": [],
                "reason": "another RTH issue is running (another process); nothing stored"}
    try:
        openings, meta = load_openings(conn, day, profile)
        why = _target_snapshot_ok(openings, day)
        if why is not None:
            return {"status": "waiting", "session_date": day, "reason": why, "stored": []}
        target = openings[day]
        newest = min(target.minutes, rth.MAX_MINUTES)
        # windows issued already by Auto or by hand - a backfill of the session does not stand in for an issue
        have = {w["elapsed_minutes"] for w in store.rth_windows(conn, defs.SYMBOL, day, rth.RTH_MATCHER_VERSION)
                if w["issued"]}
        due = sorted({m for m in rth.ALWAYS_ISSUED if m <= newest and m not in have} | {newest})
        pool = [o for d, o in openings.items() if d < day]
        stored, forecasts = [], []
        for m in due:
            rec, members, ranked = build_set(target, pool, m, meta, issued_by)
            set_id, new = store.save_rth_set(conn, rec, members)
            stored.append((m, set_id, new))
            # both evaluations' forecasts of a cutoff window, from this ranking - stored once, when issued
            forecasts.extend((m, *made) for made in rth_eval.issue_forecasts(conn, target, m, ranked, openings,
                                                                              set_id, built_at=now))
        return {"status": "issued", "session_date": day, "minutes": newest, "stored": stored, "reason": None,
                "forecasts": forecasts, "stop": meta[day]["stop"], "newest_bar_end": meta[day]["newest_bar_end"]}
    finally:
        conn.execute("SELECT pg_advisory_unlock(%s);", (_LOCK_KEY,))


def reconstruct(conn, days: List[str], minutes: Iterable[int] = rth.CHECKPOINTS,
                profile: str = defs.DEFAULT_PROFILE) -> Dict[str, Any]:
    """
    Historical reconstructions of ``days`` at each of ``minutes`` (default the checkpoints), issued by 'backfill' -
    never live, whenever made. A day or window that cannot be matched is reported, not filled. Returns ``{'new': n,
    'already': n, 'skipped': {day: reason}}``.
    """
    if not days:
        return {"new": 0, "already": 0, "skipped": {}}
    openings, meta = load_openings(conn, max(days), profile)
    new = already = 0
    skipped: Dict[str, str] = {}
    for day in sorted(days):
        why = _target_snapshot_ok(openings, day)
        if why is not None:
            skipped[day] = why
            continue
        target = openings[day]
        pool = [o for d, o in openings.items() if d < day]
        short = [m for m in minutes if m > target.minutes]
        if short:
            stop = meta[day]["stop"]
            skipped[day] = (f"{target.minutes} confirmed RTH minute(s) stored ({stop_text(stop)})"
                            + f": no window of {', '.join(map(str, short))}")
        for m in minutes:
            if m > target.minutes:
                continue
            rec, members, _ = build_set(target, pool, m, meta, "backfill")
            _, created = store.save_rth_set(conn, rec, members)
            new += created
            already += not created
    logger.info(f"RTH analogues ({rth.RTH_MATCHER_VERSION}): {new} new set(s), {already} already stored, "
                f"{len(skipped)} session(s) skipped or short.")
    return {"new": new, "already": already, "skipped": skipped}


def due_window(now: datetime) -> Optional[str]:
    """The session whose RTH sets an Auto run issues at ``now`` - the first hour's (first_hour_due) or the full
    session's (session_due) - else None: what the dashboard's Auto and Run collector schedule the rth step by."""
    return first_hour_due(now) or session_due(now)


def first_hour_due(now: datetime) -> Optional[str]:
    """The session whose first-hour (v2) sets an Auto run issues at ``now``: from the open's first completed minute
    until 30 minutes after 10:30 ET (a delayed feed still brings the last windows), else None."""
    today = now.astimezone(cal.NY_TZ).date()
    try:
        s = cal.session(today)
    except cal.CalendarCoverageError:
        return None
    if not s.is_open:
        return None
    end = s.rth_open_at + rth.MAX_MINUTES * MINUTE + rth.LIVE_MAX_LAG
    return today.isoformat() if s.rth_open_at + MINUTE <= now <= end else None


def stop_text(stop: Dict[str, Any], session: bool = False) -> str:
    """Why a session's confirmed window ends, in words (``session``: the full-session matcher's, which completes at
    the RTH close)."""
    at = utc(stop.get("minute"))
    hm = f"{at.astimezone(cal.NY_TZ):%H:%M} ET" if at else ""
    return {"complete": "the whole regular session confirmed - RTH closed" if session else "the whole first hour confirmed",
            "awaiting_confirmation": f"the {hm} bar is stored, awaiting confirmation (a later bar) - it may still "
                                     f"be forming",
            "not_stored": f"the {hm} bar is not stored yet - the feed is behind",
            "gap": f"the {hm} bar is missing while later bars are stored - a confirmed gap; the window stops "
                   f"before it, nothing is filled in"}[stop["state"]]


def describe(aset: Dict[str, Any]) -> str:
    """One line naming a stored set: 'RTH analogues - first 23 minutes - data through 09:53 ET', then checkpoint,
    provisional, how and when it was issued, the feed's delay, and whether its inputs are verified as of the
    cutoff."""
    n = int(aset["elapsed_minutes"])
    cutoff, created = utc(aset["cutoff_at"]), utc(aset["created_at"])
    text = (f"RTH analogues — first {n} minute{'s' if n != 1 else ''} — data through "
            f"{cutoff.astimezone(cal.NY_TZ):%H:%M} ET")
    tags = []
    if aset.get("checkpoint"):
        tags.append(f"{aset['checkpoint']}-minute checkpoint")
    if aset["quality"].get("provisional"):
        tags.append("provisional: little of the opening observed")
    by = {"auto": "by Auto", "manual": "by hand", "backfill": "by a backfill"}.get(aset.get("issued_by"), "")
    lag = (created - cutoff).total_seconds() / 60
    if aset["data_mode"] == "live":
        tags.append(f"issued live {by} at {created.astimezone(cal.NY_TZ):%H:%M} ET")
    else:
        tags.append(f"historical reconstruction {by}, stored {created.astimezone(cal.NY_TZ):%Y-%m-%d %H:%M} ET")
    received = utc(aset.get("inputs_received_at"))
    if aset["data_mode"] == "live" and received is not None:
        delay = (received - cutoff).total_seconds() / 60
        tags.append(f"window's bars in the store {delay:.0f} min after its cutoff" if delay >= 1
                    else "window's bars in the store within a minute of its cutoff")
    elif aset["data_mode"] == "live" and lag >= 2:
        tags.append(f"{lag:.0f} min after its cutoff")
    if aset.get("pit_status") == "verified":
        tags.append("inputs verified as of the cutoff")
    elif aset.get("pit_status") == "unverified":
        tags.append("inputs not verifiable as of the cutoff")
    if aset["quality"].get("in_calibration_sample"):
        tags.append("its session is in the tolerances' calibration sample: descriptive, not a forward test")
    if aset.get("matcher_version") == rth.SESSION_VERSION:
        cls = aset.get("issue_class")
        if cls == "timely":
            tags.append("timely: stored within 2 min of its last input reaching the store")
        elif cls == "late":
            tags.append("late: stored after its inputs had been in the store a while (a catch-up)")
        if aset.get("session_minutes") and n >= int(aset["session_minutes"]):
            tags.append("RTH closed: the session's last window")
        if aset["quality"].get("tolerance_extension"):
            tags.append("past 60 minutes the tolerances are a descriptive extension, not validated")
        tags.append(f"matcher {rth.SESSION_VERSION}")
    return text + (" · " + " · ".join(tags) if tags else "")


def windows_over(day: str, through: Optional[datetime], targets: Iterable[str]) -> Dict[str, str]:
    """{target: 'HH:MM'} - each of ``targets`` (P1 label targets) whose window (contracts/nq_prompt_v2.TARGETS
    window_et; a 16:00 end is the scheduled close on an early close) ended by ``through``, with its end in ET: for a
    session in progress its outcome is then observed information, not something still to forecast."""
    if through is None:
        return {}
    try:
        s = cal.session(day)
    except cal.CalendarCoverageError:
        return {}
    if not s.is_open:
        return {}
    out = {}
    for t in targets:
        end = defs.TARGETS[t]["window_et"][1]
        at = min(cal.ny_instant(s.session_date, time.fromisoformat(end)), s.scheduled_close_at)
        if at <= through:
            out[t] = f"{at.astimezone(cal.NY_TZ):%H:%M}"
    return out


def newest_bar_end(conn, day: str, symbol: str = defs.SYMBOL) -> Optional[datetime]:
    """The end of the newest stored 1-minute bar of ``day`` on its active contract (the one still forming
    included), or None."""
    row = conn.execute(
        "SELECT max(b.timestamp_utc) FROM bars b JOIN active_contracts a ON a.contract_id = b.contract_id "
        "AND a.trading_day = b.trading_day WHERE a.symbol = %s AND b.trading_day = %s AND b.interval = '1m' "
        "AND b.price_type = 'TRADES';", (symbol, day)).fetchone()
    return None if row is None or row[0] is None else utc(row[0]) + MINUTE


# --------------------------------------------------------------------------
# The full-session matcher (nq_match_rth_v3): loading, its cache, the issue
# --------------------------------------------------------------------------
#
# A v3 issue runs every Auto minute from the open to the close. Re-reading every earlier session's whole regular
# session each minute is wasted work: their bars only change when the vendor revises them. So the earlier sessions'
# loaded inputs - bars, receipt times, overnight sums, frozen context, relative-volume baselines - are kept on disk
# (data/rth_cache) under a fingerprint per trading day (its bars' count, latest receipt and latest start on its active
# contract; the snapshot's id and build time). A changed fingerprint reloads that day; the session in progress is read
# every time. The cache only saves time: load_session_openings(cache=False) returns the same and a test checks it.

import os
import pickle
import tempfile
from typing import Sequence

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_DIR = os.path.join(_ROOT, "data", "rth_cache")
_CACHE_FORMAT = 1


def _session_rows(conn, symbol: str, days: Sequence[str]) -> Dict[str, Dict[str, Any]]:
    """{day: {'contract_id', 'bars', 'stored', 'newest_start'}} of ``days``: each session's 1-minute bars in [09:30,
    16:01) ET on its active contract - the regular session to the latest close and the bar after it, which confirms
    the last one (an early close is cut by the calendar later) - when each one's values reached the store, and the
    start of the session's newest stored bar from 09:30 on."""
    out: Dict[str, Dict[str, Any]] = {}
    if not days:
        return out
    for r in conn.execute(
            "SELECT a.trading_day, b.contract_id, b.timestamp_utc, b.open, b.high, b.low, b.close, b.volume, "
            "b.version_stored_at "
            "FROM bars b JOIN active_contracts a ON a.contract_id = b.contract_id AND a.trading_day = b.trading_day "
            "WHERE a.symbol = %s AND a.trading_day = ANY(%s::date[]) AND b.interval = '1m' "
            "AND b.price_type = 'TRADES' "
            "AND b.timestamp_utc >= (b.trading_day + TIME '09:30') AT TIME ZONE 'America/New_York' "
            "AND b.timestamp_utc < (b.trading_day + TIME '16:01') AT TIME ZONE 'America/New_York' "
            "ORDER BY a.trading_day, b.timestamp_utc;", (symbol, list(days))).fetchall():
        d = out.setdefault(str(r[0]), {"contract_id": int(r[1]), "bars": [], "stored": {}, "newest_start": None})
        start = utc(r[2])
        d["bars"].append((start, float(r[3]), float(r[4]), float(r[5]), float(r[6]), float(r[7])))
        d["stored"][start] = r[8]
    for r in conn.execute(
            "SELECT a.trading_day, max(b.timestamp_utc) "
            "FROM bars b JOIN active_contracts a ON a.contract_id = b.contract_id AND a.trading_day = b.trading_day "
            "WHERE a.symbol = %s AND a.trading_day = ANY(%s::date[]) AND b.interval = '1m' "
            "AND b.price_type = 'TRADES' "
            "AND b.timestamp_utc >= (b.trading_day + TIME '09:30') AT TIME ZONE 'America/New_York' "
            "GROUP BY a.trading_day;", (symbol, list(days))).fetchall():
        if str(r[0]) in out:
            out[str(r[0])]["newest_start"] = utc(r[1])
    return out


def _overnight_for(conn, symbol: str, days: Sequence[str]) -> Dict[str, Dict[str, Any]]:
    """_overnight_sums for ``days`` only - summed exactly (numeric), so the same bars always give the same value: a
    floating-point sum in the database depends on the order its plan adds the rows in."""
    if not days:
        return {}
    rows = conn.execute(
        "SELECT a.trading_day, sum(((b.high + b.low + b.close) / 3 * b.volume)::numeric), sum(b.volume), "
        "max(b.version_stored_at), bool_or(b.version_stored_at IS NULL) "
        "FROM bars b JOIN active_contracts a ON a.contract_id = b.contract_id AND a.trading_day = b.trading_day "
        "WHERE a.symbol = %s AND a.trading_day = ANY(%s::date[]) AND b.interval = '1m' "
        "AND b.price_type = 'TRADES' "
        "AND b.timestamp_utc < (b.trading_day + TIME '09:30') AT TIME ZONE 'America/New_York' "
        "GROUP BY a.trading_day;", (symbol, list(days))).fetchall()
    return {str(r[0]): {"pv": float(r[1] or 0), "volume": float(r[2] or 0), "stored": r[3], "unknown": bool(r[4])}
            for r in rows}


def _fingerprints(conn, symbol: str, last_day: str) -> Dict[str, Tuple]:
    """Per trading day to ``last_day``: (active contract, its bars' count, latest receipt, latest start) - any stored,
    revised or newly received bar of the day changes it."""
    rows = conn.execute(
        "SELECT a.trading_day, a.contract_id, count(*), max(b.version_stored_at), max(b.timestamp_utc) "
        "FROM bars b JOIN active_contracts a ON a.contract_id = b.contract_id AND a.trading_day = b.trading_day "
        "WHERE a.symbol = %s AND a.trading_day <= %s AND b.interval = '1m' AND b.price_type = 'TRADES' "
        "GROUP BY a.trading_day, a.contract_id;", (symbol, last_day)).fetchall()
    return {str(r[0]): (int(r[1]), int(r[2]), str(r[3]), str(r[4])) for r in rows}


def _context_payloads(conn, snapshot_ids: Sequence[str]) -> Dict[str, Dict[str, Any]]:
    """{snapshot_id: the part of its payload the matcher reads (the daily ATR and three references)}."""
    import json
    if not snapshot_ids:
        return {}
    rows = conn.execute(
        "SELECT snapshot_id, payload -> 'atr' -> 'daily', payload -> 'references' -> 'prev_rth_close', "
        "payload -> 'references' -> 'on_high', payload -> 'references' -> 'on_low' "
        "FROM journal.snapshots WHERE snapshot_id = ANY(%s::uuid[]);", (list(snapshot_ids),)).fetchall()
    load = lambda v: None if v is None else json.loads(v) if isinstance(v, str) else v       # noqa: E731
    return {str(r[0]): {"atr": {"daily": load(r[1])}, "references": {
        "prev_rth_close": load(r[2]), "on_high": load(r[3]), "on_low": load(r[4])}} for r in rows}


def _cache_path(symbol: str) -> str:
    return os.path.join(CACHE_DIR, f"{rth.SESSION_VERSION}_{symbol}.pkl")


def _read_cache(symbol: str) -> Dict[str, Any]:
    try:
        with open(_cache_path(symbol), "rb") as f:
            cache = pickle.load(f)
        if cache.get("format") == _CACHE_FORMAT and cache.get("version") == rth.SESSION_VERSION:
            return cache
    except (OSError, EOFError, pickle.UnpicklingError, AttributeError, ImportError):
        pass
    return {"format": _CACHE_FORMAT, "version": rth.SESSION_VERSION, "days": {}, "contexts": {}, "baselines": {}}


def _write_cache(symbol: str, cache: Dict[str, Any]) -> None:
    """Atomically (a reader never sees half a file); a failure to write only costs the next run time."""
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=CACHE_DIR, suffix=".tmp")
        with os.fdopen(fd, "wb") as f:
            pickle.dump(cache, f, protocol=pickle.HIGHEST_PROTOCOL)
        os.replace(tmp, _cache_path(symbol))
    except OSError as e:
        logger.warning(f"RTH cache not written: {e}")


def load_session_openings(conn, last_day: str, profile: str = defs.DEFAULT_PROFILE, symbol: str = defs.SYMBOL,
                          cache: bool = True) -> Tuple[Dict[str, mr.Opening], Dict[str, Any]]:
    """
    load_openings for the full-session matcher (v3): every scheduled session to ``last_day`` with RTH bars, its
    confirmed window to its scheduled RTH close (the calendar's: 390 minutes, 210 on an early close), the context of
    its newest pre-open snapshot, its relative-volume baselines to the close - and ``meta`` as load_openings', with
    ``session_minutes``. ``cache``: earlier sessions' inputs from data/rth_cache, reloaded when their fingerprint
    changes; ``last_day`` is always read afresh.
    """
    version = defs.PROFILES[profile].snapshot_version
    receipts = _Receipts(conn)
    prints = _fingerprints(conn, symbol, last_day)
    refs = {str(r[0]): (str(r[1]), r[2]) for r in conn.execute(
        "SELECT DISTINCT ON (session_date) session_date, snapshot_id, built_at FROM journal.snapshots "
        "WHERE snapshot_version = %s AND symbol = %s AND session_date <= %s ORDER BY session_date, built_at DESC;",
        (version, symbol, last_day)).fetchall()}
    c = _read_cache(symbol) if cache else {"days": {}, "contexts": {}, "baselines": {}}
    dirty = False                                        # the cache is written only when an earlier session changed
    stale = sorted(d for d, fp in prints.items() if d == last_day or c["days"].get(d, {}).get("fp") != fp)
    rows, overnight = _session_rows(conn, symbol, stale), _overnight_for(conn, symbol, stale)
    for d in stale:
        dirty |= d != last_day
        if d in rows:
            c["days"][d] = {"fp": prints[d], **rows[d],
                            "overnight": overnight.get(d, {"pv": 0.0, "volume": 0.0, "stored": None, "unknown": False}),
                            "has_overnight": d in overnight}
        else:
            c["days"].pop(d, None)                       # no RTH bar of the day (yet)
    for d in [d for d in c["days"] if d not in prints]:  # a day no longer stored
        del c["days"][d]
        dirty = True
    missing = [sid for sid, _ in refs.values() if sid not in c["contexts"]]
    if missing:
        c["contexts"].update(_context_payloads(conn, missing))
        dirty = True
    openings: Dict[str, mr.Opening] = {}
    meta: Dict[str, Any] = {}
    for day in sorted(c["days"]):
        d = c["days"][day]
        try:
            s = cal.session(day)
        except cal.CalendarCoverageError:
            continue
        if not s.is_open or d["newest_start"] is None:
            continue
        L = rth.session_minutes(s)
        if "window" not in d:                            # computed once per loaded day, kept with it
            d["window"], d["stop"] = mr.completed_window(d["bars"], s.rth_open_at, limit=L,
                                                         newest_start=d["newest_start"])
            dirty |= day != last_day
        window, stop = d["window"], d["stop"]
        ref = refs.get(day)
        snap = None if ref is None else {"snapshot_id": ref[0], "built_at": ref[1], "payload": c["contexts"][ref[0]]}
        on = d["overnight"]
        openings[day] = mr.Opening(day, symbol, d["contract_id"], s.rth_open_at,
                                   context_of(snap, (on["pv"], on["volume"])) if snap else None, tuple(window))
        on_times = ([on["stored"]] + ([None] if on["unknown"] else [])) if d["has_overnight"] else []
        stored = {t: v for t, v in d["stored"].items() if t < s.scheduled_close_at + MINUTE}
        meta[day] = {"stop": stop, "newest_bar_end": d["newest_start"] + MINUTE, "stored": stored,
                     "inputs_at": receipts.bound([*(v for t, v in stored.items() if t < s.scheduled_close_at),
                                                  *on_times, *([snap["built_at"]] if snap else [])]),
                     "overnight_at": receipts.bound(on_times),
                     "snapshot_built_at": utc(snap["built_at"]) if snap else None, "receipts": receipts,
                     "session_minutes": L}
    # relative-volume baselines to the close, kept per day while its own and its 20 earlier sessions' fingerprints
    # stand (the session in progress is always recomputed)
    before = {d: [p.session_date.isoformat() for p in cal.sessions_before(d, rth.RELVOL_SESSIONS)] for d in openings}
    for d, op in openings.items():
        key = (prints.get(d), tuple(prints.get(p) for p in before[d]))
        hit = c["baselines"].get(d)
        if hit is not None and hit[0] == key and d != last_day:
            op.volume_baseline.update(hit[1])
            continue
        base = mr.volume_baselines({x: openings[x] for x in [d, *before[d]] if x in openings}, {d: before[d]},
                                   rth.SESSION_MAX_MINUTES)[d]
        op.volume_baseline.update(base)
        if d != last_day:
            c["baselines"][d] = (key, base)
            dirty = True
    if cache and dirty:                                  # the session in progress is never kept: it is read anew
        _write_cache(symbol, {**c, "days": {d: v for d, v in c["days"].items() if d != last_day},
                              "baselines": {d: v for d, v in c["baselines"].items() if d != last_day}})
    return openings, meta


def session_due(now: datetime) -> Optional[str]:
    """The session whose full-session (v3) sets an Auto run issues at ``now``: from the open's first completed minute
    until SESSION_GRACE after the scheduled RTH close (the delayed feed brings the last bars, and the bar after the
    close that confirms them, after it), else None. A grace for collection - never a forecast rule."""
    today = now.astimezone(cal.NY_TZ).date()
    try:
        s = cal.session(today)
    except cal.CalendarCoverageError:
        return None
    if not s.is_open:
        return None
    return today.isoformat() if s.rth_open_at + MINUTE <= now <= s.scheduled_close_at + rth.SESSION_GRACE else None


def _misses(prev: Optional[Dict[str, Any]], newest: int, stored: Iterable[int], expired: Iterable[int],
            started: datetime, day: str, issued_by: str) -> List[Dict[str, Any]]:
    """The windows this issue does not store, as journal.rth_issue_misses rows: those confirmed since the last live
    issue (``prev``) but before the newest - 'coalesced' when the last issue ran within two minutes (their bars arrived
    together), 'not_running' otherwise (no issue ran) - and the evaluation cutoffs too old to issue live ('expired')."""
    done = set(stored) | set(expired)
    first = 1 if prev is None else prev["elapsed_minutes"] + 1
    skipped = [n for n in range(first, newest) if n not in done]
    out, run = [], []
    recent = prev is not None and started - utc(prev["created_at"]) <= timedelta(minutes=2)
    reason = "coalesced" if recent else "not_running"
    detail = (f"confirmed together with later minutes between issues ({utc(prev['created_at']).astimezone(cal.NY_TZ):%H:%M:%S} "
              f"and {started.astimezone(cal.NY_TZ):%H:%M:%S} ET)" if recent else
              "no live issue ran while these windows were current" + (
                  f" (the last at {utc(prev['created_at']).astimezone(cal.NY_TZ):%H:%M:%S} ET)" if prev else
                  " (the session's first issue)"))
    for n in skipped + [None]:                           # contiguous runs
        if run and (n is None or n != run[-1] + 1):
            out.append({"first_minutes": run[0], "last_minutes": run[-1], "reason": reason, "detail": detail})
            run = []
        if n is not None:
            run.append(n)
    for n in sorted(expired):
        out.append({"first_minutes": n, "last_minutes": n, "reason": "expired",
                    "detail": "an evaluation cutoff never issued live, now older than the 30-minute live limit: not "
                              "issued afterwards"})
    return [{**r, "symbol": defs.SYMBOL, "session_date": day, "matcher_version": rth.SESSION_VERSION,
             "issued_by": issued_by} for r in out]


def issue_session(conn, now: Optional[datetime] = None, day: Optional[str] = None, issued_by: str = "manual",
                  profile: str = defs.DEFAULT_PROFILE) -> Dict[str, Any]:
    """
    The full-session (v3) sets of the session in progress: the newest confirmed window - however late in the
    session, to its scheduled RTH close - and any cutoff window of rth_session_v1 not stored yet and still within the
    30-minute live limit, with that evaluation's forecasts (forecaster/rth_eval.py); the windows it does not store
    are recorded with the reason (journal.rth_issue_misses), never stored later under a made-up time. Returns as
    issue(), with ``closed`` once the session's last window is stored and ``misses``. Shares issue()'s lock.
    """
    from contracts import rth_session as rs
    if issued_by not in rth.LIVE_ISSUERS:
        raise ValueError(f"an issue is made by one of {rth.LIVE_ISSUERS}; a backfill is reconstruct_session()")
    started = datetime.now(timezone.utc)
    now = now or started
    day = day or now.astimezone(cal.NY_TZ).date().isoformat()
    try:
        s = cal.session(day)
    except cal.CalendarCoverageError as e:
        return {"status": "closed", "session_date": day, "reason": str(e), "stored": [], "version": rth.SESSION_VERSION}
    if not s.is_open:
        return {"status": "closed", "session_date": day, "reason": f"no session on {day}", "stored": [],
                "version": rth.SESSION_VERSION}
    if now < s.rth_open_at + MINUTE:
        return {"status": "waiting", "session_date": day, "stored": [], "version": rth.SESSION_VERSION,
                "reason": "before the open: the pre-open set stands until the first RTH bar is complete"}
    got = conn.execute("SELECT pg_try_advisory_lock(%s);", (_LOCK_KEY,)).fetchone()[0]
    if not got:
        return {"status": "busy", "session_date": day, "stored": [], "version": rth.SESSION_VERSION,
                "reason": "another RTH issue is running (another process); nothing stored"}
    try:
        openings, meta = load_session_openings(conn, day, profile)
        why = _target_snapshot_ok(openings, day)
        if why is not None:
            return {"status": "waiting", "session_date": day, "reason": why, "stored": [],
                    "version": rth.SESSION_VERSION}
        target, L = openings[day], meta[day]["session_minutes"]
        newest = min(target.minutes, L)
        prev = store.rth_last_issue(conn, defs.SYMBOL, day, rth.SESSION_VERSION)
        issued = {w["elapsed_minutes"] for w in store.rth_windows(conn, defs.SYMBOL, day, rth.SESSION_VERSION)
                  if w["issued"]}
        pending = [n for n in rs.CUTOFFS if n < newest and n not in issued]
        expired = [n for n in pending if now > target.rth_open_at + n * MINUTE + rth.LIVE_MAX_LAG]
        recorded = {m["first_minutes"] for m in store.rth_misses(conn, defs.SYMBOL, day, rth.SESSION_VERSION)
                    if m["reason"] == "expired"}
        due = sorted(({n for n in pending if n not in expired}) | {newest})
        pool = [o for d, o in openings.items() if d < day]
        stored, forecasts = [], []
        for n in due:
            rec, members, ranked = build_set(target, pool, n, meta, issued_by, rth.SESSION_VERSION, started)
            set_id, new = store.save_rth_set(conn, rec, members)
            stored.append((n, set_id, new))
            forecasts.extend((n, *made) for made in rth_eval.issue_session_forecasts(
                conn, target, n, ranked, openings, set_id, L, built_at=now))
        advanced = prev is None or newest > prev["elapsed_minutes"]
        misses = _misses(prev if advanced else {"elapsed_minutes": newest, "created_at": started}, newest, due,
                         [n for n in expired if n not in recorded], started, day, issued_by)
        store.save_rth_misses(conn, misses)
        return {"status": "issued", "session_date": day, "minutes": newest, "stored": stored, "reason": None,
                "forecasts": forecasts, "stop": meta[day]["stop"], "newest_bar_end": meta[day]["newest_bar_end"],
                "closed": newest >= L, "session_minutes": L, "misses": misses, "version": rth.SESSION_VERSION}
    finally:
        conn.execute("SELECT pg_advisory_unlock(%s);", (_LOCK_KEY,))


def reconstruct_session(conn, days: List[str], minutes: Optional[Iterable[int]] = None,
                        profile: str = defs.DEFAULT_PROFILE) -> Dict[str, Any]:
    """Full-session (v3) reconstructions of ``days`` at ``minutes`` (default: the evaluation's cutoffs and the
    session's last window), issued by 'backfill' - never live. As reconstruct()."""
    from contracts import rth_session as rs
    if not days:
        return {"new": 0, "already": 0, "skipped": {}}
    openings, meta = load_session_openings(conn, max(days), profile)
    new = already = 0
    skipped: Dict[str, str] = {}
    for day in sorted(days):
        why = _target_snapshot_ok(openings, day)
        if why is not None:
            skipped[day] = why
            continue
        target, L = openings[day], meta[day]["session_minutes"]
        want = sorted(set(minutes) if minutes is not None else {*(n for n in rs.CUTOFFS if n <= L), L})
        pool = [o for d, o in openings.items() if d < day]
        short = [m for m in want if m > target.minutes]
        if short:
            skipped[day] = (f"{target.minutes} confirmed RTH minute(s) stored ({stop_text(meta[day]['stop'])})"
                            + f": no window of {', '.join(map(str, short))}")
        for m in want:
            if m > min(target.minutes, L):
                continue
            rec, members, _ = build_set(target, pool, m, meta, "backfill", rth.SESSION_VERSION)
            _, created = store.save_rth_set(conn, rec, members)
            new += created
            already += not created
    logger.info(f"RTH analogues ({rth.SESSION_VERSION}): {new} new set(s), {already} already stored, "
                f"{len(skipped)} session(s) skipped or short.")
    return {"new": new, "already": already, "skipped": skipped}
