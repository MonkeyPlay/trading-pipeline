# contracts/nq_preopen.py
"""
Pre-open structure definitions for guideline stage 2: the P1 fields a structure
annotation fills, the Event Risk policy, and the rule-based annotation protocol
that stands in for the Claude structure annotation (Appendix A, A1) until the
API is in place.

  FIELDS              the P1 properties an annotation fills, with their allowed
                      values: P1's own lists where it gives one (ON-v1, Premarket
                      Pattern, Fast MA Alignment, Event Risk), otherwise the live
                      Notion schema P1 points to (contracts/weekday_trades_schema.json)
  PRICE_LOCATION      P1 section 7's five Above / At / Below comparisons
  EVENT_RISK          EV-v1: P1 section 8 as a rule over the snapshot's events
  RULES               every threshold of the rule-based annotator
                      (forecaster/structure_rules.py), registered as
                      RULES_PROTOCOL_VERSION
  ANNOTATION_SCHEMA   the output both annotators produce - the rules now, Claude
                      (A1) later under its own protocol version - so the matcher and
                      the store take either
  MATCH_WEIGHTS       P1 section 7's analogue rubric (MATCHER_VERSION), used by
                      matching/structural.py

Moving averages are the user's TradingView indicator "TEMA & Session Levels" on
2-minute bars (nq_conv_v2): the long MA is EMA(100), the fast pair TEMA(14) then
SMA(3) (the faster line) and EMA(14) then SMA(3) (the slower one).

The rule-based protocol is a trial convention, not P1's text: P1 leaves dominant
trend, meaningful reversal, flat, frequent and the like unquantified. It is named
separately and is never compared with a Claude annotation as if the two were the
same protocol.
"""

import hashlib
import json
import os
from fractions import Fraction
from typing import Any, Dict

from contracts.nq_prompt_v2 import _record

_SCHEMA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "weekday_trades_schema.json")


def notion_options(prop: str) -> list:
    """The option list of one property of the Notion Weekday Trades schema (fetched 2026-10-04)."""
    with open(_SCHEMA_PATH, encoding="utf-8") as f:
        return list(json.load(f)["properties"][prop]["options"])


# --------------------------------------------------------------------------
# Fields
# --------------------------------------------------------------------------

FIELDS: Dict[str, Dict[str, Any]] = {
    "Overnight Structure": {"values": ["Uptrend", "Downtrend", "Range", "V-reversal", "Inverted-V", "Mixed"],
                            "source": "P1 section 4 (ON-v1)", "matcher": True},
    "Premarket Pattern": {"values": ["Bullish continuation", "Bearish continuation", "Bullish reversal",
                                     "Bearish reversal", "Range", "Mixed"],
                          "source": "P1 section 4", "matcher": True},
    "Short-Term Structure": {"values": notion_options("Short-Term Structure"), "source": "Notion schema",
                             "matcher": True},
    "5-Minute Trend": {"values": notion_options("5-Minute Trend"), "source": "Notion schema", "matcher": True},
    "15-Minute Trend": {"values": notion_options("15-Minute Trend"), "source": "Notion schema", "matcher": True},
    "Higher-Timeframe Bias": {"values": notion_options("Higher-Timeframe Bias"), "source": "Notion schema",
                              "matcher": False},
    "Price vs Long MA": {"values": notion_options("Price vs Long MA"), "source": "Notion schema", "matcher": True},
    "Long MA Slope": {"values": ["Rising", "Flat", "Falling"],
                      "source": "Notion schema (its 'Unavailable' option is the field's null)", "matcher": True},
    "Fast MA Alignment": {"values": ["Bullish", "Bearish", "Mixed"], "source": "P1 section 4", "matcher": False},
    "Chop Score": {"values": [0, 1, 2, 3], "source": "P1 section 4", "matcher": True},
    "Event Risk": {"values": ["Normal", "Reduced-confidence", "High-risk"], "source": "P1 section 8",
                   "matcher": True},
    "Event Notes": {"values": "text", "source": "P1 section 8", "matcher": False},
}

