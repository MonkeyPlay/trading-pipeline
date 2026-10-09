# forecaster/live_capture.py
"""
Live capture and issuance (guideline revision 2, 3D; issue policy nq_issue_live_v3
in contracts/nq_forecast.py): the pre-open job, separate from the collector's
after-the-fact catch-up.

    python scripts/nq_journal.py live            # before the open on a trading day
    python scripts/nq_journal.py live --profile candidate_0915 --wait-minutes 13 --with-d

``capture(conn, app, day)`` for one session and profile:

  1. a capture row (journal.live_captures) with its settings - the data-wait limit,
     the reserve kept for issuing, whether D is part of it; the wait plus the reserve
     must end by the deadline, or nothing starts. Every later step is an event stamped
     by the database clock (journal.live_capture_events), so the end-to-end timing
     is measured on the server
  2. restart: a session that already has a live snapshot reuses it - never a new
     one from later bars - and carries on; after the open nothing new is frozen
  3. waits for the cutoff, then asks IB for the session's 1m bars from the overnight
     start - every LIVE_RETRY_S seconds at first, every LIVE_SLOW_RETRY_S later - until
     the bar ending at the cutoff is among them, at most the data-wait limit after it;
     without it the capture is stale (a missed opportunity, recorded with the newest
     bar received) and stops - no snapshot from older bars. However long the wait, the
     evidence ends at the cutoff: later bars are stored, never used
  4. stores the day's bars as the collector does (the last minutes not completed,
     so the regular collector settles the day later) and one receipt per completed
     bar (journal.bar_receipts, database time)
  5. freezes the live snapshot - same snapshot version as the historical pool, data
     mode live_capture, point-in-time verified when every input shows it was known
     (features/nq_evidence._availability) - annotates it, matches it against the
     earlier sessions (outcomes known as of its cutoff), and issues both arms in mode
     live: the database stamps the issue time and marks a run after 09:29:50 ET late;
     an issued run is acknowledged after its commit
  6. with arm D (``synthesis``, forecaster/live_synthesis.py; approved by hand): its
     request from the same live evidence, awaited until the deadline; then the forecast
     in force is recorded (delivered: D when timely, else B, else A, else none), and a
     late answer is still stored as late

The IB connection (``app``) is passed in, so a test can stand a fake in its place;
``clock`` and ``sleep`` likewise. Nothing here has run against IB on a trading day
yet: unit coverage is not live operation.
"""

from __future__ import annotations

import logging
import time as _time
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional

import psycopg

from contracts import nq_forecast as fc
from contracts import nq_preopen as pre
from contracts import nq_prompt_v2 as defs
from database import journal_store as store
from features import calendar as cal
from features.nq_evidence import SnapshotError, build_snapshot
from forecaster.provenance import code_revision

logger = logging.getLogger(__name__)
MINUTE = timedelta(minutes=1)


