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
  save_rth_set       an RTH analogue set (migrations 0024, 0025), idempotent on its
                     session, matcher, window and input digest; two views of a replay:
                     rth_set_issued - as issued, the live set stored by the time
                     replayed - and rth_set_at - the latest calculation of the
                     window replayed; neither ever reads a later window
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


def get_version(conn: Database, version: str) -> Optional[Dict[str, Any]]:
    """One registered definition ({'version', 'kind', 'definition', 'definition_hash', 'registered_at'})."""
    row = conn.execute("SELECT * FROM journal.definition_versions WHERE version = %s;", (version,)).fetchone()
    if row is None:
        return None
    d = dict(zip(row.keys(), row))
    d["definition"] = _load(d["definition"])
    return d


def list_versions(conn: Database, kind: str) -> List[Dict[str, Any]]:
    """The registered definitions of one kind, oldest first."""
    rows = conn.execute("SELECT * FROM journal.definition_versions WHERE kind = %s ORDER BY registered_at;",
                        (kind,)).fetchall()
    return [dict(zip(r.keys(), r), definition=_load(r["definition"])) for r in rows]


# --------------------------------------------------------------------------
# Snapshots
# --------------------------------------------------------------------------

def save_snapshot(conn: Database, snap) -> Tuple[str, bool]:
    """
    Stores a ``features.nq_evidence.Snapshot``. Returns ``(snapshot_id, created)``;
    a snapshot with the same source payload already stored is returned instead.
    A preview's snapshot (an earlier cutoff, forecaster/preview.py) is never stored.
    """
    if snap.payload["cutoff"].get("preview"):
        raise ValueError(f"a preview snapshot of {snap.session_date} is never stored in the journal")
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
    if not _is_uuid(snapshot_id):
        return None
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


def _annotation(row) -> Optional[Dict[str, Any]]:
    if row is None:
        return None
    d = dict(zip(row.keys(), row))
    for key in ("fields", "price_location", "measurements"):
        d[key] = _load(d[key])
    d["annotation_id"], d["snapshot_id"] = str(d["annotation_id"]), str(d["snapshot_id"])
    return d


def latest_annotation(conn: Database, snapshot_id: str, protocol_version: str) -> Optional[Dict[str, Any]]:
    return _annotation(conn.execute(
        "SELECT * FROM journal.structure_annotations WHERE snapshot_id = %s AND protocol_version = %s "
        "ORDER BY created_at DESC LIMIT 1;", (snapshot_id, protocol_version)).fetchone())


def _is_uuid(value) -> bool:
    try:
        uuid.UUID(str(value))
        return True
    except ValueError:
        return False


