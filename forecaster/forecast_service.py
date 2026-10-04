# forecaster/forecast_service.py
"""
Forecast issuance on the journal (guideline revision 2, stage 3A-3C): the
deterministic P1 baseline (contracts/nq_forecast.py, forecaster/forecast_baseline.py)
from explicit evidence ids, frozen, validated and stored as a run.

    snapshot = load_snapshot(conn, snapshot_id)
    annotation = load_annotation(conn, annotation_id)
    analogue_set = load_analogue_set(conn, set_id)
    evidence = freeze_forecast_evidence(conn, snapshot, annotation, analogue_set, profile, mode)
    forecast = baseline_forecast(evidence)
    validated = validate_forecast(forecast, analogue_set)
    run_id = issue_forecast(conn, validated, evidence, mode)

``run_forecast`` does all of it and records one run for every attempt: issued,
unavailable (a contaminated annotation, no analogue set), failed (an exception) or
invalid (a forecast that fails its own contract); a live run inserted after its
deadline is made late by the database. Mismatched or malformed evidence ids are
rejected before anything is stored. The idempotency key covers the session, profile,
mode, evidence ids and versions, so running again returns the stored run; other
evidence for the session is a new run that supersedes the previous one. A failed
or invalid attempt is kept under its own key, so it never blocks a retry.

Only the outcome attachment reads historical outcomes - the analogues' frozen
revisions and the prior manifest of the analogue set, all of earlier sessions. The
label service stays independent of this one.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from contracts import nq_forecast as fc
from contracts import nq_preopen as pre
from contracts import nq_prompt_v2 as defs
from database import journal_store as store
from features import calendar as cal
from forecaster.forecast_baseline import baseline_forecast
from forecaster.forecast_validation import ForecastInputError, ForecastInvalid, check_inputs, validate_forecast
from forecaster.provenance import code_revision

logger = logging.getLogger(__name__)


def _digest(value: Any) -> str:
    return hashlib.sha256(defs.canonical_json(value).encode()).hexdigest()


def load_snapshot(conn, snapshot_id: str) -> Dict[str, Any]:
    snapshot = store.get_snapshot(conn, snapshot_id)
    if snapshot is None:
        raise ForecastInputError(f"no snapshot {snapshot_id!r}")
    return snapshot


def load_annotation(conn, annotation_id: str) -> Dict[str, Any]:
    annotation = store.get_annotation(conn, annotation_id)
    if annotation is None:
        raise ForecastInputError(f"no annotation {annotation_id!r}")
    return annotation


def load_analogue_set(conn, set_id: Optional[str]) -> Optional[Dict[str, Any]]:
    if set_id is None:
        return None
    aset = store.get_analogue_set(conn, set_id)
    if aset is None:
        raise ForecastInputError(f"no analogue set {set_id!r}")
    return aset


def _labels_at(history: Dict[str, List[Dict[str, Any]]], snapshot_id: str, revision: Optional[int]):
    for o in history.get(snapshot_id) or []:
        if int(o["outcome_revision"]) == revision:
            return o["labels"]
    return None


def freeze_forecast_evidence(conn, snapshot: Dict[str, Any], annotation: Dict[str, Any],
                             analogue_set: Optional[Dict[str, Any]], profile: str, mode: str,
                             history: Optional[Dict[str, List[Dict[str, Any]]]] = None) -> Dict[str, Any]:
    """
    The run's frozen evidence: the ids and hashes of the snapshot, annotation and analogue set, the analogues' labels
    at their frozen outcome revisions, the prior manifest's digest and per-target class counts, the thresholds,
    cutoff price and frozen candidate levels. Raises ForecastInputError when the ids do not fit together.
    """
    check_inputs(snapshot, annotation, analogue_set, profile, mode)
    p = snapshot["payload"]
    cp = (p.get("references") or {}).get("cutoff_price") or {}
    evidence: Dict[str, Any] = {
        "symbol": snapshot["symbol"], "session_date": str(snapshot["session_date"]), "profile": profile,
        "mode": mode, "contract_id": snapshot["contract_id"], "local_symbol": p["identity"].get("local_symbol"),
        "snapshot_id": snapshot["snapshot_id"], "snapshot_version": snapshot["snapshot_version"],
        "source_payload_hash": snapshot["source_payload_hash"], "data_mode": snapshot["data_mode"],
        "pit_availability_status": snapshot["pit_availability_status"],
        "input_cutoff_at": p["cutoff"]["input_cutoff_at"], "schedule": p["schedule"]["schedule"],
        "annotation_id": annotation["annotation_id"], "protocol_version": annotation["protocol_version"],
        "annotation_output_hash": annotation["output_hash"], "integrity_status": annotation["integrity_status"],
        "thresholds": p.get("thresholds"),
        "cutoff_price": ({"value": str(cp["value"]), "exact": cp.get("exact"), "bar_start_at": cp.get("bar_start_at")}
                         if cp.get("status") == "valid" else None),
        "candidates": (p.get("first_level_candidates") or {}).get("levels"),
        "versions": {"label": defs.LABEL_VERSION, "convention": defs.CONVENTION_VERSION,
                     "matcher": pre.MATCHER_VERSION, "algorithm": fc.BASELINE_VERSION,
                     "schema": fc.FORECAST_SCHEMA_VERSION, "issue_policy": fc.ISSUE_POLICIES[mode]},
        "analogue_set_id": None,
    }
    if analogue_set is None:
        return evidence
    history = history if history is not None else store.outcome_history(conn, analogue_set["label_version"])
    targets = [t for _, t in fc.FORECAST_TARGETS]
    members = []
    for m in analogue_set["members"]:
        labels = _labels_at(history, m["snapshot_id"], m["outcome_revision"]) if m["outcome_revision"] else None
        if m["outcome_revision"] and labels is None:
            raise ForecastInputError(f"analogue {m['session_date']}: outcome revision {m['outcome_revision']} "
                                     f"is not stored")
        members.append({"rank": m["rank"], "snapshot_id": m["snapshot_id"], "session_date": m["session_date"],
                        "similarity": str(m["similarity"]), "comparable_weight": str(m["comparable_weight"]),
                        "outcome_revision": m["outcome_revision"],
                        "labels": {t: (labels or {}).get(t, {}).get("label") for t in targets}})
    prior = analogue_set["outcome_summary"]["prior"]
    counts = {t: {c: 0 for c in defs.TARGETS[t]["labels"]} for t in targets}
    without = {t: 0 for t in targets}
    for snapshot_id, _, revision in prior["manifest"]:
        labels = _labels_at(history, snapshot_id, revision)
        if labels is None:
            raise ForecastInputError(f"prior session {snapshot_id}: outcome revision {revision} is not stored")
        for t in targets:
            label = labels.get(t, {}).get("label")
            if label is None:
                without[t] += 1
            else:
                counts[t][label] += 1
    evidence.update(
        analogue_set_id=analogue_set["set_id"], pool_size=analogue_set["pool_size"],
        pool_hash=analogue_set["pool_hash"], outcome_digest=analogue_set["outcome_digest"],
        mean_similarity=None if analogue_set["mean_similarity"] is None else str(analogue_set["mean_similarity"]),
        members=members,
        prior={"digest": prior["digest"], "sessions": prior["sessions"], "known_as_of": prior["known_as_of"],
               "excluded": prior["excluded"], "counts": counts, "without_label": without})
    return evidence


def _canonical_id(value: Optional[str]) -> Optional[str]:
    """A UUID in its canonical spelling (so two spellings of one id are one key); anything else as given."""
    if value is None:
        return None
    try:
        return str(uuid.UUID(str(value)))
    except ValueError:
        return str(value)


def idempotency_key(snapshot: Dict[str, Any], annotation_id: str, set_id: Optional[str], profile: str,
                    mode: str) -> str:
    """The official key of a run: session, profile, mode, evidence ids and versions - all known before any evidence
    is loaded, so a repeated invocation returns the stored run at once."""
    return _digest({"symbol": snapshot["symbol"], "session_date": str(snapshot["session_date"]), "profile": profile,
                    "snapshot_id": snapshot["snapshot_id"], "annotation_id": annotation_id, "analogue_set_id": set_id,
                    "mode": mode, "versions": {"label": defs.LABEL_VERSION, "convention": defs.CONVENTION_VERSION,
                                               "matcher": pre.MATCHER_VERSION, "algorithm": fc.BASELINE_VERSION,
                                               "schema": fc.FORECAST_SCHEMA_VERSION,
                                               "issue_policy": fc.ISSUE_POLICIES.get(mode)}})


def run_forecast(conn, snapshot_id: str, annotation_id: str, set_id: Optional[str],
                 profile: str = defs.DEFAULT_PROFILE, mode: str = "historical_replay",
                 history: Optional[Dict[str, List[Dict[str, Any]]]] = None) -> Tuple[Dict[str, Any], bool]:
    """
    Issues the baseline forecast of explicit evidence ids and returns ``(run, created)``; an attempt that cannot
    issue is stored with its status and reason. Raises ForecastInputError for ids that are malformed, unknown or
    do not belong together - nothing is stored then.
    """
    started = datetime.now(timezone.utc)
    snapshot = load_snapshot(conn, snapshot_id)
    key = idempotency_key(snapshot, _canonical_id(annotation_id), _canonical_id(set_id), profile, mode)
    stored = store.find_forecast_run(conn, key)
    if stored is not None:                       # its ids were checked when it was stored (and by the database)
        return store.get_forecast_run(conn, stored), False
    annotation = load_annotation(conn, annotation_id)
    aset = load_analogue_set(conn, set_id)
    evidence = freeze_forecast_evidence(conn, snapshot, annotation, aset, profile, mode, history)

    predictions: List[Dict[str, Any]] = []
    outputs: Dict[str, Any] = {}
    status, reason = "issued", None
    if annotation["integrity_status"] != "ok":
        status, reason = "unavailable", "the annotation is contaminated: no forecast (P1 stop rule)"
    elif aset is None:
        status, reason = "unavailable", f"no analogue set for annotation {annotation['annotation_id']}"
    else:
        try:
            forecast = validate_forecast(baseline_forecast(evidence), aset)
            outputs = forecast["outputs"]
            predictions = [{"target": t, **forecast["predictions"][t]} for _, t in fc.FORECAST_TARGETS]
        except ForecastInvalid as e:
            status, reason = "invalid", f"the forecast fails its contract: {e}"
        except Exception as e:  # recorded, never issued; nothing is filled in
            logger.exception("forecast %s failed", evidence["session_date"])
            status, reason = "failed", f"{type(e).__name__}: {e}"
    if status in ("failed", "invalid"):
        key = f"{key}:attempt:{uuid.uuid4()}"            # an attempt never blocks the official run
    previous = (store.current_forecast_run(conn, evidence["session_date"], profile, mode, fc.BASELINE_VERSION, key)
                if status in ("issued", "unavailable") else None)
    day = snapshot["session_date"]
    run = {
        "idempotency_key": key, "symbol": snapshot["symbol"], "session_date": str(day),
        "contract_id": snapshot["contract_id"], "profile": profile, "snapshot_id": snapshot["snapshot_id"],
        "annotation_id": annotation["annotation_id"], "analogue_set_id": aset["set_id"] if aset else None,
        "label_version": defs.LABEL_VERSION, "algorithm_version": fc.BASELINE_VERSION,
        "schema_version": fc.FORECAST_SCHEMA_VERSION, "issue_policy": fc.ISSUE_POLICIES[mode],
        "code_revision": code_revision(), "mode": mode, "input_cutoff_at": snapshot["cutoff_at"],
        "deadline_at": (cal.ny_instant(datetime.fromisoformat(str(day)).date(), fc.LIVE_DEADLINE_ET)
                        if mode == "live" else None),
        "generation_started_at": started, "generation_completed_at": datetime.now(timezone.utc),
        "lifecycle_status": status, "supersedes_run_id": previous["run_id"] if previous else None,
        "failure_reason": reason, "evidence_digest": _digest(evidence), "outputs": outputs,
    }
    run_id, created = store.save_forecast_run(conn, run, evidence, predictions)
    result = store.get_forecast_run(conn, run_id)
    if created and mode == "live" and result["lifecycle_status"] == "issued":
        store.add_forecast_event(conn, run_id, "acknowledged", "committed and read back by the issuing process")
        result = store.get_forecast_run(conn, run_id)
    return result, created


def utc(value) -> Optional[datetime]:
    """A stored timestamp (the journal reads TIMESTAMPTZ as UTC text) as an aware datetime."""
    if value is None or isinstance(value, datetime):
        return value
    t = datetime.fromisoformat(str(value).replace(" ", "T"))
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def timely(run: Dict[str, Any]) -> bool:
    """A timely live forecast: issued and acknowledged by its deadline, all by the database clock."""
    acks = [utc(e["at"]) for e in run.get("events") or [] if e["event"] == "acknowledged"]
    deadline = utc(run["deadline_at"])
    return (run["mode"] == "live" and run["lifecycle_status"] == "issued" and bool(acks)
            and utc(run["issued_at"]) <= deadline and min(acks) <= deadline)


def forecast_session(conn, day: str, profile: str = defs.DEFAULT_PROFILE,
                     protocol: str = pre.RULES_PROTOCOL_VERSION, mode: str = "historical_replay",
                     history: Optional[Dict[str, List[Dict[str, Any]]]] = None) -> Optional[Tuple[Dict[str, Any], bool]]:
    """
    Resolves one session's evidence ids explicitly - its profile snapshot, that snapshot's latest annotation under
    ``protocol``, and the newest analogue set of exactly that annotation - and runs the forecast on them. None when
    the session has no snapshot or annotation yet.
    """
    snaps = store.list_snapshots(conn, day, day, defs.PROFILES[profile].snapshot_version)
    if not snaps:
        return None
    annotation = store.latest_annotation(conn, snaps[0]["snapshot_id"], protocol)
    if annotation is None:
        return None
    aset = store.latest_analogue_set(conn, snaps[0]["snapshot_id"], pre.MATCHER_VERSION, defs.LABEL_VERSION, protocol)
    if aset is not None and aset["target_annotation_id"] != annotation["annotation_id"]:
        aset = None                                    # the newest set is of another annotation: none for this one
    return run_forecast(conn, snaps[0]["snapshot_id"], annotation["annotation_id"],
                        aset["set_id"] if aset else None, profile, mode, history)


def forecast_all(conn, profile: str = defs.DEFAULT_PROFILE, protocol: str = pre.RULES_PROTOCOL_VERSION) -> int:
    """Historical-replay forecasts of every annotated snapshot of the profile; returns how many runs are new."""
    history = store.outcome_history(conn, defs.LABEL_VERSION)
    created = 0
    for snap in store.list_snapshots(conn, "2000-01-01", "2100-01-01", defs.PROFILES[profile].snapshot_version):
        result = forecast_session(conn, str(snap["session_date"]), profile, protocol, history=history)
        if result is not None:
            run, new = result
            created += new
            if new and run["lifecycle_status"] not in ("issued", "unavailable"):
                logger.warning(f"Forecast {snap['session_date']}: {run['lifecycle_status']} - {run['failure_reason']}")
    logger.info(f"Forecasts ({fc.BASELINE_VERSION}, historical replay, {protocol}): {created} new run(s).")
    return created
