# database/journal_store.py
"""
Read/write access to the journal records (schema ``journal``, migration 0009):
definition versions, evidence snapshots and realised outcomes. Everything here
appends; the database rejects UPDATE, DELETE and TRUNCATE.

  register_version   a definition under its version name; re-registering the
                     same name with a different definition raises VersionConflict
  save_snapshot      idempotent on (contract, session, snapshot version,
                     convention version, source payload hash)
  save_outcome       the next outcome_revision when the recomputed labels,
                     measurements or source differ from the latest, else the latest
  save_annotation    a structure annotation (migration 0011), idempotent on its
                     output; a rule-based protocol may annotate a snapshot once
  save_analogue_set  a target's analogues and their outcome revisions (migration
                     0012), idempotent on the pool and the outcome revisions
  annotation review  sets of sessions whose annotations a person checks without
                     the outcome, and the verdicts (migration 0012)
"""

import hashlib
import json
import logging
import uuid
from typing import Any, Dict, List, Optional, Tuple

from contracts.nq_prompt_v2 import canonical_json
from database.connection import Database

logger = logging.getLogger(__name__)


class VersionConflict(RuntimeError):
    """A version name is already registered with a different definition."""


def _lock(conn: Database, *parts) -> None:
    key = int(hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()[:15], 16)
    conn.execute("SELECT pg_advisory_xact_lock(%s);", (key,))


def _load(value):
    return json.loads(value) if isinstance(value, str) else value


def register_version(conn: Database, rec: Dict[str, Any]) -> bool:
    """Registers ``rec`` ({'version', 'kind', 'definition', 'definition_hash'}); True when new."""
    with conn:
        _lock(conn, "journal.definition_versions", rec["version"])
        row = conn.execute("SELECT kind, definition_hash FROM journal.definition_versions WHERE version = %s;",
                           (rec["version"],)).fetchone()
        if row is not None:
            if (row["kind"], row["definition_hash"]) != (rec["kind"], rec["definition_hash"]):
                raise VersionConflict(
                    f"{rec['version']} is already registered with a different definition. Versions are "
                    f"immutable: give the changed definition a new version name.")
            return False
        conn.execute(
            "INSERT INTO journal.definition_versions (version, kind, definition_hash, definition) "
            "VALUES (%s, %s, %s, %s);",
            (rec["version"], rec["kind"], rec["definition_hash"], canonical_json(rec["definition"])),
        )
    logger.info(f"Registered {rec['kind']} version {rec['version']}.")
    return True


# --------------------------------------------------------------------------
# Snapshots
# --------------------------------------------------------------------------

def save_snapshot(conn: Database, snap) -> Tuple[str, bool]:
    """
    Stores a ``features.nq_evidence.Snapshot``. Returns ``(snapshot_id, created)``;
    a snapshot with the same source payload already stored is returned instead.
    """
    with conn:
        row = conn.execute(
            "INSERT INTO journal.snapshots (snapshot_id, symbol, contract_id, session_date, snapshot_version, "
            "convention_version, cutoff_at, rth_open_at, data_mode, pit_availability_status, source_payload_hash, "
            "payload) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (contract_id, session_date, snapshot_version, convention_version, source_payload_hash) "
            "DO NOTHING RETURNING snapshot_id;",
            (str(uuid.uuid4()), snap.symbol, snap.contract_id, snap.session_date, snap.snapshot_version,
             snap.convention_version, snap.cutoff_at, snap.rth_open_at, snap.data_mode,
             snap.pit_availability_status, snap.source_payload_hash, canonical_json(snap.payload)),
        ).fetchone()
        if row is not None:
            return str(row[0]), True
        existing = conn.execute(
            "SELECT snapshot_id FROM journal.snapshots WHERE contract_id = %s AND session_date = %s "
            "AND snapshot_version = %s AND convention_version = %s AND source_payload_hash = %s;",
            (snap.contract_id, snap.session_date, snap.snapshot_version, snap.convention_version,
             snap.source_payload_hash),
        ).fetchone()
        return str(existing[0]), False


def _snapshot_dict(row) -> Dict[str, Any]:
    d = dict(zip(row.keys(), row))
    d["payload"] = _load(d["payload"])
    d["snapshot_id"] = str(d["snapshot_id"])
    return d


