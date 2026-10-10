# Proposal: a prospective evaluation of the seven-target ML bundles

**Status: a proposal for separate review. Nothing here is registered, scheduled or sent.** `p1_ml_forward_v2`
(its registration, pinned artifacts, runs, deliveries and promotion rule) is unchanged and is not this experiment.
Registering a protocol based on this proposal is a separate, reviewed step with its own name.

## What would be tested

The three bundles of [contracts/nq_ml_bundle.py](../../contracts/nq_ml_bundle.py) - `nq_ml_bundle_nq_v1` (N),
`nq_ml_bundle_multi_v1` (M), `nq_ml_bundle_pooled_v1` (P) - each with a head per P1 target, against A (the
benchmark) and B (the incumbent), target by target. The bundles run in shadow until then: issued and shown,
never delivered, never counted as ML availability when B is delivered.

## Before registering: the development evidence

Run on the production database (not run in the environment this was prepared in, which has no market data):

```
python scripts/nq_journal.py ml-bundle-eval predict      # nested chronological folds; predictions frozen with sha256
python scripts/nq_journal.py ml-bundle-eval score        # docs/reports/ml_bundle_eval_v1.md
```

Its report gives, per target and arm, the paired Brier differences against A and B, the symmetric comparators,
the ablations, coverage and availability, and the per-session variability the sample sizes below need. Only
claims chosen **before** a fresh evaluation, from that development report, may enter the registered family.

## Information timing

A genuinely prospective forecast is fixed before the predicted event begins. The registered replay deadline
(the cutoff + 35 minutes, 10:04 ET) keeps its research meaning for `p1_ml_forward_v2`, but it is after the 09:30-09:45
window and is **not** an actionable pre-open test. The new protocol must choose one of:

1. **Live issue by the live deadline** (09:29:50 ET, `nq_issue_live_v4`) from the data actually received by then -
   on this feed (about 11 minutes late) the research_0929 cutoff bar usually is not, so most sessions would be
   missed opportunities, counted against availability; or
2. **An earlier effective input cutoff** compatible with the feed delay - the `candidate_0915` profile (bars to
   09:15 ET, issue before the open; docs/reports/preopen_timeliness.md measured a margin of about 3.5 minutes).
   That needs its own snapshot pool, labels (its thresholds differ) and retrained bundles.

A and B are evaluated on the same opportunities: the same profile, the same issue deadline, the same sessions.

## Claims

Choose the claim per target explicitly, before the evaluation:

- **Some improvement:** the paired mean difference's interval lies below 0 against both A and B. A point estimate
  of -0.01 with an interval below zero is evidence of *some* improvement, **not** that the true gain exceeds 0.01.
- **Improvement beyond a margin m:** the interval lies below -m against both. This is the claim a practical margin
  needs; with m = 0.01 it requires the sessions of a true gain of 2 x 0.01 under the "some improvement" claim
  (table below).

Recommended: "some improvement" as the decision rule, with the practical margin (0.01 on the unhalved 0-2 Brier
scale, as `p1_ml_forward_v2`) reported beside it and **not** claimed unless its interval clears -0.01.

## Multiplicity

- The registered family is the claims actually chosen, k <= 21 (3 arms x 7 targets). Each claim's interval is
  two-sided at 1 - 0.05 / k (Bonferroni). Requiring both baselines is an intersection-union test: no further
  adjustment for the pair.
- The old three-arm 98.33 % interval is **not** reused for 21 arm-target claims.
- Selection comparisons (M - N, P - its NQ-only fit, P partial - P complete, the symmetric comparators) are
  descriptive at 95 % unless registered as claims, and then they join k.
- Results are reported per target first. An aggregate, if any, needs its weights fixed in the registration and
  must not hide a materially worse target.

## Sample size (from development variability)

