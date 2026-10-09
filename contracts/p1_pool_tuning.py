# contracts/p1_pool_tuning.py
"""
A bounded benchmark of the deterministic analogue probabilities (arm B) against the
historical frequencies (arm A), fixed before it was scored (forecaster/p1_pool_tuning.py,
docs/reports/p1_pool_tuning_v1.md).

The registered experiment hist_dev_v1 found arm B - the five most similar sessions,
smoothed towards the prior with 5 pseudo-counts - no better than arm A on most P1
targets, and worse on several. This asks whether that is the pool size and the
smoothing: with both chosen on earlier sessions only, from a small grid, do the
analogue probabilities beat the frequencies?

Development data, not a test: these sessions were used to set the structure rules and
were scored before (hist_dev_v1). A pass here is a candidate for a forward test, never
a result by itself. Any change is a new version.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Dict

from contracts import nq_preopen as pre
from contracts import nq_prompt_v2 as defs

VERSION = "p1_pool_tuning_v1"
POOL_SIZES = (5, 10, 20, 40)              # the analogues counted: the K most similar earlier sessions
PSEUDO_COUNTS = (2, 5, 10, 20)            # smoothing towards the prior: (count + k x prior) / (n + k)
MIN_TRAINING = 60                         # sessions scored before the first tuned one
PRIMARY_TARGET = "direction_15m"
BOOTSTRAP = {"block": 5, "resamples": 10000, "seed": 20261009, "level": 0.95}

DEFINITION: Dict[str, Any] = {
    "version": VERSION,
    "purpose": "development benchmark: can arm B's analogue probabilities beat arm A's frequencies once their pool "
               "size and smoothing are chosen on earlier sessions only?",
    "data": f"every session with a {pre.RULES_PROTOCOL_VERSION} annotation and a {defs.LABEL_VERSION} outcome - "
            "development data (used to set the structure rules; scored before by hist_dev_v1)",
    "similarity": f"{pre.MATCHER_VERSION}'s rubric, unchanged: every earlier session scored, the 75 % coverage floor, "
                  "ties by coverage then recency",
    "grid": {"pool_sizes": list(POOL_SIZES), "pseudo_counts": list(PSEUDO_COUNTS)},
    "candidate": "per target: (class count among the K most similar earlier sessions with a label + k x prior) / "
                 "(those sessions + k); the prior = the class frequencies over every earlier session with a label",
    "baselines": {"A": "the prior alone (arm A)",
                  "B": "K = 5, k = 5 (arm B as registered)"},
    "tuning": f"walk-forward, training only: for each session from the {MIN_TRAINING + 1}st on and each target, the "
              "grid cell with the lowest mean Brier over every earlier scored session of that target; ties to the "
              "smaller K, then the smaller k. The session itself is never in its own training",
    "scores": "multiclass Brier (sum over the classes of (p - outcome)^2) primary; log loss secondary, with every "
              "zero-probability realised class counted (never dropped from the total silently)",
    "primary": f"{PRIMARY_TARGET}: tuned minus A and tuned minus B, per session",
    "secondary": "every other P1 outcome target; B minus A for reference",
    "uncertainty": "mean paired difference; 95 % circular moving-block bootstrap over sessions in date order (blocks "
                   "of 5, 10,000 resamples, seed 20261009); comparisons are reported, not corrected for "
                   "multiplicity, because none decides anything",
    "decision": "tuned analogues are a candidate for a forward test only if the primary interval against A lies "
                "wholly below zero; otherwise: no sufficiently reliable improvement over the frequencies was "
                "established on development data",
    "excluded": "sessions without a label for a target are left out of that target (counted); the first sessions "
                "train only",
}


def definition_hash() -> str:
    return hashlib.sha256(json.dumps(DEFINITION, sort_keys=True, default=str).encode()).hexdigest()[:16]
