# contracts/p1_d_research.py
"""
The first LLM comparison, D only (docs/forecasting_audit.md, section 5): does Claude's
synthesis (arm D, nq_synthesis_p1_v5) improve on the numerical forecast - arm B, and
the frequencies arm A - from the same frozen evidence?

Research, not a live pre-open experiment: all three arms read the session's official
09:29 snapshot (identical evidence cutoff) and are scored on the same P1 target
windows, but on this feed that snapshot exists only 11-20 minutes after the cutoff -
after the open - and D is issued whenever a person approves it. It measures forecast
skill from cutoff-frozen pre-open evidence, never timeliness. The live pre-open
version waits until a cutoff, a deadline and a late-result policy are chosen from the
measured timeliness (forecaster/timeliness.py) and frozen in its own definition.

Arm C is left out: its analogue pool (sessions Claude annotated) would make any
difference partly a pool-composition effect.

Three controls stay separate:

  definition   this manifest - registering it (nq_journal.py experiment-register
               --design d-research) writes one definition row and nothing else: it
               sends no request, schedules nothing and holds no budget
  activation   none exists: arm D is issued only by a run a person starts and confirms
               (the dashboard's approval, forecaster/approvals.py) - a session D was
               not run for is a case with no run, counted
  budget       each approval's own max_requests (the sessions it names); at the measured
               ~$0.12 a request, 60 sessions cost about $7 - a planning figure, not a
               reason to run it

Deferred (2026-10-09): not registered and nothing spent. It would answer whether D adds
statistical information to delayed snapshots; the agreed priority is whether D improves
forecasts delivered before trading begins (the live A/B/D comparison, frozen once the
measured timeliness supports a cutoff). Kept as a separate experiment.

Decision rule, explicitly: the primary target is direction_15m and the primary score the
multiclass Brier score in the experiments' convention - the unhalved sum over the
classes, sum_c (p_c - [c realised])^2, from 0 to 2 per session. 0.01 is an absolute
reduction of the mean on that scale (0.005 on the halved 0-1 scale), not a relative one.
D is better only when it clears it against both B and A, each interval wholly below zero:
evidence of some improvement with a point estimate at the threshold - not that the true
improvement is at least 0.01.
"""

from __future__ import annotations

from typing import Any, Dict

from contracts import nq_forecast as fc

NAME = "p1_d_research_v1"
ARMS = {"A": fc.PRIOR_VERSION, "B": fc.BASELINE_VERSION, "D": fc.SYNTHESIS_VERSION}
PAIRS = [["D", "B"], ["D", "A"], ["B", "A"]]
PRIMARY_TARGET = "direction_15m"
BRIER = ("multiclass Brier score, the unhalved sum over the classes sum_c (p_c - [c realised])^2, 0 to 2 per "
         "session, lower is better")
# An absolute reduction of the mean per-session Brier score on that unhalved scale - not relative, and twice the
# 0.005 it would be on the halved 0-1 scale. A smaller mean gain is not practical.
MINIMUM_IMPROVEMENT = "0.01"


def manifest(start: str, end: str) -> Dict[str, Any]:
    """The manifest over the prospective sessions [start, end] (start: the day it is registered)."""
    from forecaster import experiments as ex
    m = ex.experiment_manifest(NAME, start, end, purpose="test", official_run="first", mode="historical_replay",
                               arms=dict(ARMS))
    m.update({
        "pairs": [list(p) for p in PAIRS],
        "schemas": {"A": fc.FORECAST_SCHEMA_VERSION, "B": fc.FORECAST_SCHEMA_VERSION,
                    "D": fc.SYNTHESIS_SCHEMA_VERSION},
        "comparison": "D minus B (does the synthesis improve on the numerical forecast it was given), D minus A "
                      "and B minus A, paired on the cases both arms cover",
        "research_only": "forecast skill from cutoff-frozen pre-open evidence: the official 09:29 snapshot exists "
                         "only after the open on this feed, and D is issued when approved - never a timeliness "
                         "claim",
        "identical_inputs": "every arm reads the same snapshot, annotation and analogue set (arm B's evidence); D "
                            "additionally reads the thresholds T, B and A its targets need (v5) - numbers B's rules "
                            "already use, so no numerical input is D's alone",
        "availability": "every scheduled session in [from, to] is a case: no run, failed, invalid and unavailable "
                        "are counted per arm, never dropped",
        "primary": {"target": PRIMARY_TARGET, "metric": BRIER},
        "companions": ["multiclass log loss (natural log), lower is better",
                       "accuracy of the issued class (ambiguous predictions counted apart)",
                       "class-wise counts, mean probability of the realised class and accuracy"],
        "minimum_practical_improvement": f"an absolute reduction of {MINIMUM_IMPROVEMENT} in the mean per-session "
                                         "Brier score on the unhalved 0-2 scale (0.005 on the halved 0-1 scale); "
                                         "not a relative reduction",
        "decision": f"on the primary target {PRIMARY_TARGET}, D is better only when both paired mean differences, "
                    f"D - B and D - A, are at most -{MINIMUM_IMPROVEMENT} on that scale and each whole interval "
                    "lies below zero - evidence of some improvement with a point estimate at the chosen threshold, "
                    f"not that the true improvement is at least {MINIMUM_IMPROVEMENT}; otherwise: no sufficiently "
                    "reliable improvement was established. Secondary targets are reported, never decisive",
        "endpoint": "scored once, at 60 sessions with an issued D run or on the manifest's end date",
        "controls": "registering sends nothing, schedules nothing and holds no budget; D runs only on a person's "
                    "approval, each with its own max_requests",
    })
    return m
