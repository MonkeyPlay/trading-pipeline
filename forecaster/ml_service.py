# forecaster/ml_service.py
"""
Issues the ML forecasts of direction_15m (contracts/nq_ml.py) as forecast runs, beside
arms A and B, and records the forecast in force.

  predict(conn, snapshot, version, as_of)
      the session's features as of its cutoff - the context instruments' bars received
      by ``as_of`` (forecaster/ml_features.py) - the stored artifact loaded and checked
      against its manifest and its registered sha256 (never retrained here), and the
      class probabilities; or why there is none: a session inside the model's training
      window (in-sample), no frozen threshold T (the label itself would be missing), a
      required instrument stale or missing (the multi-instrument model abstains), NQ's
      own features missing
  issue(conn, snapshot, profile, mode, version)
      the run, stored once per snapshot, profile, mode, model and artifact (a repeat
      returns the stored run - its features, frozen at the first issue, never change):
      issued, unavailable with the reason, or failed; live runs follow the live issue
      policy (the database decides late) and are acknowledged after their commit
  issue_pending(conn, profile, now)
      Auto's journal step: every snapshot after the models' training window still
      without its ML runs, once the context instruments' cutoff bars are in (or
      MAX_WAIT_MINUTES after the cutoff); and the session's forecast in force, recorded
      once - when the delivery order's ML models (none while experimental) have their
      runs, and only by the replay deadline (contracts/nq_ml.replay_deadline): a session
      caught up later gets ML runs that are reconstructions and no delivery, so what was
      in force that morning is never decided after the fact
  record_delivery(conn, day, profile, mode)
      the first usable run in contracts/nq_ml.delivery_order(), or none, with the reason
      (journal.forecast_deliveries, migration 0030)
"""

from __future__ import annotations

import logging
import math
import time as _time
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from contracts import nq_forecast as fc
from contracts import nq_ml as ml
from contracts import nq_prompt_v2 as defs
from database import journal_store as store
from features import calendar as cal
from forecaster import ml_features as mf
from forecaster import ml_model as mm
from forecaster.forecast_service import _digest
from forecaster.provenance import code_revision

logger = logging.getLogger(__name__)
COVERED = (ml.TARGET,)


def _utc(v) -> Optional[datetime]:
    return mf._utc(v)


def registered_sha(conn, version: str) -> Optional[str]:
    row = store.get_version(conn, version)
    return None if row is None else ((row.get("definition") or {}).get("artifact") or {}).get("sha256")


def features(conn, snapshot: Dict[str, Any], as_of: Optional[datetime] = None) -> mf.FeatureSet:
    """The session's features as of its cutoff and ``as_of`` - built once, read by every model."""
    day = str(snapshot["session_date"])
    return mf.build(conn, [day], {day: snapshot}, as_of=as_of, profile=ml.PROFILE)


