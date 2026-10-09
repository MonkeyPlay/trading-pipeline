# tests/test_llm_arms.py
"""Arms C and D (forecaster/llm_arms.py) and the dashboard approvals (forecaster/approvals.py) - no database, no
Claude."""

import json
import re
from datetime import datetime, timedelta, timezone

import pytest

from contracts import nq_forecast as fc
from contracts import nq_preopen as pre
from contracts import nq_prompt_v2 as defs
from contracts.nq_prompt_v2 import canonical_json
from forecaster import approvals
from forecaster import llm_arms as la
from forecaster import structure_llm as sl
from forecaster import structure_rules as sr
from tests.preopen_paths import piecewise, snapshot

V = piecewise([(0, 100.0), (300, 140.0), (600, 110.0), (930, 125.0)])
TARGETS = [t for _, t in fc.FORECAST_TARGETS]


def test_an_approval_is_used_once_within_its_time_and_for_its_scope(tmp_path):
    scope = {"command": "llm-forecast", "sessions": 1, "arms": "CD", "profile": "research_0929"}
    token = approvals.issue({**scope, "max_requests": 2}, str(tmp_path))
    approved, why = approvals.redeem(token, scope, str(tmp_path))
    assert approved["max_requests"] == 2 and "at most 2" in why
    assert approvals.redeem(token, scope, str(tmp_path)) == (None, "the approval does not exist or was already used")
    other = approvals.issue({**scope, "max_requests": 2}, str(tmp_path))
    assert approvals.redeem(other, {**scope, "sessions": 5}, str(tmp_path))[0] is None      # another scope
    assert approvals.redeem(other, scope, str(tmp_path))[0] is None                         # and it is used up
    late = approvals.issue({**scope, "max_requests": 2}, str(tmp_path))
    later = datetime.now(timezone.utc) + timedelta(minutes=approvals.TTL_MINUTES + 1)
    assert "older than" in approvals.redeem(late, scope, str(tmp_path), now=later)[1]
    assert approvals.redeem("../etc/passwd", scope, str(tmp_path))[0] is None


def test_the_blinded_bundle_names_no_date_contract_or_price():
    snap = snapshot(V, T=4)
    blind = la.blinded_bundle(snap)
    text = canonical_json(blind["bundle"])
    assert not re.search(r"\d{4}-\d{2}-\d{2}", text) and "contract" not in text
    plain, _ = sl.evidence_bundle(snap)
    assert set(blind["id_map"].values()) <= sl.bundle_ids(plain)                     # every id maps back
    assert all(re.fullmatch(r"bar:\d+m:\d\d:\d\d", i) for i in blind["id_map"])
    anchor, label = blind["anchor"]
    first_plain, first_blind = plain["bars_5m"]["rows"][0], blind["bundle"]["bars_5m"]["rows"][0]
    assert first_blind[1] == pytest.approx(first_plain[1] - anchor) and first_blind[0] == "bar:5m:18:00"
    assert blind["bundle"]["session"]["anchor"] == label


def test_arm_cs_request_asks_for_two_fields_only_and_maps_its_evidence_back():
    snap = snapshot(V, T=4)
    params, _, ids = sl.build_request(snap, la.RESTRICTED)
    schema = params["output_config"]["format"]["schema"]["properties"]["fields"]
    assert schema["required"] == pre.RESTRICTED_FIELDS == list(schema["properties"])
    assert params["system"][0]["text"].startswith("# Structure annotation, restricted")
    assert not re.search(r"\d{4}-\d{2}-\d{2}", params["messages"][0]["content"])
    bar = sorted(i for i in ids if i.startswith("bar:5m:"))[0]
    answer = {"integrity_status": "ok", "contradictions": [], "fields": {
        name: {"value": value, "status": "classified", "reason": None, "evidence_ids": [bar], "basis": "shape"}
        for name, value in zip(pre.RESTRICTED_FIELDS, ("Range", "Mixed"))}}
    assert sl.validate(answer, ids, la.RESTRICTED) is None
    annotation = la.restricted_annotation(snap, answer, pre.LLM_MODEL)
    rules = sr.annotate(snap)
    assert annotation["protocol_version"] == pre.RESTRICTED_PROTOCOL_VERSION
    assert annotation["fields"]["Overnight Structure"]["value"] == "Range"
    assert annotation["fields"]["Overnight Structure"]["evidence_ids"][0].startswith("bar:5m:20")   # the real id
    assert annotation["fields"]["5-Minute Trend"] == rules["fields"]["5-Minute Trend"]                # the rules'


