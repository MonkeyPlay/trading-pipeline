# forecaster/structure_llm.py
"""
The Claude structure annotation (protocol nq_structure_llm_v1, Appendix A, A1):
the replacement for the rule-based annotation (forecaster/structure_rules.py) once
the Claude API is available. Same output shape (contracts.nq_preopen.ANNOTATION_SCHEMA),
same store, its own protocol version - the matcher never mixes the two.

  evidence_bundle(snapshot)   the frozen evidence Claude sees, every item with an id:
                              the references, the overnight 5m and 15m bars, the last
                              45 2m bars with the three moving averages, and the
                              confirmed 2/2 swing points on 5m bars
  build_request(snapshot)     the Messages API request: the runtime prompt
                              (prompts/runtime/structure_annotation_v1.md) as the
                              system prompt, the bundle as the user message, the
                              structure_annotation schema as the required output
  annotate_live(conn, snap)   one request now (server-side refusal fallback on); the
                              attempt is always stored, the annotation only when valid
  submit_batch / collect_batch  the historical backfill through the Batch API (half
                              price; no fallback there)

Validation: values inside the vocabularies, null exactly when unavailable (with a
reason), every evidence id inside the bundle, and the answer from LLM_MODEL itself -
an answer a fallback model served is kept as an attempt, not as an annotation of this
protocol. Event Risk, Event Notes and the price location come from the application's
rules (EV-v1, P1 section 7), as in the rule-based protocol. A snapshot holding anything
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

FALLBACK_BETA = "server-side-fallback-2026-07-01"
FINAL_2M_BARS = 45


def _bar_row(b: sr.Bar) -> list:
    return [b.id, b.o, b.h, b.l, b.c]


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
        refs[f"ref:{name}"] = ({"value": float(ref["value"])} if ref.get("status") == "valid"
                               else {"value": None, "status": ref.get("status")})
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
    bundle = {
        "session": {"date": p["identity"]["session_date"], "contract": p["identity"].get("local_symbol"),
                    "overnight_start": on_start.strftime("%Y-%m-%dT%H:%MZ"),
                    "cutoff": cutoff.strftime("%Y-%m-%dT%H:%MZ"), "cutoff_et": p["cutoff"].get("cutoff_et"),
                    "T": (p.get("thresholds") or {}).get("T")},
        "references": refs,
        "bars_5m": {"columns": ["id", "open", "high", "low", "close"], "rows": [_bar_row(b) for b in bars5]},
        "bars_15m": {"columns": ["id", "open", "high", "low", "close"], "rows": [_bar_row(b) for b in bars15]},
        "bars_2m_final": {"columns": ["id", "open", "high", "low", "close", "tema14_sma3", "ema14_sma3", "ema100"],
                          "rows": final2},
        "swings_5m": swings,
    }
    ids = set(refs) | {b.id for b in bars5 + bars15 + bars2[-FINAL_2M_BARS:]}
    return bundle, ids


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


def validate(answer: Dict[str, Any], ids: set) -> Optional[str]:
    """The first problem with Claude's answer, or None."""
    fields = answer.get("fields") or {}
    if set(fields) != set(pre.LLM_FIELDS):
        return f"fields {sorted(fields)} are not the protocol's"
    for name, f in fields.items():
        allowed = pre.FIELDS[name]["values"]
        if f["value"] is not None and f["value"] not in allowed:
            return f"{name}: {f['value']!r} is not an allowed value"
        if (f["value"] is None) != (f["status"] == "unavailable"):
            return f"{name}: status {f['status']} does not fit value {f['value']!r}"
        if f["value"] is None and not f.get("reason"):
            return f"{name}: unavailable without a reason"
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


