# tests/test_forecast.py
"""The deterministic baseline forecast (contracts/nq_forecast.py, forecaster/forecast_baseline.py,
forecaster/forecast_validation.py) on hand-built evidence - no database. The stored runs are tested in
tests/test_nq_journal.py."""

from fractions import Fraction

import pytest

from contracts import nq_forecast as fc
from contracts import nq_preopen as pre
from contracts import nq_prompt_v2 as defs
from forecaster import forecast_baseline as fb
from forecaster.forecast_validation import ForecastInputError, ForecastInvalid, check_inputs, validate_forecast
from forecaster.preopen_display import P1_FIELDS
from matching import structural as ms

TARGETS = [t for _, t in fc.FORECAST_TARGETS]
LEVELS = {"prev_rth_high": "120", "prev_rth_low": "90", "prev_rth_close": "99.5", "on_high": "110", "on_low": "95",
          "premarket_high": "110", "premarket_low": "96.25", "overnight_open": "97", "vwap": "104",
          "long_ma": "100"}


def labels(**chosen):
    return {t: chosen.get(t) for t in TARGETS}


def evidence(members=(), prior=(), schedule="full", levels=None, cutoff="100"):
    """Evidence as freeze_forecast_evidence builds it: member labels, prior class counts, frozen candidates."""
    counts = {t: {c: 0 for c in defs.TARGETS[t]["labels"]} for t in TARGETS}
    without = {t: 0 for t in TARGETS}
    for p in prior:
        for t in TARGETS:
            if p.get(t) is None:
                without[t] += 1
            else:
                counts[t][p[t]] += 1
    lv = {**LEVELS, **(levels or {})}
    return {"schedule": schedule, "cutoff_price": None if cutoff is None else {"value": cutoff, "exact": None},
            "candidates": {n: ({"value": v, "status": "valid", "exact": None} if v is not None
                               else {"value": None, "status": "incomplete", "exact": None}) for n, v in lv.items()},
            "members": [{"labels": m} for m in members],
            "prior": {"counts": counts, "without_label": without}}


def test_the_contract_is_p1s_47_properties_with_explicit_units():
    assert [p.display_name for p in fc.PROPERTIES] == P1_FIELDS
    assert len({p.key for p in fc.PROPERTIES}) == 47
    by_name = {p.display_name: p for p in fc.PROPERTIES}
    assert by_name["Bullish Probability"].unit == "fraction" and by_name["Analogue Similarity Score"].unit == "percent"
    assert by_name["2-Min ATR at 09:29"].key == "atr_2m_at_cutoff" and by_name["Price at 09:29"].key == "price_at_0929"
    assert "never" in by_name["Predicted Opening Bias"].missing_policy
    kinds = {r["version"]: r["kind"] for r in fc.all_records()}
    assert kinds == {fc.FORECAST_SCHEMA_VERSION: "forecast_schema", fc.BASELINE_VERSION: "forecast_algorithm",
                     fc.PRIOR_VERSION: "forecast_algorithm", "nq_issue_replay_v1": "issue_policy",
                     "nq_issue_live_v2": "issue_policy", fc.SYNTHESIS_SCHEMA_VERSION: "forecast_schema",
                     fc.RESTRICTED_VERSION: "forecast_algorithm", fc.SYNTHESIS_VERSION: "forecast_algorithm"}
    live = fc.ISSUE_POLICY_DEFINITIONS["live"]
    assert live["deadline_et"] == "09:29:50" and "age 0" in live["freshness"] and "verified" in live["verification"]


def test_the_distribution_is_the_exact_smoothed_baseline():
    prior = {"bullish": 6, "bearish": 2, "neutral_band": 2}
    d = fb.distribution("direction_15m", ["bullish", "bearish", None], prior, 1)
    assert d["distribution"] == {"bullish": Fraction(4, 7), "bearish": Fraction(2, 7), "neutral_band": Fraction(1, 7)}
    assert (d["estimation_status"], d["eligible"], d["without_label"], d["prior_sessions"],
            d["prior_without_label"]) == ("analogues", 2, 1, 10, 1)            # (1 + 5 x 0.6) / (2 + 5) = 4/7
    only = fb.distribution("direction_15m", [None, None], prior, 0)
    assert only["estimation_status"] == "prior_only" and only["distribution"]["bullish"] == Fraction(3, 5)
    none = fb.distribution("direction_15m", ["bullish"], {}, 4)
    assert none["estimation_status"] == "none" and none["distribution"] is None


