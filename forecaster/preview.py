# forecaster/preview.py
"""
Forecast now: a preview of a session's forecast from the data stored so far, at any
time from its Globex open (18:00 ET the evening before) until its official snapshot
is due (09:31 ET, forecaster/journal.py). Nothing is stored in the journal: the
preview builds the snapshot as of now (features/nq_evidence.build_snapshot ``as_of``,
which the journal refuses to store), annotates it by the rules, matches it against the
stored sessions and runs both forecast arms on it in memory - with the same functions
and checks a stored run goes through. The latest preview is kept in one file for the
dashboard (PREVIEW_FILE), replaced by the next.

  preview_target(now)   the session a preview made at ``now`` is for, and its cutoff
                        (now, or the profile's cutoff once that has passed) - or why
                        there is none (PreviewUnavailable)
  preview(conn, now)    the preview, JSON-ready: status ok or unavailable with the
                        reason, the evidence, annotation, analogue set and one
                        run-shaped record per arm (lifecycle status "preview")
  preview_session(conn, day, as_of)
                        the same for a given session and minute - the Session
                        Explorer's analogue preview of a day in progress
  latest_as_of(conn, day)
                        that minute for a day: the end of its last stored NQ bar,
                        by the cutoff - so the preview's price is never stale
  save / load           the file the dashboard reads

A preview is not a forecast run: it has no run id, no issue policy and no place in an
experiment. Before the cutoff its evidence is incomplete by design - an input not yet
known is unavailable, never filled in - so it can differ from the official forecast.
Claude is never asked: the preview uses the rule-based annotation.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Tuple

from contracts import nq_forecast as fc
from contracts import nq_preopen as pre
from contracts import nq_prompt_v2 as defs
from database import journal_store as store
from features import calendar as cal
from features.nq_evidence import SnapshotError, build_snapshot
from forecaster import journal
from forecaster import structure_rules
from forecaster.forecast_baseline import baseline_forecast
from forecaster.forecast_service import freeze_forecast_evidence
from forecaster.forecast_validation import ForecastInputError, ForecastInvalid, validate_forecast
from forecaster.provenance import code_revision
from matching import structural as ms

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PREVIEW_FILE = os.path.join(_PROJECT_ROOT, "data", "preview", "forecast_preview.json")
PREVIEW_ID = "preview"          # the id of every in-memory record; never a stored id
MINUTE = timedelta(minutes=1)


class PreviewUnavailable(RuntimeError):
    """No session can be previewed now; the message says why and when."""


def _utc_text(t: datetime) -> str:
    """A timestamp as the journal returns it: 'YYYY-MM-DD HH:MM:SS' in UTC."""
    return t.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _et(t: datetime, fmt: str = "%a %Y-%m-%d %H:%M") -> str:
    return f"{t.astimezone(cal.NY_TZ):{fmt}} ET"


def _jsonable(value: Any) -> Any:
    """``value`` as it would come back from the journal: canonical JSON, read back."""
    return json.loads(defs.canonical_json(value))


def preview_target(now: datetime, profile: str = defs.DEFAULT_PROFILE) -> Tuple[cal.Session, datetime]:
    """
    The session a preview made at ``now`` is for and the preview's cutoff: today's session until its official
    snapshot is due (cutoff + journal.PREOPEN_SETTLE), the next one from its Globex open on. The cutoff is ``now``
    to the minute, or the profile's cutoff once that has passed. Raises PreviewUnavailable between the official
    snapshot and the next Globex open, when there is nothing to preview.
    """
    cutoff_t = defs.PROFILES[profile].cutoff
    today = now.astimezone(cal.NY_TZ).date()
    try:
        s = cal.session(today)
    except cal.CalendarCoverageError as e:
        raise PreviewUnavailable(str(e)) from None
    if s.is_open and now < cal.ny_instant(today, cutoff_t) + journal.PREOPEN_SETTLE:
        target = s
    else:
        target = cal.next_session(today)
        if target is None:
            raise PreviewUnavailable("no later session in the trading calendar")
        if now < target.overnight_start_at + MINUTE:
            done = ("today's pre-open is over: its forecast is the official one, from the 09:31 snapshot (Stored "
                    "runs). " if s.is_open else "")
            raise PreviewUnavailable(f"{done}The next preview is possible from "
                                     f"{_et(target.overnight_start_at + MINUTE)}, once "
                                     f"{target.session_date:%a %Y-%m-%d}'s overnight (from 18:00 ET) has a bar.")
    as_of = min(now.astimezone(timezone.utc).replace(second=0, microsecond=0),
                cal.ny_instant(target.session_date, cutoff_t))
    return target, as_of


def _run(conn, snapshot: Dict[str, Any], annotation: Dict[str, Any], aset: Optional[Dict[str, Any]], profile: str,
         history, algorithm: str, made_at: str) -> Dict[str, Any]:
    """One arm's forecast, shaped like a stored run (forecaster/forecast_display.py reads it) but never issued."""
    run: Dict[str, Any] = {
        "run_id": PREVIEW_ID, "mode": "preview", "algorithm_version": algorithm, "profile": profile,
        "session_date": snapshot["session_date"], "symbol": snapshot["symbol"],
        "snapshot_id": PREVIEW_ID, "annotation_id": PREVIEW_ID, "analogue_set_id": aset and PREVIEW_ID,
        "label_version": defs.LABEL_VERSION, "schema_version": fc.FORECAST_SCHEMA_VERSION, "issue_policy": None,
        "input_cutoff_at": snapshot["cutoff_at"], "code_revision": code_revision(), "created_at": made_at,
        "issued_at": None, "supersedes_run_id": None, "lifecycle_status": "preview", "failure_reason": None,
        "predictions": {}, "outputs": {}, "evidence": None, "events": []}
    if annotation["integrity_status"] != "ok":
        return {**run, "lifecycle_status": "unavailable",
                "failure_reason": "the annotation is contaminated: no forecast (P1 stop rule)"}
    if aset is None:
        return {**run, "lifecycle_status": "unavailable", "failure_reason": "no analogue set"}
    try:
        # the evidence a stored run would freeze, through the same checks (those of a historical replay - a preview
        # is no live capture); a preview is never issued, so it has no issue policy
        evidence = freeze_forecast_evidence(conn, snapshot, annotation, aset, profile, "historical_replay", history,
                                            algorithm)
        evidence.update(mode="preview")
        evidence["versions"]["issue_policy"] = None
        forecast = validate_forecast(baseline_forecast(evidence, algorithm), aset, algorithm)
    except (ForecastInputError, ForecastInvalid) as e:
        return {**run, "lifecycle_status": "invalid", "failure_reason": f"{type(e).__name__}: {e}"}
    return {**run, "evidence": evidence, "outputs": forecast["outputs"],
            "predictions": {t: {"target": t, **forecast["predictions"][t]} for _, t in fc.FORECAST_TARGETS}}