def get_annotation(conn: Database, annotation_id: str) -> Optional[Dict[str, Any]]:
    """One structure annotation by its id (None for an unknown or malformed id)."""
    if not _is_uuid(annotation_id):
        return None
    return _annotation(conn.execute("SELECT * FROM journal.structure_annotations WHERE annotation_id = %s;",
                                    (str(annotation_id),)).fetchone())


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
    ``set_id``. Idempotent: the same target annotation, matcher, label version, pool,
    analogue outcome revisions and prior manifest (migration 0013) return the stored set.
    """
    key = (rec["target_annotation_id"], rec["matcher_version"], rec["label_version"], rec["pool_hash"],
           rec["outcome_digest"], rec.get("prior_digest"))
    with conn:
        _lock(conn, "journal.analogue_sets", *key)
        row = conn.execute(
            "SELECT set_id FROM journal.analogue_sets WHERE target_annotation_id = %s AND matcher_version = %s "
            "AND label_version = %s AND pool_hash = %s AND outcome_digest = %s "
            "AND prior_digest IS NOT DISTINCT FROM %s;", key).fetchone()
        if row is not None:
            return str(row[0]), False
        set_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO journal.analogue_sets (set_id, target_snapshot_id, target_annotation_id, matcher_version, "
            "label_version, data_mode, pool_size, pool_hash, excluded, outcome_digest, prior_digest, "
            "mean_similarity, outcome_summary) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s);",
            (set_id, rec["target_snapshot_id"], rec["target_annotation_id"], rec["matcher_version"],
             rec["label_version"], rec["data_mode"], rec["pool_size"], rec["pool_hash"],
             canonical_json(rec["excluded"]), rec["outcome_digest"], rec.get("prior_digest"),
             rec["mean_similarity"], canonical_json(rec["outcome_summary"])))
        conn.executemany(
            "INSERT INTO journal.analogue_members (set_id, rank, snapshot_id, annotation_id, session_date, similarity, "
            "comparable_weight, components, outcome_revision, outcome_computed_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s);",
            [(set_id, m["rank"], m["snapshot_id"], m["annotation_id"], m["session_date"], m["similarity"],
              m["comparable_weight"], canonical_json(m["components"]), m["outcome_revision"],
              m["outcome_computed_at"]) for m in members])
    return set_id, True


def _analogue_set(conn: Database, row) -> Dict[str, Any]:
    d = dict(zip(row.keys(), row))
    d["excluded"], d["outcome_summary"] = _load(d["excluded"]), _load(d["outcome_summary"])
    for k in ("set_id", "target_snapshot_id", "target_annotation_id"):
        d[k] = str(d[k])
    members = conn.execute("SELECT * FROM journal.analogue_members WHERE set_id = %s ORDER BY rank;",
                           (d["set_id"],)).fetchall()
    d["members"] = [dict(zip(m.keys(), m), snapshot_id=str(m["snapshot_id"]), annotation_id=str(m["annotation_id"]),
                         session_date=str(m["session_date"]), components=_load(m["components"])) for m in members]
    return d


def get_analogue_set(conn: Database, set_id: str) -> Optional[Dict[str, Any]]:
    """One analogue set by its id - the exact lookup a forecast run uses - with its target annotation's
    ``protocol_version`` and ``members`` in rank order; None when there is no such set."""
    if not _is_uuid(set_id):
        return None
    row = conn.execute(
        "SELECT s.*, a.protocol_version FROM journal.analogue_sets s JOIN journal.structure_annotations a "
        "ON a.annotation_id = s.target_annotation_id WHERE s.set_id = %s;", (str(set_id),)).fetchone()
    return None if row is None else _analogue_set(conn, row)


def latest_analogue_set(conn: Database, target_snapshot_id: str, matcher_version: str, label_version: str,
                        protocol_version: str) -> Optional[Dict[str, Any]]:
    """The newest analogue set of a target whose annotation is under ``protocol_version`` (a rules set and a Claude
    set of one snapshot are different sets), with ``members`` in rank order."""
    row = conn.execute(
        "SELECT s.*, a.protocol_version FROM journal.analogue_sets s JOIN journal.structure_annotations a "
        "ON a.annotation_id = s.target_annotation_id WHERE s.target_snapshot_id = %s AND s.matcher_version = %s "
        "AND s.label_version = %s AND a.protocol_version = %s ORDER BY s.created_at DESC LIMIT 1;",
        (target_snapshot_id, matcher_version, label_version, protocol_version)).fetchone()
    return None if row is None else _analogue_set(conn, row)


# --------------------------------------------------------------------------
# LLM annotation attempts (migration 0012)
# --------------------------------------------------------------------------

def save_annotation_attempt(conn: Database, rec: Dict[str, Any]) -> str:
    """Appends one LLM annotation attempt (journal.annotation_attempts columns but ``attempt_id``); ``request_id``
    names the ledger request it answers (migration 0013), at most one attempt per request."""
    attempt_id = str(uuid.uuid4())
    with conn:
        conn.execute(
            "INSERT INTO journal.annotation_attempts (attempt_id, snapshot_id, protocol_version, model, request_hash, "
            "status, error, response, usage, annotation_id, started_at, finished_at, request_id, raw_text) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s);",
            (attempt_id, rec["snapshot_id"], rec["protocol_version"], rec["model"], rec["request_hash"],
             rec["status"], rec.get("error"), None if rec.get("response") is None else canonical_json(rec["response"]),
             None if rec.get("usage") is None else canonical_json(rec["usage"]), rec.get("annotation_id"),
             rec["started_at"], rec["finished_at"], rec.get("request_id"), rec.get("raw_text")))
    return attempt_id


# --------------------------------------------------------------------------
# Inference request ledger (migration 0013)
# --------------------------------------------------------------------------

def save_inference_request(conn: Database, rec: Dict[str, Any]) -> str:
    """Records one LLM request before it is sent - the exact canonical request (``request``) with its hashes, the
    code revision and ``mode`` (live | batch); returns its id, which a batch uses as the custom_id."""
    request_id = str(uuid.uuid4())
    with conn:
        conn.execute(
            "INSERT INTO journal.inference_requests (request_id, snapshot_id, protocol_version, model, request, "
            "request_hash, prompt_sha256, schema_sha256, evidence_sha256, code_revision, mode) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s);",
            (request_id, rec["snapshot_id"], rec["protocol_version"], rec["model"], canonical_json(rec["request"]),
             rec["request_hash"], rec["prompt_sha256"], rec["schema_sha256"], rec["evidence_sha256"],
             rec["code_revision"], rec["mode"]))
    return request_id


def save_inference_batch(conn: Database, batch_id: str, request_ids: List[str], submitted_at) -> None:
    """Records a submitted batch and its requests, as soon as the batch id is known."""
    with conn:
        conn.execute("INSERT INTO journal.inference_batches (batch_id, submitted_at, request_count) "
                     "VALUES (%s, %s, %s);", (batch_id, submitted_at, len(request_ids)))
        conn.executemany("INSERT INTO journal.inference_batch_requests (batch_id, request_id) VALUES (%s, %s);",
                         [(batch_id, r) for r in request_ids])


def _request_rows(rows) -> List[Dict[str, Any]]:
    return [dict(zip(r.keys(), r), request_id=str(r["request_id"]), snapshot_id=str(r["snapshot_id"]),
                 request=_load(r["request"])) for r in rows]


def inference_batch_requests(conn: Database, batch_id: str) -> List[Dict[str, Any]]:
    """The requests of one recorded batch, as sent, each with ``answered`` (an annotation attempt, or the forecast
    run of a synthesis request, exists) and its batch's ``submitted_at``."""
    return _request_rows(conn.execute(
        "SELECT r.*, b.submitted_at, (EXISTS (SELECT 1 FROM journal.annotation_attempts t "
        "WHERE t.request_id = r.request_id) OR EXISTS (SELECT 1 FROM journal.forecast_runs f "
        "WHERE f.request_id = r.request_id)) AS answered FROM journal.inference_batch_requests m "
        "JOIN journal.inference_requests r ON r.request_id = m.request_id JOIN journal.inference_batches b "
        "ON b.batch_id = m.batch_id WHERE m.batch_id = %s ORDER BY r.created_at;", (batch_id,)).fetchall())


