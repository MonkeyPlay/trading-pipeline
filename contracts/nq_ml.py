# contracts/nq_ml.py
"""
The scikit-learn forecaster for NQ's direction_15m (docs/ml_forecaster.md): what it
predicts, from which instruments and features, under which freshness and missing-data
rules, with which models and tuning budget, and how it is evaluated and delivered.

  TARGET              direction_15m exactly as labelled (contracts/nq_prompt_v2): the
                      09:44 bar's close minus the 09:30 open against the snapshot's
                      frozen T - bullish above T, bearish below -T, else neutral_band
  PROFILE             the research_0929 profile's cutoff (09:29 ET), the cutoff arms A
                      and B forecast from, so all arms meet on identical opportunities
  INSTRUMENTS         the compact context group (ES, RTY, VIX, the 10-year yield
                      future, the dollar index future), each with its role, value kind,
                      trading hours and freshness limit; EXCLUDED the rest, with reasons
                      (docs/reports/instrument_inventory.md measured them)
  FEATURES            the bounded feature set: NQ's own (also computed for ES and RTY for
                      the pooled candidate), market context, cross-instrument, calendar -
                      each normalised by its own instrument's earlier sessions
  CONFIGS             nq_only and multi (NQ plus the context group)
  MODELS / TUNING     regularised logistic regression and a shallow gradient-boosted
                      classifier, each with a small predefined grid chosen on the last
                      part of its own training window
  SPLIT               that part by session date - every instrument's rows of a date
                      together, the embargo before it, the pooled candidate scored on NQ's
                      rows (the v1 artifacts were tuned by row position: TUNING)
  DEV_EVALUATION      the chronological development comparison (walk-forward,
                      one-session embargo); POOLED the separate pooled-training candidate
  STATUS              experimental until FORWARD's promotion rule is met on fresh forward
                      sessions; delivery_order() follows it
  replay_deadline()   when a historical-replay run stops being the session's forecast and
                      becomes a reconstruction

Every definition here is registered (journal.definition_versions) under its version name
when the model artifact it describes exists; a changed definition needs a new name.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, time, timedelta
from typing import Any, Dict, List, Optional, Tuple

from contracts import nq_forecast as fc
from contracts import nq_prompt_v2 as defs

TARGET = "direction_15m"
CLASSES = tuple(sorted(defs.TARGETS[TARGET]["labels"]))        # bearish, bullish, neutral_band
PROFILE = defs.DEFAULT_PROFILE
FEATURE_VERSION = "nq_ml_features_v1"
SCHEMA_VERSION = "nq_forecast_schema_v3"                       # schema v1 plus the 'model' estimation status
ML_NQ_VERSION = "nq_ml_nq_p1_v1"                               # NQ's own features
ML_MULTI_VERSION = "nq_ml_multi_p1_v1"                         # NQ's own plus the context group
ML_POOLED_VERSION = "nq_ml_pooled_p1_v1"                       # NQ's own, trained on NQ, ES and RTY sessions
ALGORITHMS = (ML_NQ_VERSION, ML_MULTI_VERSION, ML_POOLED_VERSION)
CONFIG_OF = {ML_NQ_VERSION: "nq_only", ML_MULTI_VERSION: "multi", ML_POOLED_VERSION: "pooled"}
# The estimator family per model: the lower development Brier score of the walk-forward comparison
# (docs/reports/ml_development.md, 2026-10-09: logistic 0.624 vs boosted 0.637 NQ-only, 0.613 vs 0.635 multi,
# boosted 0.600 vs logistic 0.603 pooled) - a development choice the forward evaluation tests.
FAMILY = {ML_NQ_VERSION: "logit", ML_MULTI_VERSION: "logit", ML_POOLED_VERSION: "gbm"}
VERSION_OF = {v: k for k, v in CONFIG_OF.items()}
ARM_LABELS = {ML_MULTI_VERSION: "ML multi-instrument", ML_NQ_VERSION: "ML NQ-only",
              ML_POOLED_VERSION: "ML pooled (NQ+ES+RTY)",
              fc.BASELINE_VERSION: "B (analogues)", fc.PRIOR_VERSION: "A (frequencies)"}
# The arms the Forecast page and the grading show: A and B (contracts/nq_forecast.ARMS) and the three ML forecasts.
ARMS = {**fc.ARMS, "N": ML_NQ_VERSION, "M": ML_MULTI_VERSION, "P": ML_POOLED_VERSION}
ARM_NAMES = {**fc.ARM_NAMES, "N": "ML NQ-only", "M": "ML multi-instrument", "P": "ML pooled"}


def arm_of(algorithm: str):
    """The arm letter of an algorithm version (A, B, N or M), None for another."""
    return next((a for a, v in ARMS.items() if v == algorithm), None)
# Probabilities are stored exactly (journal.forecast_predictions takes "n/d" fractions summing to 1): the model's
# probabilities rounded to millionths, the largest class taking the rounding remainder.
PROBABILITY_DENOMINATOR = 1_000_000

# --------------------------------------------------------------------------
# Instruments
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Instrument:
    symbol: str
    role: str                     # what it stands for in the features
    value_kind: str               # price (log returns) | yield (changes in yield units) | index_level (log changes)
    max_age_minutes: int          # freshness limit at the cutoff while its market trades
    closed_windows_et: Tuple[Tuple[str, str], ...]   # ET times it does not trade on a session day (start, end)
    closed_max_age_minutes: Optional[int]           # when closed at the cutoff: the last value's age limit
    has_volume: bool
    required: bool                # the multi model abstains without it (else imputed, with a missing indicator)
    reason: str


INSTRUMENTS: Dict[str, Instrument] = {
    "ES": Instrument("ES", "S&P 500 futures - the closest related market; NQ's beta and residual move against it",
                     "price", 5, (("17:00", "18:00"),), None, True, True,
                     "same exchange, session and calendar as NQ, complete 1m history from 2025-06; the market NQ is "
                     "most correlated with, so NQ's move net of it is the cleanest relative-performance measure"),
    "RTY": Instrument("RTY", "Russell 2000 futures - small caps; breadth and divergence", "price", 5,
                      (("17:00", "18:00"),), None, True, False,
                      "same exchange and session as NQ; differs from it more than ES does (small caps vs. large "
                      "tech), which is what a divergence measure needs; history from 2025-09-18, so earlier "
                      "sessions are missing and imputed"),
    "VIX": Instrument("VIX", "Cboe VIX index - implied volatility", "index_level", 20,
                      (("09:15", "09:30"), ("16:15", "03:15")), 20, False, False,
                      "the only volatility series with pre-open values (global trading hours until 09:15 ET); "
                      "its 09:14 bar is the latest value at a 09:29 cutoff, closed in between"),
    "10Y": Instrument("10Y", "Micro 10-Year Yield futures - rates", "yield", 30, (("17:00", "18:00"),), None, True,
                      False, "trades overnight, unlike the cash TNX index (from 08:20 ET); quoted in yield, so "
                             "changes are in yield units, not log returns"),
    "DX": Instrument("DX", "ICE US Dollar Index futures - the dollar", "price", 30, (("17:00", "20:00"),), None,
                     True, False, "the only currency series collected (the cash DXY is not licensed to IB); "
                                  "a futures proxy; its feed is about 20 minutes late, so live it is often stale"),
}
EXCLUDED: Dict[str, str] = {
    "QQQ": "the cash ETF of NQ's own index: no information NQ futures lack, and thin premarket prints",
    "SPY": "the cash ETF of ES's index: redundant with ES, thin premarket prints",
    "IWM": "the cash ETF of RTY's index: redundant with RTY, thin premarket prints",
    "SMH": "semiconductors are a large part of NQ; premarket ETF prints are thin and its feed about 20 minutes "
           "late - a candidate for a later feature set, not the compact first one",
    "TNX": "the cash 10-year yield index starts at 08:20 ET; the 10-year yield future (10Y) covers the overnight",
    "VXN": "no pre-open values (first bar 09:31 ET); VIX stands for implied volatility",
    "CL": "not collected (no stored bars)",
    "GC": "not collected (no stored bars)",
    "DXY": "not licensed to IB, nothing stored; DX stands for the dollar",
    "US2Y": "no source collected",
}
ROLLING_SESSIONS = 60            # normalisation, beta and correlation windows (earlier sessions only)
ROLLING_MIN_SESSIONS = 20        # fewer earlier sessions: the feature is missing
VOLUME_SESSIONS = 20
ATR_SESSIONS = 14

# --------------------------------------------------------------------------
# Features
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class FeatureSpec:
    name: str
    group: str                    # own | context | cross | calendar | indicator
    sources: Tuple[str, ...]      # instruments; "X" = the instrument the own features are computed for
    definition: str
    normalisation: str


_Z = f"divided by its standard deviation over the instrument's previous {ROLLING_SESSIONS} sessions (at least " \
     f"{ROLLING_MIN_SESSIONS})"
OWN_FEATURES: List[FeatureSpec] = [
    FeatureSpec("ret_on", "own", ("X",), "log(price at the cutoff / previous session's close at 16:00 ET)", _Z),
    FeatureSpec("ret_pm", "own", ("X",), "log(price at the cutoff / price at 08:00 ET)", _Z),
    FeatureSpec("ret_30", "own", ("X",), "log(price at the cutoff / price 30 minutes before it)", _Z),
    FeatureSpec("rv_on", "own", ("X",), "standard deviation of the 1m log returns from 18:00 ET to the cutoff",
                f"log of its ratio to the median of the previous {ROLLING_SESSIONS} sessions"),
    FeatureSpec("vol_on", "own", ("X",), "contracts traded from 18:00 ET to the cutoff",
                f"log of its ratio to the mean of the previous {VOLUME_SESSIONS} sessions"),
    FeatureSpec("range_pos", "own", ("X",), "where the cutoff price sits in the overnight high-low range, -0.5 at "
                                           "the low to +0.5 at the high", "none (already scale-free)"),
    FeatureSpec("gap_atr", "own", ("X",), "cutoff price minus the previous session's 16:00 close",
                f"in units of the mean daily RTH true range of the previous {ATR_SESSIONS} sessions"),
    FeatureSpec("dist_high_atr", "own", ("X",), "cutoff price minus the previous session's RTH high",
                "in the same daily-range units"),
    FeatureSpec("dist_low_atr", "own", ("X",), "cutoff price minus the previous session's RTH low",
                "in the same daily-range units"),
]
CONTEXT_FEATURES: List[FeatureSpec] = [
    FeatureSpec("es_ret_on", "context", ("ES",), "ES's ret_on", _Z),
    FeatureSpec("es_ret_pm", "context", ("ES",), "ES's ret_pm", _Z),
    FeatureSpec("es_ret_30", "context", ("ES",), "ES's ret_30", _Z),
    FeatureSpec("rty_ret_on", "context", ("RTY",), "RTY's ret_on", _Z),
    FeatureSpec("rty_ret_pm", "context", ("RTY",), "RTY's ret_pm", _Z),
    FeatureSpec("vix_chg_on", "context", ("VIX",), "log(VIX at the cutoff / VIX at the previous 16:00 close)", _Z),
    FeatureSpec("vix_level", "context", ("VIX",), "log VIX at the cutoff",
                f"minus its mean over the previous {ROLLING_SESSIONS} sessions, {_Z}"),
    FeatureSpec("y10_chg_on", "context", ("10Y",), "10-year yield at the cutoff minus at the previous 16:00 close "
                                                  "(yield units)", _Z),
    FeatureSpec("dx_ret_on", "context", ("DX",), "DX's ret_on", _Z),
]
CROSS_FEATURES: List[FeatureSpec] = [
    FeatureSpec("nq_es_resid_on", "cross", ("NQ", "ES"),
                "NQ's overnight log return minus beta x ES's, beta from the previous sessions' overnight returns",
                f"divided by the residual's standard deviation over the previous {ROLLING_SESSIONS} sessions"),
    FeatureSpec("nq_es_resid_pm", "cross", ("NQ", "ES"), "the same over the premarket (08:00 ET to the cutoff)",
                "the same"),
    FeatureSpec("nq_es_corr", "cross", ("NQ", "ES"),
                f"correlation of the two overnight returns over the previous {ROLLING_SESSIONS} sessions",
                "none (in [-1, 1])"),
    FeatureSpec("eq_agree", "cross", ("NQ", "ES", "RTY"),
                "mean sign of NQ's, ES's and RTY's overnight returns (agreement)", "none (in [-1, 1])"),
    FeatureSpec("eq_dispersion", "cross", ("NQ", "ES", "RTY"),
                "standard deviation of the three normalised overnight returns (divergence)", "none (z units)"),
    FeatureSpec("nq_rty_spread_pm", "cross", ("NQ", "RTY"), "NQ's normalised premarket return minus RTY's",
                "none (z units)"),
]
CALENDAR_FEATURES: List[FeatureSpec] = [
    FeatureSpec("event_preopen", "calendar", (), "a high-tier scheduled US economic release between the previous "
                                                "close and the cutoff (the snapshot's frozen events)", "0/1"),
    FeatureSpec("event_session", "calendar", (), "a high-tier release scheduled between the cutoff and 16:00 ET",
                "0/1"),
]
# the optional instruments' missing indicators (1 when any of its features is missing)
INDICATOR_FEATURES: List[FeatureSpec] = [
    FeatureSpec(f"{s.lower()}_missing", "indicator", (s,), f"1 when any {s} feature is missing at the cutoff",
                "0/1") for s in INSTRUMENTS if not INSTRUMENTS[s].required]
CONFIGS: Dict[str, List[str]] = {
    "nq_only": [f"nq_{f.name}" for f in OWN_FEATURES] + [f.name for f in CALENDAR_FEATURES],
}
CONFIGS["multi"] = (CONFIGS["nq_only"] + [f.name for f in CONTEXT_FEATURES] + [f.name for f in CROSS_FEATURES]
                    + [f.name for f in INDICATOR_FEATURES])
# The pooled candidate's rows: NQ, ES and RTY, each with its own features (no NQ prefix) and its identity.
POOLED_INSTRUMENTS = ("NQ", "ES", "RTY")
POOLED_FEATURES = [f.name for f in OWN_FEATURES] + [f.name for f in CALENDAR_FEATURES] + \
    [f"is_{s.lower()}" for s in POOLED_INSTRUMENTS[1:]]
CONFIGS["pooled"] = POOLED_FEATURES                 # an NQ row: its own features, is_es = is_rty = 0

# --------------------------------------------------------------------------
# Models, tuning, evaluation
# --------------------------------------------------------------------------

MODELS: Dict[str, Dict[str, Any]] = {
    "logit": {"estimator": "sklearn LogisticRegression (multinomial, L2, lbfgs, max_iter 2000)",
              "preprocessing": "median imputation, then standard scaling - both fitted on the training window",
              "grid": {"C": [0.01, 0.03, 0.1, 0.3]}},
    "gbm": {"estimator": "sklearn HistGradientBoostingClassifier (max_depth 2, min_samples_leaf 20, "
                         "l2_regularization 1.0, early stopping off, random_state 0)",
            "preprocessing": "median imputation fitted on the training window",
            "grid": {"learning_rate": [0.03, 0.1], "max_iter": [50, 150]}},
}
TUNING = {"rule": "per training window, every grid point is fitted on its first 75 % (in time order) and scored by "
                  "the mean unhalved multiclass Brier score on the last 25 %; the best is refitted on the whole "
                  "window. Nothing else is selected: the feature sets are fixed above, and there is no calibration "
                  "step (calibration is reported, not fitted)",
          "validation_share": 0.25, "budget": "4 logistic + 4 boosted fits per training window",
          # 2026-10-10 review: the v1 artifacts and ml_study_v1 cut by ROW position (forecaster/ml_model.
          # tune_rows_legacy). For N and M (one row per session, date order) that was chronological without the
          # embargo; for the pooled rows (instrument first, date second) the validation rows were RTY's on dates also
          # in training. SPLIT is the correction; the legacy cut stays only to reproduce the registered artifacts.
          "implementation": "legacy_rows for the registered v1 artifacts (reproduction only); SPLIT otherwise"}
DEV_EVALUATION = {
    "design": "chronological walk-forward over the research_0929 pool: train on every earlier session (expanding), "
              "test the next block, embargo one session between them",
    "initial_train_sessions": 120, "test_block_sessions": 20, "embargo_sessions": 1,
    "embargo_why": "direction_15m ends at 09:45 on its own session, so outcomes cannot overlap across sessions; the "
                   "one-session embargo keeps even a revised previous-day bar out of the next fold's test",
    "arms": "A (frequencies), B (analogues) - their stored historical-replay runs - and the four ML fits: nq_only "
            "and multi, each with logit and gbm",
    "common": "every comparison on the sessions where all compared arms have a forecast and the session a realised "
              "label; availability is reported over all scheduled test sessions",
    "primary": "mean unhalved multiclass Brier score, sum over the classes of (p - outcome)^2, 0 to 2",
    "secondary": "log loss (natural log), calibration (reliability by predicted-probability bin, expected "
                 "calibration error), availability, inference latency",
    "uncertainty": "moving-block bootstrap of the paired per-session differences (blocks of 5 sessions, 2000 "
                   "resamples, seed 20261009), 95 % percentile interval",
    "status": "development data: every one of these sessions has been inspected before (stage-4 hist_dev_v1, "
              "p1_pool_tuning_v1, the fan experiments) - an improvement here is a candidate, not a result",
}
# The session-date split of every inner and outer fold (forecaster/ml_split.py, docs/reports/ml_pooled_split_v1.md).
SPLIT = {
    "unit": "the session date: rows carry their date and instrument, every split is made on the sorted unique dates, "
            "never on row positions - all instruments' rows of a date fall on one side",
    "inner": "the last validation_share of the training window's dates validate; the embargo dates before them leave "
             "inner training; the best grid point is refitted on the whole window",
    "embargo_sessions": DEV_EVALUATION["embargo_sessions"],
    "objective": "the validation rows of NQ only - the pooled candidate trains on NQ's, ES's and RTY's rows, but "
                 "deployment predicts NQ",
    "preprocessing": "inside each fit's pipeline: fitted on that fit's training rows only",
    "calibration": "when a calibration is fitted (the seven-target bundles), from out-of-fold predictions of "
                   "date-grouped inner folds",
    "uncertainty": "per-session (per-date) paired differences resampled in moving blocks of sessions - never rows",
}
POOLED = {
    "question": "does training on ES and RTY sessions as well as NQ's improve the forecast for NQ?",
    "instruments": "NQ, ES and RTY: the same exchange, session hours, holidays and opening auction, and each has a "
                   "liquid opening; VIX, the 10-year and DX have no comparable opening move",
    "target": "each instrument's own direction_15m: its 09:44 close minus its 09:30 open against T_X = 0.5 x its own "
              "frozen-at-the-cutoff two-minute ATR (71 complete 2m buckets) - NQ's own label is its stored one, "
              "whose T is the same rule rounded up to a whole point",
    "features": "each instrument's own features, normalised by its own earlier sessions (no raw prices or "
                "points), plus the calendar features and an identity indicator per non-NQ instrument",
    "identity": "is_es and is_rty indicators (NQ is the reference); the gradient-boosted fit can interact them "
                "with any feature",
    "split": "by session date: every instrument's row of one session falls in the same fold, train or test; the "
             "one-session embargo applies to all of them",
    "measured_on": "NQ's test rows only, against the NQ-trained nq_only fit of the same family on the same "
                   "sessions; promoted only if it improves on it",
    "caveat": "three instruments on one day are not three independent histories: they share the day's news, so "
              "the effective sample grows far less than threefold",
}
FORWARD = {
    "name": "p1_ml_forward_v2",
    "design": "prospective sessions only, from the first session after deployment: the runs Auto issues at each "
              "snapshot by its replay deadline (historical-replay mode, as A and B), scored once at the endpoint",
    "endpoint_sessions": 60,
    "candidates": ("N", "P", "M"),             # the selection order: fewest live inputs first
    "baselines": ("B", "A"),
    "minimum_improvement": "0.01",
    "alpha": "0.05",
    "availability": "0.90",                    # the on-time share the user set for a forecast arm (2026-10-09, arm D)
    "seed": 20261012,
}
# Registered forward evaluations that are never scored: v1 was registered from 363c818 on 2026-10-09 17:55 UTC,
# before the review's promotion rule and version pins - v2 replaces it before any forward session.
SUPERSEDED = {"p1_ml_forward_v1": "registered from 363c818 before the review's promotion rule, version pins and "
                                  "reconstruction rule; replaced by p1_ml_forward_v2 before any forward session - "
                                  "never scored"}
# Experimental until FORWARD's promotion rule is met; delivery_order() then puts the promoted model first.
STATUS = {ML_MULTI_VERSION: "experimental", ML_NQ_VERSION: "experimental", ML_POOLED_VERSION: "experimental"}
# Replay forecasts wait for the context instruments' cutoff bars up to this long after the cutoff, then issue with
# whatever is fresh (a stale optional instrument is imputed and flagged; a stale required one makes the multi model
# abstain); NQ's own features come from the snapshot, which already waited for NQ's cutoff bar.
MAX_WAIT_MINUTES = 30
# A historical-replay run is the session's forecast only when Auto issued it that morning: by the cutoff plus the
# context wait plus five minutes for Auto's cycle (10:04 ET for the 09:29 cutoff), by the database clock - the margin
# keeps a busy cycle at the end of the wait from turning a morning run into one. A later run - a catch-up
# after downtime, such as the deployment's first runs for 2026-10-09 - is a reconstruction: stored and shown as one,
# never in force (no delivery is recorded after the deadline) and never a case of the forward evaluation.
REPLAY_DEADLINE_MINUTES = MAX_WAIT_MINUTES + 5


def replay_deadline(cutoff_at: datetime) -> datetime:
    """The latest issue time at which a historical-replay run of a snapshot with this cutoff is its session's
    forecast; later it is a reconstruction."""
    return cutoff_at + timedelta(minutes=REPLAY_DEADLINE_MINUTES)


def reconstruction(run: Dict[str, Any]) -> bool:
    """A historical-replay run issued after its replay deadline (see REPLAY_DEADLINE_MINUTES)."""
    if run.get("mode") != "historical_replay" or run.get("issued_at") is None:
        return False
    return _instant(run["issued_at"]) > replay_deadline(_instant(run["input_cutoff_at"]))


def _instant(value) -> datetime:
    from datetime import timezone
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


class NotRegistrable(ValueError):
    """The forward evaluation cannot be registered as it stands (an artifact missing or not the registered one, or
    code that is not a clean commit)."""


def promotion_rule() -> Dict[str, Any]:
    """FORWARD's promotion rule as registered text and parameters - applied mechanically at the endpoint
    (forecaster/experiments.promotion)."""
    k = len(FORWARD["candidates"])
    level = 1 - float(FORWARD["alpha"]) / k
    return {
        "candidates": {a: ARMS[a] for a in FORWARD["candidates"]},
        "baselines": "B, the forecast in force as the existing baseline, and A, the frequencies, the benchmark: a "
                     "candidate must beat both - the development comparison did not establish B as better than A",
        "comparison": "per candidate X and baseline Y, the mean per-session difference X - Y of the primary score on "
                      "the common sessions (every one of A, B, N, M and P scored), with a moving-block bootstrap "
                      "percentile interval (blocks of 5 sessions, 2000 resamples, seed "
                      f"{FORWARD['seed']})",
        "minimum_improvement": FORWARD["minimum_improvement"],
        "threshold": f"the mean difference at most -{FORWARD['minimum_improvement']} (absolute, unhalved 0-2 scale) "
                     "with the whole interval below zero - evidence of some improvement with a point estimate at the "
                     f"threshold, not that the true improvement is at least {FORWARD['minimum_improvement']}",
        "interval_level": round(level, 6), "block_sessions": 5, "resamples": 2000, "seed": FORWARD["seed"],
        "multiple_comparisons": f"Bonferroni over the {k} candidates: every interval the promotion and the selection "
                                f"use is two-sided at 1 - {FORWARD['alpha']}/{k} ({100 * level:.2f} %); clearing both "
                                "baselines is an intersection-union test, so the pair needs no further adjustment. "
                                "Every other comparison is reported at 95 %, descriptive only",
        "availability": f"on time in at least {float(FORWARD['availability']):.0%} of all scheduled sessions: an "
                        "issued run by the replay deadline under the registered artifact and feature version; "
                        "unavailable, failed, missing and reconstructed runs count against it - the rate, n and the "
                        "exact (Clopper-Pearson) 95 % interval are reported",
        "availability_minimum": FORWARD["availability"],
        "pooled": "P qualifies only if it also meets the rule against N: pooled training must improve on the "
                  "NQ-trained model",
        "selection": "none qualifies: nothing is promoted - B stays in force as the existing baseline and A remains "
                     "the benchmark; one qualifies: that one; several: start from the first qualifying candidate in "
                     f"the order {', '.join(FORWARD['candidates'])} (fewest live inputs first) and move to a later "
                     "qualifying one only if it meets the same rule against the current choice",
        "order": list(FORWARD["candidates"]),
        "effect": "the scoring states which model qualifies; a promotion is a change of contracts/nq_ml.STATUS in a "
                  "new commit, deployed - nothing changes by itself",
    }


def forward_manifest(conn, start: str, end: str, root: Optional[str] = None) -> Dict[str, Any]:
    """The forward ML evaluation (FORWARD) over the prospective sessions [start, end]: the promotion rule and the
    exact code, feature and model-artifact versions it evaluates. Registering it writes one definition row; it sends
    and schedules nothing (Auto issues the runs it scores). Raises NotRegistrable unless every model's artifact is
    stored, matches its registered definition, and the code is a clean commit."""
    from database import journal_store as store
    from forecaster import experiments as ex
    from forecaster import ml_model as mm
    from forecaster.provenance import code_revision
    revision = code_revision()
    if revision == "unknown" or revision.endswith("+dirty"):
        raise NotRegistrable(f"the code is not a clean commit ({revision}): register from a deployed revision")
    models = {}
    for arm in FORWARD["candidates"]:
        version = ARMS[arm]
        man = mm.manifest(version, root)
        if man is None:
            raise NotRegistrable(f"{version} has no stored artifact")
        registered = store.get_version(conn, version)
        if registered is None or registered["definition"]["artifact"]["sha256"] != man["sha256"]:
            raise NotRegistrable(f"{version}: the stored artifact ({man['sha256'][:12]}) is not the registered one")
        models[arm] = {"algorithm": version, "sha256": man["sha256"], "family": man["family"], "params": man["params"],
                       "training": {k: man["training"][k] for k in ("from", "to", "sessions", "rows")},
                       "software": man["software"], "definition_hash": registered["definition_hash"]}
    m = ex.experiment_manifest(FORWARD["name"], start, end, profile=PROFILE, purpose="test",
                               official_run="first_forward", mode="historical_replay",
                               arms={a: v for a, v in ARMS.items()})
    m["arms"]["delivered"] = {"algorithm": "delivered", "delivered": "recorded",
                              "question": "the forecast actually in force: the session's first recorded delivery "
                                          "(journal.forecast_deliveries), decided by the replay deadline - B, then A, "
                                          "while the ML forecasts are experimental"}
    m.update({
        "design": FORWARD["design"],
        "versions": {"code_revision": revision, "feature_version": FEATURE_VERSION, "forecast_schema": SCHEMA_VERSION,
                     "models": models},
        "pins": {arm: {"sha256": v["sha256"], "feature_version": FEATURE_VERSION} for arm, v in models.items()},
        "replay_deadline": f"the cutoff plus {REPLAY_DEADLINE_MINUTES} minutes (10:04 ET for the 09:29 cutoff), by "
                           "the database clock: a run issued later is a reconstruction, never a case",
        "pairs": [["N", "B"], ["P", "B"], ["M", "B"], ["N", "A"], ["P", "A"], ["M", "A"], ["P", "N"], ["M", "N"],
                  ["M", "P"], ["B", "A"]],
        "pairs_note": "reported at 95 %, descriptive; the promotion rule recomputes its comparisons at its own level",
        "common_arms": ["A", "B", "N", "M", "P"],
        "primary": {"target": TARGET, "metric": "multiclass Brier score, the unhalved sum over the classes "
                                                "sum_c (p_c - [c realised])^2, 0 to 2 per session, lower is better"},
        "companions": ["multiclass log loss (natural log), lower is better",
                       "calibration (reliability of the issued probabilities)",
                       "accuracy of the issued class (ambiguous predictions counted apart)"],
        "promotion": promotion_rule(),
        "endpoint": {"sessions": FORWARD["endpoint_sessions"],
                     "rule": f"scored once, after the {FORWARD['endpoint_sessions']}th scheduled session from the "
                             "start has its outcome, or after the end date: experiment-score refuses before; "
                             "availability may be checked before, never a score"},
        "availability_rule": "every scheduled session is an opportunity for every arm: a run that is unavailable, "
                             "failed, missing or a reconstruction counts against it (results.availability)",
        "development_note": "the arms' development comparison (docs/reports/ml_development.md) used sessions that "
                            "were inspected before; only these prospective sessions can confirm an improvement",
        "controls": "registering writes one definition; the runs are those Auto issues at each snapshot",
    })
    return m


def delivery_order() -> List[str]:
    """The forecast in force, most preferred first: a promoted ML model (multi-instrument, then NQ-only), then B,
    then A."""
    ml = [v for v in (ML_MULTI_VERSION, ML_NQ_VERSION, ML_POOLED_VERSION) if STATUS[v] == "production"]
    return ml + [fc.BASELINE_VERSION, fc.PRIOR_VERSION]


def feature_specs() -> Dict[str, FeatureSpec]:
    out = {f"nq_{f.name}": FeatureSpec(f"nq_{f.name}", "own", ("NQ",), f.definition, f.normalisation)
           for f in OWN_FEATURES}
    for f in CONTEXT_FEATURES + CROSS_FEATURES + CALENDAR_FEATURES + INDICATOR_FEATURES:
        out[f.name] = f
    return out


def features_record() -> Dict[str, Any]:
    return defs._record(FEATURE_VERSION, "ml_features", {
        "target": TARGET, "profile": PROFILE, "classes": list(CLASSES),
        "instruments": {k: asdict(v) for k, v in INSTRUMENTS.items()}, "excluded": EXCLUDED,
        "rolling_sessions": ROLLING_SESSIONS, "rolling_min_sessions": ROLLING_MIN_SESSIONS,
        "volume_sessions": VOLUME_SESSIONS, "atr_sessions": ATR_SESSIONS,
        "features": {k: asdict(v) for k, v in feature_specs().items()}, "configs": CONFIGS,
        "pooled": {"instruments": list(POOLED_INSTRUMENTS), "features": POOLED_FEATURES, **POOLED},
        "availability": "each input is the last completed 1m bar ending at or before the cutoff (a bar is complete "
                        "once its minute has ended) that was received by the as-of time when receipts exist; a "
                        "historical bar without a live receipt is a reconstruction; an input older than its "
                        "instrument's freshness limit is stale and missing; a market closed at the cutoff uses "
                        "its last value within closed_max_age_minutes; nothing is forward-filled beyond those "
                        "limits and nothing missing is read as a zero return",
        "point_in_time": "rolling statistics, normalisation, beta and correlation use earlier sessions only; "
                         "imputation and scaling are fitted on the training window only (inside the model "
                         "artifact)",
    })


def schema_record() -> Dict[str, Any]:
    base = fc.forecast_schema_record()["definition"]
    return defs._record(SCHEMA_VERSION, "forecast_schema", {
        **base, "estimation_statuses": list(fc.ESTIMATION_STATUSES) + ["model"],
        "model": "a probability distribution from a trained scikit-learn model (contracts/nq_ml.py) over the "
                 f"target's classes, rounded to 1/{PROBABILITY_DENOMINATOR:,} with the largest class taking the "
                 "remainder; only the targets the model covers are predicted, the others are unavailable with "
                 "the reason",
        "supersedes": f"{fc.FORECAST_SCHEMA_VERSION} for ML runs; the deterministic arms keep it"})


def algorithm_definition(version: str, manifest: Dict[str, Any]) -> Dict[str, Any]:
    """An ML algorithm's registered definition, from its trained artifact's manifest (forecaster/ml_model.py)."""
    return {
        "name": f"{ARM_LABELS[version]} (scikit-learn): direction_15m from {CONFIG_OF[version]} features"
                + (" - trained on NQ's, ES's and RTY's sessions, each with its own label (contracts/nq_ml.POOLED)"
                   if version == ML_POOLED_VERSION else ""),
        "target": TARGET, "config": CONFIG_OF[version], "feature_version": FEATURE_VERSION,
        "features": CONFIGS[CONFIG_OF[version]], "status": STATUS[version],
        "artifact": {k: manifest[k] for k in ("sha256", "family", "params", "training", "software", "path")},
        "inputs": "the snapshot's session and cutoff; NQ's own features from the stored bars and the snapshot's "
                  "frozen events, the context instruments' from their stored bars as of the cutoff and of the "
                  "issue time (contracts/nq_ml.features_record)",
        "issue": "the stored artifact, loaded and checked against its sha256 - never retrained at issue time",
        "fallback": "the multi-instrument model abstains (unavailable, with the reason) when a required instrument "
                    "is stale or missing; the delivery order then passes to the next forecast "
                    "(forecaster/delivery.py)",
        "other_targets": "not predicted: unavailable, with the reason; the summary shows B's forecast for them, "
                         "named as B's",
    }
