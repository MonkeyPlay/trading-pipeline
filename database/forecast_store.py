# database/forecast_store.py
"""
Read/write access to the v2 forecast records (schema ``forecast``, migration
0004). Everything here appends: nothing updates or deletes a record, and the
database refuses if anything tries.

  register_*_version      definitions; re-registering the same name with a
                          different definition raises VersionConflict
  save_feature_snapshot   idempotent on the snapshot grain (instrument, session,
                          cutoff, feature version, source revision)
  save_forecast_run       one run + its predictions, atomically
  save_outcome_metrics /  the next outcome_revision when the recomputed result
  save_realised_outcome   differs from the latest one, else the latest one
  get_prediction_outcomes the prediction/outcome join, for an explicitly
                          chosen outcome revision
"""

import hashlib
import json
import logging
import uuid
from typing import Any, Dict, List, Optional, Tuple

from database.connection import Database

logger = logging.getLogger(__name__)


class VersionConflict(RuntimeError):
    """A version name is already registered with a different definition."""


def _json(obj) -> str:
    """Strict JSON: NaN/Infinity raise instead of being written."""
    return json.dumps(obj, allow_nan=False, sort_keys=True, default=str)


def _load(value):
    return json.loads(value) if isinstance(value, str) else value


def _lock(conn: Database, *parts) -> None:
    key = int(hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()[:15], 16)
    conn.execute("SELECT pg_advisory_xact_lock(%s);", (key,))


# --------------------------------------------------------------------------
# Registries
# --------------------------------------------------------------------------

def _register(conn, table, key_col, key, definition_hash, insert):
    with conn:
        _lock(conn, table, key)
        row = conn.execute(f"SELECT definition_hash FROM forecast.{table} WHERE {key_col} = %s;",
                           (key,)).fetchone()
        if row is not None:
            if row[0] != definition_hash:
                raise VersionConflict(
                    f"{key} is already registered with a different definition. Versions are "
                    f"immutable: give the changed definition a new version name."
                )
            return False
        insert()
    logger.info(f"Registered {table[:-1].replace('_', ' ')} {key}.")
    return True


def register_feature_version(conn: Database, rec: Dict[str, Any]) -> bool:
    def insert():
        conn.execute(
            "INSERT INTO forecast.feature_versions (feature_version, definition_hash, parameters, description) "
            "VALUES (%s, %s, %s, %s);",
            (rec["feature_version"], rec["definition_hash"], _json(rec["parameters"]), rec["description"]),
        )
        conn.executemany(
            "INSERT INTO forecast.feature_definitions (feature_version, feature_name, family, data_type, unit, "
            "allowed_values, lower_bound, upper_bound, default_required, definition) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s);",
            [(rec["feature_version"], d["feature_name"], d["family"], d["data_type"], d["unit"],
              d["allowed_values"], d["lower_bound"], d["upper_bound"], d["default_required"], d["definition"])
             for d in rec["definitions"]],
        )
    return _register(conn, "feature_versions", "feature_version", rec["feature_version"],
                     rec["definition_hash"], insert)


def register_label_version(conn: Database, rec: Dict[str, Any]) -> bool:
    def insert():
        conn.execute(
            "INSERT INTO forecast.label_versions (label_version, definition_hash, description) VALUES (%s, %s, %s);",
            (rec["label_version"], rec["definition_hash"], rec["description"]),
        )
        conn.executemany(
            "INSERT INTO forecast.label_definitions (label_version, target_id, labels, definition, parameters) "
            "VALUES (%s, %s, %s, %s, %s);",
            [(rec["label_version"], t["target_id"], list(t["labels"]), t["definition"], _json(t["parameters"]))
             for t in rec["targets"]],
        )
    return _register(conn, "label_versions", "label_version", rec["label_version"],
                     rec["definition_hash"], insert)


def register_model_version(conn: Database, rec: Dict[str, Any]) -> bool:
    def insert():
        conn.execute(
            "INSERT INTO forecast.model_versions (model_version, feature_version, label_version, target_ids, "
            "required_features, description, parameters, definition_hash) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s);",
            (rec["model_version"], rec["feature_version"], rec["label_version"], list(rec["target_ids"]),
             list(rec["required_features"]), rec["description"], _json(rec["parameters"]),
             rec["definition_hash"]),
        )
    return _register(conn, "model_versions", "model_version", rec["model_version"],
                     rec["definition_hash"], insert)