FIELD_STATUSES = ("classified", "unavailable", "not_covered")
INTEGRITY_STATUSES = ("ok", "contaminated")

# P1 section 7: the cutoff price against each session's own level; At = within one point, inclusive.
PRICE_LOCATION = {"levels": ["prev_rth_high", "prev_rth_low", "prev_rth_close", "on_high", "on_low"],
                  "values": ["Above", "At", "Below"], "at_points": 1}


# --------------------------------------------------------------------------
# Event Risk (EV-v1)
# --------------------------------------------------------------------------

EVENT_RISK_VERSION = "EV-v1"

# The ten largest Nasdaq-100 constituents by weight, whose earnings P1 calls material.
# An 8-K with Item 2.02 (results of operations) is the earnings release; its EDGAR
# acceptance time is when it was published.
MATERIAL_EARNINGS = {
    "AAPL": 320193, "MSFT": 789019, "NVDA": 1045810, "AMZN": 1018724, "GOOGL": 1652044,
    "META": 1326801, "AVGO": 1730168, "TSLA": 1318605, "COST": 909832, "NFLX": 1065280,
}

EVENT_RISK = {
    "version": EVENT_RISK_VERSION,
    "sources": {
        "fed": "FOMC rate decisions (high) and minutes (moderate)",
        "bls": "CPI and Employment Situation (high), PPI and JOLTS (moderate)",
        "ism_rule": "ISM Manufacturing (high) and Services (moderate) PMIs",
        "bea": "GDP advance / initial estimates and Personal Income and Outlays (high), later GDP estimates "
               "(moderate)",
        "census": "Advance Retail Sales (high)",
        "sec_earnings": "8-K Item 2.02 of the material Nasdaq-100 companies, at their EDGAR acceptance time",
    },
    "material_earnings": sorted(MATERIAL_EARNINGS),
    "window": "scheduled releases from the previous session's scheduled close to the session's scheduled "
              "close; earnings published from the previous session's scheduled close to the cutoff (they "
              "reprice the open; a report after the close belongs to the next session)",
    "rule": "High-risk: a high-tier release in the window. Otherwise Reduced-confidence: a moderate release in "
            "the window, or a material earnings release (single-name repricing). Otherwise Normal - only when "
            "every source covers the session; a source without coverage leaves Event Risk unavailable.",
    "pre_open_vs_upcoming": "a release before the cutoff is already-released pre-open news, one after it an "
                            "upcoming catalyst (Event Notes say which)",
    "not_covered": "unscheduled shocks (P1: 'known material shocks') have no source here and are not detected; "
                   "P1 makes Normal depend on scheduled risk only",
}


# --------------------------------------------------------------------------
# Rule-based structure annotation
# --------------------------------------------------------------------------

RULES_PROTOCOL_VERSION = "nq_structure_rules_v1"

