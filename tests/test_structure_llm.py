# tests/test_structure_llm.py
"""The Claude structure annotation (forecaster/structure_llm.py, nq_structure_llm_v3) - without the API."""

import json

from contracts import nq_preopen as pre
from forecaster import structure_llm as llm
from forecaster import structure_rules as sr
from tests.preopen_paths import MINUTES, piecewise, snapshot

V = piecewise([(0, 300), (450, 100), (650, 220), (720, 170), (MINUTES, 260)], wiggle=5, period=60)


def answer_from_rules(snap):
    """A valid answer: the rule-based values in Claude's schema."""
    a = sr.annotate(snap)
    fields = {}
    for name in pre.LLM_FIELDS:
        f = a["fields"][name]
        fields[name] = {"value": f["value"], "status": "classified" if f["value"] is not None else "unavailable",
                        "reason": None if f["value"] is not None else f["reason"],
                        "evidence_ids": f["evidence_ids"], "basis": f["basis"] or "-"}
    return {"integrity_status": "ok", "contradictions": [], "fields": fields}


def test_the_request_pins_model_prompt_schema_and_evidence():
    snap = snapshot(V, T=4)
    params, request_hash, ids = llm.build_request(snap)
    assert params["model"] == pre.LLM_MODEL and params["output_config"]["effort"] == pre.LLM_EFFORT
    assert params["output_config"]["format"]["schema"] == pre.llm_output_schema()
    assert params["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert "P1 section 4 (verbatim)" in params["system"][0]["text"]
    bundle = json.loads(params["messages"][0]["content"].split("\n", 1)[1])
    assert len(bundle["bars_2m_final"]["rows"]) == llm.FINAL_2M_BARS and bundle["swings_5m"]
    assert {"ref:cutoff_price"} <= ids and all(r[0] in ids for r in bundle["bars_5m"]["rows"])
    assert llm.build_request(snap)[1] == request_hash                       # deterministic


def test_validation():
    snap = snapshot(V, T=4)
    _, _, ids = llm.build_request(snap)
    good = answer_from_rules(snap)
    assert llm.validate(good, ids) is None
    bad = json.loads(json.dumps(good))
    bad["fields"]["Overnight Structure"]["evidence_ids"] = ["bar:5m:2030-01-01T00:00Z"]
    assert "not in the bundle" in llm.validate(bad, ids)
    bad = json.loads(json.dumps(good))
    bad["fields"]["Long MA Slope"].update(value=None, status="classified")
    assert "does not fit" in llm.validate(bad, ids)
    bad = json.loads(json.dumps(good))
    bad["fields"]["Premarket Pattern"]["value"] = "Breakout"
    assert "is not one of" in llm.validate(bad, ids)


def test_an_annotation_keeps_the_applications_event_risk_and_price_location():
    snap = snapshot(V, T=4)
    a = llm.to_annotation(snap, answer_from_rules(snap), pre.LLM_MODEL)
    rules = sr.annotate(snap)
    assert a["annotator"] == "llm" and a["protocol_version"] == pre.LLM_PROTOCOL_VERSION
    assert a["fields"]["Event Risk"] == rules["fields"]["Event Risk"] and a["price_location"] == rules["price_location"]
    assert a["fields"]["Overnight Structure"]["value"] == "V-reversal" and list(a["fields"]) == list(pre.FIELDS)


def test_validation_is_strict_where_the_schema_and_the_prompt_are():
    snap = snapshot(V, T=4)
    _, _, ids = llm.build_request(snap)
    good = answer_from_rules(snap)

    def problem(change):
        bad = json.loads(json.dumps(good))
        change(bad)
        return llm.validate(bad, ids)

    def field(**kw):
        return lambda a: a["fields"]["Overnight Structure"].update(**kw)

    assert "classified without evidence ids" in problem(field(evidence_ids=[]))
    assert problem(field(status="maybe")).startswith("schema: fields/Overnight Structure/status")
    assert problem(lambda a: a["fields"]["Chop Score"].update(value=True)).startswith("schema: fields/Chop Score")
    assert problem(lambda a: a["fields"]["Chop Score"].update(value="1")).startswith("schema: fields/Chop Score")
    assert problem(field(extra="x")).startswith("schema: fields/Overnight Structure")          # unknown key
    assert problem(lambda a: a.update(integrity_status="fine")).startswith("schema: integrity_status")
    assert problem(lambda a: a.pop("contradictions")).startswith("schema: (answer)")
    assert "classified with a reason" in problem(field(reason="because"))
    assert "without a basis" in problem(field(basis="  "))
    assert "unavailable without a reason" in problem(field(value=None, status="unavailable", reason=" "))
    assert llm.validate(["not", "an", "object"], ids).startswith("schema: (answer)")
    # an unavailable field may cite nothing
    assert problem(field(value=None, status="unavailable", reason="too few bars", evidence_ids=[])) is None