def get_label_vocabulary(conn: Database, label_version: str) -> Dict[str, List[str]]:
    rows = conn.execute("SELECT target_id, labels FROM forecast.label_definitions WHERE label_version = %s;",
                        (label_version,)).fetchall()
    return {r["target_id"]: list(r["labels"]) for r in rows}


# --------------------------------------------------------------------------
# Snapshots
# --------------------------------------------------------------------------

_SNAPSHOT_GRAIN = ("instrument_id", "session_date", "cutoff_at", "feature_version", "source_revision_id")


def save_feature_snapshot(conn: Database, snap, supersedes_snapshot_id: Optional[str] = None,
                          correction_reason: Optional[str] = None) -> Tuple[str, bool]:
    """
    Stores a ``features.nq_v2.SnapshotResult``. Returns ``(snapshot_id, created)``;
    a snapshot with the same grain already stored is returned, not duplicated.
    """
    new_id = str(uuid.uuid4())
    with conn:
        conn.execute(
            "INSERT INTO forecast.source_revisions (source_revision_id, manifest) VALUES (%s, %s) "
            "ON CONFLICT (source_revision_id) DO NOTHING;",
            (snap.source_revision_id, _json(snap.manifest)),
        )
        row = conn.execute(
            "INSERT INTO forecast.feature_snapshots (snapshot_id, session_date, instrument_id, cutoff_at, "
            "features_frozen_at, data_mode, pit_availability_status, feature_version, source_revision_id, "
            "session_schedule, scheduled_close_at, source_status, feature_status, data_quality_status, "
            "reference_values, features, supersedes_snapshot_id, correction_reason) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
            f"ON CONFLICT ({', '.join(_SNAPSHOT_GRAIN)}) DO NOTHING RETURNING snapshot_id;",
            (new_id, snap.session_date, snap.instrument_id, snap.cutoff_at, snap.features_frozen_at,
             snap.data_mode, snap.pit_availability_status, snap.feature_version, snap.source_revision_id,
             snap.session_schedule, snap.scheduled_close_at, _json(snap.source_status),
             _json(snap.feature_status), snap.data_quality_status, _json(snap.reference_values),
             _json(snap.features), supersedes_snapshot_id, correction_reason),
        ).fetchone()
        if row is not None:
            return str(row[0]), True
        existing = conn.execute(
            "SELECT snapshot_id FROM forecast.feature_snapshots WHERE instrument_id = %s AND session_date = %s "
            "AND cutoff_at = %s AND feature_version = %s AND source_revision_id = %s;",
            (snap.instrument_id, snap.session_date, snap.cutoff_at, snap.feature_version,
             snap.source_revision_id),
        ).fetchone()
        return str(existing[0]), False


def _snapshot_dict(row) -> Dict[str, Any]:
    d = dict(zip(row.keys(), row))
    for k in ("source_status", "feature_status", "reference_values", "features"):
        d[k] = _load(d[k])
    d["snapshot_id"] = str(d["snapshot_id"])
    return d


def get_feature_snapshot(conn: Database, snapshot_id: str) -> Optional[Dict[str, Any]]:
    row = conn.execute("SELECT * FROM forecast.feature_snapshots WHERE snapshot_id = %s;",
                       (snapshot_id,)).fetchone()
    return _snapshot_dict(row) if row else None


def find_snapshots(conn: Database, session_date: str, feature_version: str,
                   symbol: str = "NQ") -> List[Dict[str, Any]]:
    """Snapshots of one session, live captures first, then newest first."""
    rows = conn.execute(
        "SELECT s.* FROM forecast.feature_snapshots s JOIN contracts c ON c.contract_id = s.instrument_id "
        "WHERE s.session_date = %s AND s.feature_version = %s AND c.symbol = %s "
        "ORDER BY (s.data_mode = 'live_capture') DESC, s.created_at DESC;",
        (session_date, feature_version, symbol),
    ).fetchall()
    return [_snapshot_dict(r) for r in rows]


