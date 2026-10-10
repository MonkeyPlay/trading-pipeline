# forecaster/ml_bundle_service.py
"""
Issues the seven-target bundles (contracts/nq_ml_bundle.py) as forecast runs, in shadow beside A, B
and the v1 models: stored and shown, never delivered.

  predict(conn, snapshot, version, as_of)
      the stored bundle, checked against its manifest and registered sha256 (never retrained here),
      the session's features as of ``as_of`` for the arm (N and P from NQ's data only, M with the
      context instruments - forecaster/ml_bundle_data.inference_row), then every head on its own:
      a target the session cannot have (an early close for a standard-session target, a missing
      threshold, an unavailable first-level candidate), a head stored unavailable, or a head that
      fails is unavailable with its own reason - never filled from another arm or head, and never
      blocking another head. Run-level reasons (in-sample, NQ's features missing, for M a required
      context instrument stale) make every head unavailable
  issue(conn, snapshot, profile, mode, version)
      the run, stored once per snapshot, profile, mode, bundle and artifact: issued when at least one
      head predicted, unavailable (with every head's reason) when none did, failed when the bundle
      could not run. The first level's price is the predicted candidate's frozen price from the
      snapshot. Every head's status, reason, family, parameters, calibration, smoothing and
      probabilities are in the run's evidence and outputs; label coverage (the training window's
      sessions with and without a label) is in each prediction's eligible / without_label

Run-level status against the persistence validators (migration 0014): issued has an issue time and
at least one predicted or ambiguous target; unavailable and failed carry a failure reason; every
unavailable target carries its reason; every distribution covers exactly its target's classes and
sums to 1.
"""

from __future__ import annotations

import logging
import time as _time
import uuid
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from contracts import nq_forecast as fc
from contracts import nq_ml_bundle as mb
from contracts import nq_prompt_v2 as defs
from database import journal_store as store
from features import calendar as cal
from forecaster import ml_bundle as mbun
from forecaster import ml_bundle_data as bd
from forecaster import ml_features as mf
from forecaster.forecast_service import _digest
from forecaster.provenance import code_revision

logger = logging.getLogger(__name__)


def registered_sha(conn, version: str) -> Optional[str]:
    row = store.get_version(conn, version)
    return None if row is None else ((row.get("definition") or {}).get("artifact") or {}).get("sha256")


def needs_context(version: str) -> bool:
    """Whether the bundle reads the context instruments (M) - only it waits for their cutoff bars."""
    return mb.ARM_OF.get(version) == "M"


def _head_entry(status: str, reason: Optional[str] = None, **kw) -> Dict[str, Any]:
    return {"status": status, "reason": reason, **kw}


