# forecaster/journal.py
"""
Keeps the NQ prompt-v2 journal (docs/nq_prompt_v2.md) current: one evidence
snapshot per session, its pre-open structure annotation, and its realised outcome
once the session is final.

  register        the label, convention, snapshot and annotation definitions (a
                  changed definition under an existing version name stops the run)
  take_snapshot   builds and stores one session's snapshot
  annotate        the rule-based structure annotation of a stored snapshot
                  (forecaster/structure_rules.py)
  record_outcome  labels a stored snapshot once its session is final
  match           the structural analogues of every annotated snapshot and their
                  outcomes (matching/structural.py), stored when new
  catch_up        every session since the journal's first one whose pre-open is
                  over and stored (snapshot_pending) and that has no snapshot yet,
                  oldest first; then annotations and outcomes for stored snapshots
                  without one, a re-check of the outcomes of the sessions the
                  collector re-downloads (the vendor revises recent bars), the
                  analogue sets, the historical-replay baseline forecasts
                  (forecaster/forecast_service.py) and the ML forecasts of the sessions
                  after the models' training window, with the forecast in force
                  (forecaster/ml_service.py) - each stored once

A snapshot reads nothing after its cutoff, so a session in progress gets its snapshot,
annotation, analogue set and forecasts as soon as its bars past the cutoff are stored;
its outcome only once it is final (two hours after its close). catch_up never rebuilds
a stored snapshot: a snapshot is frozen once taken - which is why a session's bars must
have been fetched after its cutoff first. A new snapshot version catches up over the
sessions the earlier versions hold.
scripts/nq_journal.py is the CLI; the IB collector runs catch_up after every
full collection (collector/ib_collector.py), so no separate call is needed.
"""

import hashlib
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from collector.coverage import REFRESH_TRAILING_DAYS
from contracts import nq_preopen as preopen
from contracts import nq_prompt_v2 as defs
from database import journal_store as store
from features import calendar as cal
from features.nq_evidence import SnapshotError, build_snapshot
from forecaster import labels_prompt_v2 as labels
from forecaster import structure_rules
from matching import structural as ms

logger = logging.getLogger("nq_journal")

# Before a session is final, its snapshot waits until this long after the cutoff, and for every bar of the day stored
# to have been fetched after that: a snapshot is frozen once taken, so one from bars fetched before the cutoff would
# keep the minutes not yet fetched as unavailable for good.
PREOPEN_SETTLE = timedelta(minutes=2)


def snapshot_pending(conn, day: str, profile: str = defs.DEFAULT_PROFILE,
                     now: Optional[datetime] = None) -> Optional[str]:
    """
    Why the snapshot of session ``day`` cannot be taken yet, or None when it can: a final session always can; one
    in progress from its cutoff + PREOPEN_SETTLE, once the day's NQ bars are stored, every instrument's bars of the
    day were fetched after then (the collector fetches them all in one run) and NQ's bar closing at the cutoff is
    stored and confirmed complete by a later bar - on a delayed feed the first fetch after the cutoff can still end
    minutes before it, and the newest bar stored may still be forming.
    """
    now = now or datetime.now(timezone.utc)
    session = cal.session(day)
    if not session.is_open:
        return "no regular session that day"
    if labels.session_finalised(day, now):
        return None
    cutoff = defs.PROFILES[profile].cutoff
    ready_at = cal.ny_instant(session.session_date, cutoff) + PREOPEN_SETTLE
    if now < ready_at:
        return f"not before {ready_at.astimezone(cal.NY_TZ):%H:%M} ET (its pre-open runs to the {cutoff:%H:%M} cutoff)"
    rows = conn.execute("SELECT c.symbol, d.fetched_at FROM session_days d JOIN contracts c USING (contract_id) "
                        "WHERE d.trading_day = %s AND d.interval = '1m';", (day,)).fetchall()
    if not any(r["symbol"] == defs.SYMBOL for r in rows):
        return f"no {defs.SYMBOL} bars of the day are stored yet - run the collector"
    early = sorted({r["symbol"] for r in rows
                    if datetime.fromisoformat(str(r["fetched_at"])).replace(tzinfo=timezone.utc) < ready_at})
    if early:
        return (f"the day's {', '.join(early)} bars were fetched before the {cutoff:%H:%M} ET cutoff - run the "
                f"collector")
    # A fetch after the cutoff is not enough on a delayed feed (2026-10-07: fetched at 09:31 ET, stored through
    # about 09:21, so the snapshot froze without its cutoff price): the bar closing at the cutoff must be stored -
    # and confirmed complete, since the collector stores the bar still forming: a later bar (from the cutoff on).
    cutoff_at = cal.ny_instant(session.session_date, cutoff)
    newest = conn.execute(
        "SELECT MAX(b.timestamp_utc) AS t FROM bars b JOIN active_contracts a "
        "ON a.contract_id = b.contract_id AND a.trading_day = b.trading_day "
        "WHERE a.symbol = %s AND b.trading_day = %s AND b.interval = '1m' AND b.price_type = 'TRADES';",
        (defs.SYMBOL, day)).fetchone()
    t = newest["t"] if newest is not None else None
    if t is None or datetime.fromisoformat(str(t)).replace(tzinfo=timezone.utc) < cutoff_at:
        stored = ("none" if t is None else
                  f"{datetime.fromisoformat(str(t)).replace(tzinfo=timezone.utc).astimezone(cal.NY_TZ):%H:%M} ET")
        return (f"{defs.SYMBOL}'s bar closing at the {cutoff:%H:%M} ET cutoff is not stored and confirmed yet "
                f"(newest: {stored}; a later bar confirms it) - the feed is delayed; the next collection takes it")
    return None


