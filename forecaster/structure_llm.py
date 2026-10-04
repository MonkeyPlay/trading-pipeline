# forecaster/structure_llm.py
"""
The Claude structure annotation (protocol nq_structure_llm_v3, Appendix A, A1):
the replacement for the rule-based annotation (forecaster/structure_rules.py) once
the Claude API is available. Same output shape (contracts.nq_preopen.ANNOTATION_SCHEMA),
same store, its own protocol version - the matcher never mixes the two.

  evidence_bundle(snapshot)   the frozen evidence Claude sees, every item with an id:
                              the references, the overnight 5m and 15m bars, the last
                              45 2m bars with the three moving averages, and the
                              confirmed 2/2 swing points on 5m bars
  build_request(snapshot)     the Messages API request: the runtime prompt
                              (prompts/runtime/structure_annotation_v2.md) as the
                              system prompt, the bundle as the user message, the
                              structure_annotation schema as the required output
  annotate_live(conn, snap)   one request now (server-side refusal fallback on); the
                              attempt is always stored, the annotation only when valid
  submit_batch / collect_batch  the historical backfill through the Batch API (half
                              price; no fallback there); a recorded batch is collected
                              later if the run that sent it ended first

Accounting (migration 0013): every request is a row in journal.inference_requests
before it is sent - the exact canonical request with its prompt, schema and evidence
hashes and the code revision - and every batch id is recorded as soon as it is known,
its requests' ids being the batch custom_ids. A batch answer is validated against the
request archived when it was sent, never a request rebuilt from today's code. A request
without an attempt is unresolved: a recorded batch is collected on the next run instead
of being sent again; a live request whose run ended before its answer was stored cannot
be fetched again and is closed by hand (``close_unresolved``), never silently resent. An
annotation and the attempt that produced it are stored in one transaction; an answer
that is not JSON keeps its raw text.

The evidence bundle carries each reference's status (an unavailable reference says why,
with no value), each bar's ``complete`` flag (every minute present once, nq_conv_v5) and
the snapshot's completeness (overnight coverage, ATRs, the Long MA history), so Claude
sees an unavailable input as unavailable rather than reconstructing it.

Validation, locally and before anything is stored: the whole answer against the
output JSON schema (types, enums, required and unknown keys - a boolean is not a
Chop Score), then status classified exactly when there is a value, a classified field
with no reason, at least one evidence id and a basis, an unavailable one with a
reason, every evidence id inside the bundle, and the answer from LLM_MODEL itself -
an answer a fallback model served is kept as an attempt, not as an annotation of this
protocol. Event Risk, Event Notes, Higher-Timeframe Bias and the price location come
from the application's rules (EV-v1, HTB-v1, P1 section 7), as in the rule-based protocol. A snapshot holding anything
after its cutoff is never sent: it is stored as contaminated without a request.
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, Optional, Tuple

from contracts import nq_preopen as pre
from contracts.nq_prompt_v2 import canonical_json
from database import journal_store as store
from forecaster import structure_rules as sr
from forecaster.provenance import code_revision

FALLBACK_BETA = "server-side-fallback-2026-07-01"
FINAL_2M_BARS = 45


def _bar_row(b: sr.Bar) -> list:
    return [b.id, b.o, b.h, b.l, b.c, b.complete]


def _sha(value) -> str:
    return hashlib.sha256((value if isinstance(value, str) else canonical_json(value)).encode()).hexdigest()


def _r2(x) -> Optional[float]:
    x = float(x)
    return None if math.isnan(x) else round(x, 2)


def evidence_bundle(snapshot: Dict[str, Any]) -> Tuple[Dict[str, Any], set]:
    """``(bundle, ids)``: the evidence of one snapshot and the set of its evidence ids."""
    p = snapshot["payload"]
    cutoff = sr._ts(p["cutoff"]["input_cutoff_at"])
    on_start = sr._ts(p["schedule"]["overnight_start_at"])
    refs = {}
    for name, ref in (p.get("references") or {}).items():
        if name.startswith("_"):
            continue
        refs[f"ref:{name}"] = ({"value": float(ref["value"]), "status": "valid"} if ref.get("status") == "valid"
                               else {"value": None, "status": ref.get("status"), "detail": ref.get("detail")})
    bars5 = [b for b in sr._bars(p, "5m") if b.start >= on_start]
    bars15 = [b for b in sr._bars(p, "15m") if b.start >= on_start]
    bars2 = [b for b in sr._bars(p, "2m") if b.start >= on_start]
    final2 = []
    if bars2:
        ma = sr.moving_averages(bars2)
        for i in range(max(0, len(bars2) - FINAL_2M_BARS), len(bars2)):
            final2.append(_bar_row(bars2[i]) + [_r2(ma["tema"][i]), _r2(ma["ema_trigger"][i]),
                                                _r2(ma["ema_trend"][i])])
    swings = [{"bar": s.bar.id, "kind": s.kind, "price": s.price,
               "confirmed_at": s.confirmed_at.strftime("%Y-%m-%dT%H:%MZ")} for s in sr.swings(bars5)]
    cov = (p.get("bars") or {}).get("coverage") or {}
    atr = p.get("atr") or {}
    long_ma = (p.get("moving_averages") or {}).get("long_ma_at_cutoff") or {}
    columns = ["id", "open", "high", "low", "close", "complete"]
    bundle = {
        "session": {"date": p["identity"]["session_date"], "contract": p["identity"].get("local_symbol"),
                    "overnight_start": on_start.strftime("%Y-%m-%dT%H:%MZ"),
                    "cutoff": cutoff.strftime("%Y-%m-%dT%H:%MZ"), "cutoff_et": p["cutoff"].get("cutoff_et"),
                    "T": (p.get("thresholds") or {}).get("T"), "units": "index points; times UTC"},
        "completeness": {
            "overnight_minutes": {"expected": cov.get("expected_minutes"), "present": cov.get("minutes"),
                                  "complete": cov.get("complete"), "duplicates": cov.get("duplicates")},
            "daily_atr": (atr.get("daily") or {}).get("status"), "two_minute_atr": (atr.get("two_minute") or {}).get(
                "status"),
            "long_ma_history": long_ma.get("status"),
            "rule": "a value of an incomplete window is unavailable; a bar with complete=false is missing minutes",
        },
        "references": refs,
        "bars_5m": {"columns": columns, "rows": [_bar_row(b) for b in bars5]},
        "bars_15m": {"columns": columns, "rows": [_bar_row(b) for b in bars15]},
        "bars_2m_final": {"columns": columns + ["tema14_sma3", "ema14_sma3", "ema100"], "rows": final2},
        "swings_5m": swings,
    }
    return bundle, bundle_ids(bundle)


def bundle_ids(bundle: Dict[str, Any]) -> set:
    """The evidence ids of a bundle: its references and its bars."""
    return set(bundle["references"]) | {r[0] for k in ("bars_5m", "bars_15m", "bars_2m_final")
                                        for r in bundle[k]["rows"]}


def request_ids(params: Dict[str, Any]) -> set:
    """The evidence ids of an archived request - the bundle that was actually sent."""
    return bundle_ids(json.loads(params["messages"][0]["content"].split("\n", 1)[1]))


def _system_prompt() -> str:
    with open(pre.LLM_PROMPT, encoding="utf-8") as f:
        return f.read()


def build_request(snapshot: Dict[str, Any]) -> Tuple[Dict[str, Any], str, set]:
    """``(params, request_hash, evidence ids)`` of one snapshot's Messages API request."""
    bundle, ids = evidence_bundle(snapshot)
    params = {
        "model": pre.LLM_MODEL,
        "max_tokens": pre.LLM_MAX_TOKENS,
        # the system prompt is the same for every session: cached
        "system": [{"type": "text", "text": _system_prompt(), "cache_control": {"type": "ephemeral"}}],
        "messages": [{"role": "user", "content": "Evidence bundle (JSON):\n" + canonical_json(bundle)}],
        "output_config": {"effort": pre.LLM_EFFORT,
                          "format": {"type": "json_schema", "schema": pre.llm_output_schema()}},
    }
    return params, hashlib.sha256(canonical_json(params).encode()).hexdigest(), ids