def get_snapshot(conn: Database, snapshot_id: str) -> Optional[Dict[str, Any]]:
    row = conn.execute("SELECT * FROM journal.snapshots WHERE snapshot_id = %s;", (snapshot_id,)).fetchone()
    return _snapshot_dict(row) if row else None


def list_snapshots(conn: Database, start: str, end: str, snapshot_version: str,
                   symbol: str = "NQ") -> List[Dict[str, Any]]:
    """The newest snapshot of every session in [start, end], oldest session first."""
    rows = conn.execute(
        "SELECT DISTINCT ON (session_date) * FROM journal.snapshots "
        "WHERE session_date BETWEEN %s AND %s AND snapshot_version = %s AND symbol = %s "
        "ORDER BY session_date, built_at DESC;",
        (start, end, snapshot_version, symbol),
    ).fetchall()
    return [_snapshot_dict(r) for r in rows]


# --------------------------------------------------------------------------
# Outcomes (revisioned)
# --------------------------------------------------------------------------

def latest_outcome(conn: Database, snapshot_id: str, label_version: str) -> Optional[Dict[str, Any]]:
    row = conn.execute(
        "SELECT * FROM journal.outcomes WHERE snapshot_id = %s AND label_version = %s "
        "ORDER BY outcome_revision DESC LIMIT 1;",
        (snapshot_id, label_version),
    ).fetchone()
    if row is None:
        return None
    d = dict(zip(row.keys(), row))
    d["labels"], d["measurements"] = _load(d["labels"]), _load(d["measurements"])
    return d


def save_outcome(conn: Database, snapshot_id: str, label_version: str, outcome: Dict[str, Any]) -> Tuple[int, bool]:
    """
    Stores ``outcome`` ({'labels', 'measurements', 'digest'}) for a snapshot.
    Returns ``(outcome_revision, created)``: a new revision only when something changed.
    """
    labels, measurements = json.loads(canonical_json(outcome["labels"])), json.loads(
        canonical_json(outcome["measurements"]))
    with conn:
        _lock(conn, "journal.outcomes", snapshot_id, label_version)
        last = latest_outcome(conn, snapshot_id, label_version)
        if (last is not None and last["labels"] == labels and last["measurements"] == measurements
                and last["source_digest"] == outcome["digest"]):
            return int(last["outcome_revision"]), False
        revision = 1 if last is None else int(last["outcome_revision"]) + 1
        conn.execute(
            "INSERT INTO journal.outcomes (snapshot_id, label_version, outcome_revision, labels, measurements, "
            "source_digest) VALUES (%s, %s, %s, %s, %s, %s);",
            (snapshot_id, label_version, revision, canonical_json(labels), canonical_json(measurements),
             outcome["digest"]),
        )
        return revision, True


# --------------------------------------------------------------------------
# Review sets (migration 0010)
# --------------------------------------------------------------------------

def create_review_set(conn: Database, name: str, label_version: str, snapshot_version: str,
                      selection: Dict[str, Any], members: List[Dict[str, Any]]) -> bool:
    """
    Stores a review set and its members ({'snapshot_id', 'reasons'}, in order).
    True when new; an existing set of that name is left as it is.
    """
    with conn:
        _lock(conn, "journal.review_sets", name)
        if conn.execute("SELECT 1 FROM journal.review_sets WHERE review_set = %s;", (name,)).fetchone():
            return False
        conn.execute("INSERT INTO journal.review_sets (review_set, label_version, snapshot_version, selection) "
                     "VALUES (%s, %s, %s, %s);", (name, label_version, snapshot_version, canonical_json(selection)))
        conn.executemany(
            "INSERT INTO journal.review_members (review_set, snapshot_id, position, reasons) VALUES (%s, %s, %s, %s);",
            [(name, m["snapshot_id"], i, canonical_json(m["reasons"])) for i, m in enumerate(members, 1)])
    return True


def review_sets(conn: Database) -> List[Dict[str, Any]]:
    rows = conn.execute("SELECT * FROM journal.review_sets ORDER BY created_at DESC;").fetchall()
    return [dict(zip(r.keys(), r), selection=_load(r["selection"])) for r in rows]


