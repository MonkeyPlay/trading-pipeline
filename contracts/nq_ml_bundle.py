# contracts/nq_ml_bundle.py
"""
The seven-target ML bundles (docs/ml_bundles.md): three arms - N (NQ-only), M (multi-instrument),
P (pooled NQ+ES+RTY) - each one artifact with a head per P1 forecast target. They run in shadow
beside A, B and the direction-only v1 models (contracts/nq_ml.py), which they do not replace: the
registered forward evaluation p1_ml_forward_v2, its artifacts and its runs are untouched.

  TARGETS / CLASSES      the authoritative registry: contracts/nq_forecast.FORECAST_TARGETS in P1's
                         order, each target's classes in contracts/nq_prompt_v2.TARGETS order - the
                         label version's exact definitions, windows, boundaries, ties and reasons
  HEADS                  per target the head kind (direction | classifier | candidates), its label
                         contract notes, its prediction-time eligibility and its feature groups
  FEATURE_GROUPS         nq_ml_features_v2: compact groups frozen before the outcome - level geometry,
                         NQ path state, market context (M only), known calendar timing
  ARMS                   information and training population of N, M and P, distinct by construction
  FAMILIES / BUDGET      the declared candidates per head kind and the small selection budget, chosen
                         inside chronological inner folds by session date (contracts/nq_ml.SPLIT)
  POOLING                P's partial pooling: shared effects plus regularised NQ deviations, the pooling
                         strength chosen on NQ validation rows; complete pooling and an NQ-only fit of the
                         same family as comparators; when a P head is unavailable
  SMOOTHING / CALIBRATION / UNSEEN    the declared handling of rare and unseen classes
  EVALUATION             nested chronological evaluation against A and B, per target first
  STATUS                 shadow: issued and shown, never delivered (contracts/nq_ml.delivery_order is
                         unchanged); a later per-target promotion needs its own registered protocol

Every definition is registered under its version name (journal.definition_versions) when an artifact
that uses it exists; a changed definition needs a new name.
"""

from __future__ import annotations

from typing import Any, Dict, List, Tuple

from contracts import market_labels as mlab
from contracts import nq_forecast as fc
from contracts import nq_ml as ml
from contracts import nq_prompt_v2 as defs

FEATURE_VERSION = "nq_ml_features_v2"
SCHEMA_VERSION = "nq_forecast_schema_v4"          # schema v3 plus per-target model provenance in the run
PROFILE = ml.PROFILE
VERSIONS = {"N": "nq_ml_bundle_nq_v1", "M": "nq_ml_bundle_multi_v1", "P": "nq_ml_bundle_pooled_v1"}
ARM_OF = {v: k for k, v in VERSIONS.items()}
# How the bundles appear beside A, B and the v1 models (dashboard, summary, grading): their own arm keys, so
# contracts/nq_ml.ARMS - which p1_ml_forward_v2 registered - never changes.
DISPLAY_ARMS = {"N7": VERSIONS["N"], "M7": VERSIONS["M"], "P7": VERSIONS["P"]}
DISPLAY_NAMES = {"N7": "ML NQ-only, 7 targets", "M7": "ML multi-instrument, 7 targets",
                 "P7": "ML pooled, 7 targets"}
ARM_LABELS = {VERSIONS["N"]: "ML NQ-only (7 targets, shadow)", VERSIONS["M"]: "ML multi-instrument (7 targets, shadow)",
              VERSIONS["P"]: "ML pooled NQ+ES+RTY (7 targets, shadow)"}
PROBABILITY_DENOMINATOR = ml.PROBABILITY_DENOMINATOR

# --------------------------------------------------------------------------
# Targets
# --------------------------------------------------------------------------

TARGETS: Tuple[str, ...] = tuple(t for _, t in fc.FORECAST_TARGETS)
CLASSES: Dict[str, Tuple[str, ...]] = {t: tuple(defs.TARGETS[t]["labels"]) for t in TARGETS}
FIELD = {t: name for name, t in fc.FORECAST_TARGETS}

