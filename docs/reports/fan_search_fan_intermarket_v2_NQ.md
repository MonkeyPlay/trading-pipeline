# The development search under review: fan_intermarket_v2, NQ, 15 minutes

25 stored checks runs, each on the checks' 90 sessions; the loss is a session's mean CRPS. **SPA** (Hansen 2005) asks whether *any* run is better than the benchmark once the search is taken into account - the consistent p-value is the test's, the upper one White's reality check; **StepM** (Romano and Wolf 2005) names the runs that are, holding the chance of naming any wrongly at 5 %. Both resample whole sessions in 5-session blocks (2000 resamples). Development only: neither replaces the sealed holdout, and neither sees the trials below that left no rebuildable definition, nor the choices made by looking at screens. Cross-checked against the arch package (8.0.0): with arch's choices - raw mean differences (its `studentize` flag changes nothing in that version) and consistent recentring - this implementation reproduces arch's moving-block p-values and StepM sets; the primary procedure studentises, as Hansen recommends, and the table under each benchmark shows what the choice changes.

## Against fan_rw_v2 - is any run better than the baseline?

SPA statistic 3.64; p lower 0.002, **consistent 0.003**, upper 0.003. StepM names `crps_ivx`, `gam_ivx`, `gbm_own`, `gbm_own_iv`, `gbm_own_ivx`, `lin_pois_ivx`, `lin_pois_ivx_shape`, `trial_a1_gbm_own`, `trial_abl_c_level`, `trial_abl_d_both_sides`, `trial_own_both_iv`, `trial_own_volatility_f2` (2 step(s); first critical value 2.38).

| Run | Where it came from | Features | Difference (bps) | Share | t | StepM |
|---|---|---|---|---|---|---|
Procedure sensitivity:

| Procedure | SPA p (lower / consistent / upper) | StepM names |
|---|---|---|
| studentised, own recentring (Romano and Wolf; the primary) | 0.0020 / 0.0030 / 0.0030 | 12: `crps_ivx`, `gam_ivx`, `gbm_own`, `gbm_own_iv`, `gbm_own_ivx`, `lin_pois_ivx`, `lin_pois_ivx_shape`, `trial_a1_gbm_own`, `trial_abl_c_level`, `trial_abl_d_both_sides`, `trial_own_both_iv`, `trial_own_volatility_f2` |
| studentised, consistent recentring | 0.0020 / 0.0030 / 0.0030 | 12: `crps_ivx`, `gam_ivx`, `gbm_own`, `gbm_own_iv`, `gbm_own_ivx`, `lin_pois_ivx`, `lin_pois_ivx_shape`, `trial_a1_gbm_own`, `trial_abl_c_level`, `trial_abl_d_both_sides`, `trial_own_both_iv`, `trial_own_volatility_f2` |
| raw means, consistent recentring (as arch 8.0.0 computes it) | 0.0020 / 0.0020 / 0.0020 | 17: `crps_ivx`, `gam_ivx`, `gbm_all`, `gbm_own`, `gbm_own_iv`, `gbm_own_ivx`, `lin_pois_ivx`, `lin_pois_ivx_shape`, `trial_a1_gbm_own`, `trial_a3_gbm_all_f2`, `trial_abl_b_rv5d`, `trial_abl_c_level`, `trial_abl_d_both_sides`, `trial_add_equity_etfs`, `trial_add_volatility`, `trial_own_both_iv`, `trial_own_volatility_f2` |