def review_members(conn: Database, name: str) -> List[Dict[str, Any]]:
    """The set's sessions in order: {'snapshot_id', 'session_date', 'position', 'reasons'}."""
    rows = conn.execute(
        "SELECT m.snapshot_id, s.session_date, m.position, m.reasons FROM journal.review_members m "
        "JOIN journal.snapshots s ON s.snapshot_id = m.snapshot_id WHERE m.review_set = %s ORDER BY m.position;",
        (name,)).fetchall()
    return [{"snapshot_id": str(r["snapshot_id"]), "session_date": str(r["session_date"]),
             "position": int(r["position"]), "reasons": _load(r["reasons"])} for r in rows]


def save_verdicts(conn: Database, name: str, snapshot_id: str, outcome_revision: int,
                  verdicts: List[Dict[str, Any]], reviewer: Optional[str] = None) -> int:
    """Appends one verdict per field ({'field', 'shown_value', 'verdict', 'note'}); returns how many."""
    with conn:
        conn.executemany(
            "INSERT INTO journal.review_verdicts (review_set, snapshot_id, outcome_revision, field, shown_value, "
            "verdict, note, reviewer) VALUES (%s, %s, %s, %s, %s, %s, %s, %s);",
            [(name, snapshot_id, outcome_revision, v["field"], v["shown_value"], v["verdict"], v.get("note") or None,
              reviewer) for v in verdicts])
    return len(verdicts)


def latest_verdicts(conn: Database, name: str) -> List[Dict[str, Any]]:
    """The verdict that counts per session and field (journal.review_latest)."""
    rows = conn.execute("SELECT * FROM journal.review_latest WHERE review_set = %s ORDER BY session_date, field;",
                        (name,)).fetchall()
    return [dict(zip(r.keys(), r), snapshot_id=str(r["snapshot_id"])) for r in rows]


# --------------------------------------------------------------------------
# Structure annotations (migration 0011)
# --------------------------------------------------------------------------

def save_annotation(conn: Database, snapshot_id: str, annotation: Dict[str, Any],
                    model: Optional[str] = None) -> Tuple[str, bool]:
    """
    Stores one structure annotation (contracts.nq_preopen.ANNOTATION_SCHEMA).
    Returns ``(annotation_id, created)``; the same output again returns the stored
    one. A rule-based protocol is deterministic, so a different output for a
    snapshot it already annotated raises VersionConflict: changed rules need a new
    protocol version.
    """
    protocol = annotation["protocol_version"]
    with conn:
        _lock(conn, "journal.structure_annotations", snapshot_id, protocol)
        stored = conn.execute(
            "SELECT annotation_id, output_hash FROM journal.structure_annotations "
            "WHERE snapshot_id = %s AND protocol_version = %s;", (snapshot_id, protocol)).fetchall()
        for row in stored:
            if row["output_hash"] == annotation["output_hash"]:
                return str(row["annotation_id"]), False
        if stored and annotation["annotator"] == "rules":
            raise VersionConflict(
                f"{protocol} already annotated snapshot {snapshot_id} differently. Rule changes need a new "
                f"protocol version.")
        annotation_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO journal.structure_annotations (annotation_id, snapshot_id, protocol_version, annotator, "
            "model, integrity_status, fields, price_location, measurements, output_hash) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s);",
            (annotation_id, snapshot_id, protocol, annotation["annotator"], model, annotation["integrity_status"],
             canonical_json(annotation["fields"]), canonical_json(annotation["price_location"]),
             canonical_json(annotation["measurements"]), annotation["output_hash"]))
    return annotation_id, True


def latest_annotation(conn: Database, snapshot_id: str, protocol_version: str) -> Optional[Dict[str, Any]]:
    row = conn.execute(
        "SELECT * FROM journal.structure_annotations WHERE snapshot_id = %s AND protocol_version = %s "
        "ORDER BY created_at DESC LIMIT 1;", (snapshot_id, protocol_version)).fetchone()
    if row is None:
        return None
    d = dict(zip(row.keys(), row))
    for key in ("fields", "price_location", "measurements"):
        d[key] = _load(d[key])
    d["annotation_id"], d["snapshot_id"] = str(d["annotation_id"]), str(d["snapshot_id"])
    return d