RULES = {
    "swings": {"timeframe": "5m", "left": 2, "right": 2,
               "rule": "a 5m bar whose high is above the 2 bars before it and at least the 2 after it (low "
                       "mirrored); confirmed at the end of the second bar after it, which must be complete by "
                       "the cutoff"},
    "moving_averages": {"timeframe": "2m", "long": "EMA(100)", "fast": "TEMA(14) then SMA(3)",
                        "slow": "EMA(14) then SMA(3)",
                        "seed": "the first overnight 2m bar (18:00); by the final pre-open hour the seed weighs "
                                "under 0.05% in the EMA(100), so the values match TradingView's to well within "
                                "a tick"},
    "overnight_structure": {
        "bars": "5m bars of [18:00, cutoff)", "min_coverage": "0.90 of the overnight minutes",
        "v_reversal": "the overnight low L lies in the middle 70% of the window's time, comes after a decline of "
                      "at least 0.50 R from the highest high before it, the cutoff price has recovered at least "
                      "0.60 of that decline, a 5m close after L is above the last confirmed swing high before L, "
                      "and a confirmed swing low after that break is above L (Inverted-V mirrored); both "
                      "qualifying: Mixed",
        "trend": "|cutoff - open| / R >= 0.50, close location in the top 0.30 of R (bottom for Downtrend) and the "
                 "cutoff price still beyond the last confirmed swing low (high)",
        "range": "|cutoff - open| / R < 0.35 and at least two alternations between the top and bottom "
                 "quarters of R",
        "mixed": "anything else with adequate coverage",
        "R": "overnight high - low; zero R is Range",
    },
    "premarket_pattern": {
        "hour": "5m bars starting at or after cutoff - 60 min", "context": "5m bars of [cutoff - 180, cutoff - 60)",
        "directional_hour": "|cutoff - hour open| >= 2T and efficiency >= 0.50",
        "directional_context": "|context close - context open| >= 2T and efficiency >= 0.40; without a "
                               "directional context the overnight structure stands in (Uptrend / V-reversal "
                               "up, Downtrend / Inverted-V down)",
        "continuation": "directional hour in the context's direction",
        "reversal": "directional hour against the context and the cutoff price beyond the context's last "
                    "confirmed swing high (low); without that break: Mixed",
        "range": "no directional progress: hour efficiency < 0.35, or |net| < 2T within a range <= 4T",
        "mixed": "anything else", "min_bars": "80% of the hour's 5m bars",
    },
    "short_term_structure": {"window": "confirmed 5m swings in [cutoff - 180, cutoff)",
                             "rule": "last two swing highs and last two swing lows both rising: Higher highs / "
                                     "Higher Lows; both falling: Lower highs / Lower lows; else Mixed; fewer than "
                                     "two of either: unavailable"},
    "trend": {"5m": "last 12 complete 5m bars", "15m": "last 12 complete 15m bars",
              "rule": "E = |last close - first open| / range: E >= 0.60 Bullish / Bearish, 0.30 <= E < 0.60 "
                      "Neutral-bullish / Neutral-bearish, below 0.30 (or zero range) Neutral",
              "min_bars": 10},
    "price_vs_long_ma": {"window": "last 15 2m bars (30 min)",
                         "rule": "|cutoff price - EMA(100)| <= 1 point: At; else the sign changes of close - "
                                 "EMA(100) over the window: two or more Crossing, one Above/crossing or "
                                 "Crossing/below by the side now, none Above / Below"},
    "long_ma_slope": {"rule": "EMA(100) change over the last 15 2m bars: |change| < T Flat, else Rising / "
                              "Falling"},
    "fast_ma_alignment": {"rule": "sign changes of TEMA - EMA(14) over the last 15 2m bars: two or more, or one "
                                  "in the last 3 bars, Mixed; else Bullish when TEMA is above, Bearish below"},
    "chop_score": {"window": "last 15 2m bars (the final 30 pre-open minutes)",
                   "flat_long_ma": "Long MA Slope is Flat",
                   "repeated_crossings": "close crossed EMA(14) and EMA(100) at least 4 times in all (TEMA "
                                         "hugs price, so its crossings are not counted)",
                   "overlap_and_wicks": "at least 0.45 of the bars mostly inside the previous one (overlap >= "
                                        "0.70 of their own range) and at least 0.55 of the bars with wicks >= "
                                        "0.50 of their range",
                   "min_bars": 12},
    "higher_timeframe_bias": "not covered by this protocol (not a matcher input)",
    "event_risk": EVENT_RISK_VERSION,
    "integrity": "a target-session bar ending after the cutoff, or an earnings row published after it, makes "
                 "the annotation contaminated: no classifications",
}


def rules_record() -> Dict[str, Any]:
    return _record(RULES_PROTOCOL_VERSION, "annotation", {
        "annotator": "rules", "fields": FIELDS, "price_location": PRICE_LOCATION, "rules": RULES,
        "event_risk": EVENT_RISK, "replaced_by": "a Claude structure annotation (Appendix A, A1) under its own "
                                                 "protocol version, producing the same ANNOTATION_SCHEMA",
    })


