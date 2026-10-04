# forecaster/journal.py
"""
Keeps the NQ prompt-v2 journal (docs/nq_prompt_v2.md) current: one evidence
snapshot per session, its pre-open structure annotation, and its realised outcome
once the session is final.

  register        the label, convention, snapshot and annotation definitions (a
                  changed definition under an existing version name stops the run)
  take_snapshot   builds and stores one session's snapshot
  annotate        the rule-based structure annotation of a stored snapshot
                  (forecaster/structure_rules.py; Claude takes this over later)
  record_outcome  labels a stored snapshot once its session is final
  match           the structural analogues of every annotated snapshot and their
                  outcomes (matching/structural.py), stored when new
  catch_up        every final session since the journal's first one that has no
                  snapshot yet, oldest first; then annotations and outcomes for
                  stored snapshots without one, a re-check of the outcomes of the
                  sessions the collector re-downloads (the vendor revises recent
                  bars), the analogue sets, and the historical-replay baseline
                  forecasts (forecaster/forecast_service.py) - each stored once

catch_up never rebuilds a stored snapshot: a snapshot is frozen once taken. A new
snapshot version catches up over the sessions the earlier versions hold.
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


def register(conn) -> None:
    from contracts import nq_forecast
    for rec in (defs.all_records() + [preopen.rules_record(), preopen.llm_record(), preopen.matcher_record()]
                + nq_forecast.all_records()):
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


def _known_as_of(history: Dict[str, List[Dict[str, Any]]], target_snapshot: Dict[str, Any]):
    """The outcome revision of a session usable for this target (nq_match_p1_v2): for a live capture only the
    revisions computed by the target's cutoff, for a historical reconstruction the latest (said so)."""
    live = target_snapshot["data_mode"] == "live_capture"
    cutoff = target_snapshot["cutoff_at"]

    def known(snapshot_id: str) -> Optional[Dict[str, Any]]:
        revisions = history.get(snapshot_id) or []
        if live:
            revisions = [r for r in revisions if r["computed_at"] <= cutoff]
        return revisions[-1] if revisions else None
    return known


def match(conn, profile: str = defs.DEFAULT_PROFILE, protocol: str = preopen.RULES_PROTOCOL_VERSION,
          only: Optional[set] = None) -> int:
    """
    Stores the analogue set of every snapshot of the profile's version annotated under
    ``protocol``: its P1-rubric analogues among the earlier sessions, then their latest
    outcomes and the earlier sessions' labels as the prior. Returns how many sets were
    new - a set changes only with its pool or its analogues' outcome revisions.
    """
    version = defs.PROFILES[profile].snapshot_version
    snaps = store.list_snapshots(conn, "2000-01-01", "2100-01-01", version)
    for snapshot_id in sorted((only or set()) - {s["snapshot_id"] for s in snaps}):
        extra = store.get_snapshot(conn, snapshot_id)      # a named target that is not its session's newest
        if extra is not None and extra["snapshot_version"] == version:
            snaps.append(extra)
    by_id = {s["snapshot_id"]: s for s in snaps}
    sessions = [ms.SessionRef(s["snapshot_id"], str(s["session_date"]), s["symbol"]) for s in snaps]
    history = store.outcome_history(conn, defs.LABEL_VERSION)
    records = []
    for snap in snaps:
        a = store.latest_annotation(conn, snap["snapshot_id"], protocol)
        if a is None:
            continue
        records.append(ms.Record(snap["snapshot_id"], str(snap["session_date"]), snap["symbol"],
                                 snap["snapshot_version"], a["annotation_id"], a["protocol_version"],
                                 a["integrity_status"], ms.features(a)))
    created = 0
    for target in records:
        if target.integrity_status != "ok" or (only is not None and target.snapshot_id not in only):
            continue
        tsnap = by_id[target.snapshot_id]
        known = _known_as_of(history, tsnap)
        ranked = ms.rank(target, records)
        prior = ms.prior_manifest(target, sessions, known)
        outcomes = {m["record"].snapshot_id: known(m["record"].snapshot_id) for m in ranked["selected"]}
        summary = ms.outcome_summary(ranked["selected"], {sid: (o["labels"] if o else None)
                                                          for sid, o in outcomes.items()}, prior["labels"])
        summary["prior"] = {"known_as_of": KNOWN_AS_OF[tsnap["data_mode"] == "live_capture"],
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
        _, new = store.save_analogue_set(conn, {
            "target_snapshot_id": target.snapshot_id, "target_annotation_id": target.annotation_id,
            "matcher_version": preopen.MATCHER_VERSION, "label_version": defs.LABEL_VERSION,
            "data_mode": tsnap["data_mode"], "pool_size": ranked["pool_size"],
            "pool_hash": ranked["pool_hash"], "excluded": ranked["excluded"], "outcome_digest": digest,
            "prior_digest": prior["digest"],
            "mean_similarity": summary["mean_similarity"], "outcome_summary": summary}, members)
        created += new
    logger.info(f"Analogues ({preopen.MATCHER_VERSION}, {protocol}): {created} new set(s) of {len(records)} "
                f"annotated session(s).")
    return created


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
    stored = store.list_snapshots(conn, "2000-01-01", now.date().isoformat(), version)
    first = str(stored[0]["session_date"]) if stored else store.first_session(conn, defs.SYMBOL)
    if first is None:
        logger.info("Journal: no snapshots yet; start it with nq_journal.py backfill.")
        return {"snapshots": 0, "annotations": 0, "outcomes": 0, "analogue_sets": 0, "forecasts": 0, "failed": []}

    have = {str(s["session_date"]) for s in stored}
    taken, failed = [], []
    for s in cal.sessions_between(first, now.date().isoformat()):
        day = s.session_date.isoformat()
        if day in have or not labels.session_finalised(day, now):
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
    for snap in store.list_snapshots(conn, "2000-01-01", now.date().isoformat(), version):
        if store.latest_annotation(conn, snap["snapshot_id"], preopen.RULES_PROTOCOL_VERSION) is None:
            annotate(conn, snap)
            annotated += 1
        due = (snap["snapshot_id"] in taken or str(snap["session_date"]) >= recheck_from
               or store.latest_outcome(conn, snap["snapshot_id"], defs.LABEL_VERSION) is None)
        if due and record_outcome(conn, snap, now) is not None:
            recorded += 1
    logger.info(f"Journal ({version}, {defs.LABEL_VERSION}): {len(taken)} new snapshot(s), {annotated} "
                f"annotation(s), {recorded} outcome(s) recorded or re-checked"
                + (f", {len(failed)} session(s) failed: {', '.join(failed)}" if failed else "") + ".")
    sets = match(conn, profile)
    from forecaster.forecast_service import forecast_all
    forecasts = forecast_all(conn, profile)
    return {"snapshots": len(taken), "annotations": annotated, "outcomes": recorded, "analogue_sets": sets,
            "forecasts": forecasts, "failed": failed}