def unresolved_inference_requests(conn: Database, protocol_version: str) -> List[Dict[str, Any]]:
    """Requests of the protocol without an attempt - an annotation attempt, or the forecast run a synthesis request
    produced (migration 0017) - in flight, in an unfinished batch, or lost when a run ended mid-request - with their
    ``batch_id`` (None: a live request, or a batch whose id was never recorded) and session date, oldest first."""
    return _request_rows(conn.execute(
        "SELECT r.*, m.batch_id, s.session_date FROM journal.inference_requests r "
        "JOIN journal.snapshots s ON s.snapshot_id = r.snapshot_id "
        "LEFT JOIN journal.inference_batch_requests m ON m.request_id = r.request_id "
        "WHERE r.protocol_version = %s AND NOT EXISTS (SELECT 1 FROM journal.annotation_attempts t "
        "WHERE t.request_id = r.request_id) AND NOT EXISTS (SELECT 1 FROM journal.forecast_runs f "
        "WHERE f.request_id = r.request_id) ORDER BY r.created_at;", (protocol_version,)).fetchall())


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


def outcome_history(conn: Database, label_version: str) -> Dict[str, List[Dict[str, Any]]]:
    """Every outcome revision under a label version, per snapshot id, oldest revision first (each with
    ``labels``, ``outcome_revision`` and ``computed_at``)."""
    out: Dict[str, List[Dict[str, Any]]] = {}
    for row in conn.execute("SELECT * FROM journal.outcomes WHERE label_version = %s "
                            "ORDER BY snapshot_id, outcome_revision;", (label_version,)).fetchall():
        d = dict(zip(row.keys(), row))
        d["labels"], d["measurements"] = _load(d["labels"]), _load(d["measurements"])
        out.setdefault(str(d["snapshot_id"]), []).append(d)
    return out


def get_outcome(conn: Database, snapshot_id: str, label_version: str, revision: int) -> Optional[Dict[str, Any]]:
    """One outcome revision (an analogue set records the revision it used)."""
    row = conn.execute("SELECT * FROM journal.outcomes WHERE snapshot_id = %s AND label_version = %s "
                       "AND outcome_revision = %s;", (snapshot_id, label_version, revision)).fetchone()
    if row is None:
        return None
    d = dict(zip(row.keys(), row))
    d["labels"], d["measurements"] = _load(d["labels"]), _load(d["measurements"])
    return d


# --------------------------------------------------------------------------
# Forecast runs (migration 0014)
# --------------------------------------------------------------------------

_RUN_COLUMNS = ("run_id", "idempotency_key", "symbol", "session_date", "contract_id", "profile", "snapshot_id",
                "annotation_id", "analogue_set_id", "label_version", "algorithm_version", "schema_version",
                "issue_policy", "code_revision", "mode", "input_cutoff_at", "deadline_at", "generation_started_at",
                "generation_completed_at", "lifecycle_status", "supersedes_run_id", "failure_reason",
                "evidence_digest", "outputs", "request_id")
_PREDICTION_COLUMNS = ("target", "status", "predicted_label", "estimation_status", "distribution", "eligible",
                       "without_label", "prior_sessions", "prior_without_label", "reason")


def find_forecast_run(conn: Database, idempotency_key: str) -> Optional[str]:
    row = conn.execute("SELECT run_id FROM journal.forecast_runs WHERE idempotency_key = %s;",
                       (idempotency_key,)).fetchone()
    return None if row is None else str(row[0])