def predict(conn, snapshot: Dict[str, Any], version: str, as_of: Optional[datetime] = None,
            root: Optional[str] = None) -> Dict[str, Any]:
    """``{'status': 'issued'|'unavailable', 'reason', 'heads': {target: ...}, 'features', 'provenance',
    'instruments', 'model', 'first_level_price', 'seconds'}`` - see the module docstring."""
    t0 = _time.perf_counter()
    art = mbun.load(version, root, registered_sha(conn, version))
    man, arm = art["manifest"], art["arm"]
    day = str(snapshot["session_date"])
    out: Dict[str, Any] = {"status": "unavailable", "reason": None, "heads": {}, "first_level_price": None,
                           "model": {"version": version, "arm": arm, "sha256": man["sha256"],
                                     "training": {k: man["training"].get(k) for k in ("from", "to", "sessions",
                                                                                      "rows")},
                                     "feature_version": man["feature_version"], "software": man["software"],
                                     "code_revision": man.get("code_revision")},
                           "as_of": None if as_of is None else as_of.strftime("%Y-%m-%dT%H:%M:%SZ")}
    if day <= man["training"]["to"]:
        out["reason"] = (f"in-sample: {day} is inside the bundle's training window ({man['training']['from']} to "
                         f"{man['training']['to']}) - no forecast of a session it was trained on")
    row = bd.inference_row(conn, snapshot, arm, as_of)
    fs = row["fs"]
    X = pd.DataFrame([{**row["features"], **row["candidates"]}])
    out.update(features={k: (None if v != v else float(v)) for k, v in row["features"].items()},
               provenance=fs.provenance[day], instruments=fs.instruments[day], observations=fs.observations.get(day))
    if out["reason"] is None:
        if all(X.iloc[0][c] != X.iloc[0][c] for c in mb.OWN):
            out["reason"] = "NQ's own features are all missing at the cutoff"
        elif arm == "M" and mf.required_missing(fs, day):
            miss = mf.required_missing(fs, day)
            out["reason"] = ("required instrument(s) not usable at the cutoff: " + ", ".join(
                f"{s} {fs.instruments[day][s]['status']}" for s in miss) + " - the multi-instrument bundle abstains")
    for t in mb.TARGETS:
        head = art["heads"].get(t)
        if out["reason"] is not None:
            out["heads"][t] = _head_entry("unavailable", out["reason"])
            continue
        why = row["eligible"][t]
        if why is not None:
            out["heads"][t] = _head_entry("unavailable", why)
            continue
        if head is None or head.status != "trained":
            out["heads"][t] = _head_entry("unavailable", f"head not trained: {getattr(head, 'reason', 'missing')}")
            continue
        try:
            p = head.predict(X)[0]
            dist = mbun.exact(p, mb.CLASSES[t])
            out["heads"][t] = _head_entry("predicted", None, probabilities=[float(x) for x in p], distribution=dist,
                                          family=head.family, params=head.params, calibration=head.calibration,
                                          n=head.n)
        except Exception as e:                                   # one head's failure never blocks another
            logger.exception("bundle %s head %s failed on %s", version, t, day)
            out["heads"][t] = _head_entry("unavailable", f"failed: {type(e).__name__}: {e}")
    predicted = [t for t, h in out["heads"].items() if h["status"] == "predicted"]
    if predicted:
        out["status"] = "issued"
        out["reason"] = None
    elif out["reason"] is None:
        out["reason"] = "no head could predict: " + "; ".join(f"{t}: {h['reason']}" for t, h in out["heads"].items())
    fl = out["heads"].get("first_level_tested") or {}
    if fl.get("distribution"):
        top = _top(fl["distribution"])
        if len(top) == 1:
            level = ((snapshot["payload"].get("first_level_candidates") or {}).get("levels") or {}).get(top[0]) or {}
            out["first_level_price"] = level.get("value")
    out["seconds"] = round(_time.perf_counter() - t0, 3)
    return out


def _top(dist: Dict[str, str]) -> List[str]:
    from fractions import Fraction
    vals = {c: Fraction(v) for c, v in dist.items()}
    best = max(vals.values())
    return [c for c, v in vals.items() if v == best]