def _ledger_record(snapshot: Dict[str, Any], params: Dict[str, Any], request_hash: str, mode: str) -> Dict[str, Any]:
    return {"snapshot_id": snapshot["snapshot_id"], "protocol_version": pre.LLM_PROTOCOL_VERSION,
            "model": pre.LLM_MODEL, "request": params, "request_hash": request_hash,
            "prompt_sha256": _sha(params["system"][0]["text"]),
            "schema_sha256": _sha(params["output_config"]["format"]["schema"]),
            "evidence_sha256": _sha(params["messages"][0]["content"]), "code_revision": code_revision(),
            "mode": mode}


def validate(answer: Any, ids: set) -> Optional[str]:
    """The first problem with Claude's answer (see the module docstring), or None."""
    from jsonschema import Draft202012Validator
    from jsonschema.exceptions import best_match
    e = best_match(Draft202012Validator(pre.llm_output_schema()).iter_errors(answer))
    if e is not None:
        return f"schema: {'/'.join(str(p) for p in e.absolute_path) or '(answer)'}: {e.message}"
    for name in pre.LLM_FIELDS:
        f = answer["fields"][name]
        if (f["value"] is None) != (f["status"] == "unavailable"):
            return f"{name}: status {f['status']} does not fit value {f['value']!r}"
        if f["status"] == "unavailable" and not (f["reason"] or "").strip():
            return f"{name}: unavailable without a reason"
        if f["status"] == "classified":
            if f["reason"] is not None:
                return f"{name}: classified with a reason ({f['reason']!r}); the prompt asks for null"
            if not f["evidence_ids"]:
                return f"{name}: classified without evidence ids"
            if not f["basis"].strip():
                return f"{name}: classified without a basis"
        unknown = [i for i in f["evidence_ids"] if i not in ids]
        if unknown:
            return f"{name}: evidence id(s) not in the bundle: {', '.join(unknown[:3])}"
    return None