def list_snapshots(conn: Database, start: str, end: str, feature_version: str,
                   symbol: str = "NQ") -> List[Dict[str, Any]]:
    rows = conn.execute(
        "SELECT s.* FROM forecast.feature_snapshots s JOIN contracts c ON c.contract_id = s.instrument_id "
        "WHERE s.session_date BETWEEN %s AND %s AND s.feature_version = %s AND c.symbol = %s "
        "ORDER BY s.session_date, s.created_at;",
        (start, end, feature_version, symbol),
    ).fetchall()
    return [_snapshot_dict(r) for r in rows]


# --------------------------------------------------------------------------
# Forecast runs
# --------------------------------------------------------------------------

def save_forecast_run(conn: Database, run: Dict[str, Any], predictions: List[Dict[str, Any]]) -> str:
    """Inserts a run and all its predictions in one transaction; returns the run id."""
    run_id = str(uuid.uuid4())
    with conn:
        conn.execute(
            "INSERT INTO forecast.forecast_runs (forecast_run_id, snapshot_id, model_version, label_version, "
            "generated_at, calibration, input_quality_status, code_revision, supersedes_run_id) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s);",
            (run_id, run["snapshot_id"], run["model_version"], run["label_version"], run["generated_at"],
             _json(run["calibration"]), run["input_quality_status"], run.get("code_revision"),
             run.get("supersedes_run_id")),
        )
        conn.executemany(
            "INSERT INTO forecast.predictions (forecast_run_id, target_id, predicted_label, probabilities, "
            "abstained, abstention_reason) VALUES (%s, %s, %s, %s, %s, %s);",
            [(run_id, p["target_id"], p.get("predicted_label"),
              _json(p["probabilities"]) if p.get("probabilities") is not None else None,
              p["abstained"], p.get("abstention_reason")) for p in predictions],
        )
    return run_id


# --------------------------------------------------------------------------
# Outcomes (revisioned)
# --------------------------------------------------------------------------

def _latest(conn, table, where, params):
    return conn.execute(
        f"SELECT * FROM forecast.{table} WHERE {where} ORDER BY outcome_revision DESC LIMIT 1;", params
    ).fetchone()


def save_outcome_metrics(conn: Database, snapshot_id: str, metric_version: str, metrics: Dict[str, Any],
                         metric_status: Dict[str, str], available_at, source_digest: str) -> Tuple[int, bool]:
    """``(outcome_revision, created)``: a new revision only when the result changed."""
    with conn:
        _lock(conn, "outcome_metrics", snapshot_id, metric_version)
        where, params = "snapshot_id = %s AND metric_version = %s", (snapshot_id, metric_version)
        last = _latest(conn, "outcome_metrics", where, params)
        if (last is not None and last["outcome_source_digest"] == source_digest
                and _load(last["metrics"]) == json.loads(_json(metrics))
                and _load(last["metric_status"]) == metric_status):
            return int(last["outcome_revision"]), False
        rev = 1 if last is None else int(last["outcome_revision"]) + 1
        conn.execute(
            "INSERT INTO forecast.outcome_metrics (snapshot_id, metric_version, outcome_revision, metrics, "
            "metric_status, available_at, outcome_source_digest) VALUES (%s, %s, %s, %s, %s, %s, %s);",
            (snapshot_id, metric_version, rev, _json(metrics), _json(metric_status), available_at,
             source_digest),
        )
        return rev, True