# The direction targets: their realised move (the window's closing bar's close minus O) against their threshold,
# as the label version measures it (forecaster/labels_prompt_v2: the measurement key of the close, the threshold).
DIRECTION = {
    "opening_bias_30m": {"close": "C30", "threshold": "T"},
    "direction_15m": {"close": "C15", "threshold": "T"},
    "close_direction_rth": {"close": "RTH_close", "threshold": "B"},
}

HEADS: Dict[str, Dict[str, Any]] = {
    "opening_bias_30m": {
        "kind": "direction", "groups": ("path", "geometry_summary", "calendar"),
        "output": "bullish / bearish / neutral_band probabilities, the band the snapshot's frozen T",
        "eligible": "a frozen T", "conditional": "every session with an O and the 09:59 bar is classifiable",
    },
    "first_move_5m": {
        "kind": "classifier", "groups": ("path", "geometry_summary", "volatility", "calendar"),
        "output": "up_first / down_first / neither probabilities: the order in which O+T and O-T are first "
                  "reached in [09:30, 09:35) - barrier order, never the sign of the five-minute close",
        "eligible": "a frozen T",
        "conditional": "both barriers reached in one unresolved 1m bar is ambiguous_intrabar - excluded from "
                       "training and scoring with its count, never a class; the distribution is conditional on a "
                       "resolved outcome",
    },
    "opening_type_15m": {
        "kind": "classifier", "groups": ("path", "geometry_summary", "geometry", "volatility", "calendar"),
        "output": "all six canonical opening-type probabilities",
        "eligible": "a frozen T",
        "conditional": "a session whose ordered rules cannot be decided (a missing sweep reference when a sweep was "
                       "possible, missing bars) has no label: excluded with its count",
    },
    "direction_15m": {
        "kind": "direction", "groups": ("path", "geometry_summary", "calendar"),
        "output": "bullish / bearish / neutral_band probabilities, the band the snapshot's frozen T",
        "eligible": "a frozen T", "conditional": "every session with an O and the 09:44 bar is classifiable",
    },
    "session_type_rth": {
        "kind": "classifier", "groups": ("path", "geometry_summary", "volatility", "calendar"),
        "output": "all five canonical session-type probabilities",
        "eligible": "a standard (full) session and frozen A and B",
        "conditional": "a complete session no rule fits is 'uncovered' - excluded with its count, never relabelled "
                       "a range day; the distribution is conditional on a covered session",
    },
    "close_direction_rth": {
        "kind": "direction", "groups": ("path", "geometry_summary", "volatility", "calendar"),
        "output": "bullish / bearish / neutral_band probabilities, the band the snapshot's frozen B",
        "eligible": "a standard (full) session and a frozen B",
        "conditional": "an early close is shortened_session: never predicted, never a training row",
    },
    "first_level_tested": {
        "kind": "candidates", "groups": ("candidates", "path", "calendar"),
        "output": "probabilities over the ten frozen candidate identities; the price is the predicted candidate's "
                  "frozen price, resolved from the snapshot - never a free-floating price",
        "eligible": "every frozen candidate valid at the cutoff (else the realised label would be "
                    "missing_reference)",
        "conditional": "none_tested is an unavailable outcome under FL-v3, not an eleventh class; missing references "
                       "and ambiguous intrabar order are excluded with their counts. Candidates at one price are one "
                       "level named by FIRST_LEVEL_PRECEDENCE, so the others at that price get probability 0 (they "
                       "can never be the label)",
    },
}
assert tuple(HEADS) == TARGETS

# --------------------------------------------------------------------------
# Features (nq_ml_features_v2)
# --------------------------------------------------------------------------