def first_session(conn: Database, symbol: str = "NQ") -> Optional[str]:
    """The earliest session any snapshot version holds for ``symbol``."""
    row = conn.execute("SELECT min(session_date) FROM journal.snapshots WHERE symbol = %s;", (symbol,)).fetchone()
    return None if row[0] is None else str(row[0])


# --------------------------------------------------------------------------
# Analogue sets (migration 0012)
# --------------------------------------------------------------------------

def save_analogue_set(conn: Database, rec: Dict[str, Any], members: List[Dict[str, Any]]) -> Tuple[str, bool]:
    """
    Stores one analogue set and its members. ``rec`` has the journal.analogue_sets
    columns but ``set_id``; ``members`` the journal.analogue_members columns but
    ``set_id``. Idempotent: the same target annotation, matcher, label version, pool
    and outcome revisions return the stored set.
    """
    key = (rec["target_annotation_id"], rec["matcher_version"], rec["label_version"], rec["pool_hash"],
           rec["outcome_digest"])
    with conn:
        _lock(conn, "journal.analogue_sets", *key)
        row = conn.execute(
            "SELECT set_id FROM journal.analogue_sets WHERE target_annotation_id = %s AND matcher_version = %s "
            "AND label_version = %s AND pool_hash = %s AND outcome_digest = %s;", key).fetchone()
        if row is not None:
            return str(row[0]), False
        set_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO journal.analogue_sets (set_id, target_snapshot_id, target_annotation_id, matcher_version, "
            "label_version, data_mode, pool_size, pool_hash, excluded, outcome_digest, mean_similarity, "
            "outcome_summary) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s);",
            (set_id, rec["target_snapshot_id"], rec["target_annotation_id"], rec["matcher_version"],
             rec["label_version"], rec["data_mode"], rec["pool_size"], rec["pool_hash"],
             canonical_json(rec["excluded"]), rec["outcome_digest"], rec["mean_similarity"],
             canonical_json(rec["outcome_summary"])))
        conn.executemany(
            "INSERT INTO journal.analogue_members (set_id, rank, snapshot_id, annotation_id, session_date, similarity, "
            "comparable_weight, components, outcome_revision, outcome_computed_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s);",
            [(set_id, m["rank"], m["snapshot_id"], m["annotation_id"], m["session_date"], m["similarity"],
              m["comparable_weight"], canonical_json(m["components"]), m["outcome_revision"],
              m["outcome_computed_at"]) for m in members])
    return set_id, True


def latest_analogue_set(conn: Database, target_snapshot_id: str, matcher_version: str,
                        label_version: str) -> Optional[Dict[str, Any]]:
    """The newest analogue set of a target, with ``members`` in rank order."""
    row = conn.execute(
        "SELECT * FROM journal.analogue_sets WHERE target_snapshot_id = %s AND matcher_version = %s "
        "AND label_version = %s ORDER BY created_at DESC LIMIT 1;",
        (target_snapshot_id, matcher_version, label_version)).fetchone()
    if row is None:
        return None
    d = dict(zip(row.keys(), row))
    d["excluded"], d["outcome_summary"] = _load(d["excluded"]), _load(d["outcome_summary"])
    for k in ("set_id", "target_snapshot_id", "target_annotation_id"):
        d[k] = str(d[k])
    members = conn.execute("SELECT * FROM journal.analogue_members WHERE set_id = %s ORDER BY rank;",
                           (d["set_id"],)).fetchall()
    d["members"] = [dict(zip(m.keys(), m), snapshot_id=str(m["snapshot_id"]), annotation_id=str(m["annotation_id"]),
                         session_date=str(m["session_date"]), components=_load(m["components"])) for m in members]
    return d


# --------------------------------------------------------------------------
# LLM annotation attempts (migration 0012)
# --------------------------------------------------------------------------

def save_annotation_attempt(conn: Database, rec: Dict[str, Any]) -> str:
    """Appends one LLM annotation attempt (journal.annotation_attempts columns but ``attempt_id``)."""
    attempt_id = str(uuid.uuid4())
    with conn:
        conn.execute(
            "INSERT INTO journal.annotation_attempts (attempt_id, snapshot_id, protocol_version, model, request_hash, "
            "status, error, response, usage, annotation_id, started_at, finished_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s);",
            (attempt_id, rec["snapshot_id"], rec["protocol_version"], rec["model"], rec["request_hash"],
             rec["status"], rec.get("error"), None if rec.get("response") is None else canonical_json(rec["response"]),
             None if rec.get("usage") is None else canonical_json(rec["usage"]), rec.get("annotation_id"),
             rec["started_at"], rec["finished_at"]))
    return attempt_id