def save_realised_outcome(conn: Database, snapshot_id: str, label_version: str, target_id: str,
                          actual_label: Optional[str], ineligibility_reason: Optional[str], available_at,
                          source_digest: str, metric_version: Optional[str] = None,
                          metric_outcome_revision: Optional[int] = None) -> Tuple[int, bool]:
    """``(outcome_revision, created)``: a new revision only when the label or its eligibility changed."""
    eligible = actual_label is not None
    with conn:
        _lock(conn, "realised_outcomes", snapshot_id, label_version, target_id)
        where = "snapshot_id = %s AND label_version = %s AND target_id = %s"
        last = _latest(conn, "realised_outcomes", where, (snapshot_id, label_version, target_id))
        if (last is not None and last["actual_label"] == actual_label
                and last["ineligibility_reason"] == ineligibility_reason
                and last["outcome_source_digest"] == source_digest):
            return int(last["outcome_revision"]), False
        rev = 1 if last is None else int(last["outcome_revision"]) + 1
        conn.execute(
            "INSERT INTO forecast.realised_outcomes (snapshot_id, label_version, target_id, outcome_revision, "
            "actual_label, eligible, ineligibility_reason, available_at, outcome_source_digest, "
            "metric_version, metric_outcome_revision) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s);",
            (snapshot_id, label_version, target_id, rev, actual_label, eligible,
             None if eligible else ineligibility_reason, available_at, source_digest, metric_version,
             metric_outcome_revision),
        )
        return rev, True


def training_outcomes(conn: Database, label_version: str, target_id: str, feature_version: str,
                      before_session: str, available_by, computed_by=None, limit: int = 250,
                      symbol: str = "NQ") -> List[Dict[str, Any]]:
    """
    Eligible realised labels of earlier sessions that were knowable by
    ``available_by`` (market time) and, when given, already computed by
    ``computed_by`` - one per session (its live capture if any), newest first.
    Each session contributes its latest qualifying outcome revision.
    """
    computed = "" if computed_by is None else " AND o.computed_at <= %(computed_by)s"
    rows = conn.execute(
        "SELECT session_date, actual_label, outcome_revision FROM ("
        "  SELECT DISTINCT ON (s.session_date) s.session_date, o.actual_label, o.outcome_revision "
        "    FROM forecast.realised_outcomes o "
        "    JOIN forecast.feature_snapshots s ON s.snapshot_id = o.snapshot_id "
        "    JOIN contracts c ON c.contract_id = s.instrument_id "
        "   WHERE o.label_version = %(lv)s AND o.target_id = %(t)s AND s.feature_version = %(fv)s "
        "     AND c.symbol = %(sym)s AND s.session_date < %(before)s "
        f"    AND o.available_at <= %(available_by)s{computed} "
        "   ORDER BY s.session_date, (s.data_mode = 'live_capture') DESC, s.created_at DESC, "
        "            o.outcome_revision DESC"
        ") x WHERE actual_label IS NOT NULL ORDER BY session_date DESC LIMIT %(limit)s;",
        {"lv": label_version, "t": target_id, "fv": feature_version, "sym": symbol,
         "before": before_session, "available_by": available_by, "computed_by": computed_by, "limit": limit},
    ).fetchall()
    return [dict(zip(r.keys(), r)) for r in rows]


def get_prediction_outcomes(conn: Database, outcome_revision: Optional[int] = None,
                            outcomes_as_of=None, model_version: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    Predictions joined to realised outcomes through snapshot_id, target_id and
    the run's label_version. The outcome revision is always chosen explicitly:
    ``outcome_revision=N`` takes exactly revision N; ``outcomes_as_of=ts`` takes,
    per outcome, the latest revision computed at or before ``ts``.
    """
    if (outcome_revision is None) == (outcomes_as_of is None):
        raise ValueError("Choose the outcome revision explicitly: pass outcome_revision or outcomes_as_of.")
    model = "" if model_version is None else " AND model_version = %(model)s"
    if outcome_revision is not None:
        sql = f"SELECT * FROM forecast.prediction_outcomes WHERE outcome_revision = %(rev)s{model}"
    else:
        sql = ("SELECT DISTINCT ON (forecast_run_id, target_id) * FROM forecast.prediction_outcomes "
               f"WHERE outcome_computed_at <= %(as_of)s{model} "
               "ORDER BY forecast_run_id, target_id, outcome_revision DESC")
    rows = conn.execute(f"SELECT * FROM ({sql}) x ORDER BY session_date, model_version, target_id;",
                        {"rev": outcome_revision, "as_of": outcomes_as_of, "model": model_version}).fetchall()
    out = []
    for r in rows:
        d = dict(zip(r.keys(), r))
        d["probabilities"] = _load(d["probabilities"])
        out.append(d)
    return out