def latest_as_of(conn, day: str, profile: str = defs.DEFAULT_PROFILE) -> Optional[datetime]:
    """
    The minute a preview of session ``day`` reads up to: the end of its last stored NQ bar (the active contract's)
    before the cutoff - the profile's cutoff once that bar is stored - or None without a bar of the day's pre-open.
    A preview as of the wall clock would find the price stale whenever the collector lags behind it.
    """
    session = cal.session(day)
    cutoff = cal.ny_instant(session.session_date, defs.PROFILES[profile].cutoff)
    row = conn.execute(
        "SELECT max(b.timestamp_utc) FROM bars b "
        "JOIN active_contracts a ON a.contract_id = b.contract_id AND a.trading_day = b.trading_day "
        "WHERE a.symbol = %s AND b.trading_day = %s AND b.interval = '1m' AND b.price_type = 'TRADES' "
        "AND b.timestamp_utc >= %s AND b.timestamp_utc < %s;",
        (defs.SYMBOL, day, session.overnight_start_at, cutoff)).fetchone()
    if row is None or row[0] is None:
        return None
    last = row[0] if isinstance(row[0], datetime) else datetime.fromisoformat(str(row[0]).replace(" ", "T"))
    last = last if last.tzinfo else last.replace(tzinfo=timezone.utc)
    return min(last + MINUTE, cutoff)