# --------------------------------------------------------------------------
# Annotation review sets (migration 0012)
# --------------------------------------------------------------------------

def create_annotation_review_set(conn: Database, name: str, protocol_version: str, snapshot_version: str,
                                 selection: Dict[str, Any], members: List[Dict[str, Any]]) -> bool:
    """Stores an annotation review set and its members ({'snapshot_id', 'annotation_id', 'reasons'}); True when new."""
    with conn:
        _lock(conn, "journal.annotation_review_sets", name)
        if conn.execute("SELECT 1 FROM journal.annotation_review_sets WHERE review_set = %s;", (name,)).fetchone():
            return False
        conn.execute("INSERT INTO journal.annotation_review_sets (review_set, protocol_version, snapshot_version, "
                     "selection) VALUES (%s, %s, %s, %s);",
                     (name, protocol_version, snapshot_version, canonical_json(selection)))
        conn.executemany(
            "INSERT INTO journal.annotation_review_members (review_set, snapshot_id, annotation_id, position, reasons) "
            "VALUES (%s, %s, %s, %s, %s);",
            [(name, m["snapshot_id"], m["annotation_id"], i, canonical_json(m["reasons"]))
             for i, m in enumerate(members, 1)])
    return True


def annotation_review_sets(conn: Database) -> List[Dict[str, Any]]:
    rows = conn.execute("SELECT * FROM journal.annotation_review_sets ORDER BY created_at DESC;").fetchall()
    return [dict(zip(r.keys(), r), selection=_load(r["selection"])) for r in rows]


def annotation_review_members(conn: Database, name: str) -> List[Dict[str, Any]]:
    rows = conn.execute(
        "SELECT m.snapshot_id, m.annotation_id, s.session_date, m.position, m.reasons "
        "FROM journal.annotation_review_members m JOIN journal.snapshots s ON s.snapshot_id = m.snapshot_id "
        "WHERE m.review_set = %s ORDER BY m.position;", (name,)).fetchall()
    return [{"snapshot_id": str(r["snapshot_id"]), "annotation_id": str(r["annotation_id"]),
             "session_date": str(r["session_date"]), "position": int(r["position"]),
             "reasons": _load(r["reasons"])} for r in rows]


def save_annotation_verdicts(conn: Database, name: str, snapshot_id: str, annotation_id: str,
                             verdicts: List[Dict[str, Any]], reviewer: Optional[str] = None) -> int:
    """Appends one verdict per field ({'field', 'shown_value', 'verdict', 'note'}); returns how many."""
    with conn:
        conn.executemany(
            "INSERT INTO journal.annotation_review_verdicts (review_set, snapshot_id, annotation_id, field, "
            "shown_value, verdict, note, reviewer) VALUES (%s, %s, %s, %s, %s, %s, %s, %s);",
            [(name, snapshot_id, annotation_id, v["field"], v["shown_value"], v["verdict"], v.get("note") or None,
              reviewer) for v in verdicts])
    return len(verdicts)


def latest_annotation_verdicts(conn: Database, name: str) -> List[Dict[str, Any]]:
    rows = conn.execute("SELECT * FROM journal.annotation_review_latest WHERE review_set = %s "
                        "ORDER BY session_date, field;", (name,)).fetchall()
    return [dict(zip(r.keys(), r), snapshot_id=str(r["snapshot_id"]), annotation_id=str(r["annotation_id"]))
            for r in rows]


def get_outcome(conn: Database, snapshot_id: str, label_version: str, revision: int) -> Optional[Dict[str, Any]]:
    """One outcome revision (an analogue set records the revision it used)."""
    row = conn.execute("SELECT * FROM journal.outcomes WHERE snapshot_id = %s AND label_version = %s "
                       "AND outcome_revision = %s;", (snapshot_id, label_version, revision)).fetchone()
    if row is None:
        return None
    d = dict(zip(row.keys(), row))
    d["labels"], d["measurements"] = _load(d["labels"]), _load(d["measurements"])
    return d