CANDIDATES = tuple(defs.FIRST_LEVEL_CANDIDATES)
# the v1 own features (forecaster/ml_features.py, normalised by the instrument's earlier sessions)
OWN = [f.name for f in ml.OWN_FEATURES]
FEATURE_GROUPS: Dict[str, Dict[str, Any]] = {
    "path": {
        "features": OWN + ["eff_on", "eff_pm", "ret_30_atr2m", "rev_30"],
        "definition": "the instrument's overnight / premarket state: the v1 own features (returns since the previous "
                      "close, 08:00 and 30 minutes before the cutoff, overnight realised volatility and volume, range "
                      "position, gap and distances to the previous RTH high / low - normalised by its earlier "
                      "sessions), plus from the snapshot's archived overnight bars: path efficiency overnight and "
                      "premarket (|net move| / sum of |1m close changes|), the last 30 minutes' move in units of the "
                      "frozen two-minute ATR, and the last 30 minutes against the premarket direction (rev_30)",
    },
    "geometry_summary": {
        "features": ["near_up_atr", "near_down_atr", "n_within_t", "n_coincident"],
        "definition": "from the frozen first-level candidates: the signed distance from the cutoff price to the "
                      "nearest valid candidate above and below in two-minute-ATR units, how many lie within T, how "
                      "many share a price with another",
    },
    "geometry": {
        "features": [f"geo_{c}" for c in CANDIDATES],
        "definition": "the signed distance from the cutoff price to every frozen candidate in units of the frozen "
                      "two-minute ATR (missing when the candidate is unavailable)",
    },
    "volatility": {
        "features": ["atr_ratio", "on_range_atr", "pm_range_atr"],
        "definition": "known volatility: log(frozen two-minute ATR / frozen daily ATR), the overnight and premarket "
                      "ranges in daily-ATR units (frozen references)",
    },
    "calendar": {
        "features": ["event_preopen", "event_session", "ev_minutes_to_next", "ev_first_30", "ev_rth"],
        "definition": "the snapshot's frozen scheduled events (time and tier only, recorded with their recorded_at - "
                      "never a surprise, an actual or a revised value): a high-tier release before the cutoff, one "
                      "between the cutoff and the close, minutes from the cutoff to the next high-tier release "
                      "(capped at 420 when none before the close), one in the first 30 RTH minutes, one during RTH",
    },
    "context": {
        "features": ([f.name for f in ml.CONTEXT_FEATURES] + [f.name for f in ml.CROSS_FEATURES]
                     + [f.name for f in ml.INDICATOR_FEATURES] + [f"{s.lower()}_age" for s in ml.INSTRUMENTS]),
        "definition": "M only: the v1 context and cross-instrument features (ES, RTY, VIX, the 10-year, DX - relative "
                      "strength, NQ's residual against ES, agreement, dispersion), the optional instruments' missing "
                      "indicators and every context instrument's data age at the cutoff (freshness; missing when "
                      "stale) - their freshness limits and market hours as contracts/nq_ml.INSTRUMENTS",
    },
    "candidates": {
        "features": ["dist_atr", "abs_dist_atr", "above", "nearest_side", "rank_side", "coincident"],
        "definition": "first level, per candidate: its signed and absolute distance from the cutoff price in "
                      "two-minute-ATR units, its side, whether it is the nearest on its side, its rank on its side, "
                      "how many candidates share its price; plus its identity - one geometry effect shared by every "
                      "candidate",
    },
}
EVENT_CAP_MINUTES = 420
IDENTITY = ("is_es", "is_rty")              # P: the instrument identity (NQ the reference)


def head_features(target: str, arm: str) -> List[str]:
    """The session-level feature columns of a head for an arm (N and P: NQ's own; M: plus the context group)."""
    cols: List[str] = []
    for g in HEADS[target]["groups"]:
        if g == "candidates":
            continue
        cols += [c for c in FEATURE_GROUPS[g]["features"] if c not in cols]
    if target == "first_level_tested":
        cols += [c for c in FEATURE_GROUPS["geometry"]["features"] if c not in cols]
    if arm == "M":
        cols += FEATURE_GROUPS["context"]["features"]
    return cols


ARMS: Dict[str, Dict[str, Any]] = {
    "N": {"information": "NQ's own history at the cutoff and the common, already-known calendar",
          "population": "earlier NQ sessions", "hypothesis": "NQ's own state predicts the target",
          "symbols": ("NQ",)},
    "M": {"information": "N's features plus timely ES / RTY / VIX / 10-year / DX context and cross-market "
                         "relationships, with missingness, freshness and market hours explicit",
          "population": "earlier NQ sessions, with NQ labels",
          "hypothesis": "other markets add information about this NQ session",
          "symbols": ("NQ",) + tuple(ml.INSTRUMENTS)},
    "P": {"information": "NQ's own features, the calendar and an NQ identity at inference - no other market's "
                         "current data",
          "population": "earlier NQ, ES and RTY sessions, each with its own labels (contracts/market_labels.py)",
          "hypothesis": "shared market behaviour improves learning for NQ",
          "symbols": ("NQ",), "training_symbols": ("NQ", "ES", "RTY")},
}