ANNOTATION_SCHEMA = {
    "protocol_version": "the registered annotation protocol",
    "annotator": "rules | llm",
    "integrity_status": list(INTEGRITY_STATUSES),
    "fields": {"<P1 property>": {"value": "one allowed value, or null", "status": list(FIELD_STATUSES),
                                 "reason": "why null", "evidence_ids": "ids resolving inside the snapshot",
                                 "basis": "short text"}},
    "price_location": {"<level>": "Above | At | Below | null"},
    "measurements": "the numbers the classifications used",
}


# --------------------------------------------------------------------------
# Structural analogue matching (guideline stage 2B / 2C)
# --------------------------------------------------------------------------

MATCHER_VERSION = "nq_match_p1_v1"

# P1 section 7, exactly: feature -> weight in percent (exact fractions, they sum to 100).
# "price:<level>" is the cutoff price Above / At / Below that session's own level.
MATCH_WEIGHTS: Dict[str, Fraction] = {
    "price:prev_rth_high": Fraction(6), "price:prev_rth_low": Fraction(6), "price:prev_rth_close": Fraction(6),
    "price:on_high": Fraction(6), "price:on_low": Fraction(6),
    "Overnight Structure": Fraction(25, 2), "Short-Term Structure": Fraction(25, 2),
    "5-Minute Trend": Fraction(25, 4), "15-Minute Trend": Fraction(25, 4),
    "Price vs Long MA": Fraction(25, 4), "Long MA Slope": Fraction(25, 4),
    "Premarket Pattern": Fraction(5), "Chop Score": Fraction(5),
    "Event Risk": Fraction(10),
}
MATCH_GROUPS = {
    "Price location": [k for k in MATCH_WEIGHTS if k.startswith("price:")],
    "Structure": ["Overnight Structure", "Short-Term Structure"],
    "Trends and MA": ["5-Minute Trend", "15-Minute Trend", "Price vs Long MA", "Long MA Slope"],
    "Final-hour condition": ["Premarket Pattern", "Chop Score"],
    "Events": ["Event Risk"],
}
MIN_COMPARABLE = Fraction(75)          # percent of the weight that must be comparable
TOP_ANALOGUES = 5
SMOOTHING_PSEUDO_COUNT = 5

# The P1 forecast targets whose analogue outcomes are counted (stage 1 label targets).
OUTCOME_TARGETS = ["first_move_5m", "opening_type_15m", "direction_15m", "opening_bias_30m",
                   "close_direction_rth", "session_type_rth", "first_level_tested"]

MATCHER = {
    "source": "P1 section 7",
    "weights": {k: str(v) for k, v in MATCH_WEIGHTS.items()},
    "groups": MATCH_GROUPS,
    "similarity": "categorical equality scores 1, inequality 0; Chop Score max(0, 1 - |a - b| / 3); price "
                  "location compares Above / At / Below, At within one point of each session's own level",
    "comparable": "a feature counts when both sessions have a classified value under the same annotation "
                  "protocol and snapshot version; missing or incompatible inputs are neither matches nor "
                  "mismatches",
    "coverage": "comparable_weight = the weights of the comparable features; candidates under 75 are rejected",
    "score": "similarity = 100 x weighted matches / comparable_weight",
    "pool": "earlier NQ sessions (never the target date or later) of the same snapshot version whose annotation "
            "has the same protocol and integrity ok; fixed before any outcome is attached",
    "selection": "the 5 highest similarities; ties by higher comparable_weight, then the more recent session, "
                 "then the snapshot id; no minimum similarity",
    "outcomes": {
        "targets": OUTCOME_TARGETS,
        "attach": "after selection, each analogue's latest outcome under the label version; an unavailable label "
                  "leaves that target's denominator smaller - the analogue is never replaced",
        "raw": "unweighted class counts and frequencies over the analogues with a label",
        "mean_similarity": "unweighted mean of the selected similarities",
        "smoothed": "(class_count + 5 x prior) / (eligible_count + 5), the prior from every earlier session's "
                    "label under the same label version; no analogue label: prior only, said so",
    },
}


