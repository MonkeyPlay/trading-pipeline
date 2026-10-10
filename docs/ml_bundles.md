# Seven-target ML bundles (shadow)

Three ML arms that each forecast **all seven** P1 targets, built to find out - honestly - where each improves on
A (the frequencies, the benchmark) and B (the analogues, the incumbent). Producing more fields and establishing an
edge are separate gates: the first is implemented here; the second needs evidence that does not exist yet.

**Where it stands.** The code, tests and commands are in place. Nothing has been trained or evaluated on the
production data (the change was prepared without access to it), so there is **no development result and no
prospective evidence** for any target yet. The bundles run in shadow: issued and shown beside A, B and the
direction-only v1 models, never delivered. B stays in force, A remains the benchmark, and `p1_ml_forward_v2`, its
artifacts, runs and deliveries are unchanged.

Contract: [contracts/nq_ml_bundle.py](../contracts/nq_ml_bundle.py), [contracts/market_labels.py](../contracts/market_labels.py).
Code: [forecaster/ml_bundle.py](../forecaster/ml_bundle.py), [ml_bundle_data.py](../forecaster/ml_bundle_data.py),
[ml_bundle_features.py](../forecaster/ml_bundle_features.py), [ml_bundle_service.py](../forecaster/ml_bundle_service.py),
[ml_bundle_eval.py](../forecaster/ml_bundle_eval.py), [market_labels.py](../forecaster/market_labels.py),
[ml_split.py](../forecaster/ml_split.py). Proposal for a prospective test:
[reports/ml_bundle_forward_proposal.md](reports/ml_bundle_forward_proposal.md).

## The arms: distinct by construction

| Arm (version, display key) | Information at prediction time | Training population | Hypothesis |
|---|---|---|---|
| N `nq_ml_bundle_nq_v1` (N7) | NQ's own history and the common, already-known calendar | earlier NQ sessions | NQ's own state predicts the target |
| M `nq_ml_bundle_multi_v1` (M7) | N's features plus timely ES / RTY / VIX / 10-year / DX context, cross-market relationships, missingness and data ages | earlier NQ sessions, NQ labels | other markets add information about this NQ session |
| P `nq_ml_bundle_pooled_v1` (P7) | NQ's own features, the calendar and an NQ identity - no other market's current data | earlier NQ, ES and RTY sessions, each with its own labels | shared market behaviour improves learning for NQ |

N and P use exactly the same feature columns, so P - N isolates pooling and M - N isolates the information; all
three choose from the same declared families and budget. Nothing forces different algorithms, different classes or
a minimum probability separation: similar tiles are an acceptable answer when the signal is weak.

## The seven targets

The registry is `contracts/nq_forecast.FORECAST_TARGETS` (order) and `contracts/nq_prompt_v2.TARGETS` (classes,
windows, thresholds, rules) - the label version `nq_prompt_v2_1_impl5` exactly.

| Field | Target | Head output |
|---|---|---|
| Opening bias | `opening_bias_30m` | bullish / bearish / neutral band (frozen T) |
| First move | `first_move_5m` | up first / down first / neither - barrier order, never the five-minute close |
| Opening type | `opening_type_15m` | the six canonical opening types |
| First 15-minute direction | `direction_15m` | bullish / bearish / neutral band (frozen T) |
| RTH session type | `session_type_rth` | the five canonical session types |
| RTH close direction | `close_direction_rth` | bullish / bearish / neutral band (frozen B) |
| First level tested | `first_level_tested` | the ten frozen candidate identities; the price is the predicted candidate's frozen price |

**Label contract, preserved:**
- Both first-move barriers in one unresolved minute stay `ambiguous_intrabar`: excluded, counted.
- `none_tested` is an unavailable outcome under FL-v3, not an eleventh class; missing references and ambiguous
  intrabar order are excluded with counts. Candidates at one price are one level named by
  `FIRST_LEVEL_PRECEDENCE`, so the others at that price get probability exactly 0.