Sessions for 80 % power, n = ((z<sub>1-0.05/(2k)</sub> + z<sub>0.8</sub>) σ / (δ - m))<sup>2</sup>, σ the per-session
standard deviation of the paired Brier difference inflated for serial dependence (moving-block bootstrap SE x √n).
The only per-session development scores available today are direction_15m's v1 arms (ml_study_v1, 158 sessions;
[ml_study_v1/scores_sessions.csv.gz](ml_study_v1/scores_sessions.csv.gz)); the bundles' own σ per target come from
`ml-bundle-eval score`, which prints this table for every target.

| comparison (direction_15m, v1 arms) | σ | δ = 0.01, k = 21 | δ = 0.02, k = 21 | δ = 0.05, k = 21 | δ = 0.02, m = 0.01, k = 21 | δ = 0.05, k = 1 |
|---|---:|---:|---:|---:|---:|---:|
| M - A | 0.110 | 1,806 | 452 | 73 | 1,806 | 38 |
| P - A | 0.144 | 3,109 | 778 | 125 | 3,109 | 65 |
| M - B | 0.213 | 6,824 | 1,706 | 273 | 6,824 | 143 |
| P - B | 0.254 | 9,740 | 2,435 | 390 | 9,740 | 204 |

Smallest true improvement detectable with 80 % power (some-improvement claim):

| σ | 60 sessions, k = 21 | 120, k = 21 | 250, k = 21 | 250, k = 3 | 500, k = 21 |
|---:|---:|---:|---:|---:|---:|
| 0.110 (vs A) | 0.055 | 0.039 | 0.027 | 0.022 | 0.019 |
| 0.144 (vs A) | 0.072 | 0.051 | 0.035 | 0.029 | 0.025 |
| 0.213 (vs B) | 0.107 | 0.075 | 0.052 | 0.044 | 0.037 |
| 0.254 (vs B) | 0.127 | 0.090 | 0.062 | 0.052 | 0.044 |

**Reading.** Sixty sessions cannot resolve small gains: against B (the noisier baseline, which every claim must
also beat) 60 sessions detect only improvements of about 0.1. A gain of 0.01 needs thousands of sessions under
21 claims. The registration must therefore (a) keep k small - only targets whose development evidence warrants a
claim - and (b) fix an endpoint long enough for the chosen δ, or accept that the evaluation can only detect large
effects and say so in advance. These σ are the v1 direction arms'; other targets' σ may be larger or smaller and
must be read from the bundles' development report before the endpoint is fixed.

## What is scored, and how

- **Model-only quality:** per arm and target on the paired sessions where the arm, A and B all forecast and the
  label is classifiable: unhalved multiclass Brier (primary), log loss, calibration, per-class diagnostics.
- **The delivery policy:** separately, the complete policy actually in force (B while the bundles are shadow),
  including misses and fallbacks. A fallback is never counted as an ML prediction or ML availability.
- **Eligibility vs failures:** a session whose label cannot exist (an early close for a standard-session target,
  a missing threshold or reference) is ineligible; an eligible session without an issued forecast is a miss,
  counted against availability with its reason.
- **Uncertainty:** moving-block bootstrap of per-session paired differences (sessions the unit; NQ, ES and RTY of
  one date are never separate observations).
- **Frozen predictions:** the ledger is append-only and the scorer reads only runs issued by the deadline under
  the registered artifacts and feature version; the scoring runs once, at the endpoint.

## Per-target promotion (not supported yet)

Delivery today records one run per session (`journal.forecast_deliveries`). Promoting one bundle head (say,
direction) must not silently deliver its six unvalidated siblings. A per-target delivery needs its own storage
(the chosen run and source per target), a migration with dry run and reverse script, and its own review - none of
which is part of this change. Until then the bundles are never in `contracts/nq_ml.delivery_order()`.

## Intraday forecasts are a different contract

The seven targets are pre-open forecasts. A model of the next 5/15/30/60 minutes during the session needs its own
issue-time and horizon contracts (see the full-session RTH analogues, `nq_match_rth_v3` / `rth_session_v1`). A
morning prediction is never overwritten after its outcome is visible, and a later refresh is never called a
forward forecast.