def _pairs(probs):
    return [{"class": c, "p": v} for c, v in probs.items()]


def synthesis_answer(bundle, top_share=None, **overrides):
    """A valid synthesis of ``bundle`` in the flat schema: the baseline's class (else the first) most probable,
    exact decimals, one item per target."""
    preds = []
    for t in TARGETS:
        if bundle["eligibility"][t] != "eligible":
            preds.append({"target": t, "status": "unavailable", "predicted_class": "", "probabilities": [],
                          "reason": "not eligible", "supporting_evidence_ids": [], "conflicting_evidence_ids": [],
                          "departure": ""})
            continue
        vocab = bundle["targets"][t]
        cls = bundle["baseline"][f"baseline:{t}"]["predicted_class"] or vocab[0]
        top = top_share or f"{1 - 0.05 * (len(vocab) - 1):.3f}"
        preds.append({"target": t, "status": "predicted", "predicted_class": cls,
                      "probabilities": _pairs({c: (top if c == cls else "0.050") for c in vocab}), "reason": "",
                      "supporting_evidence_ids": [f"baseline:{t}"], "conflicting_evidence_ids": [],
                      "departure": ""})
    answer = {"integrity_status": "ok", "predictions": preds, "confidence": 2,
              "confidence_basis": "sparse analogues"}
    for key, value in overrides.items():
        answer[key] = value
    return answer


def _item(answer, target):
    return next(i for i in answer["predictions"] if i["target"] == target)


def _bundle(eligibility=None):
    targets = {t: list(defs.TARGETS[t]["labels"]) for t in TARGETS}
    return {"eligibility": {t: (eligibility or {}).get(t, "eligible") for t in TARGETS}, "targets": targets,
            "baseline": {f"baseline:{t}": {"predicted_class": targets[t][1]} for t in TARGETS}}


def _check(answer, bundle):
    ids = {f"baseline:{t}" for t in TARGETS}
    eligibility = {t: None if v == "eligible" else v for t, v in bundle["eligibility"].items()}
    base = {t: bundle["baseline"][f"baseline:{t}"]["predicted_class"] for t in TARGETS}
    return la.validate_synthesis(json.loads(json.dumps(answer)), ids, eligibility, base)


