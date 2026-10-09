# contracts/rth_operational.py
"""
The RTH analogues' operational evaluation (docs/rth_analogues.md#the-operational-evaluation):
do the analogues describe a genuinely future 15 minutes - a window that starts after the
forecast exists - better than the frozen pre-open analogues and a same-clock history?

rth_continuation_v2 (contracts/rth_eval.py) asks what the information at the cutoff
was worth: on the delayed feed its window has mostly passed in the market by the time
the forecast is stored. It is research only - forecast skill from delayed,
cutoff-frozen inputs. This evaluation is the operational counterpart, collected beside
it from the same issues, with its own construction: the target window starts at the
second full minute after the forecast is built, and every arm's members are measured
over that same clock window. It is not v2's forecast relabelled.

Registered (kind 'rth_evaluation') by the first RTH issue that knows it, before that
run stores its first forecast. Any change is a new version.
"""

from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from typing import Any, Dict

from contracts import nq_preopen as pre
from contracts import nq_rth as rth
from contracts import rth_eval as ev

VERSION = "rth_operational_v1"
KIND = "rth_evaluation"
CUTOFFS = ev.CUTOFFS                     # issued with the 15, 30 and 45 minute windows' issues
HORIZON = timedelta(minutes=15)
LEAD_MINUTES = 2                         # the window starts at the 2nd full minute after the forecast is built
LAST_MINUTE = 120                        # a target window must end by 11:30 ET (120 minutes after the open)
MAX_ISSUE_DELAY = timedelta(0)           # eligible only when stored by the start of its window

DEFINITION: Dict[str, Any] = {
    "version": VERSION,
    "purpose": "operational: skill over a genuinely future window - one that starts after the forecast is stored",
    "question": "When the RTH set of the 15, 30 or 45 minute window is issued, do the RTH analogues' moves over the "
                "next 15 minutes from then - starting after the forecast is stored - describe the target session's "
                "move over the same window better than (a) the frozen pre-open analogue set and (b) a same-clock "
                "historical baseline - for size and for direction, separately?",
    "matcher": rth.RTH_MATCHER_VERSION + " (as for rth_continuation_v2: weights and tolerances frozen, calibrated on "
               "sessions to " + rth.CALIBRATION["last_session"] + " only)",
    "issue": "built by the run that issues the live RTH set of a 15, 30 or 45 minute window (the first issue of "
             "that window), from that set's ranking - inputs to its cutoff - and what the store holds then",
    "target_window": f"[S, S + 15 min), S = the start of the {LEAD_MINUTES}nd full minute after the forecast is built "
                     f"(so at least a minute after it); S + 15 must be by {LAST_MINUTE} minutes after the open "
                     "(11:30 ET), else no forecast (not_issued)",
    "only_at_issuance": "the forecast reads only what is stored when it is built: the set's ranking (bars to its "
                        "cutoff), the pre-open set, and the earlier sessions' bars; the target's own bars after the "
                        "cutoff are never read for the forecast",
    "move": "(close of the bar ending at S + 15 min - close of the bar ending at S) / that session's frozen daily "
            "ATR, from its bars of [S - 1 min, S + 15 min) on its active contract, all stored and confirmed",
    "forecasts": {
        "stored": "with the forecast: every arm's members with session, context snapshot, contract, similarity, "
                  "weight and move over the same clock window [S, S + 15) of their own session, the versions, the "
                  "pre-open set used, S and the matching cutoff - never rebuilt",
        "weights": "equal: 1 / n over the members with a move",
        "RTH-20": "primary: the 20 highest similarities of the set's ranking",
        "RTH-5": "secondary: the set's five members",
        "PRE-5": "baseline: the target's pre-open analogue set (" + pre.MATCHER_VERSION + ", rules protocol), the "
                 "newest stored when the forecast is built",
        "CLOCK": "baseline: every session of the set's scored pool",
    },
    "eligibility": {
        "rule": "eligible only when the database stamps the forecast at or before S - the start of its window",
        "provenance_apart": "the set's provenance (issued_by, receipt times, pit_status) is recorded on the set; "
                            "eligibility is only this rule",
        "shown": "with every forecast its lead (S minus when it was stored) and how old its information was (S "
                 "minus the matching cutoff)",
    },
    "cases": "as rth_continuation_v2: every scheduled NQ session from registration, at each of the three issues, "
             "under the first reason that keeps it out - not_issued, late, unverified_inputs, forecast_incomplete "
             "(minimum members " + json.dumps(ev.MIN_MEMBERS) + "), outcome_pending, outcome_missing - else scored",
    "scores": ev.DEFINITION["scores"],
    "comparisons": ev.DEFINITION["comparisons"],
    "aggregation": ev.DEFINITION["aggregation"],
    "availability": "reported for every scheduled opportunity at each issue: scored, each reason and its rate, the "
                    "lead and information-age distributions - a scored subset never stands for the whole",
    "endpoint": ev.DEFINITION["endpoint"],
    "uncertainty": ev.DEFINITION["uncertainty"],
    "decision": ev.DEFINITION["decision"],
    "display": ev.DEFINITION["display"],
}


def definition_hash() -> str:
    return hashlib.sha256(json.dumps(DEFINITION, sort_keys=True, default=str).encode()).hexdigest()[:16]


def record() -> Dict[str, Any]:
    """The registered definition (kind rth_evaluation); max_issue_delay_s 0: the database counts a forecast only
    when stored by its window's start (journal.rth_eval_forecasts.cutoff_at holds that start)."""
    from contracts.nq_prompt_v2 import _record
    return _record(VERSION, KIND, {**DEFINITION, "max_issue_delay_s": int(MAX_ISSUE_DELAY.total_seconds())})


def document() -> Dict[str, Any]:
    """The definition as written to docs/rth_operational_v1_definition.json."""
    return {"definition_hash": definition_hash(), **DEFINITION}