def register(conn) -> None:
    from contracts import nq_forecast
    from contracts import nq_ml_bundle
    from forecaster import ml_bundle, ml_model
    bundles = any(ml_bundle.manifest(v) is not None for v in nq_ml_bundle.VERSIONS.values())
    for rec in (defs.all_records() + [preopen.rules_record(), preopen.matcher_record()]
                + nq_forecast.all_records() + ml_model.records() + (ml_bundle.records() if bundles else [])):
        store.register_version(conn, rec)


def take_snapshot(conn, day, profile: str) -> str:
    snap = build_snapshot(conn, day, profile)
    snapshot_id, created = store.save_snapshot(conn, snap)
    th, refs = snap.payload["thresholds"], snap.payload["references"]
    not_valid = [k for k, v in refs.items() if v["status"] != "valid"]
    logger.info(f"{day} [{profile}]: snapshot {snapshot_id[:8]} ({'new' if created else 'already stored'}) "
                f"on {snap.payload['identity']['local_symbol'] or snap.contract_id}, T={th['T']} B={th['B']}; "
                f"unavailable references: {', '.join(not_valid) or 'none'}")
    return snapshot_id


def annotate(conn, snapshot: Dict[str, Any]) -> str:
    """Stores the rule-based structure annotation of one stored snapshot; returns its id."""
    annotation = structure_rules.annotate(snapshot)
    annotation_id, created = store.save_annotation(conn, snapshot["snapshot_id"], annotation)
    if created:
        f = annotation["fields"]
        logger.info(f"{snapshot['session_date']}: {annotation['protocol_version']} "
                    f"{annotation['integrity_status']}, " + ", ".join(
                        f"{k}={f[k]['value']}" for k in ("Overnight Structure", "Premarket Pattern", "Event Risk")
                        if k in f))
    return annotation_id


def record_outcome(conn, snapshot: Dict[str, Any], now: Optional[datetime] = None) -> Optional[int]:
    """Labels one stored snapshot once its session is final. Returns the revision, or None if not final."""
    if not labels.session_finalised(snapshot["session_date"], now or datetime.now(timezone.utc)):
        return None
    out = labels.compute_outcome(snapshot, labels.load_realised_bars(conn, snapshot))
    revision, created = store.save_outcome(conn, snapshot["snapshot_id"], defs.LABEL_VERSION, out)
    summary = ", ".join(f"{t}={v['label'] or v['reason'].upper()}" for t, v in out["labels"].items())
    if created and revision > 1:
        logger.warning(f"{snapshot['session_date']}: outcome revised to revision {revision}.")
    logger.info(f"{snapshot['session_date']}: outcome r{revision} {summary}")
    return revision


KNOWN_AS_OF = {
    False: "reconstruction: the latest outcome revision of each earlier session, computed after the fact",
    True: "live: for each earlier session the latest outcome revision computed by the target's cutoff",
}
PREVIEW_KNOWN_AS_OF = "preview: the latest outcome revision of each earlier session when the preview was made"


def _known_as_of(history: Dict[str, List[Dict[str, Any]]], target_snapshot: Dict[str, Any]):
    """The outcome revision of a session usable for this target (nq_match_p1_v2): for a live capture only the
    revisions computed by the target's cutoff, for a historical reconstruction the latest (said so) - and for a
    preview, made now, the latest, all of them computed by now."""
    live = target_snapshot["data_mode"] == "live_capture"
    cutoff = target_snapshot["cutoff_at"]

    def known(snapshot_id: str) -> Optional[Dict[str, Any]]:
        revisions = history.get(snapshot_id) or []
        if live:
            revisions = [r for r in revisions if r["computed_at"] <= cutoff]
        return revisions[-1] if revisions else None
    return known


