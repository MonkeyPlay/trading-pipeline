# contracts/p1_live_abd.py
"""
The live A/B/D comparison (docs/forecasting_audit.md, section 5): does Claude's
synthesis (arm D) improve forecasts *delivered before trading begins* - on the 09:15
candidate profile, issued live - over the numerical forecasts, arm B and the
frequencies arm A?

A draft, not registered. It is frozen - its capture settings checked against the
measured timing, then registered with experiments.register_experiment - only once at
least ten measured sessions support the cutoff and the v5 latency pilot
(contracts/p1_d_latency_pilot.py) has measured D's generation time. Registering writes
one definition row: it sends nothing and schedules nothing; each session's D request
still needs its own approval.

  capture      profile candidate_0915 (evidence ends at 09:15 however late its bars
               arrive), live issue policy nq_issue_live_v3, data wait WAIT_S, D's
               reserve, deadline 09:29:50 ET; a session whose cutoff bar does not
               arrive in time is a missed opportunity, counted
  cases        every scheduled session from the start: an arm's case is its first
               timely run (issued and acknowledged by the deadline); late, failed,
               invalid and missing runs stay as counted cases without a run
  skill        primary target direction_15m, the unhalved multiclass Brier score
               (0-2): D - B and D - A paired on the sessions where A, B and D were
               all timely
  delivered    the policy actually in force, D -> B -> A (the first timely run), scored
               on every scheduled session against B and A - D's successful subset
               cannot hide its failures on difficult days
  availability D's on-time rate over every scheduled opportunity
               (forecaster/live_availability.py), required to be at least 90 %
"""

from __future__ import annotations

from typing import Any, Dict

from contracts import nq_forecast as fc
from contracts import p1_d_latency_pilot as pilot
from contracts.p1_d_research import BRIER, MINIMUM_IMPROVEMENT, PRIMARY_TARGET

NAME = "p1_live_abd_v1"
PROFILE = "candidate_0915"
WAIT_S = 780                        # a candidate to test (13 minutes), confirmed or changed when frozen
ARMS = {"A": fc.PRIOR_VERSION, "B": fc.BASELINE_VERSION, "D": fc.SYNTHESIS_VERSION}
DELIVERY = ["D", "B", "A"]
PAIRS = [["D", "B"], ["D", "A"], ["B", "A"], ["delivered", "B"], ["delivered", "A"]]
AVAILABILITY = 0.90
ENDPOINT_SESSIONS = 60


def manifest(start: str, end: str, wait_s: int = WAIT_S) -> Dict[str, Any]:
    """The manifest over the prospective sessions [start, end] (start: the day it is frozen)."""
    from forecaster import experiments as ex
    m = ex.experiment_manifest(NAME, start, end, profile=PROFILE, purpose="test", official_run="first_timely",
                               mode="live", arms=dict(ARMS))
    m["arms"]["delivered"] = {
        "algorithm": "delivered", "delivered": [ARMS[a] for a in DELIVERY],
        "question": "the forecast in force at the deadline - D, else B, else A, the first timely run: what would "
                    "actually have been delivered before the open?"}
    m.update({
        "pairs": [list(p) for p in PAIRS],
        "common_arms": ["A", "B", "D"],
        "capture": {"profile": PROFILE, "issue_policy": fc.ISSUE_POLICIES["live"], "wait_limit_s": wait_s,
                    "reserve_s": fc.LIVE_RESERVE_S["D"], "deadline_et": fc.LIVE_DEADLINE_ET.strftime("%H:%M:%S"),
                    "model": fc.SYNTHESIS_VERSION + " at effort medium"},
        "excluded_sessions": {"sessions": list(pilot.SESSIONS),
                              "why": f"{pilot.NAME}: the latency pilot's sessions are never confirmatory"},
        "primary": {"target": PRIMARY_TARGET, "metric": BRIER},
        "companions": ["multiclass log loss (natural log), lower is better",
                       "accuracy of the issued class (ambiguous predictions counted apart)",
                       "class-wise counts, mean probability of the realised class and accuracy"],
        "comparison": "D - B and D - A (and B - A) paired on the sessions where A, B and D were all timely; the "
                      "delivered policy (D -> B -> A) against B and A on every scheduled session",
        "availability": {"requirement": AVAILABILITY,
                         "success": "a valid D forecast stored by the frozen deadline: issued and acknowledged by "
                                    "it on the database clock",
                         "denominator": "every scheduled session in [from, to]: missing data, no request, provider "
                                        "failure, invalid output and late completion all count as failures",
                         "report": "observed rate, numerator, denominator and an exact (Clopper-Pearson) 95 % "
                                   "interval; met when the observed rate is at least 90 % - a practical acceptance "
                                   "threshold, not evidence of skill"},
        "minimum_practical_improvement": f"an absolute reduction of {MINIMUM_IMPROVEMENT} in the mean per-session "
                                         "Brier score on the unhalved 0-2 scale; not a relative reduction",
        "decision": f"on {PRIMARY_TARGET}, D improves only when both paired mean differences, D - B and D - A, are "
                    f"at most -{MINIMUM_IMPROVEMENT} and each whole 95 % interval lies below zero. That is evidence "
                    "of some improvement with a point estimate at the chosen threshold; it does not establish that "
                    f"the true improvement is at least {MINIMUM_IMPROVEMENT}. D is adopted only if, besides, its "
                    f"on-time availability is at least {AVAILABILITY:.0%}. The delivered policy's differences are "
                    "reported beside; secondary targets are reported, never decisive",
        "endpoint": f"scored once, at {ENDPOINT_SESSIONS} scheduled sessions or on the manifest's end date; "
                    "availability may be checked before, never a score",
        "controls": "registering sends nothing and schedules nothing; each session's D request needs its own "
                    "approval",
    })
    return m