- A complete session no session-type rule fits is `uncovered`: excluded, never relabelled a range day.
- Early closes: the standard-session targets (session type, close direction) are never predicted or trained on.
- Every distribution is conditional on a classifiable outcome; each prediction stores label coverage beside it
  (`eligible` = the head's training sessions with a label, `without_label` = those without).

**Training masks are per target:** a missing first-move label never drops the row's close-direction label.
The vocabulary is fixed (a class absent from a fold stays in the distribution); the final probabilities carry
Jeffreys pseudo-counts ((n p + 0.5) / (n + 0.5 K)), recorded per head with the classes it never saw. No class
weights, no oversampling.

**Prediction-time eligibility** follows the label version: a missing T makes the T targets unavailable but not the
close direction (B) or the first level; an unavailable first-level candidate makes only the first level
unavailable. A head that cannot predict is unavailable with its own reason - never filled from another arm.

## Each market's own labels (P)

`contracts/market_labels.py` parameterises the label version by market: only the threshold rounding changes.
NQ's parameters (whole points, at least one) reproduce `threshold_t` / `threshold_b` exactly; the test
`test_each_market_is_labelled_from_its_own_snapshot_and_nq_matches_the_stored_labels` recomputes NQ's stored labels
of every target through the parameterised path. ES and RTY round to their own tick (0.25, 0.1): a whole point is a
quarter of a typical ES two-minute ATR and most of an RTY one. Each market's snapshot - references, ATRs, the ten
first-level candidates - is `features/nq_evidence.build_snapshot` for its own symbol (built, never stored) and its
outcome is `compute_outcome` on its own bars. Nothing of NQ's is copied. The dataset reports per-target,
per-instrument usable counts and reasons (`BundleData.coverage`).

## Features (`nq_ml_features_v2`)

All frozen before the outcome (the snapshot excludes every bar after the cutoff; the v1 features read only
earlier sessions and bars ended by the cutoff, received by the issue time when given):

- **Level geometry:** every candidate's signed distance from the cutoff price in frozen two-minute-ATR units; the
  nearest above / below, how many within T, how many coincident.
- **Path state:** the v1 own features (returns since the previous close, 08:00 and 30 minutes before the cutoff,
  overnight volatility and volume, range position, gap, distances to the previous RTH high / low - normalised by
  earlier sessions) plus overnight and premarket efficiency, the last 30 minutes in two-minute ATRs and against
  the premarket direction.
- **Known volatility:** two-minute against daily ATR, overnight and premarket ranges in daily ATRs.
- **Calendar:** the snapshot's frozen scheduled events (time and tier only, never a surprise or revised value):
  before the cutoff, during RTH, in the first 30 minutes, minutes to the next one; unknown coverage is missing.
- **Context (M only):** the v1 context and cross-instrument features, missing indicators and every context
  instrument's data age, with the v1 freshness limits and market hours.

## Models and selection

Per head kind, a small declared budget (`contracts/nq_ml_bundle.FAMILIES`, `BUDGET`): multinomial logistic
regression (C 0.01 / 0.1 / 1) and shallow boosted trees (60 / 150 iterations) for every head; for the direction
targets also the **q / r decomposition** (q = P(outside the band), r = P(up | outside)), a **conditional scale
distribution** (an empirical distribution of standardised moves, the exact T / B thresholds, no Gaussian tails)
and its location variant; for the first level a **conditional-logit candidate scorer** sharing geometry effects
across candidates. The symmetric decomposition (r = 0.5) is the evaluation's stronger symmetric comparator, never
selected. Per head: 10 configurations for the direction targets, 5 for the classifiers, 7 for the first level
(26 / 11 / 17 for P, with the pooling strengths).

Selection runs inside chronological inner folds by session date (`forecaster/ml_split.py`): three blocks of 20
sessions, one embargoed session, at least 60 training sessions; every candidate is scored by the unhalved Brier
score of NQ's validation rows only, ties to the simpler family. **Calibration** is optional: a single temperature,
kept only when, cross-fitted on the inner out-of-fold blocks, it lowers the held-out Brier; never isotonic, never a
manual sharpening.

**P's partial pooling:** shared coefficients on every instrument's rows plus NQ deviations (each standardised
feature again, times gamma x is_nq, under the same L2), gamma in {0, 0.5, 1} chosen on NQ validation rows - gamma
0 is complete pooling; the boosted family pools completely with the identity as a feature. No row of the
prediction date or later enters training, even another market's whose outcome is already observable. A P head
with fewer than 30 usable ES / RTY rows is unavailable rather than shown as pooled.

## Artifacts and provenance

`python scripts/nq_journal.py ml-bundle-train --until <last training session>` writes
`data/models/nq_ml/<version>/bundle.joblib` and `manifest.json`: targets and classes; label, market-label, feature
and schema versions; per head its status, reason, family, parameters, columns, preprocessing, calibration, smoothing,
training dates and counts per instrument, class counts, unseen classes, selection scores and inner folds; the data,
heads and folds digests; code revision; software versions; the sha256. Issuing refuses an artifact whose bytes
differ from its manifest or its registered definition, or whose targets / classes / feature version are not the
contract's. A version is never overwritten. `scripts/release_check.py artifacts` checks the bundles too, and that
every registered ML definition still has the code's hash.

## Issuing

Auto's journal step (`forecaster/ml_service.issue_pending`) issues each bundle like the v1 models, each arm on its
own readiness: N and P at once (NQ-only features), M once the context bars are in or 30 minutes after the cutoff.
One arm's wait or exception never holds up another. A run is **issued** when at least one head predicted,
**unavailable** (with every head's reason) when none did, **failed** when the bundle could not run - consistent with
the ledger's validators (migration 0014): no schema change is needed. The run's outputs and evidence carry every
head's status, reason, family and calibration, the first level's frozen price and the artifact identity.

## On the Forecast page

Arms N7, M7 and P7 have their own tiles ("k of 7 targets forecast", the unavailable ones' reasons, the artifact,
"shadow"), their dots on the sureness tracks, every class of every target in the class view, the per-target grid
with label coverage and the frozen first-level price, and their heads in the run's provenance. The day's summary
lists each bundle's targets with the differences from A and B in percentage points, apart from the forecast in
force: a bundle is never a source, and a fallback is never counted as an ML prediction.

## Evaluation

`ml-bundle-eval predict` then `score` ([forecaster/ml_bundle_eval.py](../forecaster/ml_bundle_eval.py),
`ml_bundle_eval_v1`): outer folds by session date (an initial 120 sessions, blocks of 20, one-session embargo);
each variant fitted on a view of the dataset whose other dates' labels are blanked; predictions frozen with their
sha256 before any outcome is read. Per target and arm: Brier, log loss, per-class calibration, label coverage,
availability, paired differences against A and B (moving-block bootstrap of per-session differences), claims at
the Bonferroni level over the 21 arm-target claims (both baselines), the symmetric comparators, the ablations
(M - N, P - P's NQ-only fit, P partial - P complete), dispersion and disagreement, rare-class diagnostics and the
sessions a given improvement would need. Development data: a candidate, never a promotion.

**Capability controls** (`tests/test_ml_bundle_models.py`, synthetic): shuffled labels leave no skill; a planted
NQ signal is learned by every arm; a planted cross-market signal by M only; a planted transferable signal makes P
beat N. They verify that the machinery can find these signals - not that the market has them.

## What is not done

- No training, evaluation or prospective test on production data (see "Where it stands").
- No per-target promotion: delivery records one run per session; promoting one head would need its own storage,
  migration and review. The bundles are never in `contracts/nq_ml.delivery_order()`.
- No live issuance of the bundles: the prospective design (live deadline or an earlier effective cutoff) belongs to
  a separately reviewed protocol.
- No joint path model: separate heads may legitimately disagree about different horizons.

## Commands

```bash
python scripts/nq_journal.py ml-bundle-train --until 2026-10-09   # the three bundles (shadow), never overwritten
python scripts/nq_journal.py ml-bundle-eval predict              # nested chronological folds, predictions frozen
python scripts/nq_journal.py ml-bundle-eval score                # docs/reports/ml_bundle_eval_v1.md
python scripts/nq_journal.py summary --date 2026-10-12           # A, B, the v1 models and the bundles
python scripts/release_check.py artifacts                        # every artifact = its manifest = its registration
```