def predict(conn, snapshot: Dict[str, Any], version: str, as_of: Optional[datetime] = None,
            root: Optional[str] = None, fs: Optional[mf.FeatureSet] = None) -> Dict[str, Any]:
    """``{'status': 'issued'|'unavailable', 'reason', 'distribution' (exact), 'probabilities', 'features',
    'provenance', 'instruments', 'model', 'seconds'}`` - see the module docstring. ``fs``: the session's features,
    when another model already built them."""
    t0 = _time.perf_counter()
    art = mm.load(version, root, registered_sha(conn, version))
    man = art["manifest"]
    day = str(snapshot["session_date"])
    cfg = ml.CONFIG_OF[version]
    out: Dict[str, Any] = {"status": "unavailable", "reason": None, "distribution": None, "probabilities": None,
                           "model": {k: man[k] for k in ("version", "family", "params", "sha256", "training",
                                                         "feature_version", "software")},
                           "as_of": None if as_of is None else as_of.strftime("%Y-%m-%dT%H:%M:%SZ")}
    if day <= man["training"]["to"]:
        out["reason"] = (f"in-sample: {day} is inside the model's training window ({man['training']['from']} to "
                         f"{man['training']['to']}) - no forecast of a session it was trained on")
    elif (snapshot["payload"].get("thresholds") or {}).get("T") is None:
        out["reason"] = "no frozen threshold T at the cutoff: the realised label would be missing_threshold"
    fs = fs if fs is not None else features(conn, snapshot, as_of)
    row = mf.matrix(fs, [day], cfg)
    out.update(features={k: (None if v != v else float(v)) for k, v in row.iloc[0].items()},
               provenance=fs.provenance[day], instruments=fs.instruments[day],
               observations=fs.observations.get(day))
    if out["reason"] is None:
        nq_own = [f"nq_{f.name}" for f in ml.OWN_FEATURES]
        if all(fs.rows[day][f] != fs.rows[day][f] for f in nq_own):
            out["reason"] = "NQ's own features are all missing at the cutoff"
        elif cfg == "multi" and mf.required_missing(fs, day):
            miss = mf.required_missing(fs, day)
            out["reason"] = ("required instrument(s) not usable at the cutoff: " + ", ".join(
                f"{s} {fs.instruments[day][s]['status']}" for s in miss) + " - the multi-instrument model abstains")
    if out["reason"] is None:
        p = mm.probabilities(art["model"], row.to_numpy())[0]
        out.update(status="issued", probabilities=[float(x) for x in p], distribution=mm.exact(p))
    out["seconds"] = round(_time.perf_counter() - t0, 3)
    return out


def _key(snapshot: Dict[str, Any], profile: str, mode: str, version: str, sha: str) -> str:
    return _digest({"symbol": snapshot["symbol"], "session_date": str(snapshot["session_date"]), "profile": profile,
                    "snapshot_id": snapshot["snapshot_id"], "mode": mode, "algorithm": version, "artifact": sha,
                    "feature_version": ml.FEATURE_VERSION, "schema": ml.SCHEMA_VERSION,
                    "issue_policy": fc.ISSUE_POLICIES[mode]})


def _predictions(result: Dict[str, Any]) -> List[Dict[str, Any]]:
    out = []
    n = (result["model"]["training"] or {}).get("sessions", 0)
    for _, target in fc.FORECAST_TARGETS:
        if target in COVERED and result["status"] == "issued":
            dist = result["distribution"]
            top = max(mm.fraction(v) for v in dist.values())
            best = [c for c, v in dist.items() if mm.fraction(v) == top]
            out.append({"target": target, "status": "predicted" if len(best) == 1 else "ambiguous_prediction",
                        "predicted_label": best[0] if len(best) == 1 else None, "estimation_status": "model",
                        "distribution": dist, "eligible": n, "without_label": 0, "prior_sessions": 0,
                        "prior_without_label": 0, "reason": None if len(best) == 1 else "an exact tie at the top"})
        else:
            out.append({"target": target, "status": "unavailable", "predicted_label": None,
                        "estimation_status": "none", "distribution": None, "eligible": 0, "without_label": 0,
                        "prior_sessions": 0, "prior_without_label": 0,
                        "reason": (result["reason"] if target in COVERED else
                                   "not a target of this model (direction_15m only): B's forecast stands for it")})
    return out