def annotated_records(conn, snaps: List[Dict[str, Any]], protocol: str) -> List[ms.Record]:
    """The matcher's record of every snapshot in ``snaps`` annotated under ``protocol``."""
    records = []
    latest = store.latest_annotations(conn, [s["snapshot_id"] for s in snaps], protocol)
    for snap in snaps:
        a = latest.get(snap["snapshot_id"])
        if a is None:
            continue
        records.append(ms.Record(snap["snapshot_id"], str(snap["session_date"]), snap["symbol"],
                                 snap["snapshot_version"], a["annotation_id"], a["protocol_version"],
                                 a["integrity_status"], ms.features(a)))
    return records


def analogue_set_record(target: ms.Record, records: List[ms.Record], sessions: List[ms.SessionRef],
                        history: Dict[str, List[Dict[str, Any]]], target_snapshot: Dict[str, Any]):
    """
    ``(set, members)`` of ``target``: its P1-rubric analogues among the earlier sessions of ``records``, their
    outcomes known for the target and the earlier ``sessions``' labels as the prior - what save_analogue_set
    stores (match), or a preview keeps in memory (forecaster/preview.py).
    """
    known = _known_as_of(history, target_snapshot)
    ranked = ms.rank(target, records)
    prior = ms.prior_manifest(target, sessions, known)
    outcomes = {m["record"].snapshot_id: known(m["record"].snapshot_id) for m in ranked["selected"]}
    summary = ms.outcome_summary(ranked["selected"], {sid: (o["labels"] if o else None)
                                                      for sid, o in outcomes.items()}, prior["labels"])
    summary["prior"] = {"known_as_of": (PREVIEW_KNOWN_AS_OF if target_snapshot["data_mode"] == "preview" else
                                        KNOWN_AS_OF[target_snapshot["data_mode"] == "live_capture"]),
                        "sessions": len(prior["manifest"]), "excluded": prior["excluded"],
                        "digest": prior["digest"], "manifest": prior["manifest"]}
    members = []
    for m in ranked["selected"]:
        r, o = m["record"], outcomes[m["record"].snapshot_id]
        members.append({
            "rank": m["rank"], "snapshot_id": r.snapshot_id, "annotation_id": r.annotation_id,
            "session_date": r.session_date, "similarity": ms.show(m["similarity"]),
            "comparable_weight": ms.show(m["comparable_weight"]),
            "components": {f: {**c, "weight": str(c["weight"]), "score": ms.show(c["score"])}
                           for f, c in m["components"].items()},
            "outcome_revision": None if o is None else int(o["outcome_revision"]),
            "outcome_computed_at": None if o is None else o["computed_at"]})
    digest = hashlib.sha256(defs.canonical_json(
        [[m["snapshot_id"], m["outcome_revision"]] for m in members]).encode()).hexdigest()
    return {"target_snapshot_id": target.snapshot_id, "target_annotation_id": target.annotation_id,
            "matcher_version": preopen.MATCHER_VERSION, "label_version": defs.LABEL_VERSION,
            "data_mode": target_snapshot["data_mode"], "pool_size": ranked["pool_size"],
            "pool_hash": ranked["pool_hash"], "excluded": ranked["excluded"], "outcome_digest": digest,
            "prior_digest": prior["digest"],
            "mean_similarity": summary["mean_similarity"], "outcome_summary": summary}, members


def match(conn, profile: str = defs.DEFAULT_PROFILE, protocol: str = preopen.RULES_PROTOCOL_VERSION,
          only: Optional[set] = None) -> int:
    """
    Stores the analogue set of every snapshot of the profile's version annotated under
    ``protocol``: its P1-rubric analogues among the earlier sessions, then their latest
    outcomes and the earlier sessions' labels as the prior. Returns how many sets were
    new - a set changes only with its pool or its analogues' outcome revisions.
    """
    version = defs.PROFILES[profile].snapshot_version
    snaps = store.list_snapshots(conn, "2000-01-01", "2100-01-01", version, payload=False)
    for snapshot_id in sorted((only or set()) - {s["snapshot_id"] for s in snaps}):
        extra = store.get_snapshot(conn, snapshot_id)      # a named target that is not its session's newest
        if extra is not None and extra["snapshot_version"] == version:
            snaps.append(extra)
    by_id = {s["snapshot_id"]: s for s in snaps}
    sessions = [ms.SessionRef(s["snapshot_id"], str(s["session_date"]), s["symbol"]) for s in snaps]
    history = store.outcome_history(conn, defs.LABEL_VERSION)
    records = annotated_records(conn, snaps, protocol)
    targets = [t for t in records if t.integrity_status == "ok" and (only is None or t.snapshot_id in only)]
    built = [analogue_set_record(t, records, sessions, history, by_id[t.snapshot_id]) for t in targets]
    # one query for the sets already stored: only a new identity goes through save_analogue_set (which checks again,
    # under its lock)
    stored = store.analogue_set_keys(conn, [t.annotation_id for t in targets], preopen.MATCHER_VERSION,
                                     defs.LABEL_VERSION)
    created = 0
    for rec, members in built:
        if (rec["target_annotation_id"], rec["matcher_version"], rec["label_version"], rec["pool_hash"],
                rec["outcome_digest"], rec.get("prior_digest")) in stored:
            continue
        _, new = store.save_analogue_set(conn, rec, members)
        created += new
    logger.info(f"Analogues ({preopen.MATCHER_VERSION}, {protocol}): {created} new set(s) of {len(records)} "
                f"annotated session(s).")
    return created