def _predictions(result: Dict[str, Any], manifest: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """One prediction row per P1 target (contracts/nq_forecast.FORECAST_TARGETS) - none ever missing."""
    out = []
    heads = (manifest or {}).get("heads") or {}
    for _, target in fc.FORECAST_TARGETS:
        h = (result.get("heads") or {}).get(target) or {"status": "unavailable",
                                                        "reason": result.get("reason") or "not run"}
        cov = heads.get(target) or {}
        n = int(cov.get("n") or 0)
        sessions = int((cov.get("training") or {}).get("sessions") or 0)
        base = {"target": target, "eligible": n, "without_label": max(0, sessions - n) if sessions else 0,
                "prior_sessions": 0, "prior_without_label": 0}
        if h["status"] == "predicted":
            best = _top(h["distribution"])
            out.append({**base, "status": "predicted" if len(best) == 1 else "ambiguous_prediction",
                        "predicted_label": best[0] if len(best) == 1 else None, "estimation_status": "model",
                        "distribution": h["distribution"],
                        "reason": None if len(best) == 1 else "an exact tie at the top: " + ", ".join(best)})
        else:
            out.append({**base, "status": "unavailable", "predicted_label": None, "estimation_status": "none",
                        "distribution": None, "reason": h.get("reason") or "unavailable"})
    return out


def _key(snapshot: Dict[str, Any], profile: str, mode: str, version: str, sha: str) -> str:
    return _digest({"symbol": snapshot["symbol"], "session_date": str(snapshot["session_date"]), "profile": profile,
                    "snapshot_id": snapshot["snapshot_id"], "mode": mode, "algorithm": version, "artifact": sha,
                    "feature_version": mb.FEATURE_VERSION, "schema": mb.SCHEMA_VERSION,
                    "issue_policy": fc.ISSUE_POLICIES[mode]})


def issue(conn, snapshot: Dict[str, Any], profile: str, mode: str, version: str, root: Optional[str] = None,
          now: Optional[datetime] = None) -> Tuple[Optional[Dict[str, Any]], bool]:
    """``(run, created)`` - see the module docstring; ``(None, False)`` when the bundle has no artifact."""
    man = mbun.manifest(version, root)
    if man is None:
        return None, False
    if store.get_version(conn, version) is None:
        for rec in mbun.records(root):
            store.register_version(conn, rec)
    key = _key(snapshot, profile, mode, version, man["sha256"])
    stored = store.find_forecast_run(conn, key)
    if stored is not None:
        return store.get_forecast_run(conn, stored), False
    started = datetime.now(timezone.utc)
    as_of = now or started
    try:
        result = predict(conn, snapshot, version, as_of=as_of, root=root)
        status, reason = result["status"], result["reason"]
    except Exception as e:                                        # recorded, never issued; nothing filled in
        logger.exception("ML bundle %s %s failed", version, snapshot["session_date"])
        result = {"status": "failed", "reason": f"{type(e).__name__}: {e}", "heads": {},
                  "model": {"version": version, "sha256": man["sha256"], "training": man["training"].get("to")},
                  "as_of": as_of.strftime("%Y-%m-%dT%H:%M:%SZ")}
        status, reason = "failed", result["reason"]
    if status == "failed":
        key = f"{key}:attempt:{uuid.uuid4()}"                    # an attempt never blocks the official run
    used = {s: (st or {}).get("status") for s, st in (result.get("instruments") or {}).items()}
    heads_out = {t: {k: v for k, v in h.items() if k in ("status", "reason", "family", "params", "calibration", "n")}
                 for t, h in (result.get("heads") or {}).items()}
    evidence = {"symbol": snapshot["symbol"], "session_date": str(snapshot["session_date"]), "profile": profile,
                "mode": mode, "snapshot_id": snapshot["snapshot_id"], "snapshot_version": snapshot["snapshot_version"],
                "data_mode": snapshot["data_mode"], "input_cutoff_at": str(snapshot["cutoff_at"]),
                "as_of": result.get("as_of"), "features": result.get("features"),
                "provenance": result.get("provenance"), "instruments": result.get("instruments"),
                "observations": result.get("observations"), "model": result.get("model"),
                "heads": result.get("heads"), "first_level_price": result.get("first_level_price"),
                "versions": {"label": defs.LABEL_VERSION, "algorithm": version, "features": mb.FEATURE_VERSION,
                             "schema": mb.SCHEMA_VERSION, "issue_policy": fc.ISSUE_POLICIES[mode],
                             "market_labels": man.get("market_labels")}}
    day = str(snapshot["session_date"])
    run = {
        "idempotency_key": key, "symbol": snapshot["symbol"], "session_date": day,
        "contract_id": snapshot["contract_id"], "profile": profile, "snapshot_id": snapshot["snapshot_id"],
        "annotation_id": None, "analogue_set_id": None, "label_version": defs.LABEL_VERSION,
        "algorithm_version": version, "schema_version": mb.SCHEMA_VERSION, "issue_policy": fc.ISSUE_POLICIES[mode],
        "code_revision": code_revision(), "mode": mode, "input_cutoff_at": snapshot["cutoff_at"],
        "deadline_at": (cal.ny_instant(date.fromisoformat(day), fc.LIVE_DEADLINE_ET) if mode == "live" else None),
        "generation_started_at": started, "generation_completed_at": datetime.now(timezone.utc),
        "lifecycle_status": status, "supersedes_run_id": None, "failure_reason": reason,
        "evidence_digest": _digest(evidence),
        "outputs": {"instruments": {s: ("used" if v in mf.USED else v) for s, v in used.items() if s != "NQ"},
                    "model": {"arm": mb.ARM_OF.get(version), "sha256": man["sha256"]},
                    "heads": heads_out, "first_level_price": result.get("first_level_price"),
                    "feature_version": mb.FEATURE_VERSION, "status": "shadow", "seconds": result.get("seconds"),
                    "forecast_confidence": None,
                    "forecast_confidence_reason": "no evidence-quality convention is registered (P1 field 36)"},
    }
    run_id, created = store.save_forecast_run(conn, run, evidence, _predictions(result, man))
    out = store.get_forecast_run(conn, run_id)
    if created and mode == "live" and out["lifecycle_status"] == "issued":
        store.add_forecast_event(conn, run_id, "acknowledged", "committed and read back by the issuing process")
        out = store.get_forecast_run(conn, run_id)
    return out, created