| `gbm_own_ivx` | candidate | 17 (`7c0f6e1c82ddb538`) | -0.02746 | -0.40 % | +3.64 | better |
| `lin_pois_ivx_shape` | candidate | 16 (`61b6a1e0c67b00c2`) | -0.02604 | -0.38 % | +3.15 | better |
| `lin_pois_ivx` | candidate | 16 (`61b6a1e0c67b00c2`) | -0.02528 | -0.37 % | +3.10 | better |
| `crps_ivx` | candidate | 17 (`7c0f6e1c82ddb538`) | -0.02488 | -0.37 % | +3.61 | better |
| `gam_ivx` | candidate | 17 (`7c0f6e1c82ddb538`) | -0.02412 | -0.35 % | +3.47 | better |
| `trial_own_volatility_f2` | research: own, the volatility group and iv_rv | 36 (`4f3a3fc79a79fbb6`) | -0.02228 | -0.33 % | +3.11 | better |
| `trial_abl_d_both_sides` | ablation D: both sides, no ratio | 16 (`61b6a1e0c67b00c2`) | -0.02186 | -0.32 % | +3.06 | better |
| `gbm_own_iv` | candidate | 15 (`6f93b3c12955a67e`) | -0.02164 | -0.32 % | +3.50 | better |
| `trial_abl_c_level` | ablation C: VXN's level | 15 (`9767c26852cd7de3`) | -0.01858 | -0.27 % | +2.84 | better |
| `trial_own_both_iv` | research: VXN and VIX iv_rv | 16 (`c5baf48ab9479fcc`) | -0.01753 | -0.26 % | +2.74 | better |
| `gbm_own` | candidate | 14 (`ba348244f5221811`) | -0.01616 | -0.24 % | +2.99 | better |
| `trial_a3_gbm_all_f2` | research: every feature with iv_rv (attempt 3) | 125 (`65aa3849bddb8e44`) | -0.01564 | -0.23 % | +2.07 | - |
| `trial_a1_gbm_own` | chunk 5, attempt 1 | 14 (`ba348244f5221811`) | -0.01542 | -0.23 % | +2.80 | better |
| `gbm_all` | candidate | 137 (`906f219fb690f87d`) | -0.01492 | -0.22 % | +2.05 | - |
| `trial_add_volatility` | research: own plus one group | 34 (`05e84e8b268899d8`) | -0.01446 | -0.21 % | +2.31 | - |
| `trial_add_equity_etfs` | research: own plus one group | 54 (`819180dbb484df2d`) | -0.01424 | -0.21 % | +2.29 | - |
| `trial_abl_b_rv5d` | ablation B: the denominator | 15 (`1ffc74d6c3b99508`) | -0.01326 | -0.19 % | +2.17 | - |
| `trial_add_dollar` | research: own plus one group | 24 (`6641a7698f6600e6`) | -0.01235 | -0.18 % | +1.97 | - |
| `trial_add_index_futures` | research: own plus one group | 34 (`8d0f4c06ef57dfcf`) | -0.01173 | -0.17 % | +1.87 | - |
| `trial_a2b_gbm_all_f1` | chunk 5, attempt 2b | 123 (`1d34b9e391cf20fa`) | -0.00963 | -0.14 % | +1.66 | - |
| `trial_add_rates` | research: own plus one group | 33 (`9594fde0d48181f7`) | -0.00790 | -0.12 % | +1.40 | - |
| `trial_a2_gbm_all_f1` | chunk 5, attempt 2 | 123 (`1d34b9e391cf20fa`) | -0.00618 | -0.09 % | +0.84 | - |
| `trial_a1_gbm_all_f1` | chunk 5, attempt 1 | 123 (`1d34b9e391cf20fa`) | -0.00403 | -0.06 % | +0.60 | - |
| `crps_scale` | candidate | 0 (`e3b0c44298fc1c14`) | -0.00032 | -0.00 % | +0.26 | - |
| `phase_scale` | reference | - | +0.00180 | +0.03 % | -1.23 | - |

## Against gbm_own - does any run that reads another market beat NQ's own features?

SPA statistic 3.59; p lower 0.003, **consistent 0.004**, upper 0.004. StepM names `crps_ivx`, `gam_ivx`, `gbm_own_ivx`, `gbm_own_iv` (3 step(s); first critical value 2.59).

| Run | Where it came from | Features | Difference (bps) | Share | t | StepM |
|---|---|---|---|---|---|---|
Procedure sensitivity:

| Procedure | SPA p (lower / consistent / upper) | StepM names |
|---|---|---|
| studentised, own recentring (Romano and Wolf; the primary) | 0.0025 / 0.0045 / 0.0045 | 4: `crps_ivx`, `gam_ivx`, `gbm_own_iv`, `gbm_own_ivx` |
| studentised, consistent recentring | 0.0025 / 0.0045 / 0.0045 | 4: `crps_ivx`, `gam_ivx`, `gbm_own_iv`, `gbm_own_ivx` |
| raw means, consistent recentring (as arch 8.0.0 computes it) | 0.0080 / 0.0085 / 0.0085 | 5: `crps_ivx`, `gam_ivx`, `gbm_own_ivx`, `lin_pois_ivx`, `lin_pois_ivx_shape` |

