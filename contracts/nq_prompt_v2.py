# contracts/nq_prompt_v2.py
"""
Definition registry for the NQ prompt-v2 implementation (docs/nq_prompt_v2.md).

The source specifications are the pre-open prompt P1 and the post-session prompt
P2 (prompts/source/, hashed in prompts/manifest.json): prompt version 2.1,
forecast/outcome definitions NQ-v2, revised 2026-09-15. This module turns their
scored targets into typed, versioned definitions:

  LABEL_VERSION       the outcome labels (P2 sections 2-6, with the LO-v1 level
                      outcomes), computed by forecaster/labels_prompt_v2.py
  CONVENTION_VERSION  the calculation conventions P1/P2 leave open - ATR session
                      basis and warm-up, bar anchoring, reference-level coverage -
                      used by features/nq_evidence.py
  PROFILES            the cutoff profiles; each is its own snapshot version, since
                      a different cutoff changes the frozen ATRs and therefore the
                      threshold-dependent labels

Labels are canonical identifiers; the prompts' display strings are a separate
mapping (``display``). A missing or unresolved value is None with a reason from
REASONS - never a market class.

Every version is registered with a hash of its definition (database/journal_store.py),
so a changed definition must take a new version name.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import time
from decimal import Decimal
from fractions import Fraction
from typing import Any, Dict, Optional, Union

DEFINITIONS = "NQ-v2"
PROMPT_VERSION = "2.1"
# nq_prompt_v2_1_impl1 (registered 2026-10-03): the first six targets only, same rules.
# impl2 adds first level tested and the LO-v1 level outcomes; impl3 the supplementary
# descriptors of P2 sections 5 and 7 (MP-v1 for the morning pullback); impl4 (FL-v2) adds
# Premarket High / Low (nq_conv_v4) and the Long MA to the first-level candidates and
# names the first level where impl3 left it unavailable: candidates at one price by
# FIRST_LEVEL_PRECEDENCE, levels on both sides of one bar's open by the one nearest the
# open (an estimate, flagged in the measurements). impl5 (2026-10-04, guideline revision 2)
# reads the snapshot's frozen first-level candidates (FL-v3: levels on both sides of one
# bar's open are unavailable again, the nearest-to-open guess kept as a measurement),
# lets a level the opening gap crossed be swept by a later RTH breach (OS-v2) and judges
# level breaches by the bar's high / low (LO-v2).
LABEL_VERSION = "nq_prompt_v2_1_impl5"
FIRST_LEVEL_CONVENTION = "FL-v3"
OPENING_SWEEP_CONVENTION = "OS-v2"
PULLBACK_RUBRIC = "MP-v1"
PATTERN_CONVENTION = "FP-v1"
LEVEL_OUTCOME_CONVENTION = "LO-v2"
# nq_conv_v1 (registered 2026-10-03) took events from the session's calendar day only and
# configured no moving averages; nq_conv_v2 widens the events to P1 section 8 (EV-v1 in
# contracts/nq_preopen.py) and adds the user's TradingView moving averages; nq_conv_v3
# adds the five prior sessions' RTH prices on the snapshot contract (HTB-v1); nq_conv_v4
# defines the premarket window, [08:00 ET, cutoff), as the user's TradingView script does;
# nq_conv_v5 requires complete data (guideline revision 2, 1B): every minute of a bar,
# window or indicator history, and freezes the first-level candidates in the snapshot.
CONVENTION_VERSION = "nq_conv_v5"
PREMARKET_START = time(8, 0)
SYMBOL = "NQ"

RTH_OPEN = time(9, 30)


@dataclass(frozen=True)
class Profile:
    """A cutoff profile: the snapshot is frozen at ``cutoff`` ET."""
    name: str
    cutoff: time
    snapshot_version: str
    description: str


PROFILES: Dict[str, Profile] = {
    "research_0929": Profile(
        "research_0929", time(9, 29), "nq_evidence_v5_r0929",
        "Prompt comparison profile: bars complete by 09:29:00 ET (last 1m bar 09:28, last 2m bar "
        "09:26-09:28)."),
    "operational_0927": Profile(
        "operational_0927", time(9, 27), "nq_evidence_v5_o0927",
        "Operational profile for live runs that need the extra time: bars complete by 09:27:00 ET (last 1m "
        "bar 09:26, last 2m bar 09:24-09:26). Its ATRs, thresholds and labels are not comparable with "
        "research_0929."),
    "candidate_0915": Profile(
        "candidate_0915", time(9, 15), "nq_evidence_v5_c0915",
        "Timeliness candidate: bars complete by 09:15:00 ET (last 1m bar 09:14, last 2m bar 09:12-09:14), so a "
        "live forecast can be issued before the open on a feed that delivers bars about 11 minutes late. The "
        "evidence ends at 09:15 however late its bars arrive. Its ATRs, thresholds and labels are not "
        "comparable with research_0929."),
}
DEFAULT_PROFILE = "research_0929"
# Profiles whose pools Auto keeps current beside the default once they have one (start a pool with
# nq_journal.py backfill --profile ..., then catch-up --profile ...).
CANDIDATE_PROFILES = ("candidate_0915",)

DATA_MODES = ("live_capture", "historical_reconstruction", "historical_as_observed")
PIT_STATUSES = ("verified", "unverified_historical")

# Why a label is None. "uncovered" is a complete session that no session-type
# rule fits (P2 section 4) - recorded apart from missing data.
REASONS = (
    "missing_bars",                 # a bar the rule needs is not stored (gap, cropped window)
    "missing_threshold",            # T, B or A unavailable in the frozen snapshot
    "missing_reference",            # a frozen sweep reference is unavailable
    "ambiguous_intrabar",           # both thresholds reached in one 1m bar, order unknown
    "shortened_session",            # full-session target on an early close
    "uncovered",                    # complete session, no rule applies
    "coincident_levels",            # first level: several candidates at the price reached first
    "none_tested",                  # first level: complete window, no candidate reached
    "approach_unresolved",          # level outcome: the level equals O, no approach side
    "no_rejection",                 # level outcome: reached, no breach, no close back on the original side
    "upstream_unavailable",         # depends on a label that is unavailable
    "not_applicable",               # opening drive strength without a qualifying drive (P2: left blank)
    "late_leg_extreme",             # MP-v1: the morning leg extreme at or after 11:58
    "ambiguous_pattern",            # FP-v1: more than one first-15-minute pattern fits
)

# The sweep references of the opening type (P1/P2: frozen ON High, ON Low,
# Previous RTH High and Previous RTH Low), as named in the snapshot.
SWEEP_REFERENCES = ("on_high", "on_low", "prev_rth_high", "prev_rth_low")

_DIRECTION = ("bullish", "bearish", "neutral_band")

# First level tested (P2 section 6): the candidate identities this convention can
# verify from the snapshot, with P2's category names. Round numbers / other named
# levels (none identified pre-open by the pipeline) are not in the candidate set;
# impl1-impl3 also lacked Premarket High/Low and the Long MA.
FIRST_LEVEL_CANDIDATES: Dict[str, str] = {
    "on_high": "ON High", "on_low": "ON Low",
    "prev_rth_high": "Previous RTH High", "prev_rth_low": "Previous RTH Low", "prev_rth_close": "Previous RTH Close",
    "overnight_open": "Overnight Open", "vwap": "VWAP",
    "premarket_high": "Premarket High", "premarket_low": "Premarket Low", "long_ma": "Long MA",
}
# FL-v2: which identity names a price several candidates share - the longer-horizon
# reference first (a premarket high equal to the ON high is the ON high).
FIRST_LEVEL_PRECEDENCE = ("prev_rth_high", "prev_rth_low", "prev_rth_close", "on_high", "on_low", "premarket_high",
                          "premarket_low", "overnight_open", "vwap", "long_ma")

# LO-v1 level outcomes: target -> the session reference it follows over standard RTH.
LEVEL_OUTCOME_REFERENCES: Dict[str, str] = {
    "on_high_outcome": "on_high", "on_low_outcome": "on_low",
    "prev_rth_high_outcome": "prev_rth_high", "prev_rth_low_outcome": "prev_rth_low",
}
_LO_DISPLAY = {"not_tested": "Not tested", "test_rejection": "Test and rejection",
               "break_acceptance": "Break and acceptance", "break_reclaim_acceptance": "Break–reclaim–acceptance",
               "break_without_acceptance": "Break without acceptance"}
_EXTENSION = ("up", "down", "both_sides", "none")
_EXTENSION_DISPLAY = {"up": "Up", "down": "Down", "both_sides": "Both sides", "none": "None"}
_TIMING = ("opening_15m", "morning", "midday", "afternoon", "closing_30m")
_TIMING_DISPLAY = {"opening_15m": "Opening 15m", "morning": "Morning", "midday": "Midday",
                   "afternoon": "Afternoon", "closing_30m": "Closing 30m"}
# minutes after 09:30 at which each timing bin ends
TIMING_BINS = (("opening_15m", 15), ("morning", 150), ("midday", 270), ("afternoon", 360), ("closing_30m", 390))
_PATTERN_DISPLAY = {"drive_continuation": "Drive continuation", "fade_reversal": "Fade reversal",
                    "v_shape_reversal": "V-shape reversal", "double_top_bottom": "Double top/bottom",
                    "balanced_rotation": "Balanced rotation", "spike_and_channel": "Spike and channel",
                    "choppy": "Choppy"}
_MATCHED = ("yes", "no", "mixed")
_MATCHED_DISPLAY = {"yes": "Yes", "no": "No", "mixed": "Mixed"}
_LO_RULE = ("LO-v2, from the side of O (the level equal to O: approach_unresolved): not tested (no trade reached it "
            "in the complete window); test and rejection (reached - an exact touch - with no trade strictly beyond, "
            "then a close strictly back on the original side); break and acceptance (a trade strictly beyond, the "
            "window's final three 1m closes strictly beyond); break-reclaim-acceptance (a trade beyond, the final "
            "three strictly on the original side); break without acceptance (a trade beyond, neither).")

TARGETS: Dict[str, Dict[str, Any]] = {
    "first_move_5m": {
        "labels": ("up_first", "down_first", "neither"),
        "window_et": ("09:30", "09:35"),
        "threshold": "T",
        "standard_session_only": False,
        "realised_property": "Realised First Move",
        "predicted_property": "Expected First Move",
        "display_realised": {"up_first": "Up", "down_first": "Down", "neither": "Two-sided"},
        "display_predicted": {"up_first": "Up", "down_first": "Down", "neither": "Two-sided"},
        "rule": "Which of O+T and O-T is reached first in [09:30, 09:35); neither when the complete window "
                "reaches neither. Both in one 1m bar: ambiguous_intrabar.",
    },
    "direction_15m": {
        "labels": _DIRECTION,
        "window_et": ("09:30", "09:45"),
        "threshold": "T",
        "standard_session_only": False,
        "realised_property": "First 15-Minute Direction",
        "predicted_property": "Predicted First 15-Minute Direction",
        "display_realised": {"bullish": "Bullish", "bearish": "Bearish", "neutral_band": "Two-sided"},
        "display_predicted": {"bullish": "Bullish", "bearish": "Bearish", "neutral_band": "Two-sided"},
        "rule": "C15 - O against T, C15 the 09:44 bar's close: > T bullish, < -T bearish, else neutral_band "
                "(boundaries included).",
    },
    "opening_type_15m": {
        "labels": ("sweep_low_rebound", "sweep_high_reverse", "opening_drive_up", "opening_drive_down",
                   "two_sided_whipsaw", "range"),
        "window_et": ("09:30", "09:45"),
        "threshold": "T",
        "references": SWEEP_REFERENCES,
        "standard_session_only": False,
        "realised_property": "Realised Opening Type",
        "predicted_property": "Most Likely Opening Type",
        "display_realised": {"sweep_low_rebound": "Sweep low then rebound",
                             "sweep_high_reverse": "Sweep high then reverse",
                             "opening_drive_up": "Opening drive up", "opening_drive_down": "Opening drive down",
                             "two_sided_whipsaw": "Two-sided whipsaw", "range": "Range"},
        "display_predicted": {"sweep_low_rebound": "Sweep low then rebound",
                              "sweep_high_reverse": "Sweep high then reverse",
                              "opening_drive_up": "Opening drive up", "opening_drive_down": "Opening drive down",
                              "two_sided_whipsaw": "Two-sided whipsaw", "range": "Range"},
        "rule": "P2 ordered list, first established rule wins; an unresolved higher rule makes the label None. "
                "1 sweep low (support < O breached by >= T in RTH, a later 1m close back above it, C15 > O+T; "
                "OS-v2: also a level the opening gap crossed); "
                "2 sweep high (mirror, C15 < O-T); 3 drive up (C15 > O+T, eff >= 0.60, O-L15 <= T); "
                "4 drive down (mirror); 5 whipsaw (both O+T and O-T reached); 6 range. "
                "eff = |C15-O|/(H15-L15).",
    },
    "opening_bias_30m": {
        "labels": _DIRECTION,
        "window_et": ("09:30", "10:00"),
        "threshold": "T",
        "standard_session_only": False,
        "realised_property": "Realised Opening Bias",
        "predicted_property": "Predicted Opening Bias",
        "display_realised": {"bullish": "Bullish", "bearish": "Bearish", "neutral_band": "Two-sided"},
        "display_predicted": {"bullish": "Bullish", "bearish": "Bearish", "neutral_band": "Neutral"},
        "rule": "C30 - O against T, C30 the 09:59 bar's close (boundaries neutral). Realised First 30-Minute "
                "Direction is the same value.",
    },
    "close_direction_rth": {
        "labels": _DIRECTION,
        "window_et": ("09:30", "16:00"),
        "threshold": "B",
        "standard_session_only": True,
        "realised_property": "RTH Close Direction",
        "predicted_property": "Predicted RTH Close Direction",
        "display_realised": {"bullish": "Bullish", "bearish": "Bearish", "neutral_band": "Two-sided"},
        "display_predicted": {"bullish": "Bullish", "bearish": "Bearish", "neutral_band": "Two-sided"},
        "rule": "RTH close (15:59 bar) - O against B (boundaries neutral); standard sessions only.",
    },
    "session_type_rth": {
        "labels": ("reversal_day", "bull_trend_day", "bear_trend_day", "two_sided_volatile_day", "range_day"),
        "window_et": ("09:30", "16:00"),
        "threshold": "A,B",
        "standard_session_only": True,
        "realised_property": "RTH Session Type",
        "predicted_property": "Predicted RTH Session Type",
        "display_realised": {"reversal_day": "Reversal Day", "bull_trend_day": "Bull Trend Day",
                             "bear_trend_day": "Bear Trend Day", "two_sided_volatile_day": "Two-sided volatile day",
                             "range_day": "Range Day"},
        "display_predicted": {"reversal_day": "Reversal Day", "bull_trend_day": "Bull Trend Day",
                              "bear_trend_day": "Bear Trend Day", "two_sided_volatile_day": "Two-sided volatile day",
                              "range_day": "Range Day"},
        "rule": "P2 ordered list over the complete standard RTH: 1 reversal (IB low <= O-0.25A and close "
                "bullish, or IB high >= O+0.25A and close bearish); 2 bull trend (bullish, E >= 0.60, CL >= "
                "0.80); 3 bear trend (bearish, E >= 0.60, CL <= 0.20); 4 two-sided volatile (R >= A, E < 0.35); "
                "5 range (R < A, E < 0.35; a zero-range session too); none: uncovered. R = high - low, "
                "E = |close-O|/R, CL = (close-low)/R.",
    },
    "first_level_tested": {
        "labels": tuple(FIRST_LEVEL_CANDIDATES),
        "window_et": ("09:30", "09:45"),
        "threshold": None,
        "references": tuple(FIRST_LEVEL_CANDIDATES),
        "standard_session_only": False,
        "realised_property": "Realised First Level Tested",
        "predicted_property": "Expected First Level Tested",
        "display_realised": FIRST_LEVEL_CANDIDATES,
        "display_predicted": FIRST_LEVEL_CANDIDATES,
        "rule": "FL-v3. The first candidate of the snapshot's frozen list reached (1m low <= level <= high) in "
                "[09:30, 09:45); its price is the measurement first_level_price. Within one bar: a level equal to "
                "the bar's open first, else the nearest on its side; levels reached on both sides of the open: "
                "ambiguous_intrabar, the level nearest the open kept as first_level_estimate. Several candidates at "
                "that price: the first in FIRST_LEVEL_PRECEDENCE, the others kept as first_level_coincident. A "
                "missing candidate: missing_reference. None reached in the complete window: none_tested.",
    },
    "first_level_outcome": {
        "labels": tuple(k for k in _LO_DISPLAY if k != "not_tested"),
        "window_et": ("09:30", "09:45"),
        "threshold": None,
        "standard_session_only": False,
        "realised_property": "First Level Outcome",
        "predicted_property": None,
        "display_realised": {k: v for k, v in _LO_DISPLAY.items() if k != "not_tested"},
        "display_predicted": {k: v for k, v in _LO_DISPLAY.items() if k != "not_tested"},
        "rule": _LO_RULE + " For the realised first level, over [09:30, 09:45); never not_tested.",
    },
    "ib_direction": {
        "labels": _DIRECTION,
        "window_et": ("09:30", "10:30"),
        "threshold": "T",
        "standard_session_only": False,
        "realised_property": "Initial Balance Direction",
        "predicted_property": None,
        "display_realised": {"bullish": "Bullish", "bearish": "Bearish", "neutral_band": "Two-sided"},
        "display_predicted": {"bullish": "Bullish", "bearish": "Bearish", "neutral_band": "Two-sided"},
        "rule": "IB close (10:29 bar) - O against T (boundaries neutral).",
    },
    "opening_drive_strength": {
        "labels": ("strong", "moderate"),
        "window_et": ("09:30", "09:45"),
        "threshold": None,
        "standard_session_only": False,
        "realised_property": "Opening Drive Strength",
        "predicted_property": None,
        "display_realised": {"strong": "Strong", "moderate": "Moderate"},
        "display_predicted": {"strong": "Strong", "moderate": "Moderate"},
        "rule": "Only for an opening drive up / down: efficiency >= 0.80 strong, else moderate; otherwise "
                "not_applicable (P2: left blank).",
    },
    "opening_range_extension": {
        "labels": _EXTENSION,
        "window_et": ("09:45", "16:00"),
        "threshold": None,
        "standard_session_only": True,
        "realised_property": "Opening Range Extension Direction",
        "predicted_property": None,
        "display_realised": _EXTENSION_DISPLAY,
        "display_predicted": _EXTENSION_DISPLAY,
        "rule": "Strict breaches (a trade beyond, not a touch) of the first-15-minute high / low during "
                "[09:45, 16:00): up, down, both_sides or none. None needs the complete window.",
    },
    "ib_extension": {
        "labels": _EXTENSION,
        "window_et": ("10:30", "16:00"),
        "threshold": None,
        "standard_session_only": True,
        "realised_property": "Initial Balance Extension",
        "predicted_property": None,
        "display_realised": _EXTENSION_DISPLAY,
        "display_predicted": _EXTENSION_DISPLAY,
        "rule": "Strict breaches of the IB high / low during [10:30, 16:00), as the opening range extension.",
    },
    "gap_outcome": {
        "labels": ("no_material_gap", "full_gap_fill", "partial_gap_fill", "gap_and_go"),
        "window_et": ("09:30", "16:00"),
        "threshold": "B,T",
        "references": ("prev_rth_close",),
        "standard_session_only": True,
        "realised_property": "Gap Outcome",
        "predicted_property": None,
        "display_realised": {"no_material_gap": "No material gap", "full_gap_fill": "Full gap fill",
                             "partial_gap_fill": "Partial gap fill", "gap_and_go": "Gap-and-go"},
        "display_predicted": {"no_material_gap": "No material gap", "full_gap_fill": "Full gap fill",
                              "partial_gap_fill": "Partial gap fill", "gap_and_go": "Gap-and-go"},
        "rule": "P = frozen previous RTH close. |O - P| <= B: no material gap; else, in order: full gap fill (a "
                "trade reached P), partial gap fill (a trade on P's side of O, P never reached in the complete "
                "session), gap-and-go (never toward P and at least T beyond O in the gap direction); none: "
                "uncovered.",
    },
    "session_high_timing": {
        "labels": _TIMING,
        "window_et": ("09:30", "16:00"),
        "threshold": None,
        "standard_session_only": True,
        "realised_property": "Session High Timing",
        "predicted_property": None,
        "display_realised": _TIMING_DISPLAY,
        "display_predicted": _TIMING_DISPLAY,
        "rule": "The bin of the first 1m bar reaching the complete RTH high: [09:30, 09:45), [09:45, 12:00), "
                "[12:00, 14:00), [14:00, 15:30), [15:30, 16:00).",
    },
    "session_low_timing": {
        "labels": _TIMING,
        "window_et": ("09:30", "16:00"),
        "threshold": None,
        "standard_session_only": True,
        "realised_property": "Session Low Timing",
        "predicted_property": None,
        "display_realised": _TIMING_DISPLAY,
        "display_predicted": _TIMING_DISPLAY,
        "rule": "As the session high timing, for the RTH low.",
    },
    "morning_pullback": {
        "labels": ("none", "minor", "moderate", "deep"),
        "window_et": ("09:30", "12:00"),
        "threshold": "T",
        "standard_session_only": False,
        "realised_property": "Morning Pullback Severity",
        "predicted_property": None,
        "display_realised": {"none": "None", "minor": "Minor", "moderate": "Moderate", "deep": "Deep"},
        "display_predicted": {"none": "None", "minor": "Minor", "moderate": "Moderate", "deep": "Deep"},
        "rule": "MP-v1. Morning direction: 11:59 close - O against T; two-sided: none when the pre-noon range "
                "<= 2T, else uncovered. Bearish: leg low = lowest low before 12:00, leg high = highest high "
                "from 09:30 to the leg low, pullback = highest high after it; bullish mirrored. Retracement = "
                "pullback / leg: < 10% none, < 38.2% minor, <= 61.8% moderate, else deep. Leg extreme at or "
                "after 11:58: late_leg_extreme.",
    },
    "afternoon_continuation": {
        "labels": ("bullish", "bearish", "none"),
        "window_et": ("09:30", "16:00"),
        "threshold": "T,B",
        "standard_session_only": True,
        "realised_property": "Afternoon Continuation",
        "predicted_property": None,
        "display_realised": {"bullish": "Bullish", "bearish": "Bearish", "none": "None"},
        "display_predicted": {"bullish": "Bullish", "bearish": "Bearish", "none": "None"},
        "rule": "Morning: 11:59 close - O against T; afternoon: RTH close - the 14:00 bar's open against B. "
                "Both bullish: bullish; both bearish: bearish; otherwise none.",
    },
    "opening_direction_matched": {
        "labels": _MATCHED,
        "window_et": ("09:30", "16:00"),
        "threshold": None,
        "standard_session_only": True,
        "realised_property": "Opening Direction Matched Session",
        "predicted_property": None,
        "display_realised": _MATCHED_DISPLAY,
        "display_predicted": _MATCHED_DISPLAY,
        "rule": "Realised opening bias against RTH close direction: same bullish / bearish yes, opposite no, "
                "either neutral_band mixed; either unavailable: upstream_unavailable.",
    },
    "direction_15m_matched": {
        "labels": _MATCHED,
        "window_et": ("09:30", "16:00"),
        "threshold": None,
        "standard_session_only": True,
        "realised_property": "First 15-Minute Direction Matched Session",
        "predicted_property": None,
        "display_realised": _MATCHED_DISPLAY,
        "display_predicted": _MATCHED_DISPLAY,
        "rule": "As opening_direction_matched, with the first 15-minute direction.",
    },
    "first_15m_pattern": {
        "labels": ("drive_continuation", "fade_reversal", "v_shape_reversal", "double_top_bottom",
                   "balanced_rotation", "spike_and_channel", "choppy"),
        "window_et": ("09:30", "09:45"),
        "threshold": "T",
        "standard_session_only": False,
        "realised_property": "First 15-Minute Pattern",
        "predicted_property": None,
        "display_realised": _PATTERN_DISPLAY,
        "display_predicted": _PATTERN_DISPLAY,
        "rule": "FP-v1 (an implementation convention: P2 describes the pattern visually). Over the complete first "
                "15 one-minute bars; P2's own mappings first: an opening drive is drive continuation, a sweep is "
                "v-shape reversal. Otherwise shapes in proportions of the window range R = H - L, with i(H), i(L) "
                "the first bar of each extreme: "
                "v-shape reversal - the extreme opposite the close in bars 3-11, O at least 0.4R from it, the "
                "close in the far fifth and beyond O by more than T; "
                "fade reversal - the extreme in bars 0-2 at least 0.4R from O, the close in the opposite third, "
                "the other extreme later, the first extreme not retested (within 0.1R) from 4 bars after it; "
                "double top / bottom - two bars at least 4 apart within 0.1R of the extreme, a pullback of at "
                "least 0.3R between them, the close beyond it; "
                "spike and channel - O in the near fifth, half the range covered within bars 0-2, the far "
                "extreme in bars 12-14, the close in the far fifth; "
                "balanced rotation - |C - O| <= 0.2R, R >= 2T, the closes crossing the midpoint at least 3 "
                "times; "
                "choppy - |C - O| <= 0.2R, at least 8 of the 14 close-to-close changes reversing, not a "
                "balanced rotation. Exactly one shape: that pattern; none (or zero range): uncovered; several: "
                "ambiguous_pattern.",
    },
    "trend_persistence": {
        "labels": ("high", "moderate", "low"),
        "window_et": ("09:30", "16:00"),
        "threshold": None,
        "standard_session_only": True,
        "realised_property": "Trend Persistence",
        "predicted_property": None,
        "display_realised": {"high": "High", "moderate": "Moderate", "low": "Low"},
        "display_predicted": {"high": "High", "moderate": "Moderate", "low": "Low"},
        "rule": "E = |close - O| / R over the complete standard RTH: >= 0.60 high, >= 0.35 moderate, else low; a "
                "zero-range session low.",
    },
    **{target: {
        "labels": tuple(_LO_DISPLAY),
        "window_et": ("09:30", "16:00"),
        "threshold": None,
        "references": (ref,),
        "standard_session_only": True,
        "realised_property": f"{FIRST_LEVEL_CANDIDATES[ref]} Outcome",
        "predicted_property": None,
        "display_realised": _LO_DISPLAY,
        "display_predicted": _LO_DISPLAY,
        "rule": _LO_RULE + f" For the frozen {FIRST_LEVEL_CANDIDATES[ref]}, over the standard RTH.",
    } for target, ref in LEVEL_OUTCOME_REFERENCES.items()},
}

UNAVAILABLE_DISPLAY = "Unavailable"

# Decisions the prompts leave to the implementation, made explicit.
LABEL_CONVENTIONS = {
    "reach": "a threshold or level is reached by a 1m bar whose high >= it (upward) or low <= it (downward)",
    "breach_by_T": "a support v is breached by at least T when a 1m low <= v - T (resistance: high >= v + T)",
    "reclaim": "a later 1m bar (strictly after the first breaching bar) closes strictly back across the level",
    "gap_crossing": "impl1-impl4: a level strictly between the last pre-open close (the 09:29 bar) and O was "
                    "crossed by the opening gap and is not an eligible sweep reference. impl5 (OS-v2): the gap "
                    "itself is never a sweep - the breach must come from RTH bars - but a level the gap crossed "
                    "can be swept by a later RTH breach by T and reclaim close",
    "extremes": "H, L of a window need every 1m bar of it; a close needs that window's last bar",
    "o": "O is the open of the 09:30 1m bar - never the cutoff price",
    "missing_sweep_reference": "a missing sweep reference blocks the opening type only when a sweep was possible: "
                               "the window went below O - T (support) or above O + T (resistance)",
    "first_level_candidates": "on_high, on_low, prev_rth_high, prev_rth_low, prev_rth_close, overnight_open, "
                              "premarket_high, premarket_low, vwap and long_ma, read from the snapshot's frozen "
                              "candidate list (impl5); round numbers / other named levels are not in the set",
    "long_ma": "the frozen cutoff Long MA: EMA(100) of the snapshot's archived overnight 2m bars at the last one "
               "complete by the cutoff (nq_conv_v2's TradingView line, as the structure annotation computes it); "
               "unavailable under 100 2m bars",
    "first_level_coincident": "candidates at one price are one level: the identity is the first of "
                              "prev_rth_high, prev_rth_low, prev_rth_close, on_high, on_low, premarket_high, "
                              "premarket_low, overnight_open, vwap, long_ma",
    "vwap": "the frozen cutoff VWAP: sum(hlc3 x volume) / sum(volume) over the snapshot's archived overnight 1m "
            "bars [18:00, cutoff), exact; unavailable when a minute of the window is missing (impl1-impl4: under "
            "90%) or volume is zero",
    "level_reach": "a level is reached by a 1m bar with low <= level <= high; a level the opening gap crossed "
                   "with no RTH trade there is not reached",
    "level_order_in_bar": "within one 1m bar price is taken to trade through every price between the bar's open "
                          "and its extremes: a level equal to the open is reached first, then the nearest on its "
                          "side; levels on both sides of the open: the order is not observed - first level "
                          "tested is ambiguous_intrabar, with the level nearest the open kept as the measurement "
                          "first_level_estimate (impl4 named it, flagged estimated)",
    "lo_breach": "LO-v2 (impl5): a breach is a 1m high strictly above (low strictly below) the level - the wick; a "
                 "trade exactly at the level is a touch, not a breach (LO-v1: a close strictly beyond)",
    "lo_return": "returns and acceptance use closes: acceptance is the window's final three 1m closes all strictly "
                 "on one side; those bars must be stored",
    "lo_order": "a bar's close is its last price, so a breach and a close back in one bar are ordered; no other "
                "LO-v2 decision needs the order inside a bar",
    "lo_original_side": "the side of O; a level equal to O has no resolvable approach",
    "measurements": "window highs / lows need every 1m bar of the window and are kept when later bars are "
                    "missing (IB high / low need only 09:30-10:29); RTH high / low / close only on a standard "
                    "session",
    "opening_confirmation_time": "the close time (bar start + 1 minute, ET) of the first 1m bar in [09:30, 09:45) "
                                 "closing strictly beyond O + T (up first) or O - T (down first); none when the "
                                 "first move is neither or unavailable, no such close, or a bar before it missing",
    "full_session_descriptors": "gap outcome, extensions, high / low timing, afternoon continuation, matched "
                                "session and trend persistence apply to standard sessions only",
    "extension_breach": "a trade strictly beyond the boundary (high > H / low < L); both sides seen is final even "
                        "with missing bars, otherwise the complete window is needed",
    "gap_toward": "the price moved toward P when a trade went strictly beyond O on P's side",
    "timing": "the first 1m bar reaching the exact RTH extreme decides the bin",
    "mp_v1": "the leg extreme is its first occurrence; the pullback is taken from the bars strictly after the "
             "leg-extreme bar; retracement bands compared exactly",
    "exact": "all comparisons exact: prices as decimal.Decimal points, A as a fraction, ratio tests (efficiency, "
             "E, CL) by cross-multiplication",
}


Exact = Union[Fraction, Decimal, int, str]


def threshold_t(two_minute_atr: Optional[Exact]) -> Optional[int]:
    """T = max(1, ceil(0.5 x frozen two-minute ATR)) points, exactly; None without the ATR.

    The ATR is taken as an exact rational (a Fraction, Decimal or 'num/den'
    string), so a value on an integer boundary cannot round across it."""
    if two_minute_atr is None:
        return None
    return max(1, math.ceil(Fraction(two_minute_atr) / 2))


def threshold_b(daily_atr: Optional[Exact]) -> Optional[int]:
    """B = max(1, ceil(0.05 x frozen daily ATR)) points, exactly; None without the ATR."""
    if daily_atr is None:
        return None
    return max(1, math.ceil(Fraction(daily_atr) / 20))


# The registered display names (TARGETS, part of the label definitions) call a net-change neutral class "Two-sided"
# and P1 calls it "Choppy" - both describe a path, while the class is only the net change ending within the band (a
# quiet session qualifies; a whipsaw ending beyond it does not). And the first move's "neither" (no +-T touch in five
# minutes) is shown "Two-sided", the opposite of what happened. Shown instead (2026-10-09 audit); the definitions,
# their hashes and every stored label are unchanged.
DISPLAY_CLARIFIED = {
    ("direction_15m", "neutral_band"): "Flat (net within +-T)",
    ("opening_bias_30m", "neutral_band"): "Flat (net within +-T)",
    ("close_direction_rth", "neutral_band"): "Flat (net within +-B)",
    ("ib_direction", "neutral_band"): "Flat (net within the band)",
    ("first_move_5m", "neither"): "Neither (no +-T touch)",
}


def display(target: str, label: Optional[str], side: str = "realised") -> str:
    """The display string for a canonical label (``side``: realised | predicted): the prompt's, except where it would
    describe a path the class does not (DISPLAY_CLARIFIED)."""
    if label is None:
        return UNAVAILABLE_DISPLAY
    return DISPLAY_CLARIFIED.get((target, label)) or TARGETS[target][f"display_{side}"][label]


# --------------------------------------------------------------------------
# Calculation convention (CONVENTION_VERSION)
# --------------------------------------------------------------------------

DAILY_ATR = {"period": 14, "window_true_ranges": 70}
TWO_MINUTE_ATR = {"period": 14, "window_true_ranges": 70}
CUTOFF_PRICE_MAX_AGE_MINUTES = 5

CONVENTION = {
    "price_basis": "raw prices of the session's active contract (active_contracts); nothing back-adjusted or "
                   "spliced across expiries",
    "bar_timestamps": "bars are named by their start; a 1m bar is complete at start + 1 minute",
    "aggregation": {
        "anchoring": "clock buckets in ET: 2m on even minutes, 5m on multiples of 5, 15m on multiples of 15",
        "ended": "a bucket is built only when its end <= the cutoff",
        "complete": "a bucket is complete when every one of its minutes has exactly one 1m bar; only complete "
                    "buckets enter a calculation needing exact OHLC - an incomplete one is kept, flagged, never "
                    "filled",
        "empty": "a bucket without 1m bars produces no bar",
    },
    "daily_atr": {
        **DAILY_ATR,
        "smoothing": "wilder: seed = mean of the first 14 true ranges, then (13 x ATR + TR) / 14",
        "session_basis": "RTH [09:30, scheduled close) of each scheduled session before the target "
                         "(13:00 on early closes), from the calendar",
        "contract": "each session's active contract",
        "previous_close": "the same contract's RTH close of its previous scheduled session",
        "missing": "strict continuity: the 70 true ranges are those of the 70 scheduled sessions before the "
                   "target, each with every RTH minute bar on its active contract and the previous session's on "
                   "the same contract; one incomplete session makes the ATR unavailable (nq_conv_v1-v4 skipped "
                   "it and searched up to 105 sessions)",
    },
    "two_minute_atr": {
        **TWO_MINUTE_ATR,
        "smoothing": "wilder, seeded as the daily ATR",
        "bars": "2m clock buckets of the snapshot contract's 1m bars in the overnight window, complete by the "
                "cutoff; true range against the previous bucket's close; the most recent 70 true ranges",
        "continuity": "the last 71 buckets must all be complete, consecutive and end at the last bucket boundary "
                      "before the cutoff; otherwise unavailable",
        "not": "never the 1m ATR rescaled",
    },
    "references": {
        "prev_rth": "previous scheduled session (calendar), same contract as the snapshot, RTH [09:30, scheduled "
                    "close); needs every RTH minute bar",
        "overnight_window": "[18:00 ET the calendar day before the session, cutoff)",
        "on_extremes": "high / low of the overnight window; needs every one of its minutes (nq_conv_v1-v4: 90%); "
                       "a partial window keeps its observed high / low only as provisional diagnostics",
        "overnight_open": "open of the 1m bar starting exactly 18:00 ET",
        "cutoff_price": "close of the last 1m bar complete by the cutoff, if it ended at most 5 minutes before it",
        "price_at_0929": "unavailable in both profiles: neither observes 09:29:00-09:29:59",
        "premarket_high_low": "high / low of the 1m bars in [08:00 ET, cutoff) - the user's TradingView "
                              "premarket session 08:00-09:30 cut at the cutoff; needs every one of its minutes",
        "vwap": "the cutoff VWAP, sum(hlc3 x volume) / sum(volume) over the overnight window's 1m bars, exact; "
                "needs every minute of the window and some volume",
    },
    "moving_averages": {
        "source": "the user's TradingView indicator 'TEMA & Session Levels' (Pine v6) at its default inputs",
        "timeframe": "2m clock buckets of the snapshot's bars",
        "long_ma": "EMA(100) of close (the script's 'EMA 50' plot)",
        "fast_ma": "TEMA(14) = 3 (e1 - e2) + e3 of close, then SMA(3) (the faster line, 'TEMA Smoothed')",
        "slow_ma": "EMA(14) of close, then SMA(3) (the slower line, 'EMA 9 Smoothed')",
        "ema": "Pine ta.ema: alpha = 2 / (length + 1), seeded with the first value",
        "history": "the overnight window's 2m buckets from 18:00, every one complete and consecutive; a gap "
                   "makes every moving-average value unavailable (no joining across it)",
        "long_ma_at_cutoff": "EMA(100) at the last 2m bucket complete by the cutoff, kept to six decimals",
    },
    "first_level_candidates": "frozen in the snapshot before the open: on_high, on_low, prev_rth_high, "
                              "prev_rth_low, prev_rth_close, overnight_open, premarket_high, premarket_low, vwap, "
                              "long_ma - each with its price or unavailable; forecasts and outcomes use this one list",
    "events": {
        "scheduled": "economic_events rows of the scheduled sources (fed, bls, ism_rule, bea, census) from the "
                     "previous session's scheduled close to the end of the session's ET day",
        "earnings": "sec_earnings rows (8-K Item 2.02 of the material Nasdaq-100 companies) from the previous "
                    "session's scheduled close to the cutoff - a release after the cutoff is not known by it",
        "coverage": "the coverage rows that vouch for the session date, per source",
        "recorded_at": "kept - a historical reconstruction does not prove the rows were known by the cutoff",
    },
    "prior_sessions": "RTH open / high / low / close of the 5 scheduled sessions before the target, oldest first, "
                      "on the snapshot contract (the collector stores warm-up sessions before a roll, so one price "
                      "basis); a session is valid only with every RTH minute bar; a hash of the bars read",
    "intermarket": "last 1m bar complete by the cutoff of every other collected instrument (active contract), "
                   "its age and the asset's max_age_minutes (config.ASSET_SOURCES); stale values are null",
    "arithmetic": "prices as decimal.Decimal; ATRs as exact fractions (Wilder smoothing divides by 14), stored "
                  "exactly as 'num/den' beside a 6-decimal display value; thresholds and ratio tests computed "
                  "exactly",
}


# --------------------------------------------------------------------------
# Registry records
# --------------------------------------------------------------------------

def canonical_json(obj: Any) -> str:
    """Deterministic JSON: sorted keys, no whitespace, Decimals as strings, NaN refused."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), allow_nan=False, ensure_ascii=False,
                      default=lambda o: str(o) if isinstance(o, (Decimal, Fraction)) else _not_json(o))


