# contracts/nq_forecast.py
"""
The forecast contract (guideline revision 2, stage 3): the canonical P1 property
specification, the deterministic baseline forecast and the issue policies, each a
registered version in journal.definition_versions (migration 0014). Changing any of
them means a new version name; registering an existing name with a different
definition stops the run.

  PROPERTIES            P1 section 9's 47 properties as local PropertySpecs: key,
                        display name, type, unit, vocabulary, owner, measurement
                        window, calculation version, missing policy. The familiar
                        names are display names; storage uses the keys (a 09:29 name
                        never relabels an earlier price)
  BASELINE              nq_baseline_p1_v1: per P1 target the matcher's smoothed
                        analogue distribution, recomputed exactly from the frozen
                        analogue outcome revisions and prior manifest; no LLM
                        (stage 4 arm B)
  PRIOR                 nq_prior_p1_v1: the earlier-session prior alone - the same
                        prior manifest, no structure, no analogues (stage 4 arm A)
  ISSUE_POLICIES        historical replay (research, never timely live) and live
                        (a live capture issued by 09:29:50 ET, checked by the
                        database clock)

Probabilities are fractions in [0, 1], stored exactly as "numerator/denominator";
analogue similarity is a percentage in [0, 100]. The two are never converted at a
call site - the units are part of each PropertySpec.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import asdict, dataclass
from datetime import time
from typing import Any, Dict, List, Optional

from contracts import nq_preopen as pre
from contracts import nq_prompt_v2 as defs

FORECAST_SCHEMA_VERSION = "nq_forecast_schema_v1"
BASELINE_VERSION = "nq_baseline_p1_v1"
PRIOR_VERSION = "nq_prior_p1_v1"
# v1 of both (registered 2026-10-05) never produced a run: arm C's annotation protocol v1 ran out of tokens, and
# the synthesis v1's response schema had 29 nullable fields where the API allows 16 (a 400). v2: the restricted
# protocol v2 for C; for D a schema without nullable fields ("" stands for none), 64,000 tokens, streamed - which
# the API refused too: its compiled grammar was too large (seven differently shaped target objects, 33 named
# probability fields). Synthesis v3 flattens it: a list of one item shape, probabilities a list of class/value pairs.
# Then (the user's choices, 2026-10-05) both arms at effort medium, with less evidence, live or through the Batch
# API: C v3 over the restricted protocol v3; synthesis v4 without the 5m bars and their swing points (it keeps the
# 15m bars and the final 2m bars with the moving averages beside the frozen annotation, analogues and baseline).
SYNTHESIS_EVIDENCE = ("bars_15m", "bars_2m_final")
RESTRICTED_VERSION = "nq_restricted_p1_v3"       # arm C: the baseline over the restricted Claude annotation
# Synthesis v5 (2026-10-09, an audit): v4's bundle carried only the threshold T, though its prompt named B and the
# RTH close direction (B) and session type (A, B) targets need them - v5 adds B and A to the session block and makes
# those targets ineligible when they are unavailable (no frozen daily ATR). Everything else is v4's.
SYNTHESIS_VERSION = "nq_synthesis_p1_v5"         # arm D: Claude's forecast synthesis (Appendix A, A2)
SYNTHESIS_THRESHOLDS = {"close_direction_rth": ("B",), "session_type_rth": ("A", "B")}
SYNTHESIS_SCHEMA_VERSION = "nq_forecast_schema_v2"
SYNTHESIS_MAX_TOKENS = 64000                     # thinking included; the request is streamed
SYNTHESIS_PROMPT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "prompts", "runtime",
                                "forecast_synthesis_v5.md")
# nq_issue_live_v1 (registered 2026-10-04) never issued a run; v2 adds the capture rules of 3D.
ISSUE_POLICIES = {"historical_replay": "nq_issue_replay_v1", "live": "nq_issue_live_v3"}
LIVE_DEADLINE_ET = time(9, 29, 50)
LIVE_FIRST_REQUEST_S = 1          # the first bar request this many seconds after the cutoff
LIVE_RETRY_S = 2                  # then every this many seconds for the first LIVE_FAST_S seconds,
LIVE_FAST_S = 30
LIVE_SLOW_RETRY_S = 15            # then every this many (a wait of minutes stays well inside IB's request pacing)
LIVE_DEFAULT_WAIT_S = 20          # the data-wait limit after the cutoff when a capture is given none
# Kept before the deadline for issuing once the data are in: arms A and B; with arm D (25-30 s at effort medium
# for v4; v5 unmeasured). The data wait plus the reserve must end by the deadline, or the capture is refused.
LIVE_RESERVE_S = {"AB": 10, "D": 90}

LIFECYCLE_STATUSES = ("issued", "unavailable", "late", "failed", "invalid")
PREDICTION_STATUSES = ("predicted", "ambiguous_prediction", "unavailable")
ESTIMATION_STATUSES = ("analogues", "prior_only", "none")
# nq_forecast_schema_v2 (arm D): a distribution the synthesis judged, not estimated from analogue counts
ESTIMATION_STATUSES_V2 = ESTIMATION_STATUSES + ("judgement",)

# P1 section 9's predicted properties, in its order, and the label target each one predicts.
FORECAST_TARGETS: List[tuple] = [
    ("Predicted Opening Bias", "opening_bias_30m"),
    ("Expected First Move", "first_move_5m"),
    ("Most Likely Opening Type", "opening_type_15m"),
    ("Predicted First 15-Minute Direction", "direction_15m"),
    ("Predicted RTH Session Type", "session_type_rth"),
    ("Predicted RTH Close Direction", "close_direction_rth"),
    ("Expected First Level Tested", "first_level_tested"),
]
assert {t for _, t in FORECAST_TARGETS} == set(pre.OUTCOME_TARGETS)
# P1's three probabilities are the 15-minute direction distribution only ("Choppy" = the neutral band).
PROBABILITY_TARGET = "direction_15m"
PROBABILITY_PROPERTIES = {"Bullish Probability": "bullish", "Bearish Probability": "bearish",
                          "Choppy Probability": "neutral_band"}
REFERENCE_TARGETS = {"Expected Upside Target 1": ("upside", 0), "Expected Upside Target 2": ("upside", 1),
                     "Expected Downside Target 1": ("downside", 0), "Expected Downside Target 2": ("downside", 1)}


@dataclass(frozen=True)
class PropertySpec:
    number: int
    display_name: str
    key: str
    type: str                     # date | weekday | text | category | price | number | integer | fraction | percent
    unit: Optional[str]           # index_points | fraction | percent | None
    vocabulary: Optional[str]     # where the allowed values are defined
    owner: str                    # calendar | snapshot | annotation | forecast | matcher
    window: Optional[str]         # the measurement window, ET
    version: str                  # the registered calculation / protocol / algorithm
    missing_policy: str


_SNAP, _ANN, _FC, _MATCH = (defs.CONVENTION_VERSION, pre.RULES_PROTOCOL_VERSION, BASELINE_VERSION,
                            pre.MATCHER_VERSION)
_NULL = "null with the reason; never a neutral class"


def _p(n, name, key, type_, unit, vocab, owner, window, version, missing=_NULL) -> PropertySpec:
    return PropertySpec(n, name, key, type_, unit, vocab, owner, window, version, missing)


PROPERTIES: List[PropertySpec] = [
    _p(1, "Day", "session_date", "date", None, None, "calendar", None, defs.CONVENTION_VERSION, "never missing"),
    _p(2, "Weekday", "weekday", "weekday", None, None, "calendar", None, defs.CONVENTION_VERSION, "never missing"),
    _p(3, "Contract", "contract", "text", None, None, "snapshot", None, _SNAP, "never missing"),
    _p(4, "Previous RTH High", "prev_rth_high", "price", "index_points", None, "snapshot",
       "previous session [09:30, close), every minute", _SNAP),
    _p(5, "Previous RTH Low", "prev_rth_low", "price", "index_points", None, "snapshot",
       "previous session [09:30, close), every minute", _SNAP),
    _p(6, "Previous RTH Close", "prev_rth_close", "price", "index_points", None, "snapshot",
       "previous session's last RTH minute", _SNAP),
    _p(7, "Overnight Open", "overnight_open", "price", "index_points", None, "snapshot", "the 18:00 bar", _SNAP),
    _p(8, "ON High", "on_high", "price", "index_points", None, "snapshot", "[18:00, cutoff), every minute",
       _SNAP, "null when the window is incomplete; the observed extreme is only a provisional diagnostic"),
    _p(9, "ON Low", "on_low", "price", "index_points", None, "snapshot", "[18:00, cutoff), every minute",
       _SNAP, "null when the window is incomplete; the observed extreme is only a provisional diagnostic"),
    _p(10, "Premarket High", "premarket_high", "price", "index_points", None, "snapshot",
       "[08:00, cutoff), every minute", _SNAP),
    _p(11, "Premarket Low", "premarket_low", "price", "index_points", None, "snapshot",
       "[08:00, cutoff), every minute", _SNAP),
    _p(12, "Price at 09:29", "price_at_0929", "price", "index_points", None, "snapshot", "09:29:00-09:29:59",
       _SNAP, "always null under research_0929 / operational_0927 (neither observes the 09:29 minute); the "
              "cutoff price is the separate key cutoff_price with cutoff_price_at"),
    _p(13, "14-Day Daily ATR", "atr_daily", "number", "index_points", None, "snapshot",
       "the 70 sessions before the session, strict continuity", _SNAP),
    _p(14, "2-Min ATR at 09:29", "atr_2m_at_cutoff", "number", "index_points", None, "snapshot",
       "the last 71 2m buckets ending atr_2m_bar_end", _SNAP,
       "null unless the profile's cutoff is 09:29; the value is atr_2m_at_cutoff, ending at atr_2m_bar_end"),
] + [
    _p(15 + i, name, "structure:" + name, "integer" if name == "Chop Score" else "category", None,
       f"nq_preopen.FIELDS[{name!r}]", "annotation", "[18:00, cutoff)", _ANN)
    for i, name in enumerate(["Overnight Structure", "Short-Term Structure", "5-Minute Trend", "15-Minute Trend",
                              "Higher-Timeframe Bias", "Price vs Long MA", "Long MA Slope", "Fast MA Alignment",
                              "Premarket Pattern", "Chop Score"])
] + [
    _p(25 + i, name, "predicted:" + target, "category", None, f"label target {target} (predicted display)",
       "forecast", "-".join(defs.TARGETS[target]["window_et"]), _FC,
       "unavailable, or ambiguous_prediction on an exact tie; never mapped to a neutral class")
    for i, (name, target) in enumerate(FORECAST_TARGETS[:6])
] + [
    _p(31 + i, name, f"probability:{PROBABILITY_TARGET}:{cls}", "fraction", "fraction", None, "forecast",
       "09:30-09:45", _FC, "null when direction_15m has no distribution")
    for i, (name, cls) in enumerate(PROBABILITY_PROPERTIES.items())
] + [
    _p(34, "Expected First Level Tested", "predicted:first_level_tested", "category", None,
       "the frozen first-level candidate list", "forecast", "09:30-09:45", _FC,
       "unavailable when any frozen candidate is (the realised first level would be too)"),
    _p(35, "Expected First Level Price", "predicted:first_level_price", "price", "index_points", None, "forecast",
       None, _FC, "null with the first level; resolved from the frozen candidate list, never typed in"),
    _p(36, "Forecast Confidence", "forecast_confidence", "integer", None, "1-5", "forecast", None, _FC,
       "null: no evidence-quality convention is registered yet (separate from probability and calibration)"),
    _p(37, "Historical Analogue Count", "analogue_count", "integer", None, None, "matcher", None, _MATCH,
       "0 when searched and none qualified; null when not searched"),
    _p(38, "Analogue Sessions Relation", "analogue_relation", "text", None, None, "matcher", None, _MATCH,
       "local session links; no external relation"),
    _p(39, "Analogue Sessions", "analogue_sessions", "text", None, None, "matcher", None, _MATCH, "empty set"),
    _p(40, "Analogue Session Similarity Scores", "analogue_similarities", "percent", "percent", None, "matcher",
       None, _MATCH, "empty set"),
    _p(41, "Analogue Similarity Score", "analogue_mean_similarity", "percent", "percent", None, "matcher", None,
       _MATCH, "null without analogues"),
] + [
    _p(42 + i, name, f"reference_target:{side}:{rank + 1}", "price", "index_points",
       "the frozen first-level candidate list", "forecast", None, _FC,
       "null when fewer supported references lie on that side; reference targets, not forecasts of a reach")
    for i, (name, (side, rank)) in enumerate(REFERENCE_TARGETS.items())
] + [
    _p(46, "Event Risk", "event_risk", "category", None, "nq_preopen.FIELDS['Event Risk']", "annotation", None,
       pre.EVENT_RISK_VERSION, "null when any source's coverage is unknown (unknown is not Normal)"),
    _p(47, "Event Notes", "event_notes", "text", None, None, "annotation", None, pre.EVENT_RISK_VERSION,
       "null with Event Risk"),
]
assert [p.number for p in PROPERTIES] == list(range(1, 48))

BASELINE = {
    "name": "deterministic P1 baseline (guideline revision 2, 3A): no LLM",
    "inputs": "an explicit snapshot, structure annotation and analogue set; the set must target that annotation of "
              "that snapshot, under the annotation's protocol, the label version and the matcher version",
    "targets": [t for _, t in FORECAST_TARGETS],
    "distribution": f"per target over its label vocabulary: (count + {pre.SMOOTHING_PSEUDO_COUNT} x prior) / (n + "
                    f"{pre.SMOOTHING_PSEUDO_COUNT}), the counts over the selected analogues with a label and the "
                    "prior over the earlier sessions with one (the analogue set's frozen outcome revisions and prior "
                    "manifest), exact fractions; it must equal the set's stored summary",
    "conditional": "an estimate over classifiable outcomes only: an ambiguous first move, an uncovered session type "
                   "or a missing label is not a class, and the excluded counts are stored beside it",
    "status": "analogues when an analogue has a label, prior_only when only the prior does, none (unavailable) "
              "when there is no prior label",
    "class": "the most probable class; an exact tie at the top is ambiguous_prediction, never resolved to a "
             "neutral class",
    "eligibility": "a standard-session target on an early close is unavailable (its realised label would be "
                   "shortened_session); the first level is unavailable when any of the target's frozen candidates "
                   "is (its realised label would be missing_reference)",
    "first_level": "the class of first_level_tested over the frozen candidate universe of the label version; its "
                   "price resolved from the target snapshot's frozen candidate list",
    "probabilities": f"P1's Bullish / Bearish / Choppy Probability are the {PROBABILITY_TARGET} distribution "
                     "(Choppy = the neutral band); fractions in [0, 1]",
    "reference_targets": "the target snapshot's frozen candidates with a valid price: upside strictly above the "
                         "cutoff price, downside strictly below, nearest first, two each; candidates at one price "
                         "are one level named by FIRST_LEVEL_PRECEDENCE; reference targets, not forecasts that a "
                         "price will be reached",
    "confidence": "not produced: no evidence-quality convention is registered (P1 field 36 stays null)",
    "smoothing_pseudo_count": pre.SMOOTHING_PSEUDO_COUNT,
    "first_level_precedence": list(defs.FIRST_LEVEL_PRECEDENCE),
}

PRIOR = {
    "name": "earlier-session prior (guideline revision 2, 4B arm A): no structure annotation, no analogues",
    "inputs": "the same explicit evidence as the baseline; only the analogue set's prior manifest is used",
    "targets": [t for _, t in FORECAST_TARGETS],
    "distribution": "per target over its label vocabulary: the class frequencies of the earlier sessions with a "
                    "label (the analogue set's prior manifest, known as of the target), exact fractions; they must "
                    "equal the set's stored prior",
    "conditional": BASELINE["conditional"],
    "status": "prior_only when an earlier session has a label, none (unavailable) otherwise",
    "class": BASELINE["class"],
    "eligibility": BASELINE["eligibility"],
    "first_level": BASELINE["first_level"],
    "probabilities": BASELINE["probabilities"],
    "reference_targets": BASELINE["reference_targets"],
    "confidence": BASELINE["confidence"],
    "first_level_precedence": list(defs.FIRST_LEVEL_PRECEDENCE),
}
RESTRICTED = {
    **{k: v for k, v in BASELINE.items() if k != "inputs"},
    "name": "arm C (guideline revision 2, 4B): the deterministic baseline over the restricted Claude annotation",
    "inputs": f"an explicit snapshot, its {pre.RESTRICTED_PROTOCOL_VERSION} annotation (Claude owns Overnight "
              f"Structure and Premarket Pattern, the rules the rest) and that annotation's analogue set, matched "
              f"among the earlier sessions annotated under the same protocol",
    "supersedes": "nq_restricted_p1_v2: the same over nq_structure_restricted_v2 (effort xhigh); v1 over "
                  "nq_structure_restricted_v1, which never produced an annotation",
}
# The deterministic algorithms (forecaster/forecast_baseline.py); arm D is the synthesis, below.
ALGORITHMS = {BASELINE_VERSION: BASELINE, PRIOR_VERSION: PRIOR, RESTRICTED_VERSION: RESTRICTED}
# What catch-up, the live capture and the preview issue from the rule-based annotation: arms A and B. Arms C and D
# need Claude, so they are issued only by a run started by hand (forecaster/llm_arms.py).
RULE_ALGORITHMS = (BASELINE_VERSION, PRIOR_VERSION)
ARMS = {"A": PRIOR_VERSION, "B": BASELINE_VERSION, "C": RESTRICTED_VERSION, "D": SYNTHESIS_VERSION}
ARM_NAMES = {"A": "prior", "B": "baseline", "C": "restricted LLM", "D": "synthesis"}
# Earlier versions of an arm: their runs stay that arm's on the Forecast page.
ARM_HISTORY = {"C": ("nq_restricted_p1_v1", "nq_restricted_p1_v2"),
               "D": ("nq_synthesis_p1_v1", "nq_synthesis_p1_v2", "nq_synthesis_p1_v3", "nq_synthesis_p1_v4")}


def arm_of(algorithm: str) -> Optional[str]:
    """The stage-4 arm letter of an algorithm version (current or earlier), None for another."""
    return next((a for a, v in ARMS.items() if v == algorithm or algorithm in ARM_HISTORY.get(a, ())), None)


SYNTHESIS_STATUSES = ("predicted", "tie", "unavailable")


def synthesis_output_schema() -> Dict[str, Any]:
    """
    The forecast_response schema (Appendix A, A2) the synthesis must return (structured outputs), kept flat for the
    API's grammar compiler: predictions a list of one item shape (each target once), probabilities a list of
    class/value pairs, no nullable field - "" for no class, reason or departure, an empty list for no
    probabilities. The vocabularies, one item per target and exact sums are checked locally (llm_arms).
    """
    item = {"type": "object", "additionalProperties": False,
            "required": ["target", "status", "predicted_class", "probabilities", "reason", "supporting_evidence_ids",
                         "conflicting_evidence_ids", "departure"],
            "properties": {
                "target": {"type": "string", "enum": [t for _, t in FORECAST_TARGETS]},
                "status": {"type": "string", "enum": list(SYNTHESIS_STATUSES)},
                "predicted_class": {"type": "string"},
                "probabilities": {"type": "array", "items": {
                    "type": "object", "additionalProperties": False, "required": ["class", "p"],
                    "properties": {"class": {"type": "string"}, "p": {"type": "string"}}}},
                "reason": {"type": "string"},
                "supporting_evidence_ids": {"type": "array", "items": {"type": "string"}},
                "conflicting_evidence_ids": {"type": "array", "items": {"type": "string"}},
                "departure": {"type": "string"}}}
    return {"type": "object", "additionalProperties": False,
            "required": ["integrity_status", "predictions", "confidence", "confidence_basis"],
            "properties": {
                "integrity_status": {"type": "string", "enum": ["ok", "contaminated", "identity_unresolved"]},
                "predictions": {"type": "array", "items": item},
                "confidence": {"type": "integer", "enum": [1, 2, 3, 4, 5]},
                "confidence_basis": {"type": "string"}}}


def synthesis_definition() -> Dict[str, Any]:
    with open(SYNTHESIS_PROMPT, "rb") as f:
        prompt_sha256 = hashlib.sha256(f.read()).hexdigest()
    return {
        "name": "arm D (guideline revision 2, 4B): Claude's forecast synthesis (Appendix A, A2)",
        "model": pre.LLM_MODEL, "effort": pre.ARMS_EFFORT, "max_tokens": SYNTHESIS_MAX_TOKENS,
        "prompt": {"path": "prompts/runtime/forecast_synthesis_v5.md", "sha256": prompt_sha256,
                   "sources": ["A (A2)", f"{defs.LABEL_VERSION} target rules"]},
        "output_schema": synthesis_output_schema(),
        "inputs": f"arm B's explicit evidence: the snapshot (its references, 15m bars and final 2m bars with the "
                  f"moving averages), its {pre.RULES_PROTOCOL_VERSION} annotation and that annotation's analogue "
                  f"set with the analogues' frozen outcome labels, the prior and the smoothed baseline per target, "
                  f"each target's eligibility and vocabulary, and the frozen thresholds T, B and A (index points) - "
                  f"date-blinded ({pre.BLINDING}); analogues are named analogue:<rank>; a target needing B or A "
                  f"(RTH close direction, session type) is ineligible when it is unavailable",
        "schema_version": SYNTHESIS_SCHEMA_VERSION,
        "request": "live, streamed (thinking counts against max_tokens; the SDK streams a request this long) with "
                   "the server-side refusal fallback - or through the Batch API (half price, no fallback there); a "
                   "batch answer is matched to its evidence by the archived request's hash",
        "shape": "predictions a list of one item per target; probabilities a list of class/value pairs - flat, so "
                 "the API can compile the schema",
        "none": "the schema has no nullable field: \"\" for no predicted_class, reason or departure, an empty "
                "list for no probabilities",
        "validation": "locally, before anything is stored: the answer against output_schema; the application's own "
                      "integrity check decides contamination; every target exactly once, every class of its "
                      "vocabulary exactly once; per target, predicted: a class and probabilities; "
                      "tie: probabilities whose top is shared, no class, a reason; unavailable: neither, a reason; "
                      "probabilities finite decimals in [0, 1] over exactly the target's classes, summing to "
                      "exactly 1, the class the single most probable; a target the application finds ineligible "
                      "is unavailable; every evidence id inside the bundle; a class other than the baseline's "
                      "has a departure; confidence 1-5 with predictions; the answer from the model itself (a "
                      "fallback model's answer is an invalid run, never this arm)",
        "storage": "a forecast run of this algorithm on arm B's evidence ids, its request in the inference ledger "
                   "(forecast_runs.request_id); probabilities stored as exact fractions, estimation status "
                   "judgement, the denominators of the analogue set's summary; tie -> ambiguous_prediction; an "
                   "invalid or failed answer is a run with that status and its raw text in the evidence",
        "confidence": "P1 field 36: the synthesis' integer 1-5 for evidence and conviction (A2), not calibration",
        "reference_targets": "the application's, from the frozen candidates (as the baseline), never the model's",
        "issue": "only by a run started by hand; the same evidence is never sent twice once a run is issued",
        "supersedes": "nq_synthesis_p1_v4: its bundle carried only T, though its prompt named B and two targets need B "
                      "or A; v3: effort xhigh (its first answer used 6,568 tokens) and the 5m bars and "
                      "swing points besides; v2: one "
                      "object per target with named probability fields - the API refused its compiled grammar as "
                      "too large; v1: 29 nullable fields and 20,000 tokens",
    }

ISSUE_POLICY_DEFINITIONS = {
    "historical_replay": {
        "mode": "historical_replay",
        "snapshot": "any data mode; a historical reconstruction is said to be one",
        "issued": "issued_at is the database server time of the insertion; there is no deadline",
        "counts_as": "a research replay from evidence reconstructed after the fact - never a timely live forecast",
        "revision": "other evidence for the same session, profile, algorithm, schema and policy is a new run linked "
                    "to the one it supersedes; repeating the same evidence returns the stored run",
    },
    "live": {
        "mode": "live",
        "snapshot": "a live_capture snapshot only",
        "deadline_et": LIVE_DEADLINE_ET.strftime("%H:%M:%S"),
        "capture": f"a job started before the cutoff (forecaster/live_capture.py) requests the session's 1m bars from "
                   f"the overnight start from IB {LIVE_FIRST_REQUEST_S} s after the cutoff, every {LIVE_RETRY_S} s "
                   f"for the first {LIVE_FAST_S} s and every {LIVE_SLOW_RETRY_S} s after, until the bar ending at "
                   f"the cutoff is among them or the capture's data-wait limit has passed; every bar is stored as "
                   f"received with the database time (journal.bar_receipts)",
        "data_wait": f"set per capture and recorded with it (journal.live_captures.wait_limit_s; {LIVE_DEFAULT_WAIT_S}"
                     f" s when none is given); the wait plus the reserve for issuing - {LIVE_RESERVE_S['AB']} s for "
                     f"arms A and B, {LIVE_RESERVE_S['D']} s with arm D - must end by the deadline, or the capture "
                     f"is refused before it starts",
        "evidence_cutoff": "the profile's cutoff, however long the wait: bars that arrive during it are stored with "
                           "their receipts but never enter the snapshot, whose bars, indicators and windows all end "
                           "at the cutoff",
        "freshness": "the bar ending at the cutoff must have been received; otherwise no live snapshot is frozen, "
                     "never from older bars",
        "missed": "a capture whose cutoff bar has not arrived within its data-wait limit is recorded stale - a "
                  "missed opportunity, with the newest bar received and the wait it used",
        "verification": "the live snapshot is point-in-time verified when every overnight bar it uses equals a "
                        "receipt of the capture received before it was frozen, every earlier session it reads was "
                        "stored before the cutoff (session_days.fetched_at) and every event and coverage row it "
                        "holds was recorded before the cutoff; otherwise it is a live capture marked "
                        "unverified_historical, with the reasons in its cutoff section",
        "restart": "a capture of a session that already has a live snapshot reuses it and carries on "
                   "(annotation, analogues, forecasts are idempotent); after the open no live snapshot can be "
                   "frozen (the database refuses)",
        "arms": "arms A and B are issued from the same evidence, the baseline first; arm D only when approved by "
                "hand (forecaster/live_synthesis.py): one request per live snapshot from B's frozen evidence, "
                "recorded before it is sent, awaited until the deadline",
        "delivered": "at the deadline the forecast in force is recorded: the first timely run of D, B, A - or none",
        "late_result": "a D answer after the deadline is stored late for at most "
                       "live_synthesis.LATE_WAIT_S and never replaces the forecast in force",
        "timing": "every capture step is an event stamped by the database clock (journal.live_capture_events)",
        "issued": "only when the database server clock at insertion is at or before the deadline; a later "
                  "insertion is recorded as late, with no issued_at - a client timestamp cannot change either",
        "acknowledgement": "an 'acknowledged' event inserted after the commit, at server time; a timely live "
                           "forecast is issued and acknowledged by the deadline",
        "fallback": "a late, failed or invalid attempt stays in the ledger; a fallback issues under its own "
                    "algorithm version, never as another arm",
        "supersedes": "nq_issue_live_v2: a fixed 20 s wait for the cutoff bar and no arm D - on a feed that "
                      "delivers bars about 11 minutes late every capture was stale; no live run was issued under it",
        "revision": "as for historical_replay",
    },
}


def forecast_schema_record() -> Dict[str, Any]:
    return defs._record(FORECAST_SCHEMA_VERSION, "forecast_schema", {
        "properties": [asdict(p) for p in PROPERTIES],
        "prediction_statuses": list(PREDICTION_STATUSES), "estimation_statuses": list(ESTIMATION_STATUSES),
        "lifecycle_statuses": list(LIFECYCLE_STATUSES),
        "units": {"probability": "fraction in [0, 1], stored exactly as numerator/denominator",
                  "similarity": "percent in [0, 100] (the matcher's)", "price": "index points"},
        "targets": {t: list(defs.TARGETS[t]["labels"]) for _, t in FORECAST_TARGETS},
        "label_version": defs.LABEL_VERSION,
    })


def forecast_schema_v2_record() -> Dict[str, Any]:
    """Arm D's schema: v1 with the judgement estimation status and the synthesis' Forecast Confidence."""
    record = forecast_schema_record()["definition"]
    return defs._record(SYNTHESIS_SCHEMA_VERSION, "forecast_schema", {
        **record, "estimation_statuses": list(ESTIMATION_STATUSES_V2),
        "forecast_confidence": "an integer 1-5 from the synthesis (A2): evidence and conviction, not calibration",
        "supersedes": f"{FORECAST_SCHEMA_VERSION} for synthesis runs; the deterministic arms keep it"})


def baseline_record() -> Dict[str, Any]:
    return defs._record(BASELINE_VERSION, "forecast_algorithm", BASELINE)


def algorithm_records() -> List[Dict[str, Any]]:
    return ([defs._record(v, "forecast_algorithm", d) for v, d in ALGORITHMS.items()]
            + [defs._record(SYNTHESIS_VERSION, "forecast_algorithm", synthesis_definition())])


def issue_policy_records() -> List[Dict[str, Any]]:
    return [defs._record(ISSUE_POLICIES[mode], "issue_policy", d) for mode, d in ISSUE_POLICY_DEFINITIONS.items()]


def all_records() -> List[Dict[str, Any]]:
    return [forecast_schema_record(), forecast_schema_v2_record()] + algorithm_records() + issue_policy_records()