def issue(conn, snapshot: Dict[str, Any], profile: str, mode: str, version: str, root: Optional[str] = None,
          now: Optional[datetime] = None, fs: Optional[mf.FeatureSet] = None
          ) -> Tuple[Optional[Dict[str, Any]], bool]:
    """``(run, created)`` - see the module docstring; ``(None, False)`` when the model has no artifact (nothing to
    issue under)."""
    man = mm.manifest(version, root)
    if man is None:
        return None, False
    if store.get_version(conn, version) is None:            # the model's definitions, from its manifest
        for rec in mm.records(root):
            store.register_version(conn, rec)
    key = _key(snapshot, profile, mode, version, man["sha256"])
    stored = store.find_forecast_run(conn, key)
    if stored is not None:
        return store.get_forecast_run(conn, stored), False
    started = datetime.now(timezone.utc)
    as_of = now or started
    try:
        result = predict(conn, snapshot, version, as_of=as_of, root=root, fs=fs)
        status, reason = result["status"], result["reason"]
    except Exception as e:                                         # recorded, never issued; nothing filled in
        logger.exception("ML forecast %s %s failed", version, snapshot["session_date"])
        result = {"status": "failed", "reason": f"{type(e).__name__}: {e}", "model": {"version": version,
                  "sha256": man["sha256"], "training": man["training"]}, "features": None, "provenance": None,
                  "instruments": None, "as_of": as_of.strftime("%Y-%m-%dT%H:%M:%SZ")}
        status, reason = "failed", result["reason"]
    if status == "failed":
        key = f"{key}:attempt:{uuid.uuid4()}"                     # an attempt never blocks the official run
    used = {s: (st or {}).get("status") for s, st in (result.get("instruments") or {}).items()}
    evidence = {"symbol": snapshot["symbol"], "session_date": str(snapshot["session_date"]), "profile": profile,
                "mode": mode, "snapshot_id": snapshot["snapshot_id"], "snapshot_version": snapshot["snapshot_version"],
                "data_mode": snapshot["data_mode"], "input_cutoff_at": str(snapshot["cutoff_at"]),
                "as_of": result.get("as_of"), "features": result.get("features"),
                "provenance": result.get("provenance"), "instruments": result.get("instruments"),
                "observations": result.get("observations"),
                "model": result.get("model"), "probabilities": result.get("probabilities"),
                "versions": {"label": defs.LABEL_VERSION, "algorithm": version, "features": ml.FEATURE_VERSION,
                             "schema": ml.SCHEMA_VERSION, "issue_policy": fc.ISSUE_POLICIES[mode]}}
    day = str(snapshot["session_date"])
    run = {
        "idempotency_key": key, "symbol": snapshot["symbol"], "session_date": day,
        "contract_id": snapshot["contract_id"], "profile": profile, "snapshot_id": snapshot["snapshot_id"],
        "annotation_id": None, "analogue_set_id": None, "label_version": defs.LABEL_VERSION,
        "algorithm_version": version, "schema_version": ml.SCHEMA_VERSION, "issue_policy": fc.ISSUE_POLICIES[mode],
        "code_revision": code_revision(), "mode": mode, "input_cutoff_at": snapshot["cutoff_at"],
        "deadline_at": (cal.ny_instant(date.fromisoformat(day), fc.LIVE_DEADLINE_ET) if mode == "live" else None),
        "generation_started_at": started, "generation_completed_at": datetime.now(timezone.utc),
        "lifecycle_status": status, "supersedes_run_id": None, "failure_reason": reason,
        "evidence_digest": _digest(evidence),
        "outputs": {"instruments": {s: ("used" if v in mf.USED else v) for s, v in used.items() if s != "NQ"},
                    "model": {"family": (result.get("model") or {}).get("family"),
                              "sha256": (result.get("model") or {}).get("sha256")},
                    "feature_version": ml.FEATURE_VERSION, "status": ml.STATUS[version],
                    "seconds": result.get("seconds")},
    }
    run_id, created = store.save_forecast_run(conn, run, evidence, _predictions(result))
    out = store.get_forecast_run(conn, run_id)
    if created and mode == "live" and out["lifecycle_status"] == "issued":
        store.add_forecast_event(conn, run_id, "acknowledged", "committed and read back by the issuing process")
        out = store.get_forecast_run(conn, run_id)
    return out, created


def context_ready(conn, snapshot: Dict[str, Any], now: datetime) -> bool:
    """Whether every context instrument's bar just before the cutoff has been received - or it is too late to wait
    (contracts/nq_ml.MAX_WAIT_MINUTES after the cutoff)."""
    cutoff = _utc(snapshot["cutoff_at"])
    if now >= cutoff + timedelta(minutes=ml.MAX_WAIT_MINUTES):
        return True
    day = str(snapshot["session_date"])
    for symbol, inst in ml.INSTRUMENTS.items():
        if mf.closed_at(symbol, cutoff - mf.MINUTE):
            continue
        row = conn.execute(
            "SELECT max(b.timestamp_utc) FROM bars b JOIN active_contracts a ON a.contract_id = b.contract_id AND "
            "a.trading_day = b.trading_day WHERE a.symbol = %s AND b.trading_day = %s AND b.interval = '1m' AND "
            "b.price_type = 'TRADES' AND b.timestamp_utc <= %s AND b.timestamp_utc > %s;",
            (symbol, day, cutoff - mf.MINUTE, cutoff - timedelta(minutes=inst.max_age_minutes))).fetchone()
        if row is None or row[0] is None:
            return False
    return True


