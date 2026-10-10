# contracts/rth_session.py
"""
The full-session matcher's evaluation (docs/rth_analogues.md#the-full-session-evaluation-rth_session_v1):
through the regular session, do the analogues of nq_match_rth_v3 describe a genuinely
future window - one that starts after the forecast is stored - better than the frozen
pre-open analogues and a same-clock history, for movement size and for direction?

It follows rth_operational_v1's construction (contracts/rth_operational.py) at cutoffs
spread over the whole session and at several horizons, with 15 minutes primary. It is
fed by nq_match_rth_v3's sets only; rth_continuation_v2 and rth_operational_v1 stay as
registered, fed by nq_match_rth_v2. Registered (kind 'rth_evaluation') by the first
issue of a v3 set, before that run stores its first forecast. Any change is a new
version.

  CUTOFFS             every 30 minutes of the session from 10:00 to 15:30 ET (30..360)
  HORIZONS            15 (primary), 5, 30 and 60 minutes
  applicable(n, h, L) whether a cutoff and horizon fit inside a session of L minutes:
                      a window that would cross the close is never forecast - it is not
                      shortened either
"""

from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from typing import Any, Dict

from contracts import nq_preopen as pre
from contracts import nq_rth as rth
from contracts import rth_eval as ev

VERSION = "rth_session_v1"
KIND = "rth_evaluation"
CUTOFFS = {n: f"{(9 * 60 + 30 + n) // 60:02d}:{(9 * 60 + 30 + n) % 60:02d}" for n in range(30, 361, 30)}
PRIMARY_HORIZON = 15
HORIZONS = (15, 5, 30, 60)
LEAD_MINUTES = 2                         # the window starts at the 2nd full minute after the forecast is built
MAX_ISSUE_DELAY = timedelta(0)           # eligible only when stored by the start of its window
PHASES = {"morning": (30, 120), "midday": (150, 240), "afternoon": (270, 360)}   # cutoff minutes, inclusive
FORECASTS = ev.FORECASTS                 # RTH-20, RTH-5, PRE-5, CLOCK
MIN_MEMBERS = ev.MIN_MEMBERS
PRIMARY = ev.PRIMARY                     # RTH-20 against CLOCK and PRE-5, for size and for direction
ENDPOINT_SESSIONS = 60
END_DATE = "2027-06-30"
MIN_SESSIONS_AT_END_DATE = 30
BOOTSTRAP = dict(ev.BOOTSTRAP)           # circular moving-block over sessions, 98.75 % (Bonferroni over 4)


def applicable(minutes: int, horizon: int, session_minutes: int) -> bool:
    """Whether a forecast at the ``minutes`` cutoff over ``horizon`` minutes fits a session of ``session_minutes``:
    its earliest window [cutoff + LEAD, + horizon) must end by the close."""
    return minutes in CUTOFFS and horizon in HORIZONS and minutes + LEAD_MINUTES + horizon <= session_minutes