# --------------------------------------------------------------------------
# Models, selection, pooling, calibration
# --------------------------------------------------------------------------

FAMILIES: Dict[str, Dict[str, Any]] = {
    "logit": {"kinds": ("direction", "classifier", "candidates"),
              "estimator": "multinomial logistic regression (L2, lbfgs) on median-imputed, standardised features",
              "grid": [{"C": c} for c in (0.01, 0.1, 1.0)]},
    "decomp": {"kinds": ("direction",),
               "estimator": "two binary logistic regressions on the same features: q = P(the move ends outside the "
                            "band), r = P(up | outside), then p_up = q r, p_down = q (1 - r), p_neutral = 1 - q",
               "grid": [{"C": c} for c in (0.01, 0.1)]},
    "decomp_sym": {"kinds": ("direction",), "comparator": True,
                   "estimator": "the decomposition with r = 0.5: movement size only, no direction view - the "
                                "stronger symmetric comparator of the evaluation, never selected into a bundle",
                   "grid": [{"C": c} for c in (0.01, 0.1)]},
    "scale": {"kinds": ("direction",),
              "estimator": "a conditional distribution of the move in threshold units z = (close - O) / threshold: "
                           "log(|z| + 0.1) regressed on the features (ridge) gives the scale s(x); the training rows' "
                           "standardised moves z / s(x), symmetrised, form an empirical distribution, and the class "
                           "probabilities are its mass beyond +1 / s(x), below -1 / s(x) and between - each target's "
                           "exact threshold, no Gaussian (or other) tail assumed",
              "grid": [{"alpha": a} for a in (10.0, 100.0)]},
    "scale_loc": {"kinds": ("direction",),
                  "estimator": "the scale model plus a location: the standardised move regressed on the features "
                               "(ridge), the residuals' empirical distribution kept asymmetric - chosen only if the "
                               "inner folds support it",
                  "grid": [{"alpha": a} for a in (10.0,)]},
    "cand": {"kinds": ("candidates",),
             "estimator": "a conditional-logit candidate scorer: each frozen candidate's score is its geometry (signed "
                          "and absolute distance in two-minute ATRs, side, nearest on its side, rank, coincident "
                          "count - one effect shared by every candidate), its side times the recent path (the last "
                          "30 minutes, the premarket return, the range position) and its identity; softmax over the "
                          "session's possible candidates; L2 on every coefficient",
             "grid": [{"l2": v} for v in (1.0, 10.0)]},
    "gbm": {"kinds": ("direction", "classifier", "candidates"),
            "estimator": "HistGradientBoostingClassifier (max_depth 2, min_samples_leaf 20, l2 1.0, learning rate "
                         "0.05, early stopping off, random_state 0) on median-imputed features",
            "grid": [{"max_iter": m} for m in (60, 150)]},
}
FAMILY_ORDER = ("logit", "decomp", "decomp_sym", "scale", "scale_loc", "cand", "gbm")   # ties: the earlier one
POOLING = {
    "partial": "P's logistic-type families (logit, decomp, decomp_sym, scale, scale_loc, cand) fit shared coefficients "
               "on every instrument's rows plus NQ-specific deviations: each standardised feature also enters "
               "multiplied by gamma x is_nq under the same L2 penalty, so a deviation's effective penalty is 1 / "
               "gamma^2 times the shared one and gamma sets the pooling strength (gamma 0: complete pooling). The "
               "instrument intercepts (is_es, is_rty) enter x10 - lightly penalised. The gradient-boosted family pools "
               "completely, with the identity as a feature the trees may split on",
    "gamma_grid": (0.0, 0.5, 1.0),
    "selection": "gamma is chosen with the family's other parameters inside the chronological inner folds, on NQ "
                 "validation rows only - an NQ deviation survives when the ES / RTY relationships transfer poorly",
    "comparators": "complete pooling (gamma 0) and an NQ-only fit of the selected family are evaluated beside it "
                   "(forecaster/ml_bundle_eval.py)",
    "rows": "every instrument's rows of a training date, each with its own label; no row of the prediction date or "
            "later - even another instrument's whose outcome is already observable",
    "min_other_rows": 30,
    "min_other_rows_rule": "a P head with fewer than this many usable ES and RTY rows in its training window is "
                           "unavailable (an NQ-only fit is N's job and is never shown as pooled training)",
    "dependence": "NQ, ES and RTY on one day share its news: correlated observations, not independent sessions - "
                  "selection and uncertainty are by session date",
}
BUDGET = {
    "rule": "per head, every declared configuration of the families its kind allows (for P the logistic-type ones at "
            "every gamma) is fitted on each inner fold and scored by the mean unhalved multiclass Brier score of the "
            "inner validation sessions (NQ rows); the lowest wins, ties to the earlier family in FAMILY_ORDER and "
            "the earlier grid point; nothing else is searched - adding targets does not widen the search",
    "configurations": {k: sum(len(f["grid"]) for f in FAMILIES.values() if k in f["kinds"] and not f.get("comparator"))
                       for k in ("direction", "classifier", "candidates")},
    "configurations_P": {k: sum(len(f["grid"]) * (1 if n == "gbm" else len(POOLING["gamma_grid"]))
                                for n, f in FAMILIES.items() if k in f["kinds"] and not f.get("comparator"))
                         for k in ("direction", "classifier", "candidates")},
    "inner_folds": {"blocks": 3, "block_sessions": 20, "embargo_sessions": 1, "min_train_sessions": 60},
}
SMOOTHING = {
    "pseudo_count": 0.5,
    "rule": "every head's final probabilities are (n p + 0.5) / (n + 0.5 K) over its K classes, n the head's NQ "
            "training sessions with a label (Jeffreys pseudo-counts toward uniform): a class absent from a fold's "
            "training rows keeps a small non-zero probability, so log loss stays finite; the vocabulary is fixed "
            "(every class always present in the distribution). First level: classes made impossible by the label "
            "contract (a candidate sharing its price with one earlier in the precedence) stay exactly 0 and the "
            "pseudo-counts go to the possible candidates",
    "class_balancing": "none: no class weights, no oversampling - either would change the class probabilities "
                       "without a valid correction",
}
CALIBRATION = {
    "options": ("none", "temperature"),
    "rule": "optional, selected from the chosen configuration's chronological out-of-fold predictions of the inner "
            "folds (training window only, grouped by session date): a single temperature (p^(1/tau), renormalised, "
            "tau in 0.5..3 by 0.1) is kept only if, fitted on the other inner blocks, it lowers the held-out block's "
            "Brier score on average; then refitted on every block. Never isotonic, never a manual sharpening",
}
UNSEEN = {
    "training": "a class with no training row in a fold stays in the vocabulary; the estimators predict the classes "
                "they saw and the smoothing gives the others their pseudo-count share",
    "recorded": "each head's manifest lists its per-class training counts and the classes it never saw",
}
SPLIT = {"design": "contracts/nq_ml.SPLIT: session-date folds, every instrument's rows of a date on one side, the "
                   "embargo between training and held-out dates, the pooled candidate scored on NQ rows"}