| `gbm_own_ivx` | candidate | 17 (`7c0f6e1c82ddb538`) | -0.01129 | -0.17 % | +3.59 | better |
| `lin_pois_ivx_shape` | candidate | 16 (`61b6a1e0c67b00c2`) | -0.00987 | -0.15 % | +2.41 | - |
| `lin_pois_ivx` | candidate | 16 (`61b6a1e0c67b00c2`) | -0.00911 | -0.13 % | +2.24 | - |
| `crps_ivx` | candidate | 17 (`7c0f6e1c82ddb538`) | -0.00872 | -0.13 % | +3.27 | better |
| `gam_ivx` | candidate | 17 (`7c0f6e1c82ddb538`) | -0.00796 | -0.12 % | +3.31 | better |
| `trial_own_volatility_f2` | research: own, the volatility group and iv_rv | 36 (`4f3a3fc79a79fbb6`) | -0.00611 | -0.09 % | +2.20 | - |
| `trial_abl_d_both_sides` | ablation D: both sides, no ratio | 16 (`61b6a1e0c67b00c2`) | -0.00569 | -0.08 % | +2.30 | - |
| `gbm_own_iv` | candidate | 15 (`6f93b3c12955a67e`) | -0.00548 | -0.08 % | +2.59 | better |
| `trial_abl_c_level` | ablation C: VXN's level | 15 (`9767c26852cd7de3`) | -0.00241 | -0.04 % | +1.40 | - |
| `trial_own_both_iv` | research: VXN and VIX iv_rv | 16 (`c5baf48ab9479fcc`) | -0.00136 | -0.02 % | +0.44 | - |
| `trial_a3_gbm_all_f2` | research: every feature with iv_rv (attempt 3) | 125 (`65aa3849bddb8e44`) | +0.00053 | +0.01 % | -0.16 | - |
| `gbm_all` | candidate | 137 (`906f219fb690f87d`) | +0.00125 | +0.02 % | -0.40 | - |
| `trial_add_volatility` | research: own plus one group | 34 (`05e84e8b268899d8`) | +0.00170 | +0.03 % | -1.14 | - |
| `trial_add_equity_etfs` | research: own plus one group | 54 (`819180dbb484df2d`) | +0.00192 | +0.03 % | -0.95 | - |
| `trial_add_dollar` | research: own plus one group | 24 (`6641a7698f6600e6`) | +0.00381 | +0.06 % | -1.94 | - |
| `trial_add_index_futures` | research: own plus one group | 34 (`8d0f4c06ef57dfcf`) | +0.00444 | +0.07 % | -2.35 | - |
| `trial_a2b_gbm_all_f1` | chunk 5, attempt 2b | 123 (`1d34b9e391cf20fa`) | +0.00653 | +0.10 % | -2.46 | - |
| `trial_add_rates` | research: own plus one group | 33 (`9594fde0d48181f7`) | +0.00826 | +0.12 % | -5.04 | - |
| `trial_a2_gbm_all_f1` | chunk 5, attempt 2 | 123 (`1d34b9e391cf20fa`) | +0.00999 | +0.15 % | -3.20 | - |
| `trial_a1_gbm_all_f1` | chunk 5, attempt 1 | 123 (`1d34b9e391cf20fa`) | +0.01213 | +0.18 % | -3.88 | - |

## Trials not in the test

Left no rebuildable definition (their results are in docs/fan_experiment.md):

- ablation G: iv_rv from the previous session's VXN close (a prototype column)
- HAR: NQ's 1- and 22-day realised variance beside rv5d (prototype columns)
- semivariance: NQ's falling- and rising-price variance over 15 / 60 / 240 minutes (prototype columns)
- HAR and semivariance together (prototype columns)
- a calibration and shrinkage layer a x m^b chosen on the last 30 training sessions (review round 1)
- E's shape refitted on its training rows' own residuals (review round 1, in sample)
- E with training origins at minute 1-4 of the five (the same model: a robustness check)
- a linear Poisson model on VXN's level and NQ's 5-day realised variance only (review round 1)
- never scored, screened by rank correlation only: premarket volume since 04:00 (QQQ, SPY, SMH) and NQ's realised variance against ES's and SMH's (15, 60, 240 minutes) - the screen that picked iv_rv

Invalidated:

- review round 1's constant-scale benchmark: it chose its scale with the normal's quantiles and was scored with v2's real shape (replaced by crps_scale)
- the first HAR / semivariance run: a selector took every new column into every variant (rerun correctly; not rebuilt, above)