def save_forecast_run(conn: Database, run: Dict[str, Any], evidence: Dict[str, Any],
                      predictions: List[Dict[str, Any]]) -> Tuple[str, bool]:
    """
    Stores one forecast run with its evidence and predictions in one transaction; ``(run_id, created)``. A run with
    an idempotency key already stored returns that run. The database decides issuance: it stamps issued_at with
    its own clock and turns a live run inserted after its deadline into a late one (migration 0014).
    """
    with conn:
        _lock(conn, "journal.forecast_runs", run["idempotency_key"])
        existing = find_forecast_run(conn, run["idempotency_key"])
        if existing is not None:
            return existing, False
        run_id = str(uuid.uuid4())
        values = {**run, "run_id": run_id, "outputs": canonical_json(run["outputs"])}
        conn.execute(f"INSERT INTO journal.forecast_runs ({', '.join(_RUN_COLUMNS)}) "
                     f"VALUES ({', '.join(['%s'] * len(_RUN_COLUMNS))});", tuple(values.get(c) for c in _RUN_COLUMNS))
        conn.execute("INSERT INTO journal.forecast_evidence (run_id, evidence, evidence_digest) VALUES (%s, %s, %s);",
                     (run_id, canonical_json(evidence), run["evidence_digest"]))
        conn.executemany(
            f"INSERT INTO journal.forecast_predictions (run_id, {', '.join(_PREDICTION_COLUMNS)}) "
            f"VALUES (%s, {', '.join(['%s'] * len(_PREDICTION_COLUMNS))});",
            [(run_id, *[canonical_json(p[c]) if c == "distribution" and p[c] is not None else p[c]
                        for c in _PREDICTION_COLUMNS]) for p in predictions])
    return run_id, True


def add_forecast_event(conn: Database, run_id: str, event: str, detail: Optional[str] = None) -> None:
    """Appends a run event; the database stamps its time."""
    with conn:
        conn.execute("INSERT INTO journal.forecast_run_events (run_id, event, detail) VALUES (%s, %s, %s);",
                     (run_id, event, detail))


def _run(row) -> Dict[str, Any]:
    d = dict(zip(row.keys(), row))
    for k in ("run_id", "snapshot_id", "annotation_id", "analogue_set_id", "supersedes_run_id", "request_id"):
        d[k] = None if d.get(k) is None else str(d[k])
    d["session_date"], d["outputs"] = str(d["session_date"]), _load(d["outputs"])
    return d


def get_forecast_run(conn: Database, run_id: str) -> Optional[Dict[str, Any]]:
    """One run by its id, with ``predictions`` (by target), ``evidence`` and ``events``; None for an unknown or
    malformed id."""
    if not _is_uuid(run_id):
        return None
    row = conn.execute("SELECT * FROM journal.forecast_runs WHERE run_id = %s;", (str(run_id),)).fetchone()
    if row is None:
        return None
    d = _run(row)
    d["predictions"] = {}
    for p in conn.execute("SELECT * FROM journal.forecast_predictions WHERE run_id = %s ORDER BY target;",
                          (d["run_id"],)).fetchall():
        p = dict(zip(p.keys(), p))
        p["distribution"] = _load(p["distribution"])
        d["predictions"][p["target"]] = p
    ev = conn.execute("SELECT evidence FROM journal.forecast_evidence WHERE run_id = %s;", (d["run_id"],)).fetchone()
    d["evidence"] = None if ev is None else _load(ev[0])
    d["events"] = [dict(zip(e.keys(), e)) for e in conn.execute(
        "SELECT event, at, detail FROM journal.forecast_run_events WHERE run_id = %s ORDER BY event_id;",
        (d["run_id"],)).fetchall()]
    return d


def list_forecast_runs(conn: Database, start: str, end: str, profile: Optional[str] = None,
                       mode: Optional[str] = None) -> List[Dict[str, Any]]:
    """Runs of sessions in [start, end] (without predictions), oldest session first, newest run first within it -
    for navigation; a forecast is always read by its run id."""
    rows = conn.execute(
        "SELECT * FROM journal.forecast_runs WHERE session_date BETWEEN %s AND %s "
        "AND (%s::text IS NULL OR profile = %s) AND (%s::text IS NULL OR mode = %s) "
        "ORDER BY session_date, created_at DESC;", (start, end, profile, profile, mode, mode)).fetchall()
    return [_run(r) for r in rows]