def test_classes_ties_and_unavailable_targets():
    prior = [labels(direction_15m="bullish"), labels(direction_15m="bearish")] * 2
    tie = fb.baseline_forecast(evidence([labels(direction_15m="bullish"), labels(direction_15m="bearish")], prior))
    p = tie["predictions"]["direction_15m"]
    assert p["status"] == "ambiguous_prediction" and p["predicted_label"] is None    # never resolved to neutral
    assert p["reason"] == "exact tie at 1/2: bullish, bearish"                     # (1 + 5 x 1/2) / (2 + 5)
    assert tie["outputs"]["probabilities"] == p["distribution"]                     # P1's three = direction_15m
    won = fb.baseline_forecast(evidence([labels(direction_15m="bullish")] * 2, prior))["predictions"]["direction_15m"]
    assert (won["status"], won["predicted_label"]) == ("predicted", "bullish")
    no_prior = fb.baseline_forecast(evidence([labels(direction_15m="bullish")], []))["predictions"]["direction_15m"]
    assert no_prior["status"] == "unavailable" and "needs a prior" in no_prior["reason"]


def test_eligibility_follows_the_realised_labels():
    prior = [labels(close_direction_rth="bullish", session_type_rth="range_day", first_level_tested="vwap")]
    early = fb.baseline_forecast(evidence([], prior, schedule="early_close"))["predictions"]
    for target in ("close_direction_rth", "session_type_rth"):
        assert early[target]["status"] == "unavailable" and "shortened_session" in early[target]["reason"]
    missing = fb.baseline_forecast(evidence([], prior, levels={"long_ma": None}))
    first = missing["predictions"]["first_level_tested"]
    assert first["status"] == "unavailable" and "long_ma" in first["reason"]
    assert missing["outputs"]["first_level_price"] is None
    full = fb.baseline_forecast(evidence([], prior))
    assert full["predictions"]["first_level_tested"]["predicted_label"] == "vwap"
    assert full["outputs"]["first_level_price"] == "104"                            # the frozen price, resolved


def test_reference_targets_are_the_nearest_frozen_levels_on_each_side():
    t = fb.reference_targets(evidence(levels={"premarket_low": None}))
    assert [(x["id"], x["price"], x["distance"]) for x in t["upside"]] == [("vwap", "104", "4/1"),
                                                                         ("on_high", "110", "10/1")]
    assert t["upside"][1]["coincident"] == ["premarket_high"]                      # one level, named by precedence
    assert [x["id"] for x in t["downside"]] == ["prev_rth_close", "overnight_open"]   # 99.5, 97; long MA = cutoff
    assert fb.reference_targets(evidence(cutoff=None))["reason"] == "no valid cutoff price"


def aset_for(members, prior):
    """An analogue set whose stored summary the matcher computed from the same labels."""
    recs = [ms.Record(f"m{i}", f"2026-06-0{i + 1}", "NQ", "v", f"a{i}", "rules", "ok", {}) for i in range(len(members))]
    selected = [{"record": r, "similarity": Fraction(90)} for r in recs]
    summary = ms.outcome_summary(selected, {r.snapshot_id: {t: {"label": m[t]} for t in TARGETS}
                                            for r, m in zip(recs, members)},
                                 [{t: {"label": p[t]} for t in TARGETS} for p in prior])
    return {"outcome_summary": summary}


def test_validation_holds_the_baseline_to_the_matchers_summary():
    members = [labels(direction_15m="bullish", first_move_5m="up_first"), labels(direction_15m="bearish")]
    prior = [labels(direction_15m=d, first_move_5m="down_first", opening_type_15m="range")
             for d in ["bullish"] * 5 + ["neutral_band"] * 3]
    aset, forecast = aset_for(members, prior), fb.baseline_forecast(evidence(members, prior))
    assert validate_forecast(forecast, aset) is forecast

    def broken(change):
        f = {"predictions": {t: dict(p) for t, p in forecast["predictions"].items()}, "outputs": forecast["outputs"]}
        change(f["predictions"])
        with pytest.raises(ForecastInvalid):
            validate_forecast(f, aset)

    broken(lambda p: p["direction_15m"].update(distribution={"bullish": "1/2", "bearish": "1/4",
                                                             "neutral_band": "1/4"}))
    broken(lambda p: p["direction_15m"].update(predicted_label="bearish"))          # not the most probable
    broken(lambda p: p["direction_15m"].update(predicted_label="sideways"))
    broken(lambda p: p["direction_15m"].update(eligible=5))                         # denominators differ
    broken(lambda p: p.pop("first_move_5m"))


