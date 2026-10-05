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

  plan(conn, sessions, arms)   the last ``sessions`` scheduled sessions up to today:
                               what each needs, how many requests, a rough cost
  run(conn, client, ...)       sends what the plan needs (at most ``max_requests``),
                               then matches and issues arm C, and synthesises arm D -
                               each request recorded in the inference ledger first,
                               and the same evidence never sent twice once answered
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
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
# The rough estimate's output tokens (thinking included) per request at effort xhigh. Measured 2026-10-05: one
# restricted annotation spent all of its 16,000 tokens thinking without answering, so these are guesses above that;
# the plan also gives the most a run can cost, every request at its cap.
RESTRICTED_OUTPUT_TOKENS = 24000
SYNTHESIS_OUTPUT_TOKENS = 32000


# --------------------------------------------------------------------------
# The date-blinded evidence (both arms)
# --------------------------------------------------------------------------

def _clock(value: str) -> str:
    """'YYYY-MM-DDTHH:MMZ' (UTC) on the New York clock."""
    t = datetime.strptime(value[:16], "%Y-%m-%dT%H:%M").replace(tzinfo=timezone.utc)
    return t.astimezone(cal.NY_TZ).strftime("%H:%M")


def blinded_bundle(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    """
    The structure annotation's evidence bundle (structure_llm.evidence_bundle) without the date, the contract and
    absolute prices: ``{'bundle', 'ids', 'id_map' (blinded id -> the snapshot's id), 'anchor' (price, name)}``.
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
    blind = blinded_bundle(snapshot)
    return blind["bundle"], blind["ids"]


RESTRICTED = sl.Protocol(pre.RESTRICTED_PROTOCOL_VERSION, pre.LLM_MODEL, pre.LLM_EFFORT, pre.RESTRICTED_MAX_TOKENS,
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
    blind = blinded_bundle(snapshot)
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
    eligibility = {t: _eligibility(t, evidence) for t in targets}
    bundle["eligibility"] = {t: why or "eligible" for t, why in eligibility.items()}
    bundle["targets"] = {t: list(defs.TARGETS[t]["labels"]) for t in targets}
    bundle["candidates"] = {
        f"candidate:{name}": ({"value": round(float(Decimal(str(c["value"]))) - anchor, 2), "status": "valid"}
                              if c.get("status") == "valid" and c.get("value") is not None
                              else {"value": None, "status": c.get("status")})
        for name, c in (evidence.get("candidates") or {}).items()}
    ids = (blind["ids"] | set(bundle["annotation"]) | {a["id"] for a in bundle["analogues"]}
           | set(bundle["baseline"]) | set(bundle["candidates"]))
    return {"bundle": bundle, "ids": ids, "id_map": blind["id_map"], "eligibility": eligibility,
            "baseline_class": {t: base["predictions"][t]["predicted_label"] for t in targets}}


def _synthesis_prompt() -> str:
    with open(fc.SYNTHESIS_PROMPT, encoding="utf-8") as f:
        return f.read()


def build_synthesis_request(bundle: Dict[str, Any]) -> Tuple[Dict[str, Any], str]:
    params = {
        "model": pre.LLM_MODEL, "max_tokens": fc.SYNTHESIS_MAX_TOKENS,
        "system": [{"type": "text", "text": _synthesis_prompt(), "cache_control": {"type": "ephemeral"}}],
        "messages": [{"role": "user", "content": "Evidence bundle (JSON):\n" + canonical_json(bundle)}],
        "output_config": {"effort": pre.LLM_EFFORT,
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


def synthesis_key(snapshot, annotation, aset, profile: str) -> str:
    return idempotency_key(snapshot, annotation["annotation_id"], aset["set_id"], profile, MODE, fc.SYNTHESIS_VERSION)


def synthesis_evidence(conn, snapshot, annotation, aset, profile: str, history) -> Dict[str, Any]:
    """Arm B's evidence frozen for a synthesis run: its versions name the synthesis and its schema (v2)."""
    evidence = freeze_forecast_evidence(conn, snapshot, annotation, aset, profile, MODE, history,
                                        fc.SYNTHESIS_VERSION)
    evidence["versions"]["schema"] = fc.SYNTHESIS_SCHEMA_VERSION
    return evidence


def synthesize(conn, client, day: str, profile: str = defs.DEFAULT_PROFILE) -> Dict[str, Any]:
    """
    Arm D for one session: ``{'status': 'sent'|'stored'|'skipped', 'run'?, 'reason'?}``. The request is recorded
    before it is sent and the run stored with its outcome - issued, invalid (failed validation, the raw text
    kept) or failed (refused, cut off, an error); evidence already answered by an issued run is not sent again.
    """
    sl._require_manual()
    found = _b_evidence(conn, day, profile)
    if found is None:
        return {"status": "skipped", "reason": "no arm B evidence yet (snapshot, annotation, analogue set)"}
    snapshot, annotation, aset = found
    if annotation["integrity_status"] != "ok":
        return {"status": "skipped", "reason": "the annotation is contaminated (P1 stop rule)"}
    key = synthesis_key(snapshot, annotation, aset, profile)
    stored = store.find_forecast_run(conn, key)
    if stored is not None:
        return {"status": "stored", "run": store.get_forecast_run(conn, stored)}
    started = datetime.now(timezone.utc)
    history = store.outcome_history(conn, defs.LABEL_VERSION)
    evidence = synthesis_evidence(conn, snapshot, annotation, aset, profile, history)
    syn = synthesis_bundle(snapshot, annotation, aset, evidence)
    params, request_hash = build_synthesis_request(syn["bundle"])
    request_id = store.save_inference_request(conn, {
        "snapshot_id": snapshot["snapshot_id"], "protocol_version": fc.SYNTHESIS_VERSION, "model": pre.LLM_MODEL,
        "request": params, "request_hash": request_hash, "prompt_sha256": sl._sha(params["system"][0]["text"]),
        "schema_sha256": sl._sha(params["output_config"]["format"]["schema"]),
        "evidence_sha256": sl._sha(params["messages"][0]["content"]), "code_revision": code_revision(),
        "mode": "live"})
    attempt: Dict[str, Any] = {"request_id": request_id, "request_hash": request_hash}
    status, reason, answer = "failed", None, None
    try:
        message = sl.send(client, params)
    except Exception as e:  # the run is kept as failed; nothing is filled in from another run
        reason = f"{type(e).__name__}: {e}"
    else:
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
    previous = store.current_forecast_run(conn, day, profile, MODE, fc.SYNTHESIS_VERSION, key) \
        if status == "issued" else None
    run = {
        "idempotency_key": key, "symbol": snapshot["symbol"], "session_date": day,
        "contract_id": snapshot["contract_id"], "profile": profile, "snapshot_id": snapshot["snapshot_id"],
        "annotation_id": annotation["annotation_id"], "analogue_set_id": aset["set_id"],
        "label_version": defs.LABEL_VERSION, "algorithm_version": fc.SYNTHESIS_VERSION,
        "schema_version": fc.SYNTHESIS_SCHEMA_VERSION, "issue_policy": fc.ISSUE_POLICIES[MODE],
        "code_revision": code_revision(), "mode": MODE, "input_cutoff_at": snapshot["cutoff_at"],
        "deadline_at": None, "generation_started_at": started, "generation_completed_at": datetime.now(timezone.utc),
        "lifecycle_status": status, "supersedes_run_id": previous["run_id"] if previous else None,
        "failure_reason": reason, "evidence_digest": _digest(evidence), "outputs": outputs,
        "request_id": request_id}
    run_id, _ = store.save_forecast_run(conn, run, {**evidence, "attempt": attempt}, predictions)
    return {"status": "sent", "run": store.get_forecast_run(conn, run_id)}


# --------------------------------------------------------------------------
# Plan and run
# --------------------------------------------------------------------------

def target_sessions(sessions: int, now: Optional[datetime] = None) -> List[str]:
    """The last ``sessions`` scheduled sessions up to today (New York), oldest first."""
    today = (now or datetime.now(timezone.utc)).astimezone(cal.NY_TZ).date()
    days: List[str] = []
    s = cal.session(today)
    if s.is_open:
        days.append(today.isoformat())
    days += [x.session_date.isoformat() for x in cal.sessions_before(today, max(0, sessions - len(days)))[::-1]]
    return sorted(days[:sessions])


def plan(conn, sessions: int = 1, arms: Sequence[str] = ("C", "D"), profile: str = defs.DEFAULT_PROFILE,
         now: Optional[datetime] = None) -> Dict[str, Any]:
    """What a run over the last ``sessions`` sessions would do, without sending anything (see the module
    docstring): per session the arms' state, the requests, a rough cost and arm C's annotated pool."""
    now = now or datetime.now(timezone.utc)
    version = defs.PROFILES[profile].snapshot_version
    rows, need_c, need_d = [], [], []
    for day in target_sessions(sessions, now):
        snaps = store.list_snapshots(conn, day, day, version)
        if not snaps:
            rows.append({"session_date": day, "skip": journal.snapshot_pending(conn, day, profile, now)
                         or "no snapshot yet - run the forecaster"})
            continue
        snap, row = snaps[0], {"session_date": day}
        if "C" in arms:
            ann = store.latest_annotation(conn, snap["snapshot_id"], pre.RESTRICTED_PROTOCOL_VERSION)
            row["C"] = "annotated" if ann is not None else "request"
            if ann is None:
                need_c.append(snap)
        if "D" in arms:
            found = _b_evidence(conn, day, profile)
            if found is None:
                row["D"] = "no arm B evidence yet - run the forecaster"
            else:
                key = synthesis_key(*found, profile)
                row["D"] = "issued" if store.find_forecast_run(conn, key) else "request"
                if row["D"] == "request":
                    need_d.append(found)
        rows.append(row)
    usd = 0.0
    usd_max = (len(need_c) * pre.RESTRICTED_MAX_TOKENS + len(need_d) * fc.SYNTHESIS_MAX_TOKENS) / 1e6 * 20
    if need_c:
        e = sl.estimate(need_c, RESTRICTED, RESTRICTED_OUTPUT_TOKENS)
        usd += e["usd_live"]
        usd_max += e["input_tokens"] / 1e6 * 4
    if need_d:
        history = store.outcome_history(conn, defs.LABEL_VERSION)
        chars = 0
        for snapshot, annotation, aset in need_d:
            evidence = synthesis_evidence(conn, snapshot, annotation, aset, profile, history)
            chars += len(canonical_json(synthesis_bundle(snapshot, annotation, aset, evidence)["bundle"]))
        chars += len(_synthesis_prompt()) * len(need_d)
        usd += chars / 3.5 / 1e6 * 4 + SYNTHESIS_OUTPUT_TOKENS * len(need_d) / 1e6 * 20
        usd_max += chars / 3.5 / 1e6 * 4
    pool = sum(1 for s in store.list_snapshots(conn, "2000-01-01", "2100-01-01", version)
               if store.latest_annotation(conn, s["snapshot_id"], pre.RESTRICTED_PROTOCOL_VERSION) is not None)
    return {"sessions": rows, "arms": list(arms), "requests": len(need_c) + len(need_d),
            "requests_c": len(need_c), "requests_d": len(need_d), "usd": round(usd, 2),
            "usd_max": round(usd_max, 2), "restricted_pool": pool,
            "model": pre.LLM_MODEL, "effort": pre.LLM_EFFORT}


def run(conn, client, sessions: int = 1, arms: Sequence[str] = ("C", "D"), profile: str = defs.DEFAULT_PROFILE,
        max_requests: Optional[int] = None, log: Callable[[str], None] = print,
        now: Optional[datetime] = None) -> Dict[str, Any]:
    """
    Arms C and D over the last ``sessions`` sessions, oldest first: the restricted annotations still missing,
    then arm C's analogue sets and runs, then arm D's syntheses - at most ``max_requests`` requests in all.
    Requests need structure_llm.manual_requests (annotate_live and synthesize check it).
    """
    version = defs.PROFILES[profile].snapshot_version
    days = [d for d in target_sessions(sessions, now) if store.list_snapshots(conn, d, d, version)]
    sent, counts = 0, {}

    def budget() -> bool:
        return max_requests is None or sent < max_requests

    def tally(key):
        counts[key] = counts.get(key, 0) + 1

    if "C" in arms:
        for day in days:
            snap = store.list_snapshots(conn, day, day, version)[0]
            if store.latest_annotation(conn, snap["snapshot_id"], pre.RESTRICTED_PROTOCOL_VERSION) is not None:
                continue
            if not budget():
                log(f"  {day} C: not sent - the approved {max_requests} request(s) are used")
                tally("C not sent")
                continue
            attempt = sl.annotate_live(conn, client, snap, RESTRICTED)
            sent += attempt["status"] != "contaminated"
            tally(f"C annotation {attempt['status']}")
            log(f"  {day} C annotation: {attempt['status']}" + (f" - {attempt['error']}" if attempt.get("error")
                                                                  else ""))
        journal.match(conn, profile, pre.RESTRICTED_PROTOCOL_VERSION)
        for day in days:
            try:
                result = forecast_session(conn, day, profile, pre.RESTRICTED_PROTOCOL_VERSION,
                                          algorithm=fc.RESTRICTED_VERSION)
            except ForecastInputError as e:
                log(f"  {day} C run: rejected - {e}")
                continue
            if result is None:
                log(f"  {day} C run: no restricted annotation")
                continue
            r, created = result
            members = len((r.get("evidence") or {}).get("members") or [])
            tally(f"C run {r['lifecycle_status']}")
            log(f"  {day} C run {r['run_id'][:8]}: {r['lifecycle_status']}{' (new)' if created else ''}, "
                f"{members} analogue(s)" + (f" - {r['failure_reason']}" if r["failure_reason"] else ""))
    if "D" in arms:
        for day in days:
            found = _b_evidence(conn, day, profile)
            if found is not None and store.find_forecast_run(conn, synthesis_key(*found, profile)):
                log(f"  {day} D: already issued on this evidence")
                continue
            if not budget():
                log(f"  {day} D: not sent - the approved {max_requests} request(s) are used")
                tally("D not sent")
                continue
            try:
                out = synthesize(conn, client, day, profile)
            except ForecastInputError as e:
                log(f"  {day} D: rejected - {e}")
                continue
            if out["status"] == "skipped":
                log(f"  {day} D: skipped - {out['reason']}")
                continue
            sent += out["status"] == "sent"
            r = out["run"]
            tally(f"D run {r['lifecycle_status']}")
            log(f"  {day} D run {r['run_id'][:8]}: {r['lifecycle_status']}"
                + (f" - {r['failure_reason']}" if r["failure_reason"] else ""))
    return {"sessions": days, "requests_sent": sent, "counts": counts}