def to_annotation(snapshot: Dict[str, Any], answer: Dict[str, Any], model: str) -> Dict[str, Any]:
    """Claude's answer as an annotation of the protocol, with the application's Event Risk and price location."""
    p = snapshot["payload"]
    fields = {name: {"value": f["value"], "status": f["status"], "reason": f["reason"],
                     "evidence_ids": list(dict.fromkeys(f["evidence_ids"])), "basis": f["basis"]}
              for name, f in answer["fields"].items()}
    fields["Event Risk"], fields["Event Notes"] = sr.event_risk(p)
    fields["Higher-Timeframe Bias"] = sr.higher_timeframe_bias(p)
    cp = (p.get("references") or {}).get("cutoff_price") or {}
    close = float(cp["value"]) if cp.get("status") == "valid" else None
    return sr.seal({
        "protocol_version": pre.LLM_PROTOCOL_VERSION, "annotator": "llm", "integrity_status": "ok",
        "fields": {name: fields[name] for name in pre.FIELDS},
        "price_location": sr.price_location(p.get("references") or {}, close),
        "measurements": {"model": model, "contradictions": answer.get("contradictions") or []},
    })


def _contaminated(snapshot: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    p = snapshot["payload"]
    bad = sr.after_cutoff(p, sr._ts(p["cutoff"]["input_cutoff_at"]))
    if not bad:
        return None
    return sr.seal({"protocol_version": pre.LLM_PROTOCOL_VERSION, "annotator": "llm",
                    "integrity_status": "contaminated", "fields": {}, "price_location": {},
                    "measurements": {"model": pre.LLM_MODEL, "after_cutoff": bad[:20], "not_sent": True}})


def _attempt(snapshot_id: str, request_id: str, request_hash: str, started_at: datetime, **kw) -> Dict[str, Any]:
    return {"snapshot_id": snapshot_id, "protocol_version": pre.LLM_PROTOCOL_VERSION, "model": pre.LLM_MODEL,
            "request_id": request_id, "request_hash": request_hash, "started_at": started_at,
            "finished_at": datetime.now(timezone.utc), **kw}


def handle_message(conn, snapshot: Dict[str, Any], message, request_hash: str, ids: set,
                   started_at: datetime, request_id: str) -> Dict[str, Any]:
    """Stores the attempt for one response to ledger request ``request_id`` (and the annotation when valid);
    returns the attempt record."""
    usage = None if getattr(message, "usage", None) is None else json.loads(message.usage.to_json())
    attempt = _attempt(snapshot["snapshot_id"], request_id, request_hash, started_at,
                       model=getattr(message, "model", pre.LLM_MODEL), usage=usage)
    text = next((b.text for b in message.content if getattr(b, "type", None) == "text"), None)
    answer = None
    if message.stop_reason == "refusal":
        attempt.update(status="refused", error=str(getattr(message, "stop_details", None)), raw_text=text)
    elif message.stop_reason == "max_tokens":
        attempt.update(status="invalid", error="output cut off at max_tokens", raw_text=text)
    else:
        try:
            answer = json.loads(text or "")
        except json.JSONDecodeError as e:
            attempt.update(status="invalid", error=f"not JSON: {e}", raw_text=text)
        if answer is None and "status" not in attempt:
            attempt.update(status="invalid", error="an empty answer (JSON null)", raw_text=text)
    annotation = None
    if answer is not None:
        attempt["response"] = answer
        problem = None
        if attempt["model"] != pre.LLM_MODEL:
            problem = f"served by {attempt['model']}, not {pre.LLM_MODEL} (a fallback answer is not this protocol)"
        elif isinstance(answer, dict) and answer.get("integrity_status") == "contaminated":
            problem = "Claude reported contamination the application's check does not find"
        else:
            problem = validate(answer, ids)
        if problem:
            attempt.update(status="invalid", error=problem)
        else:
            annotation = to_annotation(snapshot, answer, attempt["model"])
    with conn:                                     # an annotation and its attempt together, or neither
        if annotation is not None:
            attempt["annotation_id"], _ = store.save_annotation(conn, snapshot["snapshot_id"], annotation,
                                                                model=attempt["model"])
            attempt["status"] = "ok"
        attempt["attempt_id"] = store.save_annotation_attempt(conn, attempt)
    return attempt


def annotate_live(conn, client, snapshot: Dict[str, Any]) -> Dict[str, Any]:
    """One request now, with the server-side refusal fallback; returns the stored attempt (or the contaminated
    annotation's record when nothing was sent)."""
    contaminated = _contaminated(snapshot)
    if contaminated is not None:
        annotation_id, _ = store.save_annotation(conn, snapshot["snapshot_id"], contaminated, model=pre.LLM_MODEL)
        return {"status": "contaminated", "annotation_id": annotation_id}
    params, request_hash, ids = build_request(snapshot)
    request_id = store.save_inference_request(conn, _ledger_record(snapshot, params, request_hash, "live"))
    started = datetime.now(timezone.utc)
    try:
        message = client.beta.messages.create(**params, betas=[FALLBACK_BETA], fallbacks="default")
    except Exception as e:  # the attempt is kept; nothing is filled in from another run
        attempt = _attempt(snapshot["snapshot_id"], request_id, request_hash, started, status="error",
                           error=f"{type(e).__name__}: {e}")
        attempt["attempt_id"] = store.save_annotation_attempt(conn, attempt)
        return attempt
    return handle_message(conn, snapshot, message, request_hash, ids, started, request_id)


def submit_batch(conn, client, snapshots: Iterable[Dict[str, Any]]) -> Optional[str]:
    """
    Submits one request per snapshot to the Batch API (no fallback: the Batch API rejects it). Each request is in
    the ledger before the batch is created, its id the custom_id; the batch id is recorded the moment it is known.
    Returns the batch id - or None when nothing was sent, or the batch was not created (its requests are then
    closed as error attempts).
    """
    from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
    from anthropic.types.messages.batch_create_params import Request
    requests, ledger = [], []
    for snap in snapshots:
        contaminated = _contaminated(snap)
        if contaminated is not None:                   # never sent, stored as such (as annotate_live does)
            store.save_annotation(conn, snap["snapshot_id"], contaminated, model=pre.LLM_MODEL)
        else:
            params, request_hash, _ = build_request(snap)
            request_id = store.save_inference_request(conn, _ledger_record(snap, params, request_hash, "batch"))
            requests.append(Request(custom_id=request_id, params=MessageCreateParamsNonStreaming(**params)))
            ledger.append((request_id, snap["snapshot_id"], request_hash))
    if not requests:
        return None
    submitted = datetime.now(timezone.utc)
    try:
        batch_id = client.messages.batches.create(requests=requests).id
    except Exception as e:
        for request_id, snapshot_id, request_hash in ledger:
            store.save_annotation_attempt(conn, _attempt(snapshot_id, request_id, request_hash, submitted,
                                                         status="error",
                                                         error=f"batch not created: {type(e).__name__}: {e}"))
        return None
    store.save_inference_batch(conn, batch_id, [r for r, _, _ in ledger], submitted)
    return batch_id


def collect_batch(conn, client, batch_id: str) -> Dict[str, int]:
    """
    Stores every result of an ended, recorded batch as an attempt (and its annotation when valid), skipping requests
    already answered, so an interrupted collection can simply run again. Each answer is validated against the
    request archived when it was sent (its evidence ids), whatever the code does today. A request the batch has no
    result for is closed as an error. Returns status counts.
    """
    ledger = {r["request_id"]: r for r in store.inference_batch_requests(conn, batch_id)}
    counts: Dict[str, int] = {}

    def count(status):
        counts[status] = counts.get(status, 0) + 1

    seen = set()
    for result in client.messages.batches.results(batch_id):
        req = ledger.get(result.custom_id)
        if req is None or req["answered"]:
            continue
        seen.add(result.custom_id)
        snap = store.get_snapshot(conn, req["snapshot_id"])
        if result.result.type == "succeeded":
            attempt = handle_message(conn, snap, result.result.message, req["request_hash"],
                                     request_ids(req["request"]), req["submitted_at"], req["request_id"])
        else:
            attempt = _attempt(snap["snapshot_id"], req["request_id"], req["request_hash"], req["submitted_at"],
                               status="error", error=f"batch result {result.result.type}")
            store.save_annotation_attempt(conn, attempt)
        count(attempt["status"])
    for request_id, req in ledger.items():
        if not req["answered"] and request_id not in seen:
            store.save_annotation_attempt(conn, _attempt(req["snapshot_id"], request_id, req["request_hash"],
                                                         req["submitted_at"], status="error",
                                                         error="no result for this request in the ended batch"))
            count("error")
    return counts


def close_unresolved(conn, requests: Iterable[Dict[str, Any]]) -> int:
    """Closes requests whose answer can never be fetched (a live request, or a batch whose id was not recorded,
    from a run that ended mid-request) as error attempts, so their snapshots can be requested again. The user's
    decision: such a request may have been billed."""
    n = 0
    for req in requests:
        store.save_annotation_attempt(conn, _attempt(
            req["snapshot_id"], req["request_id"], req["request_hash"], req["created_at"], status="error",
            error=f"no answer stored: the {req['mode']} request's run ended before it was recorded; it may have "
                  f"been billed"))
        n += 1
    return n


def estimate(snapshots: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """A rough size and cost estimate before spending anything: ~3.5 characters per token, Opus 5.5 list prices
    ($4 / $20 per million input / output tokens; the Batch API halves them), ~4,000 output tokens per session."""
    snaps = list(snapshots)
    system = len(_system_prompt())
    chars = sum(len(canonical_json(evidence_bundle(s)[0])) for s in snaps) + system * len(snaps)
    tokens_in = chars / 3.5
    tokens_out = 4000 * len(snaps)
    usd = tokens_in / 1e6 * 4 + tokens_out / 1e6 * 20
    return {"sessions": len(snaps), "input_tokens": int(tokens_in), "output_tokens": tokens_out,
            "usd_live": round(usd, 2), "usd_batch": round(usd / 2, 2)}


def batch_poll_delay(started: datetime) -> float:
    """Seconds to wait before polling a batch again: every 30 s for the first 10 minutes, then every 2 minutes."""
    return 30.0 if datetime.now(timezone.utc) - started < timedelta(minutes=10) else 120.0