def test_a_synthesis_must_be_exact_eligible_grounded_and_explain_its_departures():
    bundle = _bundle()
    assert _check(synthesis_answer(bundle), bundle) is None
    bad = synthesis_answer(bundle, top_share="0.85")                          # sums to more than 1
    assert "sum to" in _check(bad, bundle)
    flipped = synthesis_answer(bundle)
    _item(flipped, "direction_15m")["predicted_class"] = "bullish"            # not the most probable
    assert "not the single most probable" in _check(flipped, bundle)
    departs = synthesis_answer(bundle)
    p = _item(departs, "direction_15m")
    p["predicted_class"] = "bullish"
    p["probabilities"] = _pairs({"bullish": "0.6", "bearish": "0.3", "neutral_band": "0.1"})
    assert "without a departure" in _check(departs, bundle)
    p["departure"] = "the overnight structure is a V-reversal, the analogues' is not"
    assert _check(departs, bundle) is None
    early = _bundle({"session_type_rth": "a standard-session target on an early close session"})
    assert "not eligible" in _check(synthesis_answer(bundle), early)
    assert _check(synthesis_answer(early), early) is None
    tie = synthesis_answer(bundle)
    _item(tie, "first_move_5m").update(status="tie", predicted_class="", reason="even",
                                       probabilities=_pairs({"up_first": "0.4", "down_first": "0.4", "neither": "0.2"}))
    assert _check(tie, bundle) is None
    ungrounded = synthesis_answer(bundle)
    _item(ungrounded, "direction_15m")["supporting_evidence_ids"] = ["analogue:99"]
    assert "not in the bundle" in _check(ungrounded, bundle)
    assert "reported contaminated" in _check(synthesis_answer(bundle, integrity_status="contaminated"), bundle)
    assert _check(synthesis_answer(bundle, confidence=None), bundle).startswith("schema: confidence")
    # what the flat schema cannot enforce: every target once, every class of its vocabulary once
    short = synthesis_answer(bundle)
    short["predictions"] = short["predictions"][1:]
    assert "missing opening_bias_30m" in _check(short, bundle)
    twice = synthesis_answer(bundle)
    twice["predictions"].append(dict(twice["predictions"][0]))
    assert "repeated opening_bias_30m" in _check(twice, bundle)
    doubled = synthesis_answer(bundle)
    _item(doubled, "direction_15m")["probabilities"] = _pairs({"bullish": "0.5", "bearish": "0.5"}) + [
        {"class": "bullish", "p": "0"}]
    assert "appears twice" in _check(doubled, bundle)
    unknown = synthesis_answer(bundle)
    _item(unknown, "direction_15m")["probabilities"] = _pairs({"bullish": "0.5", "bearish": "0.4", "choppy": "0.1"})
    assert "not over exactly its classes" in _check(unknown, bundle)
    half = synthesis_answer(bundle)
    _item(half, "direction_15m")["probabilities"][0]["p"] = ""
    assert "not a decimal" in _check(half, bundle)


def _unions(schema) -> int:
    """Parameters with a union type (anyOf / oneOf / a list of types): the API compiles at most 16."""
    if isinstance(schema, dict):
        own = int(any(k in schema for k in ("anyOf", "oneOf")) or isinstance(schema.get("type"), list))
        return own + sum(_unions(v) for v in schema.values())
    if isinstance(schema, list):
        return sum(_unions(v) for v in schema)
    return 0


def _properties(schema) -> int:
    """Named properties in a schema: the grammar the API compiles grows with them."""
    if isinstance(schema, dict):
        return len(schema.get("properties") or {}) + sum(_properties(v) for v in schema.values())
    if isinstance(schema, list):
        return sum(_properties(v) for v in schema)
    return 0


def test_the_schemas_sent_to_claude_stay_within_the_apis_union_limit():
    assert _unions(fc.synthesis_output_schema()) == 0
    # the synthesis v2 schema (per-target objects, 33 named probabilities: ~90 properties) compiled too large;
    # v3 is flat - one item shape, probabilities as pairs
    assert _properties(fc.synthesis_output_schema()) <= 16
    assert _unions(pre.restricted_output_schema()) <= 16
    # the full nine-field protocol (nq_structure_llm_v4) has 18 and would be refused: it needs a new version
    assert _unions(pre.llm_output_schema()) == 18


def test_long_requests_are_streamed_with_their_cap():
    calls = []

    class _Stream:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get_final_message(self):
            return "final"

    class _Messages:
        def stream(self, **kw):
            calls.append(kw)
            return _Stream()
    client = type("C", (), {"beta": type("B", (), {"messages": _Messages()})()})()
    params, _, _ = sl.build_request(snapshot(V, T=4), la.RESTRICTED)
    assert params["max_tokens"] == pre.RESTRICTED_MAX_TOKENS == 64000 and fc.SYNTHESIS_MAX_TOKENS == 64000
    assert sl.send(client, params) == "final"
    assert calls[0]["fallbacks"] == "default" and calls[0]["betas"] == [sl.FALLBACK_BETA]


