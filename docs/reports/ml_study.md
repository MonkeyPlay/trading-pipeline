# NQ direction: ML audit and bounded improvement study (ml_study_v1)

**Development research on inspected sessions, not a test.** Every session here was looked at before (hist_dev_v1, p1_pool_tuning_v1, the fan experiments, the ML development comparison). Anything this study finds is at most a candidate for a prospective study. Nothing here is registered, promoted or delivered; A and B stay as they are.

The protocol `ml_study_v1` (hash `bb93b0e15deecab8`, [research/ml_study.py](../../research/ml_study.py)) was fixed before any outer prediction. Every outer prediction was written with its sha256 before the scoring stage read an outcome (8 files checked at scoring). The trial manifest, predictions and per-session scores are in [ml_study_v1/](ml_study_v1/).

## Answer

**No edge established.** Nothing predicts NQ's direction better than having no view, before the open or at any RTH horizon. No challenger is recommended. One direction-only interval of 58 dips below no view (LR logistic (nested) at 5 min, delayed origin): about what chance alone gives (1.5).

- **Pre-open direction_15m** (158 test sessions): nothing beats A. The best candidate is P pooled NQ+ES+RTY boosted (frozen refits), at 0.5999 against A's 0.6019 (-0.0020 [-0.0234, +0.0220]). N NQ-only logit (frozen refits), LR logistic (nested), RES prior + residual (nested) and BL blend with the prior are worse than A, with intervals above zero. B scores 0.6191, N 0.6238, M 0.6132 and P 0.5999. The frequencies with no view on direction (bullish = bearish) score 0.5993, slightly better than A (-0.0026 [-0.0038, -0.0007]). So even A's own tilt carries no information; this is far below a material edge, and a post-hoc view. With one row per session this sample misses most real signals smaller than about 0.04 (sections 1 and 5), so the pre-open "no edge" rules out only a large one.
- **RTH, the next 15 minutes from the cutoff** (198 test sessions): the same-clock prior (CLOCK) scores 0.6597. Across the 8 horizons and origins, 13 of 58 arm and problem pairs beat CLOCK with an interval below zero. But CLOCK's own per-cutoff up-shares are noise: the symmetric prior (the same frequencies with no view on direction) beats CLOCK in 4 of 8 problems. Against the symmetric prior, 2 pair(s) keep an interval below zero on the three classes (GB boosted trees (nested) at 15 min, cutoff origin: -0.0042 [-0.0083, -0.0001], of which size -0.0022 [-0.0046, -0.0000] and direction -0.0041 [-0.0093, +0.0018]; TPF TabPFN v2 at 15 min, cutoff origin: -0.0039 [-0.0065, -0.0016], of which size -0.0032 [-0.0051, -0.0016] and direction -0.0016 [-0.0045, +0.0013]). On direction alone, 1 of 58 fall below no view: LR logistic (nested) at 5 min (delayed). About 1.5 would by chance alone.
- **RTH analogues:** RTH-20's member frequencies score worse than the same-clock history at every horizon (15 minutes: +0.0268 [+0.0178, +0.0363]). Adding the recent path to the similarity does not help (+0.0009 [-0.0060, +0.0080] against RTH-20).
- **Size, not direction:** a scale model forecasts the size of the next 15 minutes better than the same-clock history (section 4.6). The location added to it does not help.
- **Why N, M and P look like A:** it is shrinkage on a weak signal, not a coding bug (section 2).
- **What it would take:** detecting a true 0.01 improvement needs hundreds of sessions, and the registered rule cannot reach 80 % power for an effect of exactly 0.01 at any sample size. Sixty sessions detect only effects several times larger (section 5).

## What ran

| stage | when (UTC) | code | what |
|---|---|---|---|
| controls | 2026-10-09T23:55:31Z | `900f3cb196af+dirty` | 40 pre-open and 11 RTH control runs, the future-bar checks |
| predict_preopen | 2026-10-10T00:01:22Z | `900f3cb196af+dirty` | 279 pool sessions, 8 outer folds, test 2026-02-24 to 2026-10-09 |
| tabpfn | 2026-10-10T00:08:35Z | `900f3cb196af+dirty` | TabPFN v2 on the pre-open and the two 15-minute RTH problems (.venv-research) |
| predict_rth | 2026-10-10T00:16:43Z | `900f3cb196af+dirty` | 318 sessions 2025-07-08 to 2026-10-09, 3804 cutoff rows, 29160 windows |
| analogues | 2026-10-10T00:17:13Z | `900f3cb196af+dirty` | 198 test sessions, 36432 forecasts |
| score | 2026-10-10T00:23:03Z | `900f3cb196af+dirty` | 8 prediction files verified, then the outcomes read |

Re-run from the final code, after TabPFN and the controls had used the first run's outputs: predict_preopen (first 2026-10-09T23:14:15Z) - every prediction file byte-identical; predict_rth (first 2026-10-09T23:16:03Z) - every prediction file byte-identical; analogues (first 2026-10-09T23:17:00Z) - every prediction file byte-identical.

**The trial budget.** Every configuration of every family was tried in every outer fold, each scored on three inner folds. Nothing else was tried.

| family | configurations | grid |
|---|---:|---|
| CP conditional prior | 12 pre-open, 18 RTH | context x shrinkage kappa. Pre-open: none / overnight volatility / gap / VIX level x 5, 20, 80. RTH: clock or phase x none / 30-minute / session volatility x 10, 50, 200 |
| LR logistic | 14 | features NQ only / NQ plus others x C 0.001 to 1 |
| GB boosted trees | 12 | features x depth 1, 2 x (rate, iterations) (0.03, 50), (0.03, 150), (0.1, 50) |
| RES prior + residual | 12 | features x C 0.003 to 1 |
| TPF TabPFN v2 | 1 | defaults, NQ features |
| BL blend | 1 | w fitted on the inner predictions |
| frozen N, M, P | 4 each | their registered grids and 75/25 tuning (contracts/nq_ml) |

The outer folds train on every earlier session less one embargoed session, test the next 20, and start after 120 sessions. For the pre-open task they are exactly the development comparison's eight folds. Each tuned family is tried on two feature sets (NQ only, NQ plus other instruments), an ablation counted in its grid.

## Deviations from the protocol