EVALUATION = {
    "design": "nested chronological evaluation on the research_0929 pool: outer folds by session date as "
              "contracts/nq_ml.DEV_EVALUATION (an initial 120 sessions, blocks of 20, one-session embargo), every "
              "bundle selected and fitted inside its outer training window only (inner folds by date), every "
              "instrument's rows of a date together; the test dates' labels are never passed to a fit",
    "freeze": "the outer predictions and their manifest are written with their sha256 before the scoring stage "
              "reads a test outcome; scoring refuses a file that changed",
    "per_target": "for each arm and target on paired dates: unhalved multiclass Brier score, log loss, calibration "
                  "(per-class reliability, ECE), label coverage (sessions with a classifiable label / scheduled), "
                  "prediction availability, and the paired loss differences against A and B; per-class diagnostics "
                  "for rare labels; probability dispersion and disagreement between the arms",
    "comparators": "A and B (their stored runs of every target), the direction targets' symmetric prior (A with "
                   "bullish = bearish) and the symmetric decomposition (learned q, r = 0.5) - beating a noisy "
                   "directional prior alone is not enough",
    "ablations": "M - N (same families and budget: the information effect), P - its NQ-only fit of the same family "
                 "(the pooling effect), P partial - P complete (the deviation effect) - reported as paired loss "
                 "changes with dispersion and disagreement, selected by forecast quality only",
    "uncertainty": "moving-block bootstrap of per-session paired differences (blocks of 5 sessions, 2000 resamples), "
                   "sessions the unit - never rows",
    "multiplicity": "the primary family is the 21 arm-target claims; a claim needs its interval below zero against "
                    "both A and B (intersection-union: no further adjustment for the pair) at the Bonferroni level "
                    "1 - 0.05 / 21; ablations and every other comparison are descriptive at 95 %",
    "status": "development data: every pool session was inspected before - a result here is a candidate for a "
              "separately reviewed prospective protocol, not a finding",
}
STATUS = {v: {t: "shadow" for t in TARGETS} for v in VERSIONS.values()}
MAX_WAIT_MINUTES = ml.MAX_WAIT_MINUTES          # M waits for the context bars as the v1 multi model does