def test_the_synthesis_is_a_registered_algorithm_on_its_own_schema():
    records = {r["version"]: r for r in fc.all_records()}
    d = records[fc.SYNTHESIS_VERSION]["definition"]
    assert d["model"] == pre.LLM_MODEL and d["effort"] == "medium" and d["schema_version"] == "nq_forecast_schema_v2"
    assert pre.restricted_record()["definition"]["effort"] == "medium"
    assert pre.llm_record()["definition"]["effort"] == pre.LLM_EFFORT == "xhigh"        # the full protocol: unchanged
    params, _, _ = sl.build_request(snapshot(V, T=4), la.RESTRICTED)
    assert params["output_config"]["effort"] == "medium"
    assert la.build_synthesis_request({"x": 1})[0]["output_config"]["effort"] == "medium"
    assert "judgement" in records[fc.SYNTHESIS_SCHEMA_VERSION]["definition"]["estimation_statuses"]
    assert "judgement" not in records[fc.FORECAST_SCHEMA_VERSION]["definition"]["estimation_statuses"]
    assert fc.RULE_ALGORITHMS == (fc.BASELINE_VERSION, fc.PRIOR_VERSION)          # catch-up never issues C or D
    assert fc.arm_of(fc.SYNTHESIS_VERSION) == "D" and fc.arm_of(fc.RESTRICTED_VERSION) == "C"


def test_grading_scores_each_arm_against_what_happened_and_the_benchmark():
    from forecaster.grading import current_runs, grade
    run = lambda algo, status="issued", **preds: {"algorithm_version": algo, "lifecycle_status": status,
                                                  "predictions": preds}
    p = lambda cls, dist, status="predicted": {"status": status, "predicted_label": cls if status == "predicted"
                                               else None, "distribution": dist}
    runs = [run(fc.SYNTHESIS_VERSION, "invalid"),                                   # newest, not issued
            run(fc.SYNTHESIS_VERSION, direction_15m=p("bullish", {"bullish": "3/5", "bearish": "1/5",
                                                                  "neutral_band": "1/5"})),
            run(fc.BASELINE_VERSION, direction_15m=p("bearish", {"bullish": "1/5", "bearish": "3/5",
                                                                 "neutral_band": "1/5"})),
            run(fc.PRIOR_VERSION, direction_15m=p("bullish", {"bullish": "2/5", "bearish": "2/5",
                                                              "neutral_band": "1/5"}),
                first_move_5m=p(None, None, "unavailable"))]
    current = current_runs(runs)
    assert sorted(current) == ["A", "B", "D"] and current["D"]["lifecycle_status"] == "issued"
    g = grade(current, {"direction_15m": {"label": "bullish"}, "first_move_5m": {"label": "up_first"},
                        "opening_type_15m": {"label": None, "reason": "ambiguous_intrabar"}})
    assert [t for _, t in g["targets"]] == ["first_move_5m", "direction_15m"]
    assert ("Most Likely Opening Type", "opening_type_15m", "ambiguous_intrabar") in g["ungraded"]
    a, b, d = g["arms"]["A"], g["arms"]["B"], g["arms"]["D"]
    assert a["cells"]["direction_15m"] == {"p": 0.4, "predicted": "bullish", "hit": True}
    assert a["cells"]["first_move_5m"]["p"] is None and a["graded"] == 1
    assert (b["hits"], d["hits"]) == (0, 1) and d["mean_p"] == pytest.approx(0.6)
    assert d["vs_benchmark"] == pytest.approx(0.2) and b["vs_benchmark"] == pytest.approx(-0.2)
    assert g["chance"]["direction_15m"] == pytest.approx(1 / 3)