def _not_json(o):
    raise TypeError(f"{type(o).__name__} is not JSON-serialisable")


def _hash(definition: Dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(definition).encode()).hexdigest()


def _record(version: str, kind: str, definition: Dict[str, Any]) -> Dict[str, Any]:
    return {"version": version, "kind": kind, "definition": definition, "definition_hash": _hash(definition)}


def label_record() -> Dict[str, Any]:
    return _record(LABEL_VERSION, "labels", {
        "definitions": DEFINITIONS, "prompt_version": PROMPT_VERSION, "sources": ["P1", "P2"],
        "supersedes": "nq_prompt_v2_1_impl4: FL-v3 (the frozen candidate list; both sides of one bar's open "
                      "unavailable, the nearest-to-open guess a measurement), OS-v2 (a level the opening gap "
                      "crossed can be swept later), LO-v2 (wick breaches); every other rule unchanged",
        "thresholds": {"T": "max(1, ceil(0.5 x frozen two-minute ATR)) points",
                       "B": "max(1, ceil(0.05 x frozen daily ATR)) points", "A": "frozen daily ATR"},
        "targets": {t: {k: list(v) if isinstance(v, tuple) else v for k, v in d.items()} for t, d in TARGETS.items()},
        "reasons": list(REASONS),
        "level_outcome_convention": LEVEL_OUTCOME_CONVENTION,
        "pullback_rubric": PULLBACK_RUBRIC,
        "pattern_convention": PATTERN_CONVENTION,
        "first_level_convention": FIRST_LEVEL_CONVENTION, "first_level_precedence": list(FIRST_LEVEL_PRECEDENCE),
        "opening_sweep_convention": OPENING_SWEEP_CONVENTION,
        "conventions": LABEL_CONVENTIONS,
    })


def convention_record() -> Dict[str, Any]:
    return _record(CONVENTION_VERSION, "convention", CONVENTION)


def snapshot_record(profile: str) -> Dict[str, Any]:
    p = PROFILES[profile]
    return _record(p.snapshot_version, "snapshot", {
        "profile": p.name, "cutoff_et": p.cutoff.strftime("%H:%M"), "description": p.description,
        "symbol": SYMBOL, "convention_version": CONVENTION_VERSION, "data_modes": list(DATA_MODES),
        "payload": ["identity", "schedule", "cutoff", "references", "atr", "thresholds", "bars",
                    "previous_rth_bars", "prior_sessions", "daily_atr_inputs", "events", "intermarket",
                    "moving_averages", "first_level_candidates"],
        "excluded": "target-session bars after the cutoff, labels, outcome notes and forecasts",
    })


def all_records() -> list:
    return [label_record(), convention_record()] + [snapshot_record(p) for p in PROFILES]