def features_record() -> Dict[str, Any]:
    return defs._record(FEATURE_VERSION, "ml_features", {
        "targets": list(TARGETS), "classes": {t: list(c) for t, c in CLASSES.items()}, "profile": PROFILE,
        "groups": FEATURE_GROUPS, "event_cap_minutes": EVENT_CAP_MINUTES, "identity": list(IDENTITY),
        "heads": {t: {"groups": list(h["groups"]), "features": {a: head_features(t, a) for a in ARMS}}
                  for t, h in HEADS.items()},
        "v1_features": ml.FEATURE_VERSION, "instruments": {k: v.symbol for k, v in ml.INSTRUMENTS.items()},
        "point_in_time": "the v1 rules (contracts/nq_ml.features_record) for the v1 features; the geometry, path "
                         "extras, volatility and calendar groups read only the snapshot frozen at the cutoff",
        "market_labels": mlab.VERSION,
    })


def schema_record() -> Dict[str, Any]:
    base = fc.forecast_schema_record()["definition"]
    return defs._record(SCHEMA_VERSION, "forecast_schema", {
        **base, "estimation_statuses": list(fc.ESTIMATION_STATUSES) + ["model"],
        "model": "per target a probability distribution from the bundle's head (contracts/nq_ml_bundle.py), rounded "
                 f"to 1/{PROBABILITY_DENOMINATOR:,} with the largest class taking the remainder; a head that cannot "
                 "predict is unavailable with its own reason, never filled from another arm",
        "heads": "the run's outputs and evidence record every head's status, reason, family, parameters, "
                 "calibration, smoothing and training counts; the run is issued when at least one head predicted, "
                 "unavailable when none did, failed when the bundle could not run",
        "first_level_price": "the predicted candidate's frozen price from the snapshot, never a model output",
        "supersedes": f"{ml.SCHEMA_VERSION} for the seven-target bundles; the v1 models and A / B keep theirs"})


def algorithm_definition(version: str, manifest: Dict[str, Any]) -> Dict[str, Any]:
    arm = ARM_OF[version]
    return {
        "name": f"{ARM_LABELS[version]}: the seven P1 targets, one head each",
        "arm": arm, **{k: v for k, v in ARMS[arm].items() if k in ("information", "population", "hypothesis")},
        "targets": list(TARGETS), "classes": {t: list(c) for t, c in CLASSES.items()},
        "feature_version": FEATURE_VERSION, "label_version": defs.LABEL_VERSION, "market_labels": mlab.VERSION,
        "status": STATUS[version],
        "artifact": {k: manifest[k] for k in ("sha256", "training", "software", "path", "data_digest", "heads_digest")},
        "issue": "the stored artifact, checked against its sha256 - never retrained at issue time",
        "delivery": "shadow: never in contracts/nq_ml.delivery_order; B stays in force and A the benchmark",
    }