def record_delivery(conn, day: str, profile: str, mode: str) -> Dict[str, Any]:
    """Records the forecast in force (forecaster/delivery.py) for the session, profile and mode; returns it."""
    from forecaster.delivery import delivered
    runs = [store.get_forecast_run(conn, r["run_id"]) for r in store.list_forecast_runs(conn, day, day, profile, mode)]
    order = ml.delivery_order()
    run, why = delivered(runs, order, lambda v: ml.ARM_LABELS.get(v, v))
    store.save_delivery(conn, day, profile, mode, None if run is None else run["run_id"],
                        None if run is None else run["algorithm_version"], order, why)
    return {"run_id": None if run is None else run["run_id"],
            "algorithm": None if run is None else run["algorithm_version"], "reason": why}


def issue_pending(conn, profile: str = ml.PROFILE, now: Optional[datetime] = None,
                  root: Optional[str] = None) -> Dict[str, int]:
    """Auto's step (see the module docstring); returns how many runs (and reconstructions among them) and deliveries
    were new, and how many sessions wait for the context instruments."""
    now = now or datetime.now(timezone.utc)
    counts = {"runs": 0, "reconstructions": 0, "deliveries": 0, "waiting": 0}
    if profile != ml.PROFILE:
        return counts
    mans = {v: mm.manifest(v, root) for v in ml.ALGORITHMS}
    mans = {v: m for v, m in mans.items() if m is not None}
    if not mans:
        return counts
    start = min(m["training"]["to"] for m in mans.values())
    version = defs.PROFILES[profile].snapshot_version
    in_order = [v for v in ml.delivery_order() if v in ml.ALGORITHMS]      # promoted models: none while experimental
    today = now.date().isoformat()
    # which sessions have which runs and a delivery, in one query each; a snapshot's payload is read only to issue
    runs: Dict[str, set] = {}
    for r in store.list_forecast_runs(conn, start, today, profile, "historical_replay"):
        runs.setdefault(str(r["session_date"]), set()).add(r["algorithm_version"])
    delivered = store.delivery_days(conn, start, today, profile, "historical_replay")
    for ref in store.list_snapshots(conn, start, today, version, payload=False):
        day = str(ref["session_date"])
        if day <= start or ref["data_mode"] == "live_capture":
            continue
        deadline = ml.replay_deadline(_utc(ref["cutoff_at"]))
        have = set(runs.get(day, ()))
        todo = [v for v in mans if v not in have]
        if todo and context_ready(conn, ref, now):
            snap = store.get_snapshot(conn, ref["snapshot_id"])
            fs = features(conn, snap, now)
            for v in todo:
                _, created = issue(conn, snap, profile, "historical_replay", v, root, now, fs)
                counts["runs"] += int(created)
                counts["reconstructions"] += int(created and now > deadline)
            have |= set(todo)
        elif todo:
            counts["waiting"] += 1
        if now <= deadline and all(v in have for v in in_order) and day not in delivered:
            record_delivery(conn, day, profile, "historical_replay")
            counts["deliveries"] += 1
    if any(counts.values()):
        logger.info(f"ML forecasts ({', '.join(mans)}): {counts['runs']} new run(s)"
                    + (f" ({counts['reconstructions']} reconstruction(s): issued after the replay deadline, never in "
                       "force)" if counts["reconstructions"] else "")
                    + f", {counts['deliveries']} deliver(ies) recorded, {counts['waiting']} session(s) waiting for "
                      "the context instruments.")
    return counts