class LiveCaptureError(RuntimeError):
    pass


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _bar_time(text: str) -> datetime:
    return datetime.strptime(text[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)


def active_contract(conn, day: str):
    """The session's NQ contract: the collector's assignment, or the front contract of the stored chain by the roll
    rule (then recorded as the assignment). Raises when no stored contract covers the session."""
    from collector.ib_collector import _instrument_for
    from collector.rolls import front_contracts
    from database.queries import get_active_contract, list_future_chain, set_active_contracts
    row = get_active_contract(conn, defs.SYMBOL, day)
    if row is None:
        instrument = _instrument_for(defs.SYMBOL)
        assignment = front_contracts(list_future_chain(conn, defs.SYMBOL), [date.fromisoformat(day)], instrument.roll)
        front = assignment.get(date.fromisoformat(day))
        if front is not None:
            set_active_contracts(conn, defs.SYMBOL, {day: front["contract_id"]}, instrument.roll.describe())
            row = get_active_contract(conn, defs.SYMBOL, day)
    if row is None:
        raise LiveCaptureError(f"no stored {defs.SYMBOL} contract covers {day}: run the collector first")
    return row


def fresh_bars(app, contract_info: Dict[str, Any], session: cal.Session, cutoff: datetime, what_to_show: str,
               clock: Callable[[], datetime], sleep: Callable[[float], None], event: Callable[..., Any],
               wait_s: float = fc.LIVE_DEFAULT_WAIT_S) -> Optional[List[Dict[str, Any]]]:
    """The session's bars once the one ending at the cutoff has arrived; None when it has not within ``wait_s``
    after the cutoff (the capture is then stale). The last attempt is made at the limit itself."""
    first = cutoff + timedelta(seconds=fc.LIVE_FIRST_REQUEST_S)
    if clock() < first:
        sleep((first - clock()).total_seconds())
    give_up = cutoff + timedelta(seconds=wait_s)
    want = (cutoff - MINUTE).strftime("%Y-%m-%d %H:%M:%S")
    day = session.session_date.isoformat()
    attempt = 0
    while True:
        now = clock()
        seconds = int((now - session.overnight_start_at).total_seconds()) + 60
        attempt += 1
        bars = app.fetch_historical_bars(contract_info, now, f"{seconds} S", what_to_show=what_to_show)
        day_bars = [b for b in bars or [] if b.get("trading_day") == day]
        newest = max((b["timestamp_utc"] for b in day_bars), default=None)
        event("bars_requested", attempt=attempt, duration=f"{seconds} S", newest=newest)
        if any(b["timestamp_utc"] == want for b in day_bars):
            return day_bars
        now = clock()
        if now >= give_up:
            return None
        pause = fc.LIVE_RETRY_S if now - cutoff < timedelta(seconds=fc.LIVE_FAST_S) else fc.LIVE_SLOW_RETRY_S
        sleep(min(pause, (give_up - now).total_seconds()))


def reserve_s(with_d: bool) -> int:
    """The time a capture keeps before the deadline for issuing (contracts/nq_forecast.LIVE_RESERVE_S)."""
    return fc.LIVE_RESERVE_S["D" if with_d else "AB"]


def check_wait(day: str, profile: str, wait_s: float, with_d: bool) -> None:
    """Raises LiveCaptureError when the data wait and the reserve for issuing would end after the deadline."""
    p = defs.PROFILES[profile]
    d = date.fromisoformat(day)
    cutoff, deadline = cal.ny_instant(d, p.cutoff), cal.ny_instant(d, fc.LIVE_DEADLINE_ET)
    ends = cutoff + timedelta(seconds=wait_s + reserve_s(with_d))
    if wait_s <= 0 or ends > deadline:
        latest = (deadline - cutoff).total_seconds() - reserve_s(with_d)
        raise LiveCaptureError(
            f"a {wait_s / 60:.1f}-minute data wait after the {p.cutoff:%H:%M} cutoff and {reserve_s(with_d)} s for "
            f"issuing {'A, B and D' if with_d else 'A and B'} end at {ends.astimezone(cal.NY_TZ):%H:%M:%S} ET, after "
            f"the {fc.LIVE_DEADLINE_ET:%H:%M:%S} deadline - "
            + (f"at most {latest / 60:.1f} minutes fit" if latest > 0 else f"no wait fits after a {p.cutoff:%H:%M} "
                                                                             f"cutoff"))


def _store_bars(conn, capture_id: str, row, day: str, bars: List[Dict[str, Any]], now: datetime) -> Dict[str, Any]:
    """The day's bars through the collector's write path, and a receipt for every bar complete when received."""
    from collector.ib_collector import INTERVAL_LABEL, _build_day_bars, _instrument_for
    from collector.coverage import day_expectation
    from database.queries import save_trading_day
    instrument = _instrument_for(defs.SYMBOL)
    rows = _build_day_bars(bars, day, now)
    expected, expected_rth = day_expectation(instrument, date.fromisoformat(day))
    save_trading_day(conn, contract_id=int(row["contract_id"]), trading_day=day, bars=rows, interval=INTERVAL_LABEL,
                     price_type=instrument.what_to_show, source="IBKR", expected_bar_count=expected,
                     expected_rth_bar_count=expected_rth)
    complete = [(_bar_time(b["timestamp_utc"]), float(b["open"]), float(b["high"]), float(b["low"]),
                 float(b["close"]), int(b["volume"])) for b in rows if _bar_time(b["timestamp_utc"]) + MINUTE <= now]
    store.save_bar_receipts(conn, capture_id, int(row["contract_id"]), complete, INTERVAL_LABEL,
                            instrument.what_to_show)
    return {"bars": len(rows), "receipts": len(complete),
            "last_bar": complete[-1][0].strftime("%Y-%m-%d %H:%M:%S") if complete else None}


def capture(conn, app, day: str, profile: str = defs.DEFAULT_PROFILE, protocol: str = pre.RULES_PROTOCOL_VERSION,
            clock: Callable[[], datetime] = _utc_now, sleep: Callable[[float], None] = _time.sleep,
            synthesis=None, wait_s: float = fc.LIVE_DEFAULT_WAIT_S) -> Dict[str, Any]:
    """Captures, freezes and issues one session live (see the module docstring); returns a summary. ``synthesis``
    (a live_synthesis.LiveSynthesis) adds arm D; without it nothing is sent to Claude. ``wait_s``: the data-wait
    limit after the cutoff - with the reserve for issuing it must end by the deadline (check_wait), or
    LiveCaptureError is raised before anything is recorded."""
    from collector.ib_collector import _contract_info, _instrument_for
    from forecaster.forecast_service import run_forecast, timely
    from forecaster.journal import annotate, match
    p = defs.PROFILES[profile]
    session = cal.session(day)
    cutoff = cal.ny_instant(session.session_date, p.cutoff)
    check_wait(day, profile, wait_s, synthesis is not None)
    row = active_contract(conn, day)
    capture_id = store.start_live_capture(conn, day, profile, int(row["contract_id"]), code_revision(),
                                          fc.ISSUE_POLICIES["live"], int(round(wait_s)),
                                          reserve_s(synthesis is not None), synthesis is not None)
    summary: Dict[str, Any] = {"capture_id": capture_id, "session_date": day, "profile": profile, "runs": []}

    def event(name, **detail):
        summary.setdefault("events", []).append(name)
        return store.add_capture_event(conn, capture_id, name, detail or None)

    def fail(reason):
        event("failed", reason=reason)
        summary["status"] = "failed"
        summary["reason"] = reason
        return summary

    snapshot = store.live_snapshot(conn, day, p.snapshot_version)
    if snapshot is not None:
        event("snapshot_reused", snapshot_id=snapshot["snapshot_id"])
    else:
        if clock() >= session.rth_open_at:
            return fail("the session has opened: a live snapshot can no longer be frozen")
        info = _contract_info(row)
        bars = fresh_bars(app, info, session, cutoff, _instrument_for(defs.SYMBOL).what_to_show, clock, sleep, event,
                          wait_s)
        if bars is None:                                   # a missed opportunity, recorded as such
            requested = [e for e in store.capture_events(conn, capture_id) if e["event"] == "bars_requested"]
            event("stale", want=(cutoff - MINUTE).strftime("%H:%M UTC"), wait_limit_s=int(round(wait_s)),
                  attempts=len(requested), newest=(requested[-1]["detail"] or {}).get("newest") if requested
                  else None)
            summary["status"] = "stale"
            return summary
        event("bars_received", **_store_bars(conn, capture_id, row, day, bars, clock()))
        try:
            snap = build_snapshot(conn, day, profile, live_capture_id=capture_id)
            snapshot_id, _ = store.save_snapshot(conn, snap)
        except (SnapshotError, psycopg.Error) as e:
            return fail(f"no live snapshot: {type(e).__name__}: {e}")
        snapshot = store.get_snapshot(conn, snapshot_id)
        availability = snapshot["payload"]["cutoff"].get("availability") or {}
        event("snapshot_frozen", snapshot_id=snapshot_id, pit=snapshot["pit_availability_status"],
              verified=availability.get("verified"))
    summary["snapshot_id"] = snapshot["snapshot_id"]
    annotation_id = annotate(conn, snapshot)
    event("annotated", annotation_id=annotation_id)
    match(conn, profile, protocol, only={snapshot["snapshot_id"]})
    aset = store.latest_analogue_set(conn, snapshot["snapshot_id"], pre.MATCHER_VERSION, defs.LABEL_VERSION, protocol)
    if aset is not None and aset["target_annotation_id"] != annotation_id:
        aset = None
    event("matched", set_id=None if aset is None else aset["set_id"],
          analogues=None if aset is None else len(aset["members"]))
    for algorithm in fc.RULE_ALGORITHMS:
        run, created = run_forecast(conn, snapshot["snapshot_id"], annotation_id, aset["set_id"] if aset else None,
                                    profile, mode="live", algorithm=algorithm)
        event("forecast", run_id=run["run_id"], algorithm=algorithm, status=run["lifecycle_status"],
              issued_at=None if run["issued_at"] is None else str(run["issued_at"]), timely=timely(run),
              new=created)
        summary["runs"].append({"run_id": run["run_id"], "algorithm": algorithm, "status": run["lifecycle_status"],
                                "timely": timely(run)})
    summary["status"] = "issued" if all(r["timely"] for r in summary["runs"]) else "not timely"
    runs = [store.get_forecast_run(conn, r["run_id"]) for r in summary["runs"]]
    if synthesis is not None:
        from forecaster import live_synthesis as ls
        attempt = synthesis.request(conn, snapshot, store.get_annotation(conn, annotation_id), aset, profile, clock,
                                    event)
        if attempt.status == "sent":
            attempt.wait(conn, (ls.deadline(day) - clock()).total_seconds())
        summary["synthesis"] = attempt.status if attempt.run is None else attempt.run["lifecycle_status"]
        if attempt.run is not None:
            runs.append(_synthesis_run(attempt.run, attempt.status == "sent", event, summary))
    _deliver(runs, event, summary)
    if synthesis is not None and attempt.status == "sent" and attempt.run is None:   # late: stored, never delivered
        run = attempt.wait(conn, synthesis.late_wait_s) or attempt.abandon(conn, synthesis.late_wait_s)
        summary["synthesis"] = run["lifecycle_status"]
        _synthesis_run(run, True, event, summary)
    return summary


def _synthesis_run(run, new: bool, event, summary) -> Dict[str, Any]:
    from forecaster.forecast_service import timely
    event("forecast", run_id=run["run_id"], algorithm=run["algorithm_version"], status=run["lifecycle_status"],
          issued_at=None if run["issued_at"] is None else str(run["issued_at"]), timely=timely(run), new=new,
          request_id=run["request_id"], reason=run["failure_reason"])
    summary["runs"].append({"run_id": run["run_id"], "algorithm": run["algorithm_version"],
                            "status": run["lifecycle_status"], "timely": timely(run)})
    return run


def _deliver(runs, event, summary) -> None:
    """Records the forecast in force at the deadline (live_synthesis.delivered)."""
    from forecaster.live_synthesis import ARM, delivered
    run, why = delivered(runs)
    event("delivered", run_id=None if run is None else run["run_id"],
          arm=None if run is None else ARM[run["algorithm_version"]], reason=why)
    summary["delivered"] = {"run_id": None if run is None else run["run_id"],
                            "arm": None if run is None else ARM[run["algorithm_version"]], "reason": why}


def connect_ib(host: str, port: int, client_id: int, timeout: float = 10.0):
    """An IB connection of its own (the collector can run beside it)."""
    import threading
    from collector.ib_collector import IBCollectorApp
    app = IBCollectorApp()
    app.connect(host, port, client_id)
    threading.Thread(target=app.run, name="IBAPI_Live", daemon=True).start()
    if not app.connect_event.wait(timeout=timeout):
        raise LiveCaptureError(f"could not connect to IB at {host}:{port} (clientId={client_id})")
    return app


def timing(conn, capture: Dict[str, Any]) -> List[str]:
    """A capture's steps as seconds after the cutoff (database clock)."""
    p = defs.PROFILES[capture["profile"]]
    cutoff = cal.ny_instant(date.fromisoformat(capture["session_date"]), p.cutoff)
    out = []
    for e in capture["events"]:
        at = datetime.fromisoformat(str(e["at"]).replace(" ", "T")).replace(tzinfo=timezone.utc)
        detail = e["detail"] or {}
        note = ", ".join(f"{k}={v}" for k, v in detail.items() if k in ("algorithm", "status", "timely", "pit",
                                                                           "verified", "receipts", "attempt", "arm",
                                                                           "reason"))
        out.append(f"{(at - cutoff).total_seconds():+7.1f}s {e['event']}" + (f" ({note})" if note else ""))
    return out
