# NQ direction model: development comparison

**Development data, not a test.** Every one of these sessions has been inspected before (hist_dev_v1, p1_pool_tuning_v1, the fan experiments). An improvement here would be a candidate for the forward evaluation (p1_ml_forward_v1), not a result.

- **Target:** direction_15m, exactly as labelled (bullish above T, bearish below -T, else the neutral band).
- **Sessions:** 279 in the research_0929 pool (278 with a label); walk-forward test span 2026-02-24 to 2026-10-09 (159 sessions, 8 folds: train on every earlier session, one-session embargo, test the next 20).
- **Compared on** the 158 test sessions where every arm has a forecast and the session a label (classes: bearish 65, bullish 74, neutral_band 19).
- **Primary score:** the unhalved multiclass Brier score (0 to 2, lower is better); intervals are 95 % moving-block bootstrap intervals (blocks of 5 sessions) of the paired per-session differences.

## Scores on the common sessions

| arm | Brier | log loss | calibration error (mean over classes) | available / scheduled |
|---|---:|---:|---:|---:|
| A frequencies | 0.6019 | 0.9828 | 0.022 | 159 / 159 |
| B analogues | 0.6191 | 1.0190 | 0.079 | 159 / 159 |
| NQ-only, logistic | 0.6238 | 1.0618 | 0.065 | 159 / 159 |
| NQ-only, boosted | 0.6373 | 1.0586 | 0.097 | 159 / 159 |
| multi-instrument, logistic | 0.6132 | 1.0069 | 0.060 | 159 / 159 |
| multi-instrument, boosted | 0.6347 | 1.0775 | 0.098 | 159 / 159 |
| pooled (NQ+ES+RTY), logistic | 0.6034 | 0.9976 | 0.033 | 159 / 159 |
| pooled (NQ+ES+RTY), boosted | 0.5999 | 0.9993 | 0.032 | 159 / 159 |

## Paired differences (negative favours the first arm)

| comparison | sessions | Brier difference | 95 % interval | log-loss difference | 95 % interval |
|---|---:|---:|---|---:|---|
| NQ-only, logistic - A frequencies | 158 | 0.0219 | [+0.0020, +0.0443] | 0.0790 | [+0.0115, +0.1739] |
| NQ-only, boosted - A frequencies | 158 | 0.0353 | [+0.0071, +0.0639] | 0.0758 | [+0.0194, +0.1346] |
| multi-instrument, logistic - A frequencies | 158 | 0.0112 | [-0.0069, +0.0289] | 0.0241 | [-0.0038, +0.0573] |
| multi-instrument, boosted - A frequencies | 158 | 0.0328 | [+0.0017, +0.0637] | 0.0947 | [+0.0302, +0.1771] |
| NQ-only, logistic - B analogues | 158 | 0.0047 | [-0.0316, +0.0386] | 0.0427 | [-0.0319, +0.1299] |
| NQ-only, boosted - B analogues | 158 | 0.0181 | [-0.0220, +0.0593] | 0.0396 | [-0.0298, +0.1097] |
| multi-instrument, logistic - B analogues | 158 | -0.0059 | [-0.0386, +0.0272] | -0.0121 | [-0.0574, +0.0348] |
| multi-instrument, boosted - B analogues | 158 | 0.0156 | [-0.0275, +0.0580] | 0.0584 | [-0.0141, +0.1454] |
| multi-instrument, logistic - NQ-only, logistic | 158 | -0.0107 | [-0.0302, +0.0099] | -0.0548 | [-0.1240, -0.0004] |
| multi-instrument, boosted - NQ-only, boosted | 158 | -0.0026 | [-0.0297, +0.0286] | 0.0189 | [-0.0227, +0.0775] |
| B analogues - A frequencies | 158 | 0.0172 | [-0.0132, +0.0468] | 0.0362 | [-0.0073, +0.0810] |
| pooled (NQ+ES+RTY), logistic - NQ-only, logistic | 158 | -0.0204 | [-0.0350, -0.0050] | -0.0642 | [-0.1273, -0.0158] |
| pooled (NQ+ES+RTY), boosted - NQ-only, boosted | 158 | -0.0373 | [-0.0647, -0.0068] | -0.0593 | [-0.1068, -0.0034] |
| pooled (NQ+ES+RTY), logistic - A frequencies | 158 | 0.0015 | [-0.0108, +0.0152] | 0.0148 | [-0.0138, +0.0505] |
| pooled (NQ+ES+RTY), boosted - A frequencies | 158 | -0.0020 | [-0.0230, +0.0205] | 0.0165 | [-0.0288, +0.0705] |
| pooled (NQ+ES+RTY), logistic - B analogues | 158 | -0.0157 | [-0.0474, +0.0179] | -0.0214 | [-0.0696, +0.0291] |
| pooled (NQ+ES+RTY), boosted - B analogues | 158 | -0.0192 | [-0.0573, +0.0212] | -0.0197 | [-0.0789, +0.0439] |