DEFINITION: Dict[str, Any] = {
    "version": VERSION,
    "purpose": "operational, through the regular session: skill over a genuinely future window - one that starts "
               "after the forecast is stored",
    "question": "At each 30-minute cutoff of the session (10:00 to 15:30 ET), do the RTH analogues' moves over the "
                "window starting at the second full minute after the forecast is built describe the target "
                "session's move over the same window better than (a) the frozen pre-open analogue set and (b) a "
                "same-clock historical baseline - for size and for direction, separately?",
    "matcher": rth.SESSION_VERSION + " (v2's weights and tolerances; past 60 minutes the scaled tolerances are a "
               "descriptive extension, never validated - " + rth.TOLERANCE_SCOPE + ")",
    "fed_by": "sets of " + rth.SESSION_VERSION + " only; rth_continuation_v2 and rth_operational_v1 stay fed by "
              + rth.RTH_MATCHER_VERSION,
    "cutoffs": {v: k for k, v in CUTOFFS.items()},
    "horizons_minutes": list(HORIZONS),
    "primary_horizon_minutes": PRIMARY_HORIZON,
    "issue": "built by the run that issues the live v3 set of a cutoff window (its first issue), from that set's "
             "ranking - inputs to its cutoff - and what the store holds then; one forecast per cutoff and horizon",
    "target_window": f"[S, S + h), S = the start of the {LEAD_MINUTES}nd full minute after the forecast is built (so at "
                     "least a minute after it); S + h must be by the session's scheduled RTH close (390 minutes, 210 "
                     "on an early close), else no forecast: a window is never shortened to fit, and nothing is "
                     "forecast past the close",
    "only_at_issuance": "the forecast reads only what is stored when it is built: the set's ranking (bars to its "
                        "cutoff), the pre-open set and the earlier sessions' bars; the target's own bars after the "
                        "cutoff are never read for it. The minutes between the cutoff and S - unseen on the delayed "
                        "feed - are never an input",
    "move": "(close of the bar ending at S + h - close of the bar ending at S) / that session's frozen daily ATR, "
            "from its confirmed bars on its active contract",
    "forecasts": {
        "members": "each arm's members with session, context snapshot, contract, similarity, weight and their own "
                   "move over the same clock window [S, S + h) of their session - never rebuilt",
        "weights": "equal: 1 / n over the members with a move",
        "RTH-20": "primary: the 20 highest similarities of the set's ranking (the probability sample size, fixed in "
                  "advance; the five displayed analogues are never the probability model)",
        "RTH-5": "secondary: the set's five members",
        "PRE-5": "baseline: the target's pre-open analogue set (" + pre.MATCHER_VERSION + ", rules protocol), the "
                 "newest stored when the forecast is built",
        "CLOCK": "baseline: every session of the set's scored pool",
    },
    "eligibility": "eligible only when the database stamps the forecast at or before S, the start of its window; the "
                   "set's provenance (issued_by, receipt times, pit_status, issue_class) is recorded on the set",
    "cases": {
        "universe": "every scheduled NQ session from registration, at every cutoff and horizon that fits the "
                    "session (applicable)",
        "reasons": "not_issued, late, unverified_inputs, forecast_incomplete (minimum members "
                   + json.dumps(MIN_MEMBERS) + "), outcome_pending, outcome_missing - the first that applies; else "
                   "scored",
    },
    "scores": ev.DEFINITION["scores"],
    "comparisons": {
        "primary": [f"{a} vs {b}: {m} at {PRIMARY_HORIZON} minutes" for a, b, m in PRIMARY],
        "secondary": ["the same at 5, 30 and 60 minutes", "RTH-5 against both baselines", "the signed CRPS",
                      "each phase (morning 10:00-11:30, midday 12:00-13:30, afternoon 14:00-15:30 cutoffs) - "
                      "exploratory, not a registered finding"],
    },
    "aggregation": "per session, the difference of two arms' scores averaged over its scored cutoffs at the horizon "
                   "(sessions weigh equally, so a dense minute grid adds no precision); then over sessions",
    "endpoint": {
        "counts": f"a session counts when at least one of its {PRIMARY_HORIZON}-minute cutoffs is scored",
        "when": f"the first scoring at or after {ENDPOINT_SESSIONS} counted sessions - or on {END_DATE}, whichever "
                f"comes first, then only with at least {MIN_SESSIONS_AT_END_DATE} counted sessions (else "
                "insufficient, no comparison)",
        "once": "scored once; the stored result stands. Sixty sessions is a checkpoint, not a promise of power - "
                "more data means a new version",
        "before": "until then only operational health (rth-eval-status --version rth_session_v1), never a score",
    },
    "uncertainty": ev.DEFINITION["uncertainty"],
    "decision": ev.DEFINITION["decision"],
    "display": "none while the evaluation runs: no analogue continuation, frequency or path is aggregated or "
               "overlaid on the dashboard",
}


def definition_hash() -> str:
    return hashlib.sha256(json.dumps(DEFINITION, sort_keys=True, default=str).encode()).hexdigest()[:16]


def record() -> Dict[str, Any]:
    """The registered definition (journal.definition_versions, kind rth_evaluation)."""
    from contracts.nq_prompt_v2 import _record
    return _record(VERSION, KIND, {**DEFINITION, "max_issue_delay_s": int(MAX_ISSUE_DELAY.total_seconds())})