- TabPFN refuses more than 1000 training rows on a CPU by default (a speed guard); the RTH folds train on 1400 to 3000 rows, so the guard was lifted (TABPFN_ALLOW_CPU_LARGE_DATASET=1). The model and its defaults are unchanged.
- The direction-or-size decomposition (symmetrised forecasts, direction alone on the rows that moved) and the symmetric prior (PRIOR_SYM: the prior's bullish and bearish shares replaced by their mean) were added after the first scoring pass, when the RTH arms' gains over the same-clock prior needed explaining. They are analysis views computed from the stored predictions; no prediction, configuration or fold changed.
- The planted-signal control first used fixed betas whose oracle gains (0.065 and 0.145 pre-open, 0.022 and 0.072 RTH) missed the protocol's targets. The final run solves beta for the targets (ml_study.beta_for: 0.04 and 0.015). The shuffled-label controls are also scored against the symmetric prior: with shuffled labels, the conditional prior beat CLOCK itself in 3 of 5 RTH runs, so beating CLOCK needs no signal.
- An RTH label first required only its window's two end bars; the protocol (and rth_eval.window_move) requires every bar of the window. Fixed before the final run; no window in the data lacks an inner bar (0 of 29,160), so no label changed.

## 1. Implementation checks: correctness, not skill

| check | result | evidence |
|---|---|---|
| The development comparison reproduced | identical | 158 common sessions, eight arms. The largest Brier difference from docs/reports/ml_development.json is 6e-12, which is floating-point summation order |
| The five-session check reproduced, full vectors | identical | Every vector equals the earlier check's saved output (ml_study_v1/week_check_2026-10-09.json; largest difference 0e+00). The mean Brier scores are A 0.6600, B 0.7194, N 0.6615, M 0.6686, P 0.7629. The brief's N 0.662 was a mean of rounded session scores |
| Class order and full-vector Brier | pass | tests/test_ml_study.py: probabilities are mapped by class name, and the Brier score uses every class, never the realised class's probability alone |
| Folds, embargo and label windows | pass | Every outer fold is checked (ml_study.check_fold), and the inner folds stay inside their outer training window. Each label ends inside its own session |
| Pooled split by session date | pass | In the production fold code (forecaster/ml_eval._fold), the 8 folds have 0 training rows on test or embargoed dates and 0 outside the training dates. Scoring uses NQ's rows only; the test is tests/test_ml_study.py::test_the_pooled_fit_trains_on_training_dates_only |
| Outer test labels cannot move a prediction | pass | tests/test_ml_study.py: every outer test label was flipped and the embargoed session's labels removed; the predictions, choices and blend did not change |
| Future bars: pre-open features | pass | Rebuilt from bars truncated at 2026-04-08 (150 sessions identical), 2026-08-03 (230 sessions identical) |
| Future bars: RTH features | pass | 144 cutoff rows had every later bar of their session altered: 0 changed |
| Future bars: analogue ranks | pass | 12 rankings had the target's later bars altered: 0 changed |
| Shuffled labels, pre-open | pass | 20 permutations x 8 arms: against the symmetric prior, 0 interval(s) below zero (about 4.0 expected by chance); against the prior itself, 0. Mean differences from the prior: BL +0.0108, CP +0.0055, GB +0.0191, LR +0.0098, M +0.0186, N +0.0090, P +0.0167, RES +0.0103 |
| Shuffled labels, RTH, 15 minutes | pass | 5 permutations x 5 arms: against the symmetric prior, 0 interval(s) below zero (about 0.6 expected by chance); against the prior itself, 4. Mean differences from the prior: BL -0.0014, CP -0.0018, GB -0.0011, LR -0.0012, RES +0.0024 |
| Planted signal, pre-open, oracle gain 0.018 (beta 0.202) | rarely found (1/10) | The oracle's gain is 0.0180. Detected (interval below zero) in CP 0/10, LR 1/10, GB 0/10, RES 1/10, BL 0/10, N 0/10, M 1/10, P 0/10 runs. Mean gains: CP -0.0045, LR 0.0062, GB -0.0026, RES 0.0065, BL -0.0018, N 0.0039, M 0.0034, P -0.0070 |
| Planted signal, pre-open, oracle gain 0.042 (beta 0.345) | rarely found (3/10) | The oracle's gain is 0.0423. Detected (interval below zero) in CP 0/10, LR 1/10, GB 2/10, RES 1/10, BL 1/10, N 1/10, M 3/10, P 1/10 runs. Mean gains: CP -0.0065, LR 0.0218, GB 0.0221, RES 0.0232, BL 0.0175, N 0.0244, M 0.0272, P 0.0072 |
| Planted signal, RTH, 15 minutes, oracle gain 0.015 (beta 0.241) | found (3/3) | The oracle's gain is 0.0151. Detected (interval below zero) in CP 1/3, LR 3/3, GB 3/3, RES 3/3, BL 3/3 runs. Mean gains: CP 0.0013, LR 0.0145, GB 0.0125, RES 0.0116, BL 0.0139 |
| Planted signal, RTH, 15 minutes, oracle gain 0.040 (beta 0.416) | found (3/3) | The oracle's gain is 0.0398. Detected (interval below zero) in CP 1/3, LR 3/3, GB 3/3, RES 3/3, BL 3/3 runs. Mean gains: CP 0.0012, LR 0.0375, GB 0.0358, RES 0.0351, BL 0.0377 |

The code does what it claims:

- the reproductions are identical;
- nothing leaks across the date split or from later bars;
- shuffled labels give no skill against the symmetric prior;
- a planted signal is found where the sample allows: RTH (15 minutes): a planted gain of 0.015 in at most 3 of 3 runs; a planted gain of 0.040 in at most 3 of 3 runs; pre-open: a planted gain of 0.018 in at most 1 of 10 runs; a planted gain of 0.042 in at most 3 of 10 runs.

So the two tasks' silences mean different things:

- The RTH task, with twelve cutoffs per session, finds a signal of the size that would matter. Its "no edge" is informative.
- The pre-open task has one row per session. It misses most planted signals of 0.04, so its "no edge" says only that no large signal exists (section 5).

None of this says whether NQ is predictable. The next sections do.

## 2. Why N, M and P look like the prior

**The sample.** Each development fold trains on 119 to 259 sessions, one row per session. The classes in the last training window are bearish 113, bullish 115, neutral_band 31. The pooled model's rows grow to 765. But ES's label agrees with NQ's on 72 % of dates and RTY's on 48 %: they share the day's news, so the pooled rows carry far less than three times the information.

**The inputs.**

- Missing features are imputed with the training median, with a missing indicator: rty_ret_on 11.5 %, rty_ret_pm 11.5 %, nq_rty_spread_pm 11.5 %, dx_ret_on 10.0 %, y10_chg_on 7.5 %.
- Nearly constant features (one value on at least 90 % of sessions): vix_missing 100 %, 10y_missing 92 %, event_session 91 %.
- The continuous features' standard deviations run from 0.03 (nq_es_corr) to 1.24 (vix_level). The logistic models standardise each training window anyway.

**Were the inputs there at the time?** In the development data, 277 of 279 sessions' bars are backfilled history: their status is 'reconstructed', meaning they were stored more than two hours after the bar ended. So the development rows show what the history says, not what Auto would have had. Rebuilt as of each session's replay deadline (10:04 ET, receipts respected), only 2 rows equal the development rows: 2026-10-07, 2026-10-09, the sessions Auto collected live. No other session's inputs had been received by then.

Two more points on availability:

- VIX is closed at the 09:29 cutoff, so by design it uses its 09:14 bar (14 minutes old).
- VXN is not used. No released economic value is an input, only the scheduled times from the snapshot's frozen calendar.

The development comparison therefore says nothing about live availability. Only the forward sessions can.

**Shrinkage.** N's frozen grid is C 0.01 to 0.3.

- N's tuning chose C 0.01 in 7 fold(s), C 0.3 in 1 fold(s).
- M's tuning chose C 0.01 in 8 fold(s).
- With a grid down to 0.001, the study's nested logistic regression chose C 0.001 in 6, C 0.003 in 1, C 1.0 in 1.

The table refits along a wider path. It is post hoc: scored here, never used to choose.

| C | N: test Brier | N: mean TV from the frequencies | N: p(bullish) sd | M: test Brier | M: mean TV | M: p(bullish) sd |
|---:|---:|---:|---:|---:|---:|---:|
| 0.001 | 0.6024 | 0.011 | 0.010 | 0.6030 | 0.014 | 0.016 |
| 0.003 | 0.6039 | 0.018 | 0.021 | 0.6055 | 0.028 | 0.032 |
| 0.01 | 0.6075 | 0.034 | 0.041 | 0.6132 | 0.060 | 0.065 |
| 0.03 | 0.6144 | 0.056 | 0.065 | 0.6289 | 0.103 | 0.106 |
| 0.1 | 0.6273 | 0.086 | 0.094 | 0.6551 | 0.153 | 0.150 |
| 0.3 | 0.6426 | 0.113 | 0.117 | 0.6813 | 0.191 | 0.180 |
| 1 | 0.6591 | 0.138 | 0.139 | 0.7097 | 0.221 | 0.207 |
| 3 | 0.6709 | 0.154 | 0.156 | 0.7328 | 0.242 | 0.227 |
| 10 | 0.6785 | 0.164 | 0.167 | 0.7542 | 0.261 | 0.245 |
| 100 | 0.6828 | 0.169 | 0.174 | 0.7971 | 0.288 | 0.271 |

A looser penalty moves the forecasts further from the frequencies and makes them vary more from day to day. The Brier score gets steadily worse as it does: N's best point is C 0.001 (0.6024), which is essentially the frequencies, against A's 0.6019. The frozen grid's strongest penalty, C 0.01, is still looser than that.

**Dispersion and divergence** on the test sessions:

| arm | p(bearish) sd | p(bullish) sd | p(neutral) sd | mean TV from A_s | argmax differs from A_s | mean KL from A_s |
|---|---:|---:|---:|---:|---:|---:|
| A frequencies (stored, frozen) | 0.007 | 0.008 | 0.007 | 0.008 | 20 % | 0.0002 |
| B analogues (stored, frozen) | 0.111 | 0.119 | 0.078 | 0.125 | 42 % | 0.0517 |
| N NQ-only logit (frozen refits) | 0.084 | 0.087 | 0.037 | 0.054 | 42 % | 0.0232 |
| M multi-instrument logit (frozen refits) | 0.058 | 0.065 | 0.030 | 0.060 | 56 % | 0.0134 |
| P pooled NQ+ES+RTY boosted (frozen refits) | 0.068 | 0.076 | 0.037 | 0.082 | 54 % | 0.0261 |
| A_s smoothed training frequencies | 0.008 | 0.007 | 0.007 | 0.000 | 0 % | 0.0000 |
| CP conditional prior | 0.038 | 0.041 | 0.019 | 0.029 | 34 % | 0.0045 |
| LR logistic (nested) | 0.089 | 0.092 | 0.045 | 0.042 | 34 % | 0.0284 |
| GB boosted trees (nested) | 0.097 | 0.102 | 0.051 | 0.097 | 59 % | 0.0345 |
| RES prior + residual (nested) | 0.087 | 0.093 | 0.038 | 0.044 | 39 % | 0.0249 |
| BL blend with the prior | 0.080 | 0.084 | 0.039 | 0.061 | 34 % | 0.0215 |
| TPF TabPFN v2 | 0.023 | 0.043 | 0.033 | 0.032 | 48 % | 0.0067 |

N, M and P do condition on their inputs. On an average day they move 5 to 8 points of probability away from the frequencies, and their most likely class differs from A_s's on 42 % to 56 % of days. Those moves do not improve the score. The frozen grids stop at C 0.01, looser than what the data support (the path above), so the models carry a little more noise than the best shrinkage would.

**Fallbacks.** N, M and P never return a fallback prior. Each issues its model's probabilities or is unavailable, with the reason: a session in the training window, no frozen T, NQ's own features missing, or, for M only, a required instrument stale. M abstained on 0 development test sessions.

In production so far:

- nq_baseline_p1_v1 (historical_replay): 280 issued
- nq_ml_multi_p1_v1 (historical_replay): 1 issued
- nq_ml_nq_p1_v1 (historical_replay): 1 issued
- nq_ml_pooled_p1_v1 (historical_replay): 1 issued
- nq_prior_p1_v1 (historical_replay): 280 issued
- deliveries: nq_baseline_p1_v1 1. The one delivery is 2026-10-09's, recorded after the fact by 363c818 and shown as a reconstruction.

**Verdict.** The probabilities track A because the data leave little better to do:

- the tuning prefers the strongest penalty on offer;
- a looser penalty scores worse;
- the same code learns a planted signal (section 1);
- the features come from history only.

This is shrinkage on a weak signal, not a bug.

## 3. Pre-open direction_15m: the candidate ladder

There are 159 scheduled test sessions, of which 158 have a recorded label, and 158 are scored. 2026-10-09 has no recorded label yet. The score is the unhalved Brier score (0 to 2, lower is better). Differences are paired per session, with 95 % moving-block bootstrap intervals. They are descriptive: development data, many comparisons.

| arm | Brier | log loss | ECE | vs A (95 % interval) | vs B (95 % interval) | vs A_s (95 % interval) | mean TV from prior | p(bullish) sd |
|---|---:|---:|---:|---|---|---|---:|---:|
| A frequencies (stored, frozen) | 0.6019 | 0.9828 | 0.022 | - | -0.0172 [-0.0468, +0.0148] | +0.0005 [-0.0016, +0.0022] | 0.008 | 0.008 |
| B analogues (stored, frozen) | 0.6191 | 1.0190 | 0.079 | +0.0172 [-0.0148, +0.0468] | - | +0.0177 [-0.0137, +0.0472] | 0.125 | 0.119 |
| N NQ-only logit (frozen refits) | 0.6238 | 1.0618 | 0.065 | +0.0219 [+0.0023, +0.0440] | +0.0047 [-0.0317, +0.0416] | +0.0224 [+0.0031, +0.0435] | 0.054 | 0.087 |
| M multi-instrument logit (frozen refits) | 0.6132 | 1.0069 | 0.060 | +0.0112 [-0.0059, +0.0277] | -0.0059 [-0.0398, +0.0262] | +0.0118 [-0.0052, +0.0278] | 0.060 | 0.065 |
| P pooled NQ+ES+RTY boosted (frozen refits) | 0.5999 | 0.9993 | 0.032 | -0.0020 [-0.0234, +0.0220] | -0.0192 [-0.0569, +0.0223] | -0.0015 [-0.0231, +0.0219] | 0.082 | 0.076 |
| A_s smoothed training frequencies | 0.6014 | 0.9818 | 0.024 | -0.0005 [-0.0022, +0.0016] | -0.0177 [-0.0472, +0.0137] | - | 0.000 | 0.007 |
| the prior, symmetrised (no direction view) | 0.5993 | 0.9795 | 0.023 | -0.0026 [-0.0038, -0.0007] | -0.0198 [-0.0488, +0.0122] | -0.0021 [-0.0035, -0.0004] | 0.005 | 0.003 |
| CP conditional prior | 0.6055 | 0.9923 | 0.051 | +0.0036 [-0.0056, +0.0143] | -0.0136 [-0.0448, +0.0187] | +0.0041 [-0.0047, +0.0145] | 0.029 | 0.041 |
| LR logistic (nested) | 0.6270 | 1.1032 | 0.059 | +0.0250 [+0.0027, +0.0507] | +0.0078 [-0.0302, +0.0459] | +0.0256 [+0.0038, +0.0503] | 0.042 | 0.092 |
| GB boosted trees (nested) | 0.6230 | 1.0359 | 0.084 | +0.0211 [-0.0099, +0.0493] | +0.0039 [-0.0407, +0.0445] | +0.0216 [-0.0094, +0.0488] | 0.097 | 0.102 |
| RES prior + residual (nested) | 0.6254 | 1.0482 | 0.061 | +0.0234 [+0.0026, +0.0474] | +0.0063 [-0.0311, +0.0437] | +0.0240 [+0.0035, +0.0470] | 0.044 | 0.093 |
| BL blend with the prior | 0.6220 | 1.0320 | 0.061 | +0.0201 [+0.0000, +0.0405] | +0.0029 [-0.0353, +0.0396] | +0.0206 [+0.0008, +0.0400] | 0.061 | 0.084 |
| TPF TabPFN v2 | 0.6057 | 0.9952 | 0.035 | +0.0037 [-0.0040, +0.0117] | -0.0135 [-0.0450, +0.0172] | +0.0043 [-0.0036, +0.0117] | 0.032 | 0.043 |

**Direction or size**, against the symmetric prior (A_s with no view on direction). The direction-only score is 2 x (q - [bullish])^2, with q = p(bullish) / (p(bullish) + p(bearish)), on the sessions that moved past the band; having no view scores exactly 0.5:

| arm | three classes vs the prior, symmetrised (no direction view) | symmetrised: size only | direction only (moved rows) | its own tilt: original - symmetrised |
|---|---|---|---|---|
| A frequencies (stored, frozen) | +0.0026 [+0.0007, +0.0038] | +0.0002 [-0.0006, +0.0009] | +0.0032 [+0.0010, +0.0045] | +0.0025 [+0.0006, +0.0034] |
| B analogues (stored, frozen) | +0.0198 [-0.0122, +0.0488] | +0.0077 [-0.0037, +0.0192] | +0.0194 [-0.0208, +0.0587] | +0.0121 [-0.0199, +0.0423] |
| N NQ-only logit (frozen refits) | +0.0245 [+0.0044, +0.0462] | +0.0094 [+0.0004, +0.0223] | +0.0136 [-0.0063, +0.0325] | +0.0151 [-0.0008, +0.0302] |
| M multi-instrument logit (frozen refits) | +0.0139 [-0.0036, +0.0297] | +0.0034 [-0.0020, +0.0102] | +0.0126 [-0.0086, +0.0322] | +0.0104 [-0.0063, +0.0259] |
| P pooled NQ+ES+RTY boosted (frozen refits) | +0.0006 [-0.0213, +0.0242] | +0.0038 [-0.0053, +0.0143] | -0.0043 [-0.0302, +0.0220] | -0.0032 [-0.0228, +0.0171] |
| A_s smoothed training frequencies | +0.0021 [+0.0004, +0.0035] | +0.0000 [+0.0000, +0.0000] | +0.0027 [+0.0006, +0.0046] | +0.0021 [+0.0004, +0.0035] |
| CP conditional prior | +0.0062 [-0.0035, +0.0169] | +0.0026 [-0.0003, +0.0063] | +0.0044 [-0.0077, +0.0176] | +0.0036 [-0.0054, +0.0134] |
| LR logistic (nested) | +0.0277 [+0.0047, +0.0526] | +0.0112 [+0.0008, +0.0259] | +0.0137 [-0.0036, +0.0329] | +0.0165 [+0.0011, +0.0338] |
| GB boosted trees (nested) | +0.0237 [-0.0078, +0.0509] | +0.0054 [-0.0016, +0.0114] | +0.0267 [-0.0108, +0.0587] | +0.0183 [-0.0116, +0.0435] |
| RES prior + residual (nested) | +0.0261 [+0.0044, +0.0496] | +0.0101 [+0.0015, +0.0219] | +0.0139 [-0.0044, +0.0339] | +0.0160 [+0.0012, +0.0328] |
| BL blend with the prior | +0.0227 [+0.0023, +0.0426] | +0.0089 [+0.0013, +0.0191] | +0.0141 [-0.0077, +0.0341] | +0.0138 [-0.0037, +0.0295] |
| TPF TabPFN v2 | +0.0064 [-0.0018, +0.0137] | +0.0036 [-0.0015, +0.0100] | +0.0031 [-0.0065, +0.0095] | +0.0027 [-0.0037, +0.0076] |

No arm is better than no view on direction. The best direction-only score is P pooled NQ+ES+RTY boosted (frozen refits)'s 0.4957, against no view's 0.5000 (-0.0043 [-0.0302, +0.0220]). Even A's own small tilt between bullish and bearish costs +0.0026 [+0.0007, +0.0038] against the symmetric prior.

**Blend weights.** w goes on the best inner family and 1 - w on A_s, fitted on earlier inner predictions: LR 0.73, CP 1.00, CP 1.00, CP 0.00, CP 0.00, GB 0.62, GB 1.00, GB 1.00. The weight jumps between 0 and 1 across folds. That is what noise looks like to the inner folds, not a stable signal.

**Chosen configurations per fold:**

- 2026-02-24: LR C 1.0 (nq); GB nq, depth 2, 150 iterations; RES C 1.0; CP nq_rv_on with kappa 20
- 2026-03-24: LR C 0.003 (nq); GB nq, depth 1, 150 iterations; RES C 0.003; CP vix_level with kappa 5
- 2026-04-22: LR C 0.001 (nq); GB nq, depth 2, 50 iterations; RES C 0.003; CP vix_level with kappa 80
- 2026-05-20: LR C 0.001 (nq); GB multi, depth 1, 50 iterations; RES C 0.003; CP no context with kappa 80
- 2026-06-18: LR C 0.001 (nq); GB multi, depth 1, 50 iterations; RES C 0.003; CP no context with kappa 80
- 2026-07-20: LR C 0.001 (nq); GB multi, depth 1, 50 iterations; RES C 0.003; CP nq_rv_on with kappa 80
- 2026-08-17: LR C 0.001 (nq); GB multi, depth 1, 50 iterations; RES C 0.003; CP nq_rv_on with kappa 5
- 2026-09-15: LR C 0.001 (nq); GB multi, depth 1, 50 iterations; RES C 0.003; CP nq_rv_on with kappa 5

**Each outer fold** (mean per-session Brier score; the symmetric prior is the frequencies with no view on direction):

| outer fold (test sessions) | n | A frequencies (stored, frozen) | B analogues (stored, frozen) | N NQ-only logit (frozen refits) | M multi-instrument logit (frozen refits) | P pooled NQ+ES+RTY boosted (frozen refits) | the prior, symmetrised (no direction view) | LR logistic (nested) | GB boosted trees (nested) | TPF TabPFN v2 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 2026-02-24 to 2026-03-23 | 20 | 0.6861 | 0.7143 | 0.8390 | 0.7261 | 0.7341 | 0.6805 | 0.8839 | 0.7359 | 0.7253 |
| 2026-03-24 to 2026-04-21 | 20 | 0.6216 | 0.5936 | 0.6339 | 0.6441 | 0.6391 | 0.6166 | 0.6290 | 0.6357 | 0.6153 |
| 2026-04-22 to 2026-05-19 | 20 | 0.5910 | 0.6428 | 0.5831 | 0.5823 | 0.5995 | 0.5876 | 0.5862 | 0.6996 | 0.5869 |
| 2026-05-20 to 2026-06-17 | 20 | 0.6494 | 0.6527 | 0.6725 | 0.6882 | 0.6572 | 0.6459 | 0.6518 | 0.6793 | 0.6592 |
| 2026-06-18 to 2026-07-17 | 20 | 0.5589 | 0.6190 | 0.5692 | 0.5965 | 0.5171 | 0.5580 | 0.5591 | 0.5206 | 0.5524 |
| 2026-07-20 to 2026-08-14 | 20 | 0.5589 | 0.6001 | 0.5462 | 0.5353 | 0.5368 | 0.5571 | 0.5554 | 0.5076 | 0.5574 |
| 2026-08-17 to 2026-09-14 | 20 | 0.5554 | 0.5471 | 0.5651 | 0.5687 | 0.5305 | 0.5554 | 0.5572 | 0.5870 | 0.5560 |
| 2026-09-15 to 2026-10-08 | 18 | 0.5934 | 0.5796 | 0.5770 | 0.5590 | 0.5837 | 0.5928 | 0.5895 | 0.6182 | 0.5914 |

Folds in which an arm beats A, of 8: N NQ-only logit (frozen refits) 3, M multi-instrument logit (frozen refits) 3, P pooled NQ+ES+RTY boosted (frozen refits) 4, LR logistic (nested) 3, GB boosted trees (nested) 2, TPF TabPFN v2 5, the prior, symmetrised (no direction view) 7. No arm beats A in every fold. P pooled NQ+ES+RTY boosted (frozen refits) beats A in each of the last 4 folds (from 2026-06-18; differences -0.042, -0.022, -0.025, -0.010) after trailing earlier. With 20 sessions a fold (a standard error of about 0.03 each), and noticed after the fact, that is within noise. It is what p1_ml_forward_v2's prospective sessions test. No regime split was declared before the study, so none is reported.

**NQ only, or NQ plus other instruments?** The inner folds chose: GB multi 5, nq 3; LR nq 8; RES nq 8. No arm found an edge that the other instruments would be needed for.

**The five supplied sessions.** These are walk-forward refits on every earlier labelled session, the earlier check's method, shown as full vectors:

| session | realised | A: bear/bull/neutral %, Brier | B: bear/bull/neutral %, Brier | N: bear/bull/neutral %, Brier | M: bear/bull/neutral %, Brier | P: bear/bull/neutral %, Brier |
|---|---|---|---|---|---|---|
| 2026-10-05 | bullish | 43.1/45.3/11.7, 0.499 | 31.5/42.6/25.8, 0.495 | 44.8/42.7/12.4, 0.545 | 42.0/47.7/10.2, 0.461 | 49.2/40.2/10.6, 0.611 |
| 2026-10-06 | neutral_band | 42.9/45.5/11.6, 1.172 | 31.5/52.7/15.8, 1.086 | 43.2/45.6/11.2, 1.183 | 34.9/58.5/6.7, 1.335 | 40.8/51.5/7.6, 1.285 |
| 2026-10-07 | bearish | 42.8/45.3/12.0, 0.547 | 31.4/52.6/16.0, 0.774 | 43.5/45.1/11.5, 0.536 | 43.5/42.9/13.7, 0.522 | 29.4/55.5/15.0, 0.829 |
| 2026-10-08 | bearish | 43.0/45.1/11.9, 0.543 | 41.5/52.6/6.0, 0.622 | 42.8/44.7/12.5, 0.543 | 44.2/46.5/9.3, 0.536 | 47.9/42.7/9.4, 0.463 |
| 2026-10-09 | bearish * | 43.2/45.0/11.9, 0.539 | 41.6/52.5/5.9, 0.620 | 45.3/43.4/11.3, 0.501 | 46.2/43.4/10.4, 0.489 | 39.5/50.0/10.5, 0.626 |

Means: A 0.660, B 0.719, N 0.661, M 0.669, P 0.763.

\* 2026-10-09's label is computed from the stored bars; no outcome is recorded yet.

Five sessions establish nothing: not equality, not non-inferiority, not a lasting disadvantage.

## 4. RTH rolling targets

**The target.** At every 30 minutes from 10:00 to 15:30 ET, the direction of the next h minutes (15 primary; 5, 30 and 60 secondary). The band is 0.5 x the two-minute ATR frozen at the cutoff x sqrt(h / 15). At 15 minutes this is direction_15m's rule, and it was fixed ex ante.

**Two origins.**

- *Cutoff origin:* the window starts at the cutoff.
- *Delayed origin:* the window starts 14 minutes later. That allows for the feed's ~10-minute delay, the confirming bar, and the start at the second full minute after the build. The features still end at the cutoff.

Sessions weigh equally: each session's cutoffs are averaged first. Every row has a label (label reasons: {'ok': 29160}).

### 4.1 The primary horizon: 15 minutes

**Cutoff origin.** 198 sessions and 2376 matched rows (every arm present). Rows where an arm had no forecast and fell back to the prior: none.

| arm | Brier | log loss | ECE | vs CLOCK (95 % interval) | vs symmetric prior (95 % interval) | vs B_rth (95 % interval) | mean TV from prior | p(bullish) sd |
|---|---:|---:|---:|---|---|---|---:|---:|
| CLOCK same-clock frequencies | 0.6597 | 1.0879 | 0.017 | - | +0.0008 [-0.0013, +0.0034] | -0.0268 [-0.0363, -0.0178] | 0.000 | 0.034 |
| the prior, symmetrised (no direction view) | 0.6589 | 1.0868 | 0.010 | -0.0008 [-0.0034, +0.0013] | - | -0.0276 [-0.0376, -0.0179] | 0.024 | 0.011 |
| CP conditional prior | 0.6586 | 1.0859 | 0.010 | -0.0011 [-0.0043, +0.0016] | -0.0003 [-0.0023, +0.0016] | -0.0279 [-0.0382, -0.0185] | 0.027 | 0.023 |
| LR logistic (nested) | 0.6561 | 1.0823 | 0.010 | -0.0036 [-0.0076, +0.0001] | -0.0028 [-0.0063, +0.0006] | -0.0304 [-0.0408, -0.0211] | 0.050 | 0.040 |
| GB boosted trees (nested) | 0.6547 | 1.0803 | 0.015 | -0.0050 [-0.0095, -0.0007] | -0.0042 [-0.0083, -0.0001] | -0.0318 [-0.0419, -0.0221] | 0.053 | 0.043 |
| RES prior + residual (nested) | 0.6581 | 1.0854 | 0.017 | -0.0016 [-0.0053, +0.0019] | -0.0009 [-0.0053, +0.0037] | -0.0285 [-0.0383, -0.0193] | 0.041 | 0.055 |
| BL blend with the prior | 0.6588 | 1.0865 | 0.016 | -0.0009 [-0.0053, +0.0036] | -0.0001 [-0.0042, +0.0041] | -0.0277 [-0.0383, -0.0178] | 0.050 | 0.046 |
| TPF TabPFN v2 | 0.6550 | 1.0804 | 0.006 | -0.0047 [-0.0080, -0.0017] | -0.0039 [-0.0065, -0.0016] | -0.0315 [-0.0420, -0.0224] | 0.043 | 0.033 |
| B_rth analogues RTH-20 (v3) | 0.6865 | 1.1323 | 0.073 | +0.0268 [+0.0178, +0.0363] | +0.0276 [+0.0179, +0.0376] | - | 0.123 | 0.099 |
| B_rth_recent analogues + recent path | 0.6874 | 1.1329 | 0.078 | +0.0277 [+0.0175, +0.0385] | +0.0285 [+0.0176, +0.0395] | +0.0009 [-0.0060, +0.0080] | 0.126 | 0.101 |

**Delayed origin.** 198 sessions and 2376 matched rows (every arm present). Rows where an arm had no forecast and fell back to the prior: none.

| arm | Brier | log loss | ECE | vs CLOCK (95 % interval) | vs symmetric prior (95 % interval) | vs B_rth (95 % interval) | mean TV from prior | p(bullish) sd |
|---|---:|---:|---:|---|---|---|---:|---:|
| CLOCK same-clock frequencies | 0.6620 | 1.0906 | 0.028 | - | +0.0041 [+0.0018, +0.0065] | -0.0227 [-0.0302, -0.0163] | 0.000 | 0.038 |
| the prior, symmetrised (no direction view) | 0.6579 | 1.0851 | 0.014 | -0.0041 [-0.0065, -0.0018] | - | -0.0268 [-0.0349, -0.0200] | 0.030 | 0.023 |
| CP conditional prior | 0.6581 | 1.0849 | 0.011 | -0.0038 [-0.0065, -0.0014] | +0.0003 [-0.0022, +0.0029] | -0.0265 [-0.0353, -0.0194] | 0.037 | 0.026 |
| LR logistic (nested) | 0.6573 | 1.0837 | 0.007 | -0.0047 [-0.0081, -0.0014] | -0.0006 [-0.0029, +0.0018] | -0.0274 [-0.0361, -0.0203] | 0.054 | 0.017 |
| GB boosted trees (nested) | 0.6579 | 1.0846 | 0.009 | -0.0040 [-0.0071, -0.0008] | +0.0000 [-0.0020, +0.0021] | -0.0268 [-0.0353, -0.0196] | 0.054 | 0.021 |
| RES prior + residual (nested) | 0.6634 | 1.0928 | 0.030 | +0.0014 [-0.0010, +0.0039] | +0.0055 [+0.0025, +0.0085] | -0.0213 [-0.0286, -0.0152] | 0.033 | 0.045 |
| BL blend with the prior | 0.6585 | 1.0856 | 0.013 | -0.0034 [-0.0060, -0.0011] | +0.0007 [-0.0018, +0.0032] | -0.0261 [-0.0346, -0.0193] | 0.036 | 0.027 |
| TPF TabPFN v2 | 0.6567 | 1.0830 | 0.005 | -0.0052 [-0.0081, -0.0024] | -0.0012 [-0.0031, +0.0010] | -0.0280 [-0.0367, -0.0206] | 0.048 | 0.005 |
| B_rth analogues RTH-20 (v3) | 0.6847 | 1.1302 | 0.075 | +0.0227 [+0.0163, +0.0302] | +0.0268 [+0.0200, +0.0349] | - | 0.117 | 0.099 |
| B_rth_recent analogues + recent path | 0.6867 | 1.1323 | 0.080 | +0.0247 [+0.0170, +0.0328] | +0.0288 [+0.0207, +0.0377] | +0.0020 [-0.0047, +0.0083] | 0.120 | 0.099 |

**Each outer fold**, 15 minutes from the cutoff:

| outer fold (test sessions) | n | CLOCK same-clock frequencies | the prior, symmetrised (no direction view) | LR logistic (nested) | GB boosted trees (nested) | TPF TabPFN v2 | B_rth analogues RTH-20 (v3) |
|---|---:|---:|---:|---:|---:|---:|---:|
| 2025-12-26 to 2026-01-26 | 20 | 0.6739 | 0.6719 | 0.6641 | 0.6625 | 0.6635 | 0.7017 |
| 2026-01-27 to 2026-02-24 | 20 | 0.6568 | 0.6600 | 0.6560 | 0.6585 | 0.6575 | 0.6966 |
| 2026-02-25 to 2026-03-24 | 20 | 0.6685 | 0.6646 | 0.6616 | 0.6668 | 0.6603 | 0.7043 |
| 2026-03-25 to 2026-04-22 | 20 | 0.6570 | 0.6520 | 0.6524 | 0.6581 | 0.6499 | 0.6740 |
| 2026-04-23 to 2026-05-20 | 20 | 0.6618 | 0.6673 | 0.6603 | 0.6609 | 0.6620 | 0.6764 |
| 2026-05-21 to 2026-06-18 | 20 | 0.6447 | 0.6488 | 0.6371 | 0.6405 | 0.6395 | 0.6800 |
| 2026-06-22 to 2026-07-20 | 20 | 0.6687 | 0.6657 | 0.6703 | 0.6621 | 0.6646 | 0.6843 |
| 2026-07-21 to 2026-08-17 | 20 | 0.6611 | 0.6591 | 0.6633 | 0.6529 | 0.6583 | 0.6898 |
| 2026-08-18 to 2026-09-15 | 20 | 0.6582 | 0.6514 | 0.6543 | 0.6558 | 0.6518 | 0.6783 |
| 2026-09-16 to 2026-10-09 | 18 | 0.6448 | 0.6470 | 0.6402 | 0.6262 | 0.6414 | 0.6790 |

**NQ only, or NQ plus other instruments?** Over all eight problems and their folds, the inner folds chose: GB multi 30, nq 50; LR multi 21, nq 59; RES multi 20, nq 60. NQ's own features win most choices. ES, RTY and VXN win about a third, and none of it adds up to a directional edge (section 4.3).

### 4.2 Direction or size, 15 minutes

Each arm is compared with the symmetric prior: the same-clock frequencies with no view on direction. Having no view scores exactly 0.5 on direction alone.

**Cutoff origin**

| arm | three classes vs the prior, symmetrised (no direction view) | symmetrised: size only | direction only (moved rows) | its own tilt: original - symmetrised |
|---|---|---|---|---|
| CLOCK same-clock frequencies | +0.0008 [-0.0013, +0.0034] | +0.0000 [+0.0000, +0.0000] | +0.0017 [-0.0024, +0.0064] | +0.0008 [-0.0013, +0.0034] |
| CP conditional prior | -0.0003 [-0.0023, +0.0016] | -0.0013 [-0.0030, +0.0003] | +0.0019 [-0.0001, +0.0043] | +0.0010 [-0.0001, +0.0022] |
| LR logistic (nested) | -0.0028 [-0.0063, +0.0006] | -0.0023 [-0.0044, -0.0004] | -0.0011 [-0.0053, +0.0034] | -0.0005 [-0.0028, +0.0021] |
| GB boosted trees (nested) | -0.0042 [-0.0083, -0.0001] | -0.0022 [-0.0046, -0.0000] | -0.0041 [-0.0093, +0.0018] | -0.0020 [-0.0050, +0.0014] |
| RES prior + residual (nested) | -0.0009 [-0.0053, +0.0037] | -0.0002 [-0.0020, +0.0015] | -0.0013 [-0.0080, +0.0058] | -0.0007 [-0.0044, +0.0034] |
| BL blend with the prior | -0.0001 [-0.0042, +0.0041] | -0.0006 [-0.0028, +0.0016] | +0.0008 [-0.0043, +0.0063] | +0.0005 [-0.0027, +0.0038] |
| TPF TabPFN v2 | -0.0039 [-0.0065, -0.0016] | -0.0032 [-0.0051, -0.0016] | -0.0016 [-0.0045, +0.0013] | -0.0007 [-0.0024, +0.0009] |
| B_rth analogues RTH-20 (v3) | +0.0276 [+0.0179, +0.0376] | +0.0158 [+0.0095, +0.0230] | +0.0245 [+0.0129, +0.0366] | +0.0118 [+0.0058, +0.0179] |
| B_rth_recent analogues + recent path | +0.0285 [+0.0176, +0.0395] | +0.0180 [+0.0104, +0.0261] | +0.0225 [+0.0096, +0.0357] | +0.0105 [+0.0040, +0.0172] |

**Delayed origin**

| arm | three classes vs the prior, symmetrised (no direction view) | symmetrised: size only | direction only (moved rows) | its own tilt: original - symmetrised |
|---|---|---|---|---|
| CLOCK same-clock frequencies | +0.0041 [+0.0018, +0.0065] | +0.0000 [+0.0000, +0.0000] | +0.0068 [+0.0024, +0.0113] | +0.0041 [+0.0018, +0.0065] |
| CP conditional prior | +0.0003 [-0.0022, +0.0029] | -0.0010 [-0.0028, +0.0007] | +0.0020 [-0.0010, +0.0056] | +0.0013 [-0.0003, +0.0031] |
| LR logistic (nested) | -0.0006 [-0.0029, +0.0018] | -0.0010 [-0.0036, +0.0013] | +0.0004 [-0.0015, +0.0029] | +0.0003 [-0.0007, +0.0018] |
| GB boosted trees (nested) | +0.0000 [-0.0020, +0.0021] | -0.0012 [-0.0036, +0.0009] | +0.0019 [-0.0004, +0.0049] | +0.0012 [-0.0001, +0.0029] |
| RES prior + residual (nested) | +0.0055 [+0.0025, +0.0085] | +0.0001 [-0.0019, +0.0019] | +0.0088 [+0.0040, +0.0142] | +0.0054 [+0.0028, +0.0083] |
| BL blend with the prior | +0.0007 [-0.0018, +0.0032] | -0.0005 [-0.0023, +0.0011] | +0.0017 [-0.0013, +0.0054] | +0.0012 [-0.0004, +0.0030] |
| TPF TabPFN v2 | -0.0012 [-0.0031, +0.0010] | -0.0010 [-0.0031, +0.0009] | -0.0003 [-0.0019, +0.0017] | -0.0001 [-0.0010, +0.0010] |
| B_rth analogues RTH-20 (v3) | +0.0268 [+0.0200, +0.0349] | +0.0130 [+0.0075, +0.0193] | +0.0255 [+0.0166, +0.0344] | +0.0138 [+0.0091, +0.0189] |
| B_rth_recent analogues + recent path | +0.0288 [+0.0207, +0.0377] | +0.0149 [+0.0091, +0.0215] | +0.0261 [+0.0153, +0.0369] | +0.0139 [+0.0084, +0.0195] |

How to read the two tables:

- The symmetrised column is the size part: the chance of a move past the band.
- The direction-only column is measured against no view, which scores exactly 0.5. A gain over CLOCK that disappears here was CLOCK's noisy tilt, not the arm's skill.
- RES, the prior plus residual, carries CLOCK's tilt in its offset (log CLOCK). Its direction-only score is worse than no view in 2 of 8 problems.

### 4.3 Every horizon and origin

| horizon | origin | sessions | CLOCK | symmetric prior - CLOCK | best arm | its Brier | best - CLOCK | best - symmetric prior | best direction-only - no view | RTH-20 - CLOCK |
|---:|---|---:|---:|---|---|---:|---|---|---|---|
| 15 | cutoff | 198 | 0.6597 | -0.0008 [-0.0034, +0.0013] | GB boosted trees (nested) | 0.6547 | -0.0050 [-0.0095, -0.0007] | -0.0042 [-0.0083, -0.0001] | GB boosted trees (nested) -0.0041 [-0.0093, +0.0018] | +0.0268 [+0.0178, +0.0363] |
| 15 | delayed | 198 | 0.6620 | -0.0041 [-0.0065, -0.0018] | TPF TabPFN v2 | 0.6567 | -0.0052 [-0.0081, -0.0024] | -0.0012 [-0.0031, +0.0010] | TPF TabPFN v2 -0.0003 [-0.0019, +0.0017] | +0.0227 [+0.0163, +0.0302] |
| 5 | cutoff | 198 | 0.6489 | -0.0021 [-0.0041, -0.0003] | GB boosted trees (nested) | 0.6453 | -0.0035 [-0.0074, +0.0006] | -0.0014 [-0.0049, +0.0023] | GB boosted trees (nested) -0.0016 [-0.0071, +0.0040] | +0.0243 [+0.0159, +0.0324] |
| 5 | delayed | 198 | 0.6599 | -0.0009 [-0.0038, +0.0015] | LR logistic (nested) | 0.6550 | -0.0049 [-0.0102, -0.0000] | -0.0041 [-0.0081, +0.0001] | LR logistic (nested) -0.0048 [-0.0098, -0.0000] | +0.0266 [+0.0186, +0.0347] |
| 30 | cutoff | 198 | 0.6604 | -0.0026 [-0.0052, -0.0003] | CP conditional prior | 0.6569 | -0.0034 [-0.0057, -0.0016] | -0.0008 [-0.0026, +0.0008] | CP conditional prior -0.0006 [-0.0029, +0.0020] | +0.0231 [+0.0141, +0.0303] |
| 30 | delayed | 198 | 0.6660 | -0.0028 [-0.0054, -0.0002] | LR logistic (nested) | 0.6617 | -0.0043 [-0.0073, -0.0014] | -0.0014 [-0.0039, +0.0010] | BL blend with the prior +0.0000 [-0.0035, +0.0037] | +0.0189 [+0.0109, +0.0281] |
| 60 | cutoff | 198 | 0.6637 | -0.0022 [-0.0053, +0.0012] | CP conditional prior | 0.6619 | -0.0018 [-0.0038, +0.0005] | +0.0004 [-0.0025, +0.0034] | CP conditional prior +0.0003 [-0.0044, +0.0051] | +0.0197 [+0.0093, +0.0292] |
| 60 | delayed | 198 | 0.6641 | -0.0022 [-0.0069, +0.0028] | CP conditional prior | 0.6617 | -0.0024 [-0.0049, +0.0007] | -0.0002 [-0.0043, +0.0041] | CP conditional prior +0.0007 [-0.0064, +0.0075] | +0.0219 [+0.0124, +0.0301] |

Across the 8 problems and 8 arms (58 pairs), the direction-only score has 1 interval(s) below zero against no view: LR logistic (nested) at 5 min (delayed). At 95 %, about 1.5 would fall below zero by chance alone. The symmetrised (size) score has 3 interval(s) below zero against the symmetric prior.

### 4.4 By session phase: 15 minutes, cutoff origin (exploratory)

| phase | LR logistic (nested) - symmetric prior | GB boosted trees (nested) - symmetric prior | BL blend with the prior - symmetric prior | B_rth analogues RTH-20 (v3) - symmetric prior | B_rth_recent analogues + recent path - symmetric prior |
|---|---|---|---|---|---|
| morning | -0.0000 [-0.0052, +0.0052] | +0.0003 [-0.0046, +0.0058] | +0.0042 [-0.0007, +0.0097] | +0.0364 [+0.0212, +0.0525] | +0.0346 [+0.0192, +0.0518] |
| midday | -0.0032 [-0.0078, +0.0012] | -0.0038 [-0.0094, +0.0025] | +0.0001 [-0.0051, +0.0058] | +0.0205 [+0.0055, +0.0359] | +0.0218 [+0.0045, +0.0399] |
| afternoon | -0.0052 [-0.0097, -0.0006] | -0.0091 [-0.0153, -0.0037] | -0.0046 [-0.0110, +0.0017] | +0.0260 [+0.0105, +0.0415] | +0.0291 [+0.0136, +0.0450] |

### 4.5 The analogues: an expanding prefix against expanding plus recent

B_rth_recent ranks the same pool by the mean of two similarities: v3's, and a recent-path similarity (the last 30 minutes, re-anchored, judged at the path tolerance for 30 minutes). Against RTH-20 at 15 minutes from the cutoff it scores +0.0009 [-0.0060, +0.0080]. The recent path does not rescue the analogues: both variants lose to the same-clock history at every horizon (section 4.3). A later reversal is not what they are missing.

These are development estimates from reconstructed rankings. rth_session_v1 is the prospective test, and it scores direction as up or not up, with size separately.

### 4.6 Size and direction as distributions: 15 minutes

CRPS is in units of sigma_1m x sqrt(15).

| origin | CLOCK empirical | LS0: scale model, zero drift | LSmu: scale + ridge location | LS0 - CLOCK | LSmu - LS0 | 90 % coverage, empirical / LS | 90 % width, empirical / LS |
|---|---:|---:|---:|---|---|---|---|
| cutoff | 0.4949 | 0.4819 | 0.4826 | -0.0130 [-0.0182, -0.0087] | +0.0007 [+0.0002, +0.0013] | 89.5 % / 91.3 % | 2.87 / 2.85 |
| delayed | 0.4842 | 0.4703 | 0.4701 | -0.0139 [-0.0185, -0.0094] | -0.0002 [-0.0008, +0.0005] | 90.2 % / 91.8 % | 2.88 / 2.83 |

**Size is forecastable.** The scale is a regression of the log squared move on the volatility so far, relative volume, the range, the time of day and scheduled events. It beats the same-clock history with intervals below zero at both origins. Its log correlates with the log size of the move at 0.34.

**Direction is not.** The ridge location's penalty went to its maximum in every fold. Its out-of-sample correlation with the move is -0.055, and it gets the sign right 49.3 % of the time.

This repeats 2026-09's finding. A narrower calibrated range is not a directional edge. The project's random-walk fan (fan_rw_v1) is the deployed size benchmark. Its own reports (docs/reports/fan_rw_v1_NQ_*.md) cover it, and it was not re-run here.

## 5. Power

These figures come from the development per-session differences: their standard deviation, and the moving-block bootstrap's design effect (the variance of the mean against independent sessions). The table gives the sessions needed for 80 % power at a true improvement delta. It uses the normal approximation and two-sided 98.33 % intervals (Bonferroni over three candidates). Two rules:

- *registered* (p1_ml_forward_v2): the point estimate at most -0.01 and the upper bound below 0;
- *material* (recommended for any new study): the upper bound below -0.01.

| comparison | n | sd | design effect | registered: delta 0.01 / 0.02 / 0.03 | material: delta 0.02 / 0.03 / 0.05 | material at 95 % (fixed sequence): 0.02 / 0.03 / 0.05 | power at 60 sessions, registered, delta 0.02 / 0.03 | detectable at 60 (registered) |
|---|---:|---:|---:|---|---|---|---|---:|
| N - A (pre-open) | 158 | 0.158 | 0.76 | never / 505 / 225 | 2008 / 505 / 125 | 1516 / 377 / 94 | 10 % / 24 % | 0.058 |
| M - A (pre-open) | 158 | 0.119 | 0.86 | never / 318 / 141 | 1265 / 318 / 79 | 953 / 240 / 60 | 16 % / 39 % | 0.046 |
| P - A (pre-open) | 158 | 0.144 | 1.02 | never / 560 / 245 | 2219 / 560 / 138 | 1676 / 419 / 104 | 9 % / 21 % | 0.061 |
| LR - A (pre-open) | 158 | 0.171 | 0.90 | never / 688 / 311 | 2769 / 688 / 172 | 2090 / 516 / 129 | 8 % / 17 % | 0.068 |
| BL - A (pre-open) | 158 | 0.156 | 0.72 | never / 465 / 205 | 1853 / 465 / 115 | 1399 / 346 / 86 | 11 % / 26 % | 0.055 |
| N - B (pre-open) | 158 | 0.250 | 0.83 | never / 1371 / 608 | 5449 / 1371 / 339 | 4125 / 1034 / 256 | 4 % / 9 % | 0.095 |
| M - B (pre-open) | 158 | 0.236 | 0.81 | never / 1191 / 527 | 4742 / 1191 / 298 | 3588 / 897 / 225 | 5 % / 10 % | 0.089 |
| P - B (pre-open) | 158 | 0.259 | 0.96 | never / 1710 / 762 | 6781 / 1710 / 428 | 5134 / 1265 / 318 | 4 % / 7 % | 0.106 |
| LR - CLOCK (RTH 15 min, cutoff origin) | 198 | 0.027 | 0.99 | never / 20 / 9 | 78 / 20 / 5 | 59 / 15 / 4 | 100 % / 100 % | 0.013 |
| GB - CLOCK (RTH 15 min, cutoff origin) | 198 | 0.030 | 1.05 | never / 25 / 11 | 98 / 25 / 7 | 74 / 19 / 5 | 99 % / 100 % | 0.013 |
| B_rth - CLOCK (RTH 15 min, cutoff origin) | 198 | 0.065 | 0.98 | never / 108 / 48 | 437 / 108 / 27 | 325 / 81 / 21 | 51 % / 89 % | 0.027 |
| LR - symmetric prior (RTH 15 min, cutoff origin) | 198 | 0.023 | 1.12 | never / 16 / 7 | 61 / 16 / 4 | 46 / 12 / 3 | 100 % / 100 % | 0.013 |
| GB - symmetric prior (RTH 15 min, cutoff origin) | 198 | 0.027 | 1.21 | never / 23 / 11 | 91 / 23 / 6 | 68 / 17 / 5 | 100 % / 100 % | 0.013 |

A difference without significance does not show that two forecasts are equally good. A claim of non-inferiority would need a margin registered in advance and an adjusted upper bound below it. Even then, it would not meet the goal of a positive edge.

**What the table shows:**

- *An effect of exactly 0.01 never reaches 80 % power under the registered rule.* Its point threshold is 0.01 itself, so power stays at most 50 % however many sessions accrue.
- *Pre-open differences are noisy.* Their standard deviation is 0.12 to 0.26 per session, so 60 sessions detect only improvements of 0.046 to 0.106 - several times what any development estimate suggests.
- *RTH differences are much tighter*, at 0.023 to 0.065 per session, because each session averages twelve cutoffs. An RTH study is where 60 sessions could settle something - provided the comparator is the symmetric prior, not CLOCK.

## 6. Forecast edge is not trading edge

Nothing here measures money. A lower Brier score is a forecast edge. A trading claim would need:

- a simple execution and risk policy, fixed before the evaluation;
- net expectancy after fees, spread, slippage and the measured delivery delay;
- a comparison against the same policy run on A and B, and against no trade;
- conservative counting of ambiguous fills, because minute bars cannot settle the fill order inside a bar.

No trading claim is made, and this study authorises no trade.

## 7. Recommendation

**No edge established. No challenger is recommended.**

- A and B stay in force as they are: B as the existing baseline, A as the benchmark.
- N, M and P stay experimental.
- p1_ml_forward_v2 runs as registered. Its prospective sessions are the only clean test of N, M and P.
- Registering another direction model now would spend the forward sample on a candidate the development data do not support.

**Worth keeping in view, as size forecasts rather than direction challengers:**

- the scale model (section 4.6);
- the models' small gain on the neutral-or-not part at 15 minutes (section 4.2).

**Any later RTH comparison** should be made against the symmetric prior, not CLOCK. CLOCK's noisy per-cutoff tilt is beaten even with shuffled labels (section 1).

Both belong to the fan's territory. There they would compete with fan_rw_v1, not with A.

**The analogues.** rth_session_v1 (registered by its first issue) tests the full-session analogues prospectively. The development estimate above gives little reason to expect a directional result.

## Reproduce

```
python scripts/ml_study.py all    # or the stages one by one: predict-preopen, predict-rth, analogues,
                                  # tabpfn, controls, score
```

- TabPFN runs in .venv-research (research/requirements-tabpfn.txt), never in the production .venv. Its weights are pinned by sha256 in research/tabpfn_arm.py.
- The database is read with default_transaction_read_only; nothing is written to it.