## Reading

- **Do the other instruments help (logit)?** no difference established (the interval spans zero): -0.0107 [-0.0302, +0.0099].
- **Do the other instruments help (gbm)?** no difference established (the interval spans zero): -0.0026 [-0.0297, +0.0286].
- **Pooled training (logit):** better on NQ, interval below zero: -0.0204 [-0.0350, -0.0050] - development data: a candidate (arm P, experimental) that only the forward evaluation can promote.
- **Pooled training (gbm):** better on NQ, interval below zero: -0.0373 [-0.0647, -0.0068] - development data: a candidate (arm P, experimental) that only the forward evaluation can promote.
- **multi-instrument, logistic against B:** -0.0059 [-0.0386, +0.0272].
- **NQ-only, logistic against B:** 0.0047 [-0.0316, +0.0386].
- **multi-instrument, boosted against B:** 0.0156 [-0.0275, +0.0580].
- **NQ-only, boosted against B:** 0.0181 [-0.0220, +0.0593].
- **pooled (NQ+ES+RTY), logistic against B:** -0.0157 [-0.0474, +0.0179].
- **pooled (NQ+ES+RTY), boosted against B:** -0.0192 [-0.0573, +0.0212].

Pooled training rows: NQ 278, ES 279, RTY 267 - three instruments on one day share its news, so the effective sample grows far less than threefold.

## Chosen for the artifacts

The family with the lower development Brier score per configuration (a development choice; the forward evaluation tests it):

- **nq_only:** logit
- **multi:** logit
- **pooled:** gbm

## Inference latency

From nothing loaded to probabilities - the session's features as of the cutoff from the database, the artifact loaded and checked, the prediction:

- **nq_ml_nq_p1_v1:** median 5.15 s, slowest 5.28 s (5 issues)
- **nq_ml_multi_p1_v1:** median 5.08 s, slowest 5.32 s (5 issues)
- **nq_ml_pooled_p1_v1:** median 5.17 s, slowest 5.22 s (5 issues)

## Missing inputs (share of pool sessions, multi-instrument features)

- rty_ret_on: 11.5%
- rty_ret_pm: 11.5%
- nq_rty_spread_pm: 11.5%
- dx_ret_on: 10.0%
- y10_chg_on: 7.5%

## Calibration (reliability, common sessions)

- **A frequencies:** bearish ECE 0.023; bullish ECE 0.033; neutral_band ECE 0.010
- **B analogues:** bearish ECE 0.078; bullish ECE 0.091; neutral_band ECE 0.067
- **NQ-only, logistic:** bearish ECE 0.054; bullish ECE 0.079; neutral_band ECE 0.060
- **NQ-only, boosted:** bearish ECE 0.117; bullish ECE 0.132; neutral_band ECE 0.042
- **multi-instrument, logistic:** bearish ECE 0.078; bullish ECE 0.055; neutral_band ECE 0.047
- **multi-instrument, boosted:** bearish ECE 0.097; bullish ECE 0.122; neutral_band ECE 0.076
- **pooled (NQ+ES+RTY), logistic:** bearish ECE 0.039; bullish ECE 0.032; neutral_band ECE 0.029
- **pooled (NQ+ES+RTY), boosted:** bearish ECE 0.026; bullish ECE 0.032; neutral_band ECE 0.038

Folds: 2026-02-20 → 2026-02-24..2026-03-23; 2026-03-20 → 2026-03-24..2026-04-21; 2026-04-20 → 2026-04-22..2026-05-19; 2026-05-18 → 2026-05-20..2026-06-17; 2026-06-16 → 2026-06-18..2026-07-17; 2026-07-16 → 2026-07-20..2026-08-14; 2026-08-13 → 2026-08-17..2026-09-14; 2026-09-11 → 2026-09-15..2026-10-09