def test_before_the_outcome_the_arms_are_compared_with_the_benchmark():
    from forecaster.grading import compare
    p = lambda cls, dist, status="predicted": {"status": status, "predicted_label": cls if status == "predicted"
                                               else None, "distribution": dist}
    runs = {"A": {"predictions": {"direction_15m": p("bullish", {"bullish": "1/2", "bearish": "1/4",
                                                                 "neutral_band": "1/4"})}},
            "B": {"predictions": {"direction_15m": p("bearish", {"bullish": "1/5", "bearish": "3/5",
                                                                 "neutral_band": "1/5"})}},
            "D": {"predictions": {"direction_15m": p(None, {"bullish": "2/5", "bearish": "2/5",
                                                            "neutral_band": "1/5"}, "ambiguous_prediction"),
                                  "first_move_5m": p(None, None, "unavailable")}}}
    g = compare(runs)
    assert [t for _, t in g["targets"]] == ["direction_15m"]                 # no arm forecast anything else
    a, b, d = (g["arms"][k]["cells"]["direction_15m"] for k in "ABD")
    assert (a["cls"], a["p"], a["agrees"]) == ("bullish", 0.5, None)          # the benchmark itself
    assert (b["cls"], b["p"], b["agrees"]) == ("bearish", 0.6, False)
    assert d["cls"] is None and d["top"] == ["bullish", "bearish"] and d["agrees"] is None
    assert g["arms"]["D"]["cells"]["first_move_5m"]["p"] is None
    assert (g["arms"]["B"]["agrees"], g["arms"]["B"]["comparable"]) == (0, 1)


def test_chosen_days_are_the_scheduled_sessions_among_them():
    assert la.target_sessions(1, days=["2026-10-02", "2026-10-03", "2026-10-02", "2026-09-30", "not-a-day"]) == \
        ["2026-09-30", "2026-10-02"]                                         # Saturday, a repeat and junk dropped
    assert la.target_sessions(5, days=[]) == []                              # chosen, but none: nothing


def test_the_llm_job_names_its_days():
    from dashboard.jobs import llm_command
    cmd = llm_command(["2026-10-01", "2026-10-02"], "CD", "ab12")
    assert cmd[-8:] == ["--date", "2026-10-01", "--date", "2026-10-02", "--arms", "CD", "--approval", "ab12"]
    assert "--approval" not in llm_command(["2026-10-01"], "C")


def test_each_arm_sends_only_its_evidence():
    snap = snapshot(V, T=4)
    c, ids = la.RESTRICTED.bundle(snap)
    assert {"bars_5m", "swings_5m"} <= set(c) and not {"bars_15m", "bars_2m_final"} & set(c)
    assert ids == sl.request_ids(sl.build_request(snap, la.RESTRICTED)[0])       # what a batch collection reads
    d = la.blinded_bundle(snap, fc.SYNTHESIS_EVIDENCE)["bundle"]
    assert {"bars_15m", "bars_2m_final"} <= set(d) and not {"bars_5m", "swings_5m"} & set(d)
    full = canonical_json(la.blinded_bundle(snap)["bundle"])
    assert len(canonical_json(c)) < 0.8 * len(full) and len(canonical_json(d)) < 0.6 * len(full)


def test_the_llm_job_asks_for_the_batch_api_when_chosen():
    from dashboard.jobs import llm_command
    assert "--batch" in llm_command(["2026-10-01"], "CD", "ab12", batch=True)
    assert "--batch" not in llm_command(["2026-10-01"], "CD", "ab12")


def test_a_section_of_the_bundle_can_be_cited():
    bundle = {"references": {"ref:on_high": {}}, "bars_15m": {"rows": [["bar:15m:18:00"]]},
              "price_location": {"on_high": "Below", "on_low": "Above"}, "eligibility": {"direction_15m": "eligible"}}
    ids = sl.bundle_ids(bundle)
    assert {"price_location", "price_location:on_high", "eligibility", "eligibility:direction_15m"} <= ids
    assert "price_location:vwap" not in ids and "completeness" not in ids          # only what is in the bundle
    answer = synthesis_answer(_bundle())
    _item(answer, "first_level_tested")["supporting_evidence_ids"] = ["price_location"]   # 2026-09-30's citation
    b = _bundle()
    assert la.validate_synthesis(json.loads(json.dumps(answer)), {f"baseline:{t}" for t in TARGETS} | ids,
                                 {t: None for t in TARGETS},
                                 {t: b["baseline"][f"baseline:{t}"]["predicted_class"] for t in TARGETS}) is None