def test_arm_a_is_the_prior_alone_and_must_equal_the_sets_prior():
    members = [labels(direction_15m="bullish")] * 3
    prior = [labels(direction_15m=d) for d in ["bearish"] * 6 + ["bullish"] * 2 + ["neutral_band"] * 2]
    aset = aset_for(members, prior)
    a = fb.baseline_forecast(evidence(members, prior), fc.PRIOR_VERSION)
    d = a["predictions"]["direction_15m"]
    assert (d["estimation_status"], d["eligible"], d["predicted_label"]) == ("prior_only", 0, "bearish")
    assert d["distribution"] == {"bullish": "1/5", "bearish": "3/5", "neutral_band": "1/5"}   # the analogues ignored
    assert validate_forecast(a, aset, fc.PRIOR_VERSION) is a
    b = fb.baseline_forecast(evidence(members, prior))                     # arm B: (3 + 5 x 1/5) / (3 + 5)
    assert b["predictions"]["direction_15m"]["distribution"]["bullish"] == "1/2"
    with pytest.raises(ForecastInvalid):
        validate_forecast(a, aset)                                         # arm A is not the smoothed baseline
    with pytest.raises(ValueError):
        fb.baseline_forecast(evidence(members, prior), "nq_magic_v1")


def snapshot(**over):
    return {"snapshot_id": "s1", "snapshot_version": defs.PROFILES[defs.DEFAULT_PROFILE].snapshot_version,
            "data_mode": "historical_reconstruction", "session_date": "2026-06-12",
            "payload": {"first_level_candidates": {"ids": list(defs.FIRST_LEVEL_CANDIDATES)}}, **over}


ANNOTATION = {"annotation_id": "a1", "snapshot_id": "s1", "protocol_version": pre.RULES_PROTOCOL_VERSION}
ASET = {"set_id": "x", "target_snapshot_id": "s1", "target_annotation_id": "a1",
        "protocol_version": pre.RULES_PROTOCOL_VERSION, "label_version": defs.LABEL_VERSION,
        "matcher_version": pre.MATCHER_VERSION, "members": [{"session_date": "2026-06-11"}], "prior_digest": "d",
        "outcome_summary": {"prior": {"digest": "d", "manifest": [["s0", "2026-06-10", 1]]}}}


@pytest.mark.parametrize("change, message", [
    (lambda s, a, x: None, None),
    (lambda s, a, x: a.update(snapshot_id="s2"), "is of snapshot s2"),
    (lambda s, a, x: x.update(target_annotation_id="a2"), "targets annotation a2"),
    (lambda s, a, x: x.update(target_snapshot_id="s2"), "targets snapshot s2"),
    (lambda s, a, x: x.update(protocol_version=pre.LLM_PROTOCOL_VERSION), "protocol"),
    (lambda s, a, x: x.update(members=[{"session_date": "2026-06-12"}]), "not earlier"),
    (lambda s, a, x: x["outcome_summary"]["prior"].update(manifest=[["s3", "2026-06-15", 1]]), "prior session"),
    (lambda s, a, x: x.update(prior_digest="other"), "prior digest"),
    (lambda s, a, x: s.update(snapshot_version=defs.PROFILES["operational_0927"].snapshot_version), "profile"),
    (lambda s, a, x: s["payload"]["first_level_candidates"].update(ids=["on_high"]), "candidate universe"),
])
def test_check_inputs_rejects_evidence_that_does_not_belong_together(change, message):
    import copy
    s, a, x = snapshot(), copy.deepcopy(ANNOTATION), copy.deepcopy(ASET)
    change(s, a, x)
    if message is None:
        check_inputs(s, a, x, defs.DEFAULT_PROFILE, "historical_replay")
        with pytest.raises(ForecastInputError, match="live_capture"):            # a reconstruction is never live
            check_inputs(s, a, x, defs.DEFAULT_PROFILE, "live")
    else:
        with pytest.raises(ForecastInputError, match=message):
            check_inputs(s, a, x, defs.DEFAULT_PROFILE, "historical_replay")


def test_a_runs_issue_time_says_whether_it_was_a_forecast():
    """The per-target view states when a run was issued against its cutoff and the open, and marks the targets whose
    window had ended by then - a record, not a forecast (2026-10-06: issued 09:40 ET, after the first move)."""
    from dashboard.views.forecast import issue_timing
    run = {"session_date": "2026-10-06", "issued_at": "2026-10-06 13:40:25", "input_cutoff_at": "2026-10-06 13:29:00",
           "mode": "historical_replay"}
    line, over = issue_timing(run)
    assert "11 min after its 09:29 ET cutoff and 10 min after the open" in line and over == {"first_move_5m": "09:35"}
    late = issue_timing({**run, "session_date": "2025-12-11", "issued_at": "2026-10-04 14:36:00",
                         "input_cutoff_at": "2025-12-11 14:29:00"})[0]
    assert "297 days after" in late and "a record rather than a forecast" in late
    early = issue_timing({**run, "issued_at": "2026-10-06 13:29:40", "mode": "live"})
    assert "before the open - issued live" in early[0] and early[1] == {}
