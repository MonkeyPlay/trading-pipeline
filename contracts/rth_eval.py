# contracts/rth_eval.py
"""
The RTH analogues' usefulness evaluation (docs/rth_analogues.md#the-evaluation): do
the analogues' next 15 minutes describe the session's next 15 minutes better than the
frozen pre-open analogues and a same-clock history - for movement size and for
direction, separately?

Fixed before the forward sample: it is registered (kind 'rth_evaluation') by the
first RTH issue, in the same run and before that run stores its first evaluation
forecast. The forecasts it scores are stored when they are issued
(journal.rth_eval_forecasts, forecaster/rth_eval.py) and never rebuilt; the scorer
only adds the realised moves. Provenance (who issued, when the inputs arrived) and
eligibility (whether a forecast was issued in time to count) are kept apart. Any
change is a new version.

  rth_continuation_v1   committed 2026-10-09 (hash 977d4c124a257b14), never
                        registered or scored: it rebuilt RTH-20 at scoring time and
                        had no rule for issue time. Superseded before any data
  rth_continuation_v2   forecasts stored at issue, an issue-delay limit, explicit case
                        exclusions and endpoint
"""

from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from typing import Any, Dict

from contracts import nq_preopen as pre
from contracts import nq_rth as rth

VERSION = "rth_continuation_v2"
KIND = "rth_evaluation"
HISTORY = {"rth_continuation_v1": "977d4c124a257b14"}

CUTOFFS = {15: "09:45", 30: "10:00", 45: "10:15"}      # window minutes -> ET
HORIZON = timedelta(minutes=15)
# A forecast counts only when stored at most this long after its cutoff - before its 15-minute window ends. Measured on
# the user's feed (2026-10-07, first hour): bars reached the store a median 10.2 minutes after their minute ended (90 %
# by 10.3, worst 16.0), so a set for cutoff C is issued about C + 12 at the earliest (the last bar, the later bar that
# confirms it, the next Auto run).
MAX_ISSUE_DELAY = timedelta(minutes=14)
FORECASTS = ("RTH-20", "RTH-5", "PRE-5", "CLOCK")
MIN_MEMBERS = {"RTH-20": 10, "RTH-5": 3, "PRE-5": 3, "CLOCK": 30}
PRIMARY = (("RTH-20", "CLOCK", "size"), ("RTH-20", "CLOCK", "direction"),
           ("RTH-20", "PRE-5", "size"), ("RTH-20", "PRE-5", "direction"))
SECONDARY = (("RTH-5", "CLOCK", "size"), ("RTH-5", "CLOCK", "direction"),
             ("RTH-5", "PRE-5", "size"), ("RTH-5", "PRE-5", "direction"),
             ("RTH-20", "CLOCK", "signed"), ("RTH-20", "PRE-5", "signed"))
ENDPOINT_SESSIONS = 60
END_DATE = "2027-06-30"
MIN_SESSIONS_AT_END_DATE = 30
BOOTSTRAP = {"block": 5, "resamples": 10000, "seed": 20261009, "level": 0.9875}
REASONS = ("not_issued", "late", "unverified_inputs", "forecast_incomplete", "outcome_pending", "outcome_missing")

