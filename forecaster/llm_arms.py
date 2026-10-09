# forecaster/llm_arms.py
"""
Stage 4B arms C and D (guideline revision 2): the two forecasts that need Claude.
Both are started by hand only (structure_llm.manual_requests): from a terminal after
typing "send", or from the dashboard after its confirmation (forecaster/approvals.py).

  arm C  restricted LLM: Claude classifies only Overnight Structure and Premarket
         Pattern (nq_structure_restricted_v1), the rules the rest; that annotation is
         matched among the earlier sessions annotated the same way and smoothed like
         arm B (nq_restricted_p1_v1). Without an annotated pool it has no analogues
         and falls back to the prior - plan() says how much of the pool is annotated.
  arm D  synthesis (Appendix A, A2): Claude forecasts every target itself from arm
         B's frozen evidence - snapshot, rule-based annotation, analogue set with the
         analogues' outcomes, the prior and the smoothed baseline - stored as a run of
         nq_synthesis_p1_v1 after local validation (validate_synthesis).

Both requests are date-blinded (blinded_bundle): no session date, contract or absolute
price - times on the New York clock, prices relative to the previous RTH close - so a
historical replay cannot draw on a remembered outcome. Evidence ids in an answer are
mapped back to the snapshot's own ids before anything is stored.

  plan(conn, sessions, arms)   the last ``sessions`` scheduled sessions up to today - or
                               the chosen ``days`` - what each needs, how many requests,
                               a rough cost
  run(conn, client, ...)       sends what the plan needs (at most ``max_requests``),
                               then matches and issues arm C, and synthesises arm D -
                               each request recorded in the inference ledger first,
                               and the same evidence never sent twice once answered
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from contracts import nq_forecast as fc
from contracts import nq_preopen as pre
from contracts import nq_prompt_v2 as defs
from contracts.nq_prompt_v2 import canonical_json
from database import journal_store as store
from features import calendar as cal
from forecaster import journal
from forecaster import structure_llm as sl
from forecaster import structure_rules as sr
from forecaster.forecast_baseline import _eligibility, baseline_forecast, exact, reference_targets
from forecaster.forecast_service import (_digest, forecast_session, freeze_forecast_evidence, idempotency_key)
from forecaster.forecast_validation import ForecastInputError
from forecaster.provenance import code_revision

MODE = "historical_replay"
# The plan estimates a request's output tokens (thinking included) from the arm's answered requests
# (measured_output_tokens): its current version's, else its earlier versions' - at effort xhigh those thought longer,
# so the estimate errs high - else these guesses (the first answers at xhigh, 2026-10-05: 23,239 and 6,568). It
# also gives the most a run can cost, every request at its cap.
RESTRICTED_OUTPUT_TOKENS = 24000
SYNTHESIS_OUTPUT_TOKENS = 8000
RESTRICTED_PROTOCOLS = (pre.RESTRICTED_PROTOCOL_VERSION, "nq_structure_restricted_v2", "nq_structure_restricted_v1")


# --------------------------------------------------------------------------
# The date-blinded evidence (both arms)
# --------------------------------------------------------------------------

def _clock(value: str) -> str:
    """'YYYY-MM-DDTHH:MMZ' (UTC) on the New York clock."""
    t = datetime.strptime(value[:16], "%Y-%m-%dT%H:%M").replace(tzinfo=timezone.utc)
    return t.astimezone(cal.NY_TZ).strftime("%H:%M")


ALL_EVIDENCE = ("bars_5m", "bars_15m", "bars_2m_final", "swings_5m")


def blinded_bundle(snapshot: Dict[str, Any], keep: Sequence[str] = ALL_EVIDENCE) -> Dict[str, Any]:
    """
    The structure annotation's evidence bundle (structure_llm.evidence_bundle) without the date, the contract and
    absolute prices, with only the bar sets in ``keep`` (each arm's protocol sends its own: RESTRICTED_EVIDENCE,
    SYNTHESIS_EVIDENCE): ``{'bundle', 'ids', 'id_map' (blinded id -> the snapshot's id), 'anchor' (price, name)}``.
    """
    plain, _ = sl.evidence_bundle(snapshot)
    refs = plain["references"]
    anchor_name = next((n for n in ("prev_rth_close", "overnight_open") if refs.get(f"ref:{n}", {}).get("value")
                        is not None), None)
    first = (plain["bars_5m"]["rows"] or [[None, 0.0]])[0]
    anchor = refs[f"ref:{anchor_name}"]["value"] if anchor_name else first[1]
    anchor_label = {"prev_rth_close": "the previous RTH close", "overnight_open": "the overnight open"}.get(
        anchor_name, "the first overnight bar's open")
    id_map: Dict[str, str] = {}

    def bar_id(original: str) -> str:
        _, tf, start = original.split(":", 2)
        blind = f"bar:{tf}:{_clock(start)}"
        id_map[blind] = original
        return blind

    def px(value):
        return None if value is None else round(float(value) - float(anchor), 2)

    def rows(key: str, prices: int) -> Dict[str, Any]:
        block = plain[key]
        return {"columns": block["columns"],
                "rows": [[bar_id(r[0])] + [px(v) for v in r[1:5]] + [r[5]] + [px(v) for v in r[6:6 + prices]]
                         for r in block["rows"]]}

    session = plain["session"]
    bundle = {
        "session": {"cutoff_et": session["cutoff_et"], "T": session["T"],
                    "schedule": (snapshot["payload"].get("schedule") or {}).get("schedule"),
                    "anchor": anchor_label, "units": f"index points relative to {anchor_label}; times New York clock"},
        "completeness": plain["completeness"],
        "references": {k: ({**v, "value": px(v["value"])} if v.get("value") is not None else v)
                       for k, v in refs.items()},
        "bars_5m": rows("bars_5m", 0),
        "bars_15m": rows("bars_15m", 0),
        "bars_2m_final": rows("bars_2m_final", 3),
        "swings_5m": [{"bar": bar_id(s["bar"]), "kind": s["kind"], "price": px(s["price"]),
                       "confirmed_at": _clock(s["confirmed_at"])} for s in plain["swings_5m"]],
    }
    bundle = {k: v for k, v in bundle.items() if k not in ALL_EVIDENCE or k in keep}
    return {"bundle": bundle, "ids": sl.bundle_ids(bundle), "id_map": id_map, "anchor": (float(anchor), anchor_label)}


# --------------------------------------------------------------------------
# Arm C: the restricted annotation
# --------------------------------------------------------------------------

def restricted_annotation(snapshot: Dict[str, Any], answer: Dict[str, Any], model: str) -> Dict[str, Any]:
    """Claude's two fields over the rule-based annotation of the same snapshot, evidence ids mapped back."""
    id_map = blinded_bundle(snapshot)["id_map"]
    rules = sr.annotate(snapshot)
    fields = dict(rules["fields"])
    for name in pre.RESTRICTED_FIELDS:
        f = answer["fields"][name]
        fields[name] = {"value": f["value"], "status": f["status"], "reason": f["reason"],
                        "evidence_ids": [id_map.get(i, i) for i in dict.fromkeys(f["evidence_ids"])],
                        "basis": f["basis"]}
    return sr.seal({
        "protocol_version": pre.RESTRICTED_PROTOCOL_VERSION, "annotator": "llm", "integrity_status": "ok",
        "fields": {name: fields[name] for name in pre.FIELDS},
        "price_location": rules["price_location"],
        "measurements": {"model": model, "contradictions": answer.get("contradictions") or [],
                         "from_the_rules": pre.RULES_PROTOCOL_VERSION,
                         "rules_values": {n: rules["fields"][n]["value"] for n in pre.RESTRICTED_FIELDS}},
    })


def _restricted_bundle(snapshot: Dict[str, Any]) -> Tuple[Dict[str, Any], set]:
    blind = blinded_bundle(snapshot, pre.RESTRICTED_EVIDENCE)
    return blind["bundle"], blind["ids"]


RESTRICTED = sl.Protocol(pre.RESTRICTED_PROTOCOL_VERSION, pre.LLM_MODEL, pre.ARMS_EFFORT, pre.RESTRICTED_MAX_TOKENS,
                         pre.RESTRICTED_PROMPT, tuple(pre.RESTRICTED_FIELDS), pre.restricted_output_schema,
                         _restricted_bundle, restricted_annotation)


# --------------------------------------------------------------------------
# Arm D: the synthesis
# --------------------------------------------------------------------------

def _b_evidence(conn, day: str, profile: str) -> Optional[Tuple[Dict, Dict, Dict]]:
    """Arm B's explicit evidence of a session: its snapshot, rule-based annotation and that annotation's newest
    analogue set - None when one is missing (the forecaster has not caught the session up)."""
    snaps = store.list_snapshots(conn, day, day, defs.PROFILES[profile].snapshot_version)
    if not snaps:
        return None
    annotation = store.latest_annotation(conn, snaps[0]["snapshot_id"], pre.RULES_PROTOCOL_VERSION)
    if annotation is None:
        return None
    aset = store.latest_analogue_set(conn, snaps[0]["snapshot_id"], pre.MATCHER_VERSION, defs.LABEL_VERSION,
                                     pre.RULES_PROTOCOL_VERSION)
    if aset is None or aset["target_annotation_id"] != annotation["annotation_id"]:
        return None
    return snaps[0], annotation, aset


def synthesis_bundle(snapshot: Dict[str, Any], annotation: Dict[str, Any], aset: Dict[str, Any],
                     evidence: Dict[str, Any]) -> Dict[str, Any]:
    """
    The synthesis' evidence: the blinded pre-open bundle, the rule-based annotation, the analogues (by rank) with
    their frozen eligible outcomes and match components, per target the baseline's counts, prior, smoothed
    distribution and class, the eligibility, the vocabulary and the candidate levels. ``{'bundle', 'ids',
    'id_map', 'eligibility', 'baseline_class'}``.
    """
    blind = blinded_bundle(snapshot, fc.SYNTHESIS_EVIDENCE)
    bundle = dict(blind["bundle"])
    anchor = blind["anchor"][0]
    targets = [t for _, t in fc.FORECAST_TARGETS]
    bundle["annotation"] = {f"annotation:{name}": {"value": f.get("value"), "status": f.get("status")}
                            for name, f in annotation["fields"].items()}
    bundle["price_location"] = annotation.get("price_location")
    components = {m["snapshot_id"]: m.get("components") or {} for m in aset["members"]}
    bundle["analogues"] = [
        {"id": f"analogue:{m['rank']}", "similarity_percent": m["similarity"],
         "comparable_weight": m["comparable_weight"],
         "components": {f: {"score": c.get("score"), "weight": c.get("weight")}
                        for f, c in components.get(m["snapshot_id"], {}).items()},
         "outcomes": m["labels"]} for m in evidence.get("members") or []]
    base = baseline_forecast(evidence, fc.BASELINE_VERSION)
    summary = aset["outcome_summary"]["targets"]
    bundle["baseline"] = {
        f"baseline:{t}": {k: summary[t].get(k) for k in ("status", "eligible", "without_label", "raw", "prior",
                                                          "prior_sessions", "prior_without_label", "smoothed")}
        | {"predicted_class": base["predictions"][t]["predicted_label"]} for t in targets}
    # the frozen thresholds in index points (v5): T, B and A - a target needing one that is unavailable is ineligible
    th = evidence.get("thresholds") or {}
    a_points = None if th.get("A") is None else round(float(Fraction(str(th["A"]))), 2)
    bundle["session"] = {**bundle["session"], "B": th.get("B"), "A": a_points,
                         "threshold_units": "T, B and A are distances in index points, not prices"}
    eligibility = {t: _eligibility(t, evidence) or _threshold_missing(t, th) for t in targets}
    bundle["eligibility"] = {t: why or "eligible" for t, why in eligibility.items()}
    bundle["targets"] = {t: list(defs.TARGETS[t]["labels"]) for t in targets}
    bundle["candidates"] = {
        f"candidate:{name}": ({"value": round(float(Decimal(str(c["value"]))) - anchor, 2), "status": "valid"}
                              if c.get("status") == "valid" and c.get("value") is not None
                              else {"value": None, "status": c.get("status")})
        for name, c in (evidence.get("candidates") or {}).items()}
    ids = (sl.bundle_ids(bundle) | set(bundle["annotation"]) | {a["id"] for a in bundle["analogues"]}
           | set(bundle["baseline"]) | set(bundle["candidates"]))
    return {"bundle": bundle, "ids": ids, "id_map": blind["id_map"], "eligibility": eligibility,
            "baseline_class": {t: base["predictions"][t]["predicted_label"] for t in targets}}


def _threshold_missing(target: str, thresholds: Dict[str, Any]) -> Optional[str]:
    """Why ``target`` is ineligible for want of a frozen threshold (synthesis v5), or None."""
    missing = [n for n in fc.SYNTHESIS_THRESHOLDS.get(target, ()) if thresholds.get(n) is None]
    return (f"threshold {' and '.join(missing)} unavailable at the cutoff (no frozen daily ATR; the realised label "
            f"would be missing_threshold)") if missing else None


def _synthesis_prompt() -> str:
    with open(fc.SYNTHESIS_PROMPT, encoding="utf-8") as f:
        return f.read()


def build_synthesis_request(bundle: Dict[str, Any]) -> Tuple[Dict[str, Any], str]:
    params = {
        "model": pre.LLM_MODEL, "max_tokens": fc.SYNTHESIS_MAX_TOKENS,
        "system": [{"type": "text", "text": _synthesis_prompt(), "cache_control": {"type": "ephemeral"}}],
        "messages": [{"role": "user", "content": "Evidence bundle (JSON):\n" + canonical_json(bundle)}],
        "output_config": {"effort": pre.ARMS_EFFORT,
                          "format": {"type": "json_schema", "schema": fc.synthesis_output_schema()}},
    }
    return params, hashlib.sha256(canonical_json(params).encode()).hexdigest()


def structure_problem(answer: Dict[str, Any]) -> Optional[str]:
    """What the flat schema cannot enforce about its shape: every target exactly once, each class once per target."""
    targets = [i["target"] for i in answer["predictions"]]
    missing = [t for _, t in fc.FORECAST_TARGETS if t not in targets]
    repeated = sorted({t for t in targets if targets.count(t) > 1})
    if missing or repeated:
        return ("predictions must name every target once"
                + (f"; missing {', '.join(missing)}" if missing else "")
                + (f"; repeated {', '.join(repeated)}" if repeated else ""))
    for item in answer["predictions"]:
        classes = [p["class"] for p in item["probabilities"]]
        if len(classes) != len(set(classes)):
            return f"{item['target']}: a class appears twice in its probabilities"
    return None


def normalize(answer: Any) -> Any:
    """
    A synthesis answer in the per-target form the checks read: ``predictions`` keyed by target, the schema's ""
    (it has no null) as None for predicted_class, reason and departure, and the probability pairs as a
    class -> value dict (None when the list is empty). Anything that is not an answer is returned as it is.
    """
    if not isinstance(answer, dict) or not isinstance(answer.get("predictions"), list):
        return answer
    preds = {}
    for item in answer["predictions"]:
        if not isinstance(item, dict) or item.get("target") in preds:
            continue
        pairs = item.get("probabilities") or []
        preds[item.get("target")] = {
            **item, **{k: (item.get(k) or None) for k in ("predicted_class", "reason", "departure")},
            "probabilities": {p["class"]: p["p"] for p in pairs} if pairs else None}
    return {**answer, "predictions": preds}


def _probabilities(target: str, probs: Dict[str, str]) -> Tuple[Optional[Dict[str, Fraction]], Optional[str]]:
    vocab = list(defs.TARGETS[target]["labels"])
    if set(probs) != set(vocab):
        return None, f"{target}: probabilities are not over exactly its classes"
    out = {}
    for c in vocab:
        try:
            d = Decimal(str(probs[c]))
        except InvalidOperation:
            return None, f"{target}: {probs[c]!r} is not a decimal"
        if not d.is_finite() or not 0 <= d <= 1:
            return None, f"{target}: {probs[c]!r} is not in [0, 1]"
        out[c] = Fraction(d)
    if sum(out.values()) != 1:
        return None, f"{target}: the probabilities sum to {float(sum(out.values()))}, not exactly 1"
    return out, None


def validate_synthesis(answer: Any, ids: set, eligibility: Dict[str, Optional[str]],
                       baseline_class: Dict[str, Optional[str]]) -> Optional[str]:
    """The first problem with a synthesis answer (nq_synthesis_p1_v1 validation), or None."""
    from jsonschema import Draft202012Validator
    from jsonschema.exceptions import best_match
    e = best_match(Draft202012Validator(fc.synthesis_output_schema()).iter_errors(answer))
    if e is not None:
        return f"schema: {'/'.join(str(p) for p in e.absolute_path) or '(answer)'}: {e.message}"
    problem = structure_problem(answer)
    if problem:
        return problem
    answer = normalize(answer)
    if answer["integrity_status"] != "ok":
        return f"Claude reported {answer['integrity_status']}, which the application's check does not find"
    predicted_any = False
    for _, t in fc.FORECAST_TARGETS:
        p = answer["predictions"][t]
        status, cls, probs = p["status"], p["predicted_class"], p["probabilities"]
        if eligibility.get(t) and status != "unavailable":
            return f"{t}: not eligible for this session ({eligibility[t]}), yet {status}"
        if status == "unavailable":
            if cls is not None or probs is not None or not (p["reason"] or "").strip():
                return f"{t}: unavailable needs no class, no probabilities and a reason"
        else:
            if probs is None:
                return f"{t}: {status} without probabilities"
            dist, problem = _probabilities(t, probs)
            if problem:
                return problem
            top = [c for c, v in dist.items() if v == max(dist.values())]
            if status == "predicted":
                if cls is None or top != [cls]:
                    return f"{t}: {cls!r} is not the single most probable class ({', '.join(top)})"
                if baseline_class.get(t) not in (None, cls) and not (p["departure"] or "").strip():
                    return f"{t}: {cls} departs from the baseline's {baseline_class[t]} without a departure"
            elif cls is not None or len(top) < 2 or not (p["reason"] or "").strip():
                return f"{t}: a tie needs a shared top, no class and the reason"
            predicted_any = True
        unknown = [i for i in p["supporting_evidence_ids"] + p["conflicting_evidence_ids"] if i not in ids]
        if unknown:
            return f"{t}: evidence id(s) not in the bundle: {', '.join(unknown[:3])}"
    if predicted_any and answer["confidence"] is None:
        return "predictions without a confidence"
    return None


def _predictions(answer: Dict[str, Any], summary: Dict[str, Any]) -> List[Dict[str, Any]]:
    answer = normalize(answer)
    out = []
    for _, t in fc.FORECAST_TARGETS:
        p, s = answer["predictions"][t], summary[t]
        dist = None if p["probabilities"] is None else _probabilities(t, p["probabilities"])[0]
        status = {"predicted": "predicted", "tie": "ambiguous_prediction", "unavailable": "unavailable"}[p["status"]]
        out.append({"target": t, "status": status,
                    "predicted_label": p["predicted_class"] if status == "predicted" else None,
                    "estimation_status": "judgement" if dist is not None else "none",
                    "distribution": None if dist is None else {c: exact(v) for c, v in dist.items()},
                    "eligible": s["eligible"], "without_label": s["without_label"],
                    "prior_sessions": s["prior_sessions"], "prior_without_label": s["prior_without_label"],
                    "reason": None if status == "predicted" else p["reason"]})
    return out


def synthesis_key(snapshot, annotation, aset, profile: str, mode: str = MODE) -> str:
    return idempotency_key(snapshot, annotation["annotation_id"], aset["set_id"], profile, mode, fc.SYNTHESIS_VERSION)


def synthesis_evidence(conn, snapshot, annotation, aset, profile: str, history, mode: str = MODE) -> Dict[str, Any]:
    """Arm B's evidence frozen for a synthesis run: its versions name the synthesis and its schema (v2)."""
    evidence = freeze_forecast_evidence(conn, snapshot, annotation, aset, profile, mode, history,
                                        fc.SYNTHESIS_VERSION)
    evidence["versions"]["schema"] = fc.SYNTHESIS_SCHEMA_VERSION
    return evidence


def _prepare(conn, snapshot, annotation, aset, profile: str, history, mode: str = MODE) -> Dict[str, Any]:
    """Everything a synthesis request of this evidence is built from, and the request itself; ``mode`` live for a
    live capture's snapshot (forecaster/live_synthesis.py)."""
    evidence = synthesis_evidence(conn, snapshot, annotation, aset, profile, history, mode)
    syn = synthesis_bundle(snapshot, annotation, aset, evidence)
    params, request_hash = build_synthesis_request(syn["bundle"])
    return {"snapshot": snapshot, "annotation": annotation, "aset": aset, "profile": profile, "mode": mode,
            "key": synthesis_key(snapshot, annotation, aset, profile, mode), "evidence": evidence, "syn": syn,
            "params": params, "request_hash": request_hash}


def _ready(conn, day: str, profile: str, history) -> Dict[str, Any]:
    """``{'status': 'ready', ...the preparation}`` for a session whose synthesis is due, else ``{'status':
    'skipped'|'stored', ...}``."""
    found = _b_evidence(conn, day, profile)
    if found is None:
        return {"status": "skipped", "reason": "no arm B evidence yet (snapshot, annotation, analogue set)"}
    snapshot, annotation, aset = found
    if annotation["integrity_status"] != "ok":
        return {"status": "skipped", "reason": "the annotation is contaminated (P1 stop rule)"}
    stored = store.find_forecast_run(conn, synthesis_key(snapshot, annotation, aset, profile))
    if stored is not None:
        return {"status": "stored", "run": store.get_forecast_run(conn, stored)}
    return {"status": "ready", **_prepare(conn, snapshot, annotation, aset, profile, history)}


def _record(conn, prep: Dict[str, Any], mode: str) -> str:
    params = prep["params"]
    return store.save_inference_request(conn, {
        "snapshot_id": prep["snapshot"]["snapshot_id"], "protocol_version": fc.SYNTHESIS_VERSION,
        "model": pre.LLM_MODEL, "request": params, "request_hash": prep["request_hash"],
        "prompt_sha256": sl._sha(params["system"][0]["text"]),
        "schema_sha256": sl._sha(params["output_config"]["format"]["schema"]),
        "evidence_sha256": sl._sha(params["messages"][0]["content"]), "code_revision": code_revision(), "mode": mode})


def _finish(conn, prep: Dict[str, Any], request_id: str, started: datetime, message=None,
            error: Optional[str] = None) -> Dict[str, Any]:
    """Stores the run answering ``request_id``: issued, invalid (failed validation, the raw text kept) or failed
    (no answer, refused, cut off, an error) - nothing is filled in from another run. A live run is the database's
    to issue: inserted after its deadline it is late (migration 0014); issued, it is acknowledged after its
    commit, as arms A and B are."""
    snapshot, annotation, aset, evidence, syn = (prep[k] for k in ("snapshot", "annotation", "aset", "evidence",
                                                                   "syn"))
    key, profile, day, mode = prep["key"], prep["profile"], str(snapshot["session_date"]), prep.get("mode", MODE)
    attempt: Dict[str, Any] = {"request_id": request_id, "request_hash": prep["request_hash"]}
    status, reason, answer = "failed", error, None
    if message is not None:
        attempt.update(model=getattr(message, "model", pre.LLM_MODEL), stop_reason=message.stop_reason,
                       usage=None if getattr(message, "usage", None) is None else json.loads(message.usage.to_json()))
        text = next((b.text for b in message.content if getattr(b, "type", None) == "text"), None)
        if message.stop_reason == "refusal":
            reason = f"refused: {getattr(message, 'stop_details', None)}"
        elif message.stop_reason == "max_tokens":
            reason, attempt["raw_text"] = "output cut off at max_tokens", text
        elif attempt["model"] != pre.LLM_MODEL:
            status, reason = "invalid", f"served by {attempt['model']}, not {pre.LLM_MODEL} (a fallback is not arm D)"
        else:
            try:
                answer = json.loads(text or "")
            except json.JSONDecodeError as e:
                status, reason, attempt["raw_text"] = "invalid", f"not JSON: {e}", text
            if answer is not None:
                attempt["answer"] = answer
                problem = validate_synthesis(answer, syn["ids"], syn["eligibility"], syn["baseline_class"])
                status, reason = ("invalid", problem) if problem else ("issued", None)
                if problem:
                    attempt["raw_text"] = text
    predictions = _predictions(answer, aset["outcome_summary"]["targets"]) if status == "issued" else []
    if status != "issued":
        key = f"{key}:attempt:{uuid.uuid4()}"            # an attempt never blocks the official run
    outputs: Dict[str, Any] = {}
    if status == "issued":
        first = normalize(answer)["predictions"]["first_level_tested"]["predicted_class"]
        level = (evidence.get("candidates") or {}).get(first) or {}
        outputs = {"confidence": answer["confidence"], "confidence_basis": answer["confidence_basis"],
                   "first_level_price": level.get("value") if first else None,
                   "reference_targets": reference_targets(evidence),
                   "forecast_confidence_reason": "the synthesis' own rating (A2): evidence and conviction, not "
                                                 "calibration"}
    previous = store.current_forecast_run(conn, day, profile, mode, fc.SYNTHESIS_VERSION, key) \
        if status == "issued" else None
    run = {
        "idempotency_key": key, "symbol": snapshot["symbol"], "session_date": day,
        "contract_id": snapshot["contract_id"], "profile": profile, "snapshot_id": snapshot["snapshot_id"],
        "annotation_id": annotation["annotation_id"], "analogue_set_id": aset["set_id"],
        "label_version": defs.LABEL_VERSION, "algorithm_version": fc.SYNTHESIS_VERSION,
        "schema_version": fc.SYNTHESIS_SCHEMA_VERSION, "issue_policy": fc.ISSUE_POLICIES[mode],
        "code_revision": code_revision(), "mode": mode, "input_cutoff_at": snapshot["cutoff_at"],
        "deadline_at": cal.ny_instant(datetime.fromisoformat(day).date(), fc.LIVE_DEADLINE_ET) if mode == "live"
        else None,
        "generation_started_at": started, "generation_completed_at": datetime.now(timezone.utc),
        "lifecycle_status": status, "supersedes_run_id": previous["run_id"] if previous else None,
        "failure_reason": reason, "evidence_digest": _digest(evidence), "outputs": outputs,
        "request_id": request_id}
    run_id, created = store.save_forecast_run(conn, run, {**evidence, "attempt": attempt}, predictions)
    result = store.get_forecast_run(conn, run_id)
    if created and mode == "live" and result["lifecycle_status"] == "issued":
        store.add_forecast_event(conn, run_id, "acknowledged", "committed and read back by the issuing process")
        result = store.get_forecast_run(conn, run_id)
    return result


def synthesize(conn, client, day: str, profile: str = defs.DEFAULT_PROFILE) -> Dict[str, Any]:
    """
    Arm D for one session, live: ``{'status': 'sent'|'stored'|'skipped', 'run'?, 'reason'?}``. The request is
    recorded before it is sent and the run stored with its outcome; evidence already answered by an issued run is
    not sent again.
    """
    sl._require_manual()
    prep = _ready(conn, day, profile, store.outcome_history(conn, defs.LABEL_VERSION))
    if prep["status"] != "ready":
        return prep
    started = datetime.now(timezone.utc)
    request_id = _record(conn, prep, "live")
    try:
        message = sl.send(client, prep["params"])
    except Exception as e:
        return {"status": "sent", "run": _finish(conn, prep, request_id, started, error=f"{type(e).__name__}: {e}")}
    return {"status": "sent", "run": _finish(conn, prep, request_id, started, message)}


def submit_synthesis_batch(conn, client, days: Sequence[str], profile: str = defs.DEFAULT_PROFILE,
                           limit: Optional[int] = None) -> Tuple[Optional[str], int]:
    """
    Arm D for ``days`` through the Batch API (half price; no refusal fallback there): ``(batch id or None,
    requests)``. Each request is recorded before the batch is created, its id the custom_id, and the batch id the
    moment it is known; a batch the API refuses closes its requests as failed runs.
    """
    sl._require_manual()
    from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
    from anthropic.types.messages.batch_create_params import Request
    history = store.outcome_history(conn, defs.LABEL_VERSION)
    preps = []
    for day in days:
        if limit is not None and len(preps) >= limit:
            break
        prep = _ready(conn, day, profile, history)
        if prep["status"] == "ready" and not pending_snapshots(conn, "D") & {prep["snapshot"]["snapshot_id"]}:
            preps.append(prep)
    if not preps:
        return None, 0
    recorded = [(prep, _record(conn, prep, "batch")) for prep in preps]
    submitted = datetime.now(timezone.utc)
    try:
        batch_id = client.messages.batches.create(requests=[
            Request(custom_id=request_id, params=MessageCreateParamsNonStreaming(**prep["params"]))
            for prep, request_id in recorded]).id
    except Exception as e:
        for prep, request_id in recorded:
            _finish(conn, prep, request_id, submitted, error=f"batch not created: {type(e).__name__}: {e}")
        return None, len(recorded)
    store.save_inference_batch(conn, batch_id, [r for _, r in recorded], submitted)
    return batch_id, len(recorded)


def _prep_for_request(conn, req: Dict[str, Any], profile: str, history) -> Optional[Dict[str, Any]]:
    """The evidence an archived synthesis request was built from: among the snapshot's rule-based analogue sets,
    the one whose rebuilt request has the archived hash - None when none has (the request cannot be matched)."""
    snapshot = store.get_snapshot(conn, req["snapshot_id"])
    rows = conn.execute(
        "SELECT s.set_id FROM journal.analogue_sets s JOIN journal.structure_annotations a "
        "ON a.annotation_id = s.target_annotation_id WHERE s.target_snapshot_id = %s AND a.protocol_version = %s "
        "ORDER BY s.created_at DESC;", (req["snapshot_id"], pre.RULES_PROTOCOL_VERSION)).fetchall()
    for row in rows:
        aset = store.get_analogue_set(conn, str(row["set_id"]))
        annotation = store.get_annotation(conn, aset["target_annotation_id"])
        prep = _prepare(conn, snapshot, annotation, aset, profile, history)
        if prep["request_hash"] == req["request_hash"]:
            return prep
    return None


def collect_synthesis_batch(conn, client, batch_id: str, profile: str = defs.DEFAULT_PROFILE) -> Dict[str, int]:
    """
    Stores a run for every request of an ended, recorded synthesis batch not answered yet (an interrupted
    collection simply runs again): its answer, or failed when the batch has no result for it. Each answer is
    matched to the evidence of the request archived when it was sent. Returns status counts.
    """
    sl._require_manual()
    ledger = {r["request_id"]: r for r in store.inference_batch_requests(conn, batch_id)}
    history = store.outcome_history(conn, defs.LABEL_VERSION)
    counts: Dict[str, int] = {}
    seen = set()

    def store_run(req, message=None, error=None):
        prep = _prep_for_request(conn, req, profile, history)
        if prep is None:
            _unmatched_run(conn, req, profile)
            counts["failed"] = counts.get("failed", 0) + 1
            return
        run = _finish(conn, prep, req["request_id"], _as_datetime(req["submitted_at"]), message, error)
        counts[run["lifecycle_status"]] = counts.get(run["lifecycle_status"], 0) + 1

    for result in client.messages.batches.results(batch_id):
        req = ledger.get(result.custom_id)
        if req is None or req["answered"]:
            continue
        seen.add(result.custom_id)
        if result.result.type == "succeeded":
            store_run(req, message=result.result.message)
        else:
            store_run(req, error=f"batch result {result.result.type}")
    for request_id, req in ledger.items():
        if not req["answered"] and request_id not in seen:
            store_run(req, error="no result for this request in the ended batch")
    return counts


def _unmatched_run(conn, req: Dict[str, Any], profile: str) -> None:
    """Closes a batch request whose evidence cannot be found again as a failed run of its snapshot - so it is not
    pending for ever - with no annotation, analogue set or predictions."""
    snapshot = store.get_snapshot(conn, req["snapshot_id"])
    now = datetime.now(timezone.utc)
    reason = "the evidence of the archived request was not found again; the answer is not used"
    store.save_forecast_run(conn, {
        "idempotency_key": f"unmatched:{req['request_id']}", "symbol": snapshot["symbol"],
        "session_date": str(snapshot["session_date"]), "contract_id": snapshot["contract_id"], "profile": profile,
        "snapshot_id": snapshot["snapshot_id"], "annotation_id": None, "analogue_set_id": None,
        "label_version": defs.LABEL_VERSION, "algorithm_version": fc.SYNTHESIS_VERSION,
        "schema_version": fc.SYNTHESIS_SCHEMA_VERSION, "issue_policy": fc.ISSUE_POLICIES[MODE],
        "code_revision": code_revision(), "mode": MODE, "input_cutoff_at": snapshot["cutoff_at"], "deadline_at": None,
        "generation_started_at": _as_datetime(req["submitted_at"]), "generation_completed_at": now,
        "lifecycle_status": "failed", "supersedes_run_id": None, "failure_reason": reason,
        "evidence_digest": _digest({"request": req["request_id"]}), "outputs": {}, "request_id": req["request_id"]},
        {"attempt": {"request_id": req["request_id"], "request_hash": req["request_hash"]}}, [])


def _as_datetime(value) -> datetime:
    if isinstance(value, datetime):
        return value
    t = datetime.fromisoformat(str(value).replace(" ", "T"))
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def pending_snapshots(conn, arm: str) -> set:
    """Snapshots with an arm's request recorded but not answered - in a batch still processing, or lost (a live
    request whose run ended): never sent again until answered or closed."""
    version = pre.RESTRICTED_PROTOCOL_VERSION if arm == "C" else fc.SYNTHESIS_VERSION
    return {r["snapshot_id"] for r in store.unresolved_inference_requests(conn, version)}


def pending_batches(conn) -> Dict[str, List[str]]:
    """Per arm the recorded batches with unanswered requests, to collect before anything new is sent."""
    return {arm: sorted({r["batch_id"] for r in store.unresolved_inference_requests(conn, version) if r["batch_id"]})
            for arm, version in (("C", pre.RESTRICTED_PROTOCOL_VERSION), ("D", fc.SYNTHESIS_VERSION))}


# --------------------------------------------------------------------------
# Plan and run
# --------------------------------------------------------------------------

def target_sessions(sessions: int, now: Optional[datetime] = None,
                    days: Optional[Sequence[str]] = None) -> List[str]:
    """The chosen ``days`` that are scheduled sessions, or else the last ``sessions`` scheduled sessions up to today
    (New York); oldest first."""
    if days is not None:
        chosen = set()
        for d in days:
            try:
                if cal.session(d).is_open:
                    chosen.add(cal.session(d).session_date.isoformat())
            except (cal.CalendarCoverageError, ValueError):
                continue
        return sorted(chosen)
    today = (now or datetime.now(timezone.utc)).astimezone(cal.NY_TZ).date()
    days: List[str] = []
    s = cal.session(today)
    if s.is_open:
        days.append(today.isoformat())
    days += [x.session_date.isoformat() for x in cal.sessions_before(today, max(0, sessions - len(days)))[::-1]]
    return sorted(days[:sessions])


def _median(values: List[int]) -> int:
    values = sorted(values)
    return values[len(values) // 2]


def measured_output_tokens(conn, arm: str) -> Tuple[int, str]:
    """``(tokens, basis)``: the median output tokens of the arm's answered requests - its current version's, else
    its earlier versions' (an upper estimate where they thought at a higher effort) - else the guess."""
    if arm == "C":
        versions, guess = RESTRICTED_PROTOCOLS, RESTRICTED_OUTPUT_TOKENS
        sql = ("SELECT usage FROM journal.annotation_attempts WHERE protocol_version = %s AND status = 'ok' "
               "AND usage IS NOT NULL;")
        read = lambda row: _load(row["usage"])
    else:
        versions, guess = (fc.SYNTHESIS_VERSION,) + tuple(reversed(fc.ARM_HISTORY["D"])), SYNTHESIS_OUTPUT_TOKENS
        sql = ("SELECT f.evidence FROM journal.forecast_evidence f JOIN journal.forecast_runs r USING (run_id) "
               "WHERE r.algorithm_version = %s AND r.lifecycle_status = 'issued' AND r.request_id IS NOT NULL;")
        read = lambda row: (_load(row["evidence"]).get("attempt") or {}).get("usage")
    for version in versions:
        tokens = [int(u["output_tokens"]) for u in (read(r) for r in conn.execute(sql, (version,)).fetchall())
                  if u and u.get("output_tokens")]
        if tokens:
            earlier = "" if version == versions[0] else " (an earlier version: likely high)"
            return _median(tokens), f"the median of {len(tokens)} {version} request(s){earlier}"
    return guess, "a guess - no request answered yet"


def _load(value):
    return json.loads(value) if isinstance(value, str) else (value or {})


def _input_chars_per_token(conn, arm: str) -> Tuple[float, str]:
    """Characters per input token of the arm's answered requests (system prompt plus evidence, any version), so
    the plan sizes a request from its text; dense numeric JSON runs near 1.6. ``(ratio, basis)``."""
    if arm == "C":
        rows = conn.execute(
            "SELECT r.request, t.usage FROM journal.annotation_attempts t JOIN journal.inference_requests r "
            "ON r.request_id = t.request_id WHERE t.status = 'ok' AND t.usage IS NOT NULL AND "
            "r.protocol_version LIKE 'nq_structure_restricted_%%';").fetchall()
        pairs = [(_load(row["request"]), _load(row["usage"])) for row in rows]
    else:
        rows = conn.execute(
            "SELECT r.request, f.evidence FROM journal.forecast_runs x JOIN journal.inference_requests r "
            "ON r.request_id = x.request_id JOIN journal.forecast_evidence f ON f.run_id = x.run_id "
            "WHERE x.lifecycle_status = 'issued';").fetchall()
        pairs = [(_load(row["request"]), (_load(row["evidence"]).get("attempt") or {}).get("usage")) for row in rows]
    chars = tokens = 0
    for request, usage in pairs:
        if not usage:
            continue
        chars += len(request["system"][0]["text"]) + len(request["messages"][0]["content"])
        tokens += (usage.get("input_tokens") or 0) + (usage.get("cache_creation_input_tokens") or 0) + \
            (usage.get("cache_read_input_tokens") or 0)
    if tokens:
        return chars / tokens, f"{chars / tokens:.2f} characters per input token, measured"
    return 1.7, "1.7 characters per input token, a guess"


def _cost(conn, arm: str, request_chars: List[int], cap: int, batch: bool) -> Dict[str, Any]:
    """The rough cost of an arm's requests (list prices; input at the base price, the Batch API halving all)."""
    ratio, input_basis = _input_chars_per_token(conn, arm)
    output, output_basis = measured_output_tokens(conn, arm)
    tokens_in = sum(request_chars) / ratio
    factor = 0.5 if batch else 1.0
    return {"usd": factor * (tokens_in * 4 + output * len(request_chars) * 20) / 1e6,
            "usd_max": factor * (tokens_in * 4 + cap * len(request_chars) * 20) / 1e6,
            "basis": f"per request: output {output:,} tokens ({output_basis}), input "
                     f"{tokens_in / max(1, len(request_chars)):,.0f} tokens ({input_basis})"}


def plan(conn, sessions: int = 1, arms: Sequence[str] = ("C", "D"), profile: str = defs.DEFAULT_PROFILE,
         now: Optional[datetime] = None, days: Optional[Sequence[str]] = None, batch: bool = False) -> Dict[str, Any]:
    """
    What a run over the last ``sessions`` sessions - or the chosen ``days`` - would do, without sending anything
    (see the module docstring): per session the arms' state (a request recorded but not answered is pending, never
    sent again), the requests, a rough cost - halved through the Batch API - with its basis, the recorded batches
    to collect, and arm C's pool.
    """
    now = now or datetime.now(timezone.utc)
    version = defs.PROFILES[profile].snapshot_version
    pending = {arm: pending_snapshots(conn, arm) for arm in arms}
    rows, need_c, need_d = [], [], []
    history = store.outcome_history(conn, defs.LABEL_VERSION) if "D" in arms else None
    for day in target_sessions(sessions, now, days):
        snaps = store.list_snapshots(conn, day, day, version)
        if not snaps:
            rows.append({"session_date": day, "skip": journal.snapshot_pending(conn, day, profile, now)
                         or "no snapshot yet - run the forecaster"})
            continue
        snap, row = snaps[0], {"session_date": day}
        if "C" in arms:
            if store.latest_annotation(conn, snap["snapshot_id"], pre.RESTRICTED_PROTOCOL_VERSION) is not None:
                row["C"] = "annotated"
            elif snap["snapshot_id"] in pending["C"]:
                row["C"] = "pending - its answer is collected first"
            else:
                row["C"] = "request"
                need_c.append(snap)
        if "D" in arms:
            prep = _ready(conn, day, profile, history)
            if prep["status"] == "skipped":
                row["D"] = prep["reason"]
            elif prep["status"] == "stored":
                row["D"] = "issued"
            elif snap["snapshot_id"] in pending["D"]:
                row["D"] = "pending - its answer is collected first"
            else:
                row["D"] = "request"
                need_d.append(prep)
        rows.append(row)
    usd = usd_max = 0.0
    basis = {}
    marker = len("Evidence bundle (JSON):\n")
    if need_c:
        prompt = len(sl._system_prompt(RESTRICTED))
        c = _cost(conn, "C", [prompt + marker + len(canonical_json(RESTRICTED.bundle(s)[0])) for s in need_c],
                  pre.RESTRICTED_MAX_TOKENS, batch)
        usd, usd_max, basis["C"] = usd + c["usd"], usd_max + c["usd_max"], c["basis"]
    if need_d:
        d = _cost(conn, "D", [len(p["params"]["system"][0]["text"]) + len(p["params"]["messages"][0]["content"])
                              for p in need_d], fc.SYNTHESIS_MAX_TOKENS, batch)
        usd, usd_max, basis["D"] = usd + d["usd"], usd_max + d["usd_max"], d["basis"]
    pool = sum(1 for s in store.list_snapshots(conn, "2000-01-01", "2100-01-01", version)
               if store.latest_annotation(conn, s["snapshot_id"], pre.RESTRICTED_PROTOCOL_VERSION) is not None)
    batches = {arm: ids for arm, ids in pending_batches(conn).items() if arm in arms and ids}
    return {"sessions": rows, "arms": list(arms), "requests": len(need_c) + len(need_d),
            "requests_c": len(need_c), "requests_d": len(need_d), "usd": round(usd, 2),
            "usd_max": round(usd_max, 2), "batch": batch, "pending_batches": batches, "restricted_pool": pool,
            "model": pre.LLM_MODEL, "effort": pre.ARMS_EFFORT, "estimate_basis": basis}


def _wait(client, batch_id: str, until: datetime, sleep: Callable[[float], None], log) -> bool:
    """Polls a batch until it has ended (True) or ``until`` passes (False)."""
    started = datetime.now(timezone.utc)
    while client.messages.batches.retrieve(batch_id).processing_status != "ended":
        if datetime.now(timezone.utc) >= until:
            log(f"  batch {batch_id} is still processing - the next run collects it")
            return False
        delay = sl.batch_poll_delay(started)
        log(f"  batch {batch_id} processing; checking again in {delay:.0f} s")
        sleep(delay)
    return True


def _collect(conn, client, arm: str, batch_id: str, profile: str, log) -> None:
    counts = (sl.collect_batch(conn, client, batch_id, RESTRICTED) if arm == "C" else
              collect_synthesis_batch(conn, client, batch_id, profile))
    log(f"  collected arm {arm} batch {batch_id}: " + (", ".join(f"{k} {v}" for k, v in counts.items()) or "nothing"))


def run(conn, client, sessions: int = 1, arms: Sequence[str] = ("C", "D"), profile: str = defs.DEFAULT_PROFILE,
        max_requests: Optional[int] = None, log: Callable[[str], None] = print,
        now: Optional[datetime] = None, days: Optional[Sequence[str]] = None, batch: bool = False,
        wait_minutes: float = 30, sleep: Callable[[float], None] = time.sleep,
        connect: Optional[Callable[[], Any]] = None) -> Dict[str, Any]:
    """
    Arms C and D over the last ``sessions`` sessions - or the chosen ``days`` - oldest first: first the recorded
    batches of earlier runs that have ended are collected; then the restricted annotations still missing and arm
    D's syntheses, sent live side by side (D never reads C's answers) or each as one batch; a batch is waited for at most ``wait_minutes`` (one
    still processing is collected by a later run); then arm C's analogue sets and runs. At most ``max_requests``
    requests in all - C's live requests are counted before D's, so the two side by side never exceed it; a request
    recorded but not answered is never sent again. Requests need structure_llm.manual_requests (every sending and
    collecting function checks it). ``connect`` opens a database connection of its own for C's side when the two run
    side by side (the CLI passes it); without it they share ``conn``, whose statements are serialised.
    """
    version = defs.PROFILES[profile].snapshot_version
    days = [d for d in target_sessions(sessions, now, days) if store.list_snapshots(conn, d, d, version)]
    sent, counts = 0, {}
    until = datetime.now(timezone.utc) + timedelta(minutes=wait_minutes)

    def budget() -> int:
        return 10 ** 9 if max_requests is None else max(0, max_requests - sent)

    def tally(key, n=1):
        counts[key] = counts.get(key, 0) + n

    if client is not None:                              # earlier runs' batches first
        for arm, ids in pending_batches(conn).items():
            for batch_id in ids if arm in arms else []:
                if client.messages.batches.retrieve(batch_id).processing_status == "ended":
                    _collect(conn, client, arm, batch_id, profile, log)
                else:
                    log(f"  arm {arm}'s batch {batch_id} from an earlier run is still processing")
    submitted = []
    todo_c: List[Dict[str, Any]] = []
    if "C" in arms:
        waiting = pending_snapshots(conn, "C")
        todo_c = [s for s in (store.list_snapshots(conn, d, d, version)[0] for d in days)
                  if s["snapshot_id"] not in waiting
                  and store.latest_annotation(conn, s["snapshot_id"], pre.RESTRICTED_PROTOCOL_VERSION) is None]
        if len(todo_c) > budget():
            log(f"  C: {len(todo_c) - budget()} session(s) not sent - the approved request(s) are used")
            todo_c = todo_c[:budget()]
        if batch and todo_c:
            batch_id = sl.submit_batch(conn, client, todo_c, RESTRICTED)
            sent += len(todo_c)
            tally("C requests in a batch", len(todo_c))
            log(f"  C: {len(todo_c)} annotation request(s) in batch {batch_id}")
            if batch_id:
                submitted.append(("C", batch_id))
    c_live = [] if batch else todo_c
    budget_d = budget() - len(c_live)                    # C's requests are counted before D's, as when in turn

    def run_c(c_conn) -> Tuple[int, Dict[str, int]]:
        """Arm C's live annotation requests - beside arm D's, which do not read them."""
        n, tallies = 0, {}
        for snap in c_live:
            attempt = sl.annotate_live(c_conn, client, snap, RESTRICTED)
            n += attempt["status"] != "contaminated"
            key = f"C annotation {attempt['status']}"
            tallies[key] = tallies.get(key, 0) + 1
            log(f"  {snap['session_date']} C annotation: {attempt['status']}"
                + (f" - {attempt['error']}" if attempt.get("error") else ""))
        return n, tallies

    def run_d() -> Tuple[int, Dict[str, int]]:
        n, tallies = 0, {}

        def mark(key):
            tallies[key] = tallies.get(key, 0) + 1
        if batch:
            batch_id, k = (submit_synthesis_batch(conn, client, days, profile, budget_d) if budget_d else (None, 0))
            n += k
            if k:
                tallies["D requests in a batch"] = k
                log(f"  D: {k} synthesis request(s) in batch {batch_id}")
            if batch_id:
                submitted.append(("D", batch_id))
            return n, tallies
        waiting = pending_snapshots(conn, "D")
        history = store.outcome_history(conn, defs.LABEL_VERSION)
        for day in days:
            prep = _ready(conn, day, profile, history)
            if prep["status"] == "stored":
                log(f"  {day} D: already issued on this evidence")
                continue
            if prep["status"] == "skipped":
                log(f"  {day} D: skipped - {prep['reason']}")
                continue
            if prep["snapshot"]["snapshot_id"] in waiting:
                log(f"  {day} D: a request is pending - not sent again")
                continue
            if n >= budget_d:
                log(f"  {day} D: not sent - the approved request(s) are used")
                mark("D not sent")
                continue
            out = synthesize(conn, client, day, profile)
            n += out["status"] == "sent"
            r = out["run"]
            mark(f"D run {r['lifecycle_status']}")
            log(f"  {day} D run {r['run_id'][:8]}: {r['lifecycle_status']}"
                + (f" - {r['failure_reason']}" if r["failure_reason"] else ""))
        return n, tallies

    # Arms C and D are independent requests (D reads arm B's evidence, never C's): live, they run side by side.
    results = []
    if c_live and "D" in arms:
        c_conn = connect() if connect is not None else conn       # its own connection: nothing shared with D's side
        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                c_future = pool.submit(run_c, c_conn)
                results.append(run_d())
                results.append(c_future.result())
        finally:
            if c_conn is not conn:
                c_conn.close()
    else:
        if c_live:
            results.append(run_c(conn))
        if "D" in arms:
            results.append(run_d())
    for n, tallies in results:
        sent += n
        for key, k in tallies.items():
            tally(key, k)
    for arm, batch_id in submitted:
        if _wait(client, batch_id, until, sleep, log):
            _collect(conn, client, arm, batch_id, profile, log)
    if "C" in arms:
        journal.match(conn, profile, pre.RESTRICTED_PROTOCOL_VERSION)
        for day in days:
            try:
                result = forecast_session(conn, day, profile, pre.RESTRICTED_PROTOCOL_VERSION,
                                          algorithm=fc.RESTRICTED_VERSION)
            except ForecastInputError as e:
                log(f"  {day} C run: rejected - {e}")
                continue
            if result is None:
                log(f"  {day} C run: no restricted annotation yet")
                continue
            r, created = result
            members = len((r.get("evidence") or {}).get("members") or [])
            tally(f"C run {r['lifecycle_status']}")
            log(f"  {day} C run {r['run_id'][:8]}: {r['lifecycle_status']}{' (new)' if created else ''}, "
                f"{members} analogue(s)" + (f" - {r['failure_reason']}" if r["failure_reason"] else ""))
    return {"sessions": days, "requests_sent": sent, "counts": counts}