def test_the_synthesis_gets_the_thresholds_its_targets_need():
    """v4's bundle carried only T although its prompt named B and two targets need B or A: v5 adds them, and a
    target whose threshold is unavailable (no frozen daily ATR) is ineligible, not predicted from nothing."""
    assert fc.SYNTHESIS_VERSION == "nq_synthesis_p1_v5" and "nq_synthesis_p1_v4" in fc.ARM_HISTORY["D"]
    assert fc.SYNTHESIS_THRESHOLDS == {"close_direction_rth": ("B",), "session_type_rth": ("A", "B")}
    for t in fc.SYNTHESIS_THRESHOLDS:                     # the rule text names the thresholds the bundle now carries
        assert set(fc.SYNTHESIS_THRESHOLDS[t]) <= set(re.findall(r"\b[AB]\b", defs.TARGETS[t]["rule"] + " "
                                                                   + str(defs.TARGETS[t].get("threshold"))))
    full = {"T": 7, "B": 20, "A": "1903/5"}
    assert la._threshold_missing("close_direction_rth", full) is None
    assert "threshold B unavailable" in la._threshold_missing("close_direction_rth", {"T": 7, "B": None, "A": None})
    assert "threshold A and B unavailable" in la._threshold_missing("session_type_rth", {"T": 7})
    assert la._threshold_missing("direction_15m", {"T": 7}) is None                  # needs T only
    with open(fc.SYNTHESIS_PROMPT, encoding="utf-8") as f:
        assert "T, B and A are the thresholds in the bundle's session block" in f.read()


def test_arms_c_and_d_run_side_by_side(monkeypatch):
    """D never reads C's answers: live, their requests overlap instead of waiting for each other."""
    import time as _time
    seen = {}

    def slow(name):
        def call(*args, **kwargs):
            seen[name] = [_time.monotonic()]
            _time.sleep(0.4)
            seen[name].append(_time.monotonic())
            return ({"status": "ok"} if name == "C" else
                    {"status": "sent", "run": {"run_id": "r" * 36, "lifecycle_status": "issued", "failure_reason": None}})
        return call
    day = "2026-10-01"
    monkeypatch.setattr(la.store, "list_snapshots", lambda *a, **k: [{"snapshot_id": "s1", "session_date": day}])
    monkeypatch.setattr(la.store, "latest_annotation", lambda *a, **k: None)
    monkeypatch.setattr(la.store, "outcome_history", lambda *a, **k: {})
    monkeypatch.setattr(la, "pending_snapshots", lambda conn, arm: set())
    monkeypatch.setattr(la, "pending_batches", lambda conn: {})
    monkeypatch.setattr(la, "_ready", lambda *a, **k: {"status": "ready", "snapshot": {"snapshot_id": "s1"}})
    monkeypatch.setattr(la.sl, "annotate_live", slow("C"))
    monkeypatch.setattr(la, "synthesize", slow("D"))
    monkeypatch.setattr(la.journal, "match", lambda *a, **k: 0)
    monkeypatch.setattr(la, "forecast_session", lambda *a, **k: None)
    started = _time.monotonic()
    out = la.run(None, object(), days=[day], arms=("C", "D"), log=lambda line: None)
    assert seen["C"][0] < seen["D"][1] and seen["D"][0] < seen["C"][1]                 # they overlapped
    assert _time.monotonic() - started < 0.75                                           # not 0.8 s in turn
    assert out["requests_sent"] == 2 and out["counts"] == {"C annotation ok": 1, "D run issued": 1}
    one = la.run(None, object(), days=[day], arms=("C", "D"), max_requests=1, log=lambda line: None)
    assert one["requests_sent"] == 1                                                    # C first, as before