def current_forecast_run(conn: Database, session_date: str, profile: str, mode: str, algorithm_version: str,
                         exclude_key: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """The newest official run (issued or unavailable) of a session under one profile, mode and algorithm - the run
    a new one supersedes."""
    row = conn.execute(
        "SELECT * FROM journal.forecast_runs WHERE session_date = %s AND profile = %s AND mode = %s "
        "AND algorithm_version = %s AND lifecycle_status IN ('issued', 'unavailable') "
        "AND idempotency_key IS DISTINCT FROM %s ORDER BY created_at DESC LIMIT 1;",
        (session_date, profile, mode, algorithm_version, exclude_key)).fetchone()
    return None if row is None else _run(row)


# --------------------------------------------------------------------------
# Experiments (migration 0015)
# --------------------------------------------------------------------------

_CASE_COLUMNS = ("session_date", "arm", "run_id", "snapshot_id", "outcome_revision", "status", "detail")


def issued_runs(conn: Database, start: str, end: str, profile: str, mode: str, algorithm_version: str,
                label_version: str) -> List[Dict[str, Any]]:
    """The issued runs of sessions in [start, end] under one profile, mode, algorithm and label version, with their
    events - the candidates an experiment's official-run rule chooses from - oldest issue first."""
    rows = conn.execute(
        "SELECT * FROM journal.forecast_runs WHERE session_date BETWEEN %s AND %s AND profile = %s AND mode = %s "
        "AND algorithm_version = %s AND label_version = %s AND lifecycle_status = 'issued' "
        "ORDER BY session_date, issued_at, run_id;",
        (start, end, profile, mode, algorithm_version, label_version)).fetchall()
    runs = [_run(r) for r in rows]
    events: Dict[str, List[Dict[str, Any]]] = {}
    if runs:
        for e in conn.execute("SELECT run_id, event, at, detail FROM journal.forecast_run_events "
                              "WHERE run_id = ANY(%s::uuid[]) ORDER BY event_id;",
                              ([r["run_id"] for r in runs],)).fetchall():
            events.setdefault(str(e["run_id"]), []).append(dict(zip(e.keys(), e)))
    for r in runs:
        r["events"] = events.get(r["run_id"], [])
    return runs


def experiment_cases(conn: Database, experiment: str) -> List[Dict[str, Any]]:
    rows = conn.execute("SELECT * FROM journal.experiment_cases WHERE experiment = %s ORDER BY session_date, arm;",
                        (experiment,)).fetchall()
    return [dict(zip(r.keys(), r), session_date=str(r["session_date"]),
                 run_id=None if r["run_id"] is None else str(r["run_id"]),
                 snapshot_id=None if r["snapshot_id"] is None else str(r["snapshot_id"])) for r in rows]


def freeze_experiment_cases(conn: Database, experiment: str, cases: List[Dict[str, Any]]) -> Tuple[int, bool]:
    """Stores an experiment's cases once: ``(count, created)``; cases already frozen are kept as they are."""
    with conn:
        _lock(conn, "journal.experiment_cases", experiment)
        n = conn.execute("SELECT count(*) FROM journal.experiment_cases WHERE experiment = %s;",
                         (experiment,)).fetchone()[0]
        if n:
            return int(n), False
        conn.executemany(
            f"INSERT INTO journal.experiment_cases (experiment, {', '.join(_CASE_COLUMNS)}) "
            f"VALUES (%s, {', '.join(['%s'] * len(_CASE_COLUMNS))});",
            [(experiment, *[c[k] for k in _CASE_COLUMNS]) for c in cases])
    return len(cases), True


def save_experiment_result(conn: Database, experiment: str, results: Dict[str, Any], code_revision: str) -> str:
    result_id = str(uuid.uuid4())
    body = canonical_json(results)
    with conn:
        conn.execute("INSERT INTO journal.experiment_results (result_id, experiment, results, results_hash, "
                     "code_revision) VALUES (%s, %s, %s, %s, %s);",
                     (result_id, experiment, body, hashlib.sha256(body.encode()).hexdigest(), code_revision))
    return result_id


def experiment_results(conn: Database, experiment: str) -> List[Dict[str, Any]]:
    """Every scoring of an experiment, newest first."""
    rows = conn.execute("SELECT * FROM journal.experiment_results WHERE experiment = %s ORDER BY computed_at DESC;",
                        (experiment,)).fetchall()
    return [dict(zip(r.keys(), r), result_id=str(r["result_id"]), results=_load(r["results"])) for r in rows]


# --------------------------------------------------------------------------
# Live capture (migration 0016)
# --------------------------------------------------------------------------

def start_live_capture(conn: Database, session_date: str, profile: str, contract_id: Optional[int],
                       code_revision: str) -> str:
    capture_id = str(uuid.uuid4())
    with conn:
        conn.execute("INSERT INTO journal.live_captures (capture_id, session_date, profile, contract_id, code_revision) "
                     "VALUES (%s, %s, %s, %s, %s);", (capture_id, session_date, profile, contract_id, code_revision))
    return capture_id


def add_capture_event(conn: Database, capture_id: str, event: str, detail: Optional[Dict[str, Any]] = None) -> str:
    """Appends a capture step; returns its database time."""
    with conn:
        row = conn.execute("INSERT INTO journal.live_capture_events (capture_id, event, detail) VALUES (%s, %s, %s) "
                           "RETURNING at;", (capture_id, event, None if detail is None else canonical_json(detail))
                           ).fetchone()
    return row[0]


def save_bar_receipts(conn: Database, capture_id: str, contract_id: int, bars: List[tuple],
                      interval: str = "1m", price_type: str = "TRADES") -> int:
    """Records the bars a capture received ((start, open, high, low, close, volume) each), stamped by the database."""
    with conn:
        conn.executemany(
            "INSERT INTO journal.bar_receipts (capture_id, contract_id, interval, price_type, bar_start_at, open, high, "
            "low, close, volume) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s);",
            [(capture_id, contract_id, interval, price_type, *b) for b in bars])
    return len(bars)


def capture_receipts(conn: Database, capture_id: str) -> Dict[str, Dict[str, Any]]:
    """A capture's receipts by bar start ('YYYY-MM-DD HH:MM:SS' UTC), the last receipt of a bar winning."""
    out: Dict[str, Dict[str, Any]] = {}
    for r in conn.execute("SELECT * FROM journal.bar_receipts WHERE capture_id = %s ORDER BY receipt_id;",
                          (capture_id,)).fetchall():
        d = dict(zip(r.keys(), r))
        out[str(d["bar_start_at"])[:19]] = d
    return out


def capture_events(conn: Database, capture_id: str) -> List[Dict[str, Any]]:
    return [dict(zip(r.keys(), r), detail=_load(r["detail"])) for r in conn.execute(
        "SELECT event, at, detail FROM journal.live_capture_events WHERE capture_id = %s ORDER BY event_id;",
        (capture_id,)).fetchall()]


def live_captures(conn: Database, start: str, end: str) -> List[Dict[str, Any]]:
    """Captures of sessions in [start, end], oldest first, each with its events."""
    rows = conn.execute("SELECT * FROM journal.live_captures WHERE session_date BETWEEN %s AND %s "
                        "ORDER BY started_at;", (start, end)).fetchall()
    return [dict(zip(r.keys(), r), capture_id=str(r["capture_id"]), session_date=str(r["session_date"]),
                 events=capture_events(conn, str(r["capture_id"]))) for r in rows]


def live_snapshot(conn: Database, session_date: str, snapshot_version: str) -> Optional[Dict[str, Any]]:
    """The session's live-capture snapshot of a version, if one was frozen (the oldest: a live snapshot is frozen
    once)."""
    row = conn.execute("SELECT * FROM journal.snapshots WHERE session_date = %s AND snapshot_version = %s "
                       "AND data_mode = 'live_capture' ORDER BY built_at LIMIT 1;",
                       (session_date, snapshot_version)).fetchone()
    return None if row is None else _snapshot_dict(row)


def fetched_at(conn: Database, days: List[tuple]) -> Dict[tuple, Optional[Any]]:
    """When each (contract_id, trading_day) was last stored by the collector (session_days.fetched_at)."""
    out: Dict[tuple, Optional[Any]] = {d: None for d in days}
    if days:
        for r in conn.execute("SELECT contract_id, trading_day, fetched_at FROM session_days WHERE interval = '1m' "
                              "AND price_type = 'TRADES' AND (contract_id, trading_day) IN "
                              "(SELECT * FROM unnest(%s::bigint[], %s::date[]));",
                              ([int(c) for c, _ in days], [str(d) for _, d in days])).fetchall():
            out[(int(r[0]), str(r[1]))] = r[2]
    return out


# --------------------------------------------------------------------------
# RTH analogue sets (migration 0024)
# --------------------------------------------------------------------------

def save_rth_set(conn: Database, rec: Dict[str, Any], members: List[Dict[str, Any]]) -> Tuple[str, bool]:
    """
    Stores one RTH analogue set and its members (``rec``: the journal.rth_analogue_sets columns but ``set_id``,
    ``checkpoint``, ``data_mode`` and ``created_at``, which the database derives and stamps - live only when
    ``issued_by`` is auto or manual and it is within 30 minutes of the cutoff). Idempotent: an issue (auto, manual)
    of a session, matcher version, window and input digest already issued returns that set; a backfill of inputs
    already stored in any way returns the stored set. An issue is recorded even after a backfill of the same inputs.
    Repeated runs on unchanged inputs add nothing; a revised input adds a set beside the earlier one.
    """
    key = (rec["symbol"], rec["session_date"], rec["matcher_version"], rec["elapsed_minutes"], rec["input_digest"])
    backfill = rec["issued_by"] == "backfill"
    with conn:
        _lock(conn, "journal.rth_analogue_sets", *key)
        row = conn.execute(
            "SELECT set_id FROM journal.rth_analogue_sets WHERE symbol = %s AND session_date = %s "
            "AND matcher_version = %s AND elapsed_minutes = %s AND input_digest = %s "
            "AND (%s OR issued_by IN ('auto', 'manual')) ORDER BY created_at LIMIT 1;", (*key, backfill)).fetchone()
        if row is not None:
            return str(row[0]), False
        set_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO journal.rth_analogue_sets (set_id, symbol, session_date, contract_id, matcher_version, "
            "context_snapshot_id, elapsed_minutes, cutoff_at, input_digest, pool_size, pool_hash, excluded, "
            "mean_similarity, target_features, quality, code_revision, issued_by, inputs_received_at, "
            "pool_received_at, pit_status, data_mode) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
            "%s, %s, %s, %s, %s, %s, %s, 'historical_reconstruction');",
            (set_id, rec["symbol"], rec["session_date"], rec["contract_id"], rec["matcher_version"],
             rec["context_snapshot_id"], rec["elapsed_minutes"], rec["cutoff_at"], rec["input_digest"],
             rec["pool_size"], rec["pool_hash"], canonical_json(rec["excluded"]), rec["mean_similarity"],
             canonical_json(rec["target_features"]), canonical_json(rec["quality"]), rec["code_revision"],
             rec["issued_by"], rec.get("inputs_received_at"), rec.get("pool_received_at"), rec.get("pit_status")))
        conn.executemany(
            "INSERT INTO journal.rth_analogue_members (set_id, rank, session_date, contract_id, snapshot_id, "
            "similarity, comparable_weight, components) VALUES (%s, %s, %s, %s, %s, %s, %s, %s);",
            [(set_id, m["rank"], m["session_date"], m["contract_id"], m["snapshot_id"], m["similarity"],
              m["comparable_weight"], canonical_json(m["components"])) for m in members])
    return set_id, True


def _rth_set(conn: Database, row) -> Dict[str, Any]:
    d = dict(zip(row.keys(), row))
    for k in ("excluded", "target_features", "quality"):
        d[k] = _load(d[k])
    d["set_id"], d["context_snapshot_id"], d["session_date"] = (str(d["set_id"]), str(d["context_snapshot_id"]),
                                                                str(d["session_date"]))
    members = conn.execute("SELECT * FROM journal.rth_analogue_members WHERE set_id = %s ORDER BY rank;",
                           (d["set_id"],)).fetchall()
    d["members"] = [dict(zip(m.keys(), m), set_id=d["set_id"], snapshot_id=str(m["snapshot_id"]),
                         session_date=str(m["session_date"]), components=_load(m["components"])) for m in members]
    return d


def get_rth_set(conn: Database, set_id: str) -> Optional[Dict[str, Any]]:
    """One RTH analogue set by its id, with ``members`` in rank order; None when there is no such set."""
    if not _is_uuid(set_id):
        return None
    row = conn.execute("SELECT * FROM journal.rth_analogue_sets WHERE set_id = %s;", (str(set_id),)).fetchone()
    return None if row is None else _rth_set(conn, row)


def rth_set_at(conn: Database, symbol: str, session_date: str, matcher_version: str, minutes: int,
               stored_by=None) -> Optional[Dict[str, Any]]:
    """
    The reconstructed view of ``session_date`` at ``minutes`` after the open: the set of the longest stored window
    that is not longer - never a later window - and of that window the newest set, live or not (stored by
    ``stored_by``, when given). It can be a correction stored hours later: rth_set_issued is what was issued.
    None when no window that short is stored.
    """
    row = conn.execute(
        "SELECT * FROM journal.rth_analogue_sets WHERE symbol = %s AND session_date = %s AND matcher_version = %s "
        "AND elapsed_minutes <= %s AND (%s::timestamptz IS NULL OR created_at <= %s::timestamptz) "
        "ORDER BY elapsed_minutes DESC, created_at DESC LIMIT 1;",
        (symbol, session_date, matcher_version, int(minutes), stored_by, stored_by)).fetchone()
    return None if row is None else _rth_set(conn, row)


def rth_set_issued(conn: Database, symbol: str, session_date: str, matcher_version: str, at=None,
                   minutes: Optional[int] = None) -> Optional[Dict[str, Any]]:
    """
    The as-issued view: of the session's live sets (issued by Auto or by hand within 30 minutes of their cutoff),
    the newest stored by ``at`` (None: by now) - what was on offer at that moment, never a later correction or a
    backfill. ``minutes``: of that window only. None when nothing was issued live by then.
    """
    row = conn.execute(
        "SELECT * FROM journal.rth_analogue_sets WHERE symbol = %s AND session_date = %s AND matcher_version = %s "
        "AND data_mode = 'live' AND (%s::timestamptz IS NULL OR created_at <= %s::timestamptz) "
        "AND (%s::int IS NULL OR elapsed_minutes = %s::int) ORDER BY created_at DESC, elapsed_minutes DESC LIMIT 1;",
        (symbol, session_date, matcher_version, at, at, minutes, minutes)).fetchone()
    return None if row is None else _rth_set(conn, row)


def rth_windows(conn: Database, symbol: str, session_date: str, matcher_version: str) -> List[Dict[str, Any]]:
    """The session's stored RTH windows, shortest first: ``elapsed_minutes``, ``checkpoint``, ``sets`` (how many -
    more than one when an input was revised), ``live`` (how many were issued live), ``issued`` (how many by Auto or
    by hand, live or late - not backfills), the first and latest ``created_at``."""
    rows = conn.execute(
        "SELECT elapsed_minutes, max(checkpoint) AS checkpoint, count(*) AS sets, min(created_at) AS first_at, "
        "max(created_at) AS latest_at, count(*) FILTER (WHERE data_mode = 'live') AS live, "
        "count(*) FILTER (WHERE issued_by IN ('auto', 'manual')) AS issued "
        "FROM journal.rth_analogue_sets "
        "WHERE symbol = %s AND session_date = %s AND matcher_version = %s GROUP BY elapsed_minutes "
        "ORDER BY elapsed_minutes;", (symbol, session_date, matcher_version)).fetchall()
    return [dict(zip(r.keys(), r)) for r in rows]


# --------------------------------------------------------------------------
# The RTH evaluation (migration 0026)
# --------------------------------------------------------------------------

def save_rth_eval_forecast(conn: Database, rec: Dict[str, Any]) -> Tuple[str, bool]:
    """Stores the evaluation forecasts of one session and cutoff (``rec``: the journal.rth_eval_forecasts columns
    but ``forecast_id``, ``created_at``, ``issue_delay_s`` and ``eligible``, which the database stamps). Only the
    first per evaluation, session and cutoff is stored; a later one returns the first."""
    key = (rec["evaluation_version"], rec["symbol"], rec["session_date"], rec["elapsed_minutes"])
    with conn:
        _lock(conn, "journal.rth_eval_forecasts", *key)
        row = conn.execute(
            "SELECT forecast_id FROM journal.rth_eval_forecasts WHERE evaluation_version = %s AND symbol = %s "
            "AND session_date = %s AND elapsed_minutes = %s;", key).fetchone()
        if row is not None:
            return str(row[0]), False
        forecast_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO journal.rth_eval_forecasts (forecast_id, evaluation_version, set_id, symbol, session_date, "
            "elapsed_minutes, cutoff_at, horizon_minutes, target_atr, forecasts, sources, digest, code_revision) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s);",
            (forecast_id, rec["evaluation_version"], rec["set_id"], rec["symbol"], rec["session_date"],
             rec["elapsed_minutes"], rec["cutoff_at"], rec["horizon_minutes"], rec["target_atr"],
             canonical_json(rec["forecasts"]), canonical_json(rec["sources"]), rec["digest"], rec["code_revision"]))
    return forecast_id, True


def rth_eval_forecasts(conn: Database, evaluation_version: str) -> List[Dict[str, Any]]:
    """Every stored evaluation forecast of a version, oldest session first."""
    rows = conn.execute("SELECT * FROM journal.rth_eval_forecasts WHERE evaluation_version = %s "
                        "ORDER BY session_date, elapsed_minutes;", (evaluation_version,)).fetchall()
    out = []
    for r in rows:
        d = dict(zip(r.keys(), r))
        d.update(forecasts=_load(d["forecasts"]), sources=_load(d["sources"]), forecast_id=str(d["forecast_id"]),
                 set_id=str(d["set_id"]), session_date=str(d["session_date"]))
        out.append(d)
    return out


def save_rth_eval_result(conn: Database, evaluation_version: str, results: Dict[str, Any], code_revision: str) -> None:
    """Stores an evaluation's one scoring (a second is refused by the primary key)."""
    with conn:
        conn.execute("INSERT INTO journal.rth_eval_results (evaluation_version, results, code_revision) "
                     "VALUES (%s, %s, %s);", (evaluation_version, canonical_json(results), code_revision))


def rth_eval_result(conn: Database, evaluation_version: str) -> Optional[Dict[str, Any]]:
    row = conn.execute("SELECT * FROM journal.rth_eval_results WHERE evaluation_version = %s;",
                       (evaluation_version,)).fetchone()
    if row is None:
        return None
    d = dict(zip(row.keys(), row))
    d["results"] = _load(d["results"])
    return d