def matcher_record() -> Dict[str, Any]:
    return _record(MATCHER_VERSION, "matcher", MATCHER)


# --------------------------------------------------------------------------
# Claude structure annotation (Appendix A, A1)
# --------------------------------------------------------------------------

LLM_PROTOCOL_VERSION = "nq_structure_llm_v1"
LLM_MODEL = "claude-opus-5-5"
LLM_EFFORT = "high"
LLM_MAX_TOKENS = 16000
LLM_PROMPT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "prompts", "runtime",
                          "structure_annotation_v1.md")
# The fields Claude annotates; Event Risk, Event Notes and the price location come from the
# application's own rules (EV-v1, P1 section 7), so both protocols carry them identically.
LLM_FIELDS = ["Overnight Structure", "Premarket Pattern", "Short-Term Structure", "5-Minute Trend",
              "15-Minute Trend", "Higher-Timeframe Bias", "Price vs Long MA", "Long MA Slope", "Fast MA Alignment",
              "Chop Score"]


def llm_output_schema() -> Dict[str, Any]:
    """The structure_annotation JSON schema Claude must return (structured outputs)."""
    def field_schema(name):
        values = FIELDS[name]["values"]
        value = {"type": "integer", "enum": values} if name == "Chop Score" else {"type": "string", "enum": values}
        return {"type": "object", "additionalProperties": False,
                "required": ["value", "status", "reason", "evidence_ids", "basis"],
                "properties": {"value": {"anyOf": [value, {"type": "null"}]},
                               "status": {"type": "string", "enum": ["classified", "unavailable"]},
                               "reason": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                               "evidence_ids": {"type": "array", "items": {"type": "string"}},
                               "basis": {"type": "string"}}}
    return {"type": "object", "additionalProperties": False,
            "required": ["integrity_status", "contradictions", "fields"],
            "properties": {"integrity_status": {"type": "string", "enum": list(INTEGRITY_STATUSES)},
                           "contradictions": {"type": "array", "items": {"type": "string"}},
                           "fields": {"type": "object", "additionalProperties": False, "required": LLM_FIELDS,
                                      "properties": {f: field_schema(f) for f in LLM_FIELDS}}}}


def llm_record() -> Dict[str, Any]:
    with open(LLM_PROMPT, "rb") as f:
        prompt_sha256 = hashlib.sha256(f.read()).hexdigest()
    return _record(LLM_PROTOCOL_VERSION, "annotation", {
        "annotator": "llm", "model": LLM_MODEL, "effort": LLM_EFFORT, "max_tokens": LLM_MAX_TOKENS,
        "prompt": {"path": "prompts/runtime/structure_annotation_v1.md", "sha256": prompt_sha256,
                   "sources": ["A (A1)", "P1 section 4", "P1 section 3 (trend timeframes)"]},
        "output_schema": llm_output_schema(), "fields": LLM_FIELDS,
        "from_the_application": {"Event Risk": EVENT_RISK_VERSION, "Event Notes": EVENT_RISK_VERSION,
                                 "price_location": "P1 section 7, At within one point"},
        "evidence": "the snapshot's references, 5m and 15m bars of the overnight window, the last 45 2m bars with "
                    "the three moving averages, and the confirmed 2/2 swing points on 5m bars (ids inside the "
                    "bundle)",
        "validation": "values in the vocabularies, null exactly when unavailable (with a reason), every evidence "
                      "id inside the bundle; an answer from any other model than LLM_MODEL (a server-side "
                      "fallback) is kept as an attempt, not as an annotation of this protocol",
    })
