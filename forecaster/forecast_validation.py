# forecaster/forecast_validation.py
"""
Checks before a forecast is issued (guideline revision 2, 3B): pure functions that
raise, never repair.

  check_inputs(...)        referential and temporal: the annotation is of the
                           snapshot, the analogue set targets that annotation of that
                           snapshot under the annotation's protocol, label version and
                           matcher; the snapshot is the profile's; every analogue and
                           prior session is earlier; the candidate universe is the
                           label version's; a live run is a live capture
  validate_forecast(...)   schema and numerical: vocabularies, exact distributions in
                           [0, 1] summing to 1, the predicted class the most probable,
                           and every distribution equal to the analogue set's stored
                           summary (counts exactly, probabilities to its 6 decimals)
"""

from __future__ import annotations

from decimal import Decimal
from fractions import Fraction
from typing import Any, Dict, Optional

from contracts import nq_forecast as fc
from contracts import nq_preopen as pre
from contracts import nq_prompt_v2 as defs


class ForecastInputError(ValueError):
    """The evidence ids do not belong together; nothing is issued."""


class ForecastInvalid(ValueError):
    """A forecast that fails its own contract; recorded as an invalid run, never issued."""


def check_inputs(snapshot: Optional[Dict[str, Any]], annotation: Optional[Dict[str, Any]],
                 analogue_set: Optional[Dict[str, Any]], profile: str, mode: str) -> None:
    """Raises ForecastInputError when the explicit evidence does not fit together (see the module docstring)."""
    if snapshot is None:
        raise ForecastInputError("no such snapshot")
    if profile not in defs.PROFILES:
        raise ForecastInputError(f"unknown profile {profile!r}")
    if snapshot["snapshot_version"] != defs.PROFILES[profile].snapshot_version:
        raise ForecastInputError(f"snapshot {snapshot['snapshot_id']} is {snapshot['snapshot_version']}, not the "
                                 f"{profile} profile's {defs.PROFILES[profile].snapshot_version}")
    if mode not in fc.ISSUE_POLICIES:
        raise ForecastInputError(f"unknown mode {mode!r}")
    if mode == "live" and snapshot["data_mode"] != "live_capture":
        raise ForecastInputError(f"a live forecast needs a live_capture snapshot, not {snapshot['data_mode']}")
    ids = (snapshot["payload"].get("first_level_candidates") or {}).get("ids")
    if ids != list(defs.FIRST_LEVEL_CANDIDATES):
        raise ForecastInputError(f"the snapshot's frozen candidate universe {ids} is not {defs.LABEL_VERSION}'s")
    if annotation is None:
        raise ForecastInputError("no such annotation")
    if annotation["snapshot_id"] != snapshot["snapshot_id"]:
        raise ForecastInputError(f"annotation {annotation['annotation_id']} is of snapshot "
                                 f"{annotation['snapshot_id']}, not {snapshot['snapshot_id']}")
    if analogue_set is None:
        return
    if analogue_set["target_snapshot_id"] != snapshot["snapshot_id"]:
        raise ForecastInputError(f"analogue set {analogue_set['set_id']} targets snapshot "
                                 f"{analogue_set['target_snapshot_id']}, not {snapshot['snapshot_id']}")
    if analogue_set["target_annotation_id"] != annotation["annotation_id"]:
        raise ForecastInputError(f"analogue set {analogue_set['set_id']} targets annotation "
                                 f"{analogue_set['target_annotation_id']}, not {annotation['annotation_id']}")
    if analogue_set["protocol_version"] != annotation["protocol_version"]:
        raise ForecastInputError(f"analogue set protocol {analogue_set['protocol_version']} is not the "
                                 f"annotation's {annotation['protocol_version']}")
    if analogue_set["label_version"] != defs.LABEL_VERSION or analogue_set["matcher_version"] != pre.MATCHER_VERSION:
        raise ForecastInputError(f"analogue set {analogue_set['set_id']} is {analogue_set['matcher_version']} / "
                                 f"{analogue_set['label_version']}, not {pre.MATCHER_VERSION} / {defs.LABEL_VERSION}")
    day = str(snapshot["session_date"])
    later = [m["session_date"] for m in analogue_set["members"] if m["session_date"] >= day]
    if later:
        raise ForecastInputError(f"analogue(s) not earlier than {day}: {', '.join(later)}")
    prior = analogue_set["outcome_summary"].get("prior") or {}
    if prior.get("digest") != analogue_set.get("prior_digest"):
        raise ForecastInputError("the analogue set's prior manifest does not match its prior digest")
    if any(entry[1] >= day for entry in prior.get("manifest") or []):
        raise ForecastInputError(f"a prior session is not earlier than {day}")


def _close(stored: Optional[str], value: Fraction) -> bool:
    return stored is not None and abs(Decimal(stored) - Decimal(value.numerator) / Decimal(value.denominator)) \
        <= Decimal("0.0000005")


def validate_forecast(forecast: Dict[str, Any], analogue_set: Dict[str, Any],
                      algorithm: str = fc.BASELINE_VERSION) -> Dict[str, Any]:
    """Returns the forecast unchanged, or raises ForecastInvalid with the first problem. The baseline must equal the
    set's smoothed summary and denominators, the prior alone the set's prior."""
    summary = analogue_set["outcome_summary"]["targets"]
    prior_only = algorithm == fc.PRIOR_VERSION
    predictions = forecast["predictions"]
    if set(predictions) != {t for _, t in fc.FORECAST_TARGETS}:
        raise ForecastInvalid(f"targets {sorted(predictions)} are not the forecast schema's")
    for target, p in predictions.items():
        vocab = list(defs.TARGETS[target]["labels"])
        if p["status"] not in fc.PREDICTION_STATUSES or p["estimation_status"] not in fc.ESTIMATION_STATUSES:
            raise ForecastInvalid(f"{target}: status {p['status']} / {p['estimation_status']}")
        if (p["status"] == "predicted") != (p["predicted_label"] is not None):
            raise ForecastInvalid(f"{target}: status {p['status']} with label {p['predicted_label']!r}")
        if p["predicted_label"] is not None and p["predicted_label"] not in vocab:
            raise ForecastInvalid(f"{target}: {p['predicted_label']!r} is not in the vocabulary")
        s = summary[target]
        expected = ((0, 0, s["prior_sessions"]) if prior_only
                    else (s["eligible"], s["without_label"], s["prior_sessions"]))
        if (p["eligible"], p["without_label"], p["prior_sessions"]) != expected:
            raise ForecastInvalid(f"{target}: denominators differ from the analogue set's summary")
        if p["distribution"] is None:
            continue
        dist = {c: Fraction(v) for c, v in p["distribution"].items()}
        if list(dist) != vocab or sum(dist.values()) != 1 or any(not 0 <= v <= 1 for v in dist.values()):
            raise ForecastInvalid(f"{target}: the distribution is not a probability over the vocabulary")
        if p["status"] == "predicted" and dist[p["predicted_label"]] != max(dist.values()):
            raise ForecastInvalid(f"{target}: {p['predicted_label']} is not the most probable class")
        stored = s.get("prior") if prior_only else s.get("smoothed")
        if not all(_close((stored or {}).get(c), dist[c]) for c in vocab):
            raise ForecastInvalid(f"{target}: the distribution differs from the analogue set's "
                                  f"{'prior' if prior_only else 'smoothed summary'}")
    return forecast