def handle_message(conn, snapshot: Dict[str, Any], message, request_hash: str, ids: set,
                   started_at: datetime) -> Dict[str, Any]:
    """Stores the attempt for one response (and the annotation when valid); returns the attempt record."""
    usage = None if getattr(message, "usage", None) is None else json.loads(message.usage.to_json())
    attempt = {"snapshot_id": snapshot["snapshot_id"], "protocol_version": pre.LLM_PROTOCOL_VERSION,
               "model": getattr(message, "model", pre.LLM_MODEL), "request_hash": request_hash, "usage": usage,
               "started_at": started_at, "finished_at": datetime.now(timezone.utc)}
    text = next((b.text for b in message.content if getattr(b, "type", None) == "text"), None)
    answer = None
    if message.stop_reason == "refusal":
        attempt.update(status="refused", error=str(getattr(message, "stop_details", None)))
    elif message.stop_reason == "max_tokens":
        attempt.update(status="invalid", error="output cut off at max_tokens")
    else:
        try:
            answer = json.loads(text or "")
        except json.JSONDecodeError as e:
            attempt.update(status="invalid", error=f"not JSON: {e}")
    if answer is not None:
        attempt["response"] = answer
        problem = None
        if attempt["model"] != pre.LLM_MODEL:
            problem = f"served by {attempt['model']}, not {pre.LLM_MODEL} (a fallback answer is not this protocol)"
        elif answer.get("integrity_status") == "contaminated":
            problem = "Claude reported contamination the application's check does not find"
        else:
            problem = validate(answer, ids)
        if problem:
            attempt.update(status="invalid", error=problem)
        else:
            annotation = to_annotation(snapshot, answer, attempt["model"])
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
    started = datetime.now(timezone.utc)
    try:
        message = client.beta.messages.create(**params, betas=[FALLBACK_BETA], fallbacks="default")
    except Exception as e:  # the attempt is kept; nothing is filled in from another run
        attempt = {"snapshot_id": snapshot["snapshot_id"], "protocol_version": pre.LLM_PROTOCOL_VERSION,
                   "model": pre.LLM_MODEL, "request_hash": request_hash, "status": "error",
                   "error": f"{type(e).__name__}: {e}", "started_at": started,
                   "finished_at": datetime.now(timezone.utc)}
        attempt["attempt_id"] = store.save_annotation_attempt(conn, attempt)
        return attempt
    return handle_message(conn, snapshot, message, request_hash, ids, started)


def submit_batch(client, snapshots: Iterable[Dict[str, Any]]) -> str:
    """Submits one request per snapshot to the Batch API (no fallback: the Batch API rejects it); returns its id."""
    from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
    from anthropic.types.messages.batch_create_params import Request
    requests = []
    for snap in snapshots:
        if _contaminated(snap) is None:
            params, _, _ = build_request(snap)
            requests.append(Request(custom_id=snap["snapshot_id"], params=MessageCreateParamsNonStreaming(**params)))
    return client.messages.batches.create(requests=requests).id


def collect_batch(conn, client, batch_id: str, snapshots: Dict[str, Dict[str, Any]],
                  submitted_at: datetime) -> Dict[str, int]:
    """Stores every result of an ended batch as an attempt (and its annotation when valid); returns status counts."""
    counts: Dict[str, int] = {}
    for result in client.messages.batches.results(batch_id):
        snap = snapshots[result.custom_id]
        params, request_hash, ids = build_request(snap)
        if result.result.type == "succeeded":
            attempt = handle_message(conn, snap, result.result.message, request_hash, ids, submitted_at)
        else:
            attempt = {"snapshot_id": snap["snapshot_id"], "protocol_version": pre.LLM_PROTOCOL_VERSION,
                       "model": pre.LLM_MODEL, "request_hash": request_hash, "status": "error",
                       "error": f"batch result {result.result.type}", "started_at": submitted_at,
                       "finished_at": datetime.now(timezone.utc)}
            store.save_annotation_attempt(conn, attempt)
        counts[attempt["status"]] = counts.get(attempt["status"], 0) + 1
    return counts


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