DEFINITION: Dict[str, Any] = {
    "version": VERSION,
    "supersedes": "rth_continuation_v1 (hash 977d4c124a257b14): committed, never registered or scored",
    "question": "At 09:45, 10:00 and 10:15 ET, do the RTH analogues' next 15 minutes describe the target session's "
                "next 15 minutes better than (a) the frozen pre-open analogue set and (b) a same-clock historical "
                "baseline - for the size of the move and for its direction, measured separately?",
    "matcher": rth.RTH_MATCHER_VERSION + " (weights and tolerances frozen; tolerances calibrated on sessions to "
               + rth.CALIBRATION["last_session"] + " only)",
    "cutoffs": {v: k for k, v in CUTOFFS.items()},
    "horizon_minutes": 15,
    "move": "(close of the bar ending at cutoff + 15 min - close of the bar ending at the cutoff) / that session's "
            "frozen daily ATR (its pre-open snapshot), from confirmed 1-minute bars on its active contract",
    "forecasts": {
        "stored": "at issue, by the same run that stores the live RTH set of the cutoff's window "
                  "(journal.rth_eval_forecasts): every forecast's members with session, context snapshot, contract, "
                  "similarity, weight and move, the versions and the pre-open set used - never rebuilt; the scorer "
                  "only adds the target's realised move",
        "weights": "equal: 1 / n over the members with a move",
        "RTH-20": "primary: the 20 highest similarities of the matcher's ranking at the cutoff (the live set's own "
                  "ranking)",
        "RTH-5": "secondary: the live set's five members - what was on screen",
        "PRE-5": "baseline: the members of the target's pre-open analogue set (" + pre.MATCHER_VERSION + ", rules "
                 "protocol), the newest stored when the forecast is issued",
        "CLOCK": "baseline: every session of the matcher's scored pool at the cutoff - the same-clock history",
        "member_move": "a member's own move over the same clock window, from its bars as stored at issue; a member "
                       "without the whole window is left out of that forecast (counted)",
    },
    "eligibility": {
        "provenance_apart": "who issued a set and when its inputs reached the store are provenance; whether a "
                            "forecast counts is decided separately, from the database's stamp of when it was stored",
        "max_issue_delay_minutes": int(MAX_ISSUE_DELAY.total_seconds() // 60),
        "rule": "eligible only when stored within 14 minutes of its cutoff - before its 15-minute window ends; a "
                "forecast stored after the window ends never counts. The database stamps the time and decides "
                "(the limit is read from this registered definition)",
        "feed": "on the user's ~10-minute-delayed feed a forecast is issued about 12 minutes into its window: the "
                "evaluation measures the information at the cutoff, out of sample (nothing after the cutoff is "
                "read), not a tradeable lead time",
        "first_counts": "one forecast per session and cutoff: the first stored; a later issue of the same cutoff is "
                        "not stored",
    },
    "cases": {
        "universe": "every scheduled NQ session from the day this definition is registered, at each of the three "
                    "cutoffs",
        "reasons": {
            "not_issued": "no forecast stored for the cutoff (Auto not running, no live RTH set of that window)",
            "late": "stored after the issue-delay limit",
            "unverified_inputs": "its RTH set's inputs not verified as of the cutoff (pit_status)",
            "forecast_incomplete": "a forecast with fewer members with a move than " + json.dumps(MIN_MEMBERS),
            "outcome_pending": "the window has not ended, or its bars are not all confirmed yet",
            "outcome_missing": "the session's bars of the window are incomplete (a gap) - never filled in",
        },
        "order": "the first reason that applies, in the order above; every case is reported under one",
        "scored": "a case with none of these reasons",
    },
    "scores": {
        "size": "fair ensemble CRPS (Ferro 2014: no penalty for the number of members) of |y| against the members' "
                "|moves|",
        "direction": "Brier score of p_up = (members up + 1) / (members + 2) against y > 0 (y = 0 counts as not up)",
        "signed": "secondary: fair ensemble CRPS of y against the members' moves",
    },
    "comparisons": {
        "primary": [f"{a} vs {b}: {m}" for a, b, m in PRIMARY],
        "secondary": [f"{a} vs {b}: {m}" for a, b, m in SECONDARY] + ["each cutoff separately"],
    },
    "aggregation": "per session, the difference of the two scores averaged over its scored cutoffs (sessions weigh "
                   "equally); each cutoff also reported alone (secondary)",
    "endpoint": {
        "counts": "a session counts when at least one of its cutoffs is scored",
        "when": f"the first scoring at or after {ENDPOINT_SESSIONS} counted sessions - or on {END_DATE}, whichever "
                f"comes first, then only with at least {MIN_SESSIONS_AT_END_DATE} counted sessions (else reported as "
                "insufficient, no comparison)",
        "once": "scored once; the stored result stands. Sixty sessions is a checkpoint, not a promise of a "
                "conclusive result - more data means a new version",
        "before": "until then only operational health is looked at (rth-eval-status: cases by reason, issue "
                  "delays, member counts) - never a score",
    },
    "uncertainty": "the mean of the session differences; an interval from a circular moving-block bootstrap over "
                   "sessions in date order (blocks of 5, 10,000 resamples, seed 20261009), percentile, each primary "
                   "at 98.75 % (95 % Bonferroni over the four)",
    "decision": "a primary comparison shows RTH-20 better only when its whole interval lies below zero (a lower "
                "score is better); otherwise: no sufficiently reliable improvement was established - never 'ruled "
                "out'. Size and direction are concluded separately; the five displayed analogues are never the "
                "probability model",
    "display": "none while the evaluation runs: no analogue continuation, frequency or path is aggregated or "
               "overlaid on the dashboard",
}


def definition_hash() -> str:
    return hashlib.sha256(json.dumps(DEFINITION, sort_keys=True, default=str).encode()).hexdigest()[:16]


def record() -> Dict[str, Any]:
    """The registered definition (journal.definition_versions, kind rth_evaluation)."""
    from contracts.nq_prompt_v2 import _record
    return _record(VERSION, KIND, {**DEFINITION, "max_issue_delay_s": int(MAX_ISSUE_DELAY.total_seconds())})


def document() -> Dict[str, Any]:
    """The definition as written to docs/rth_continuation_v2_definition.json."""
    return {"definition_hash": definition_hash(), **DEFINITION}
