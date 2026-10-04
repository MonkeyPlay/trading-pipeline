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
  catch_up        every final session since the journal's first one that has no
                  snapshot yet, oldest first; then annotations and outcomes for
                  stored snapshots without one, and a re-check of the outcomes of
                  the sessions the collector re-downloads (the vendor revises
                  recent bars)

catch_up never rebuilds a stored snapshot: a snapshot is frozen once taken. A new
snapshot version catches up over the sessions the earlier versions hold.
scripts/nq_journal.py is the CLI; the IB collector runs catch_up after every
full collection (collector/ib_collector.py), so no separate call is needed.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from collector.coverage import REFRESH_TRAILING_DAYS
from contracts import nq_preopen as preopen
from contracts import nq_prompt_v2 as defs
from database import journal_store as store
from features import calendar as cal
from features.nq_evidence import SnapshotError, build_snapshot
from forecaster import labels_prompt_v2 as labels
from forecaster import structure_rules

logger = logging.getLogger("nq_journal")


def register(conn) -> None:
    for rec in defs.all_records() + [preopen.rules_record()]:
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


def catch_up(conn, profile: str = defs.DEFAULT_PROFILE, now: Optional[datetime] = None) -> Dict[str, Any]:
    """
    Brings the journal up to date (see the module docstring). Returns
    ``{'snapshots': taken, 'annotations': new, 'outcomes': recorded or re-checked,
    'failed': [session dates]}``. Does nothing until the journal has a first
    snapshot (start it with ``nq_journal.py backfill``).
    """
    now = now or datetime.now(timezone.utc)
    register(conn)
    version = defs.PROFILES[profile].snapshot_version
    stored = store.list_snapshots(conn, "2000-01-01", now.date().isoformat(), version)
    first = str(stored[0]["session_date"]) if stored else store.first_session(conn, defs.SYMBOL)
    if first is None:
        logger.info("Journal: no snapshots yet; start it with nq_journal.py backfill.")
        return {"snapshots": 0, "annotations": 0, "outcomes": 0, "failed": []}

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
    return {"snapshots": len(taken), "annotations": annotated, "outcomes": recorded, "failed": failed}