def pool_started(conn, profile: str) -> bool:
    """Whether the profile has any snapshot yet - a candidate profile's pool is started by hand (backfill), then
    kept current by Auto (collector.update_journal)."""
    return conn.execute("SELECT 1 FROM journal.snapshots WHERE snapshot_version = %s LIMIT 1;",
                        (defs.PROFILES[profile].snapshot_version,)).fetchone() is not None


def catch_up(conn, profile: str = defs.DEFAULT_PROFILE, now: Optional[datetime] = None) -> Dict[str, Any]:
    """
    Brings the journal up to date (see the module docstring). Returns
    ``{'snapshots': taken, 'annotations': new, 'outcomes': recorded or re-checked,
    'analogue_sets': new, 'failed': [session dates]}``. Does nothing until the journal has a first
    snapshot (start it with ``nq_journal.py backfill``).
    """
    now = now or datetime.now(timezone.utc)
    register(conn)
    version = defs.PROFILES[profile].snapshot_version
    stored = store.list_snapshots(conn, "2000-01-01", now.date().isoformat(), version, payload=False)
    first = str(stored[0]["session_date"]) if stored else store.first_session(conn, defs.SYMBOL)
    if first is None:
        logger.info("Journal: no snapshots yet; start it with nq_journal.py backfill.")
        return {"snapshots": 0, "annotations": 0, "outcomes": 0, "analogue_sets": 0, "forecasts": 0, "failed": []}

    have = {str(s["session_date"]) for s in stored}
    taken, failed = [], []
    for s in cal.sessions_between(first, now.date().isoformat()):
        day = s.session_date.isoformat()
        if day in have:
            continue
        pending = snapshot_pending(conn, day, profile, now)
        if pending:
            if s.session_date <= now.astimezone(cal.NY_TZ).date():       # not tomorrow's, late in the evening
                logger.info(f"Journal: {day} not taken yet: {pending}.")
            continue
        try:
            taken.append(take_snapshot(conn, day, profile))
        except SnapshotError as e:
            logger.error(f"Journal: {day}: {e}")
            failed.append(day)

    # Annotations for snapshots without one; outcomes for new snapshots, stored ones
    # still without an outcome, and the sessions whose bars the collector may just
    # have re-downloaded.
    recheck_from = (now.date() - timedelta(days=REFRESH_TRAILING_DAYS)).isoformat()
    annotated = recorded = 0
    refs = store.list_snapshots(conn, "2000-01-01", now.date().isoformat(), version, payload=False)
    have_annotation = set(store.latest_annotations(conn, [r["snapshot_id"] for r in refs],
                                                   preopen.RULES_PROTOCOL_VERSION))
    have_outcome = store.outcome_snapshot_ids(conn, defs.LABEL_VERSION)
    for ref in refs:                     # the full snapshot is read only for one that has something to do
        needs_annotation = ref["snapshot_id"] not in have_annotation
        due = (ref["snapshot_id"] in taken or str(ref["session_date"]) >= recheck_from
               or ref["snapshot_id"] not in have_outcome)
        if not (needs_annotation or due):
            continue
        snap = store.get_snapshot(conn, ref["snapshot_id"])
        if needs_annotation:
            annotate(conn, snap)
            annotated += 1
        if due and record_outcome(conn, snap, now) is not None:
            recorded += 1
    logger.info(f"Journal ({version}, {defs.LABEL_VERSION}): {len(taken)} new snapshot(s), {annotated} "
                f"annotation(s), {recorded} outcome(s) recorded or re-checked"
                + (f", {len(failed)} session(s) failed: {', '.join(failed)}" if failed else "") + ".")
    sets = match(conn, profile)
    from forecaster.forecast_service import forecast_all
    from forecaster.ml_service import issue_pending
    forecasts = forecast_all(conn, profile)
    ml_runs = issue_pending(conn, profile, now)
    return {"snapshots": len(taken), "annotations": annotated, "outcomes": recorded, "analogue_sets": sets,
            "forecasts": forecasts, "ml_runs": ml_runs, "failed": failed}
