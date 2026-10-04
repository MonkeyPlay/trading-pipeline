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
  ISSUE_POLICIES        historical replay (research, never timely live) and live
                        (a live capture issued by 09:29:50 ET, checked by the
                        database clock)

Probabilities are fractions in [0, 1], stored exactly as "numerator/denominator";
analogue similarity is a percentage in [0, 100]. The two are never converted at a
call site - the units are part of each PropertySpec.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import time
from typing import Any, Dict, List, Optional

from contracts import nq_preopen as pre
from contracts import nq_prompt_v2 as defs

FORECAST_SCHEMA_VERSION = "nq_forecast_schema_v1"
BASELINE_VERSION = "nq_baseline_p1_v1"
ISSUE_POLICIES = {"historical_replay": "nq_issue_replay_v1", "live": "nq_issue_live_v1"}
LIVE_DEADLINE_ET = time(9, 29, 50)

LIFECYCLE_STATUSES = ("issued", "unavailable", "late", "failed", "invalid")
PREDICTION_STATUSES = ("predicted", "ambiguous_prediction", "unavailable")
ESTIMATION_STATUSES = ("analogues", "prior_only", "none")

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
        "issued": "only when the database server clock at insertion is at or before the deadline; a later "
                  "insertion is recorded as late, with no issued_at - a client timestamp cannot change either",
        "acknowledgement": "an 'acknowledged' event inserted after the commit, at server time; a timely live "
                           "forecast is issued and acknowledged by the deadline",
        "fallback": "a late, failed or invalid attempt stays in the ledger; a fallback issues under its own "
                    "algorithm version, never as another arm",
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


def baseline_record() -> Dict[str, Any]:
    return defs._record(BASELINE_VERSION, "forecast_algorithm", BASELINE)


def issue_policy_records() -> List[Dict[str, Any]]:
    return [defs._record(ISSUE_POLICIES[mode], "issue_policy", d) for mode, d in ISSUE_POLICY_DEFINITIONS.items()]


def all_records() -> List[Dict[str, Any]]:
    return [forecast_schema_record(), baseline_record()] + issue_policy_records()