def preview(conn, now: Optional[datetime] = None, profile: str = defs.DEFAULT_PROFILE) -> Dict[str, Any]:
    """The preview of the session ``preview_target`` names, as of ``now`` (see the module docstring)."""
    now = now or datetime.now(timezone.utc)
    try:
        session, as_of = preview_target(now, profile)
    except PreviewUnavailable as e:
        return _jsonable({"kind": "forecast_preview", "made_at": _utc_text(now), "profile": profile,
                          "status": "unavailable", "reason": str(e)})
    return preview_session(conn, session.session_date.isoformat(), as_of, profile, now)


def preview_session(conn, day: str, as_of: datetime, profile: str = defs.DEFAULT_PROFILE,
                    now: Optional[datetime] = None) -> Dict[str, Any]:
    """The preview of session ``day`` as of ``as_of`` (after its Globex open, by its cutoff), made at ``now``."""
    now = now or datetime.now(timezone.utc)
    made_at = _utc_text(now)
    base: Dict[str, Any] = {"kind": "forecast_preview", "made_at": made_at, "profile": profile}
    session = cal.session(day)
    cutoff = cal.ny_instant(session.session_date, defs.PROFILES[profile].cutoff)
    base.update(session_date=day, as_of=_utc_text(as_of), as_of_et=_et(as_of), complete=as_of == cutoff,
                cutoff_et=_et(cutoff, "%H:%M"))
    try:
        snap = build_snapshot(conn, day, profile, as_of=as_of)
    except SnapshotError as e:
        return _jsonable({**base, "status": "unavailable", "reason": f"{e} - run the collector"})
    snapshot = _jsonable({
        "snapshot_id": PREVIEW_ID, "symbol": snap.symbol, "contract_id": snap.contract_id, "session_date": day,
        "snapshot_version": snap.snapshot_version, "convention_version": snap.convention_version,
        "cutoff_at": _utc_text(snap.cutoff_at), "rth_open_at": _utc_text(snap.rth_open_at), "data_mode": "preview",
        "pit_availability_status": snap.pit_availability_status, "source_payload_hash": snap.source_payload_hash,
        "payload": snap.payload, "built_at": made_at})
    last = snapshot["payload"]["cutoff"]["last_completed_bar"]
    if last is None:
        return _jsonable({**base, "status": "unavailable",
                          "reason": f"no {defs.SYMBOL} bar of {day}'s overnight is stored yet - run the collector"})
    base["data_through"] = last["bar_end_at"]

    annotation = _jsonable({**structure_rules.annotate(snapshot), "annotation_id": PREVIEW_ID,
                            "snapshot_id": PREVIEW_ID, "model": None, "created_at": made_at})
    stored = store.list_snapshots(conn, "2000-01-01", "2100-01-01", snap.snapshot_version)
    records = journal.annotated_records(conn, stored, pre.RULES_PROTOCOL_VERSION)
    target = ms.Record(PREVIEW_ID, day, snap.symbol, snap.snapshot_version, PREVIEW_ID,
                       annotation["protocol_version"], annotation["integrity_status"], ms.features(annotation))
    sessions = [ms.SessionRef(s["snapshot_id"], str(s["session_date"]), s["symbol"]) for s in stored]
    history = store.outcome_history(conn, defs.LABEL_VERSION)
    aset = None
    if annotation["integrity_status"] == "ok":
        record, members = journal.analogue_set_record(target, records + [target],
                                                      sessions + [ms.SessionRef(PREVIEW_ID, day, snap.symbol)],
                                                      history, snapshot)
        aset = _jsonable({**record, "set_id": PREVIEW_ID, "protocol_version": annotation["protocol_version"],
                          "created_at": made_at, "members": members})
    runs = [_run(conn, snapshot, annotation, aset, profile, history, algorithm, made_at)
            for algorithm in fc.RULE_ALGORITHMS]
    return _jsonable({**base, "status": "ok", "snapshot": snapshot, "annotation": annotation, "analogue_set": aset,
                      "runs": runs})


def save(result: Dict[str, Any], path: str = PREVIEW_FILE) -> str:
    """Replaces the preview file with ``result`` (atomically: a reader never sees half a file)."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(result, f)
    os.replace(tmp, path)
    return path


def load(path: str = PREVIEW_FILE) -> Optional[Dict[str, Any]]:
    """The latest preview, or None when there is none (or the file cannot be read)."""
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None
