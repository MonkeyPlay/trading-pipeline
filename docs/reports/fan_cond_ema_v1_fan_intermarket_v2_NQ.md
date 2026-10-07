# Conditional EMA Direction: fan_cond_ema_v1, NQ

Definition `3ec8838a731fb229` (docs/fan_cond_ema.md), fixed before this run; code b7078a7b7b55. 150 sessions in 5 chronological blocks, every origin; all previously examined - development.

**Conclusion: FAIL** - 5 min: not below both; 15 min: not below both.

Arms: A `zero`, B `context` (returns, VWAP distance, volatility, relative volume, phase), C `ema` (B plus the EMA set). Differences per session in CRPS basis points (share of the second arm's CRPS), 95 % moving-block interval; the decision uses 97.5 % (Bonferroni over two horizons).

## Pooled

| Horizon | Arm | CRPS (bps) | Brier | 50 % / 90 % coverage | Shifted origins | Mean abs shift (sigma / bps) |
|---|---|---|---|---|---|---|
| 5 min | zero | 3.6472 | 0.25002 | 48.2 / 89.4 % | 0.0 % | 0.000 / 0.00 |
| 5 min | context | 3.6472 | 0.25002 | 48.2 / 89.4 % | 0.0 % | 0.000 / 0.00 |
| 5 min | ema | 3.6472 | 0.25002 | 48.2 / 89.4 % | 0.0 % | 0.000 / 0.00 |
| 15 min | zero | 6.3539 | 0.25000 | 47.6 / 89.5 % | 0.0 % | 0.000 / 0.00 |
| 15 min | context | 6.3539 | 0.25000 | 47.6 / 89.5 % | 0.0 % | 0.000 / 0.00 |
| 15 min | ema | 6.3539 | 0.25000 | 47.6 / 89.5 % | 0.0 % | 0.000 / 0.00 |

| Horizon | Comparison | CRPS | Brier | Decision (97.5 %) |
|---|---|---|---|---|
| 5 min | ema - zero | +0.000 % [+0.00000, +0.00000] | +0.00000 [+0.00000, +0.00000] | +0.000 % [+0.00000, +0.00000] |
| 5 min | ema - context | +0.000 % [+0.00000, +0.00000] | +0.00000 [+0.00000, +0.00000] | +0.000 % [+0.00000, +0.00000] |
| 5 min | context - zero | +0.000 % [+0.00000, +0.00000] | +0.00000 [+0.00000, +0.00000] | - |
| 15 min | ema - zero | +0.000 % [+0.00000, +0.00000] | +0.00000 [+0.00000, +0.00000] | +0.000 % [+0.00000, +0.00000] |
| 15 min | ema - context | +0.000 % [+0.00000, +0.00000] | +0.00000 [+0.00000, +0.00000] | +0.000 % [+0.00000, +0.00000] |
| 15 min | context - zero | +0.000 % [+0.00000, +0.00000] | +0.00000 [+0.00000, +0.00000] | - |

## Per block

| Block | Trained on | Validation | Activated | C - A 5 / 15 min | C - B 5 / 15 min |
|---|---|---|---|---|---|
| check 1 (2026-03-03 to 2026-04-14) | 153 | 31 (2026-01-15 to 2026-03-02) | none | +0.000 % / +0.000 % | +0.000 % / +0.000 % |
| check 2 (2026-04-15 to 2026-05-27) | 183 | 37 (2026-02-20 to 2026-04-14) | none | +0.000 % / +0.000 % | +0.000 % / +0.000 % |
| check 3 (2026-05-28 to 2026-07-10) | 213 | 43 (2026-03-26 to 2026-05-27) | none | +0.000 % / +0.000 % | +0.000 % / +0.000 % |
| holdout 1 (2026-07-13 to 2026-08-21) | 243 | 49 (2026-04-30 to 2026-07-10) | none | +0.000 % / +0.000 % | +0.000 % / +0.000 % |
| holdout 2 (2026-08-24 to 2026-10-05) | 273 | 55 (2026-06-04 to 2026-08-21) | none | +0.000 % / +0.000 % | +0.000 % / +0.000 % |

## Selection on validation

Best option per block, arm and horizon: its mean CRPS change against zero on the validation sessions and the 95 % interval (activation reads 99.58 %).

| Block | Arm, horizon | Best option | Validation change (bps) | Interval | Activated |
|---|---|---|---|---|---|
| check 1 | context|h5 | depth 2, 50 trees, lambda 1.0 | -0.00250 | [-0.00674, +0.00072] | no |
| check 1 | context|h15 | zero | +0.00000 | - | no |
| check 1 | ema|h5 | depth 3, 50 trees, lambda 1.0 | -0.00141 | [-0.00617, +0.00188] | no |
| check 1 | ema|h15 | depth 3, 50 trees, lambda 0.25 | -0.00024 | [-0.00303, +0.00223] | no |
| check 2 | context|h5 | depth 2, 50 trees, lambda 1.0 | -0.00172 | [-0.00520, +0.00049] | no |
| check 2 | context|h15 | depth 2, 50 trees, lambda 0.5 | -0.00034 | [-0.00301, +0.00209] | no |
| check 2 | ema|h5 | depth 2, 50 trees, lambda 1.0 | -0.00143 | [-0.00450, +0.00077] | no |
| check 2 | ema|h15 | depth 2, 150 trees, lambda 0.5 | -0.00187 | [-0.00620, +0.00352] | no |
| check 3 | context|h5 | depth 2, 50 trees, lambda 0.25 | -0.00007 | [-0.00076, +0.00060] | no |
| check 3 | context|h15 | zero | +0.00000 | - | no |
| check 3 | ema|h5 | depth 2, 50 trees, lambda 0.5 | -0.00015 | [-0.00153, +0.00116] | no |
| check 3 | ema|h15 | depth 2, 50 trees, lambda 0.25 | -0.00005 | [-0.00068, +0.00114] | no |
| holdout 1 | context|h5 | depth 3, 50 trees, lambda 0.25 | -0.00018 | [-0.00107, +0.00068] | no |
| holdout 1 | context|h15 | depth 2, 50 trees, lambda 0.5 | -0.00071 | [-0.00529, +0.00184] | no |
| holdout 1 | ema|h5 | depth 2, 150 trees, lambda 0.5 | -0.00047 | [-0.00233, +0.00112] | no |
| holdout 1 | ema|h15 | depth 2, 50 trees, lambda 1.0 | -0.00279 | [-0.01198, +0.00263] | no |
| holdout 2 | context|h5 | depth 2, 50 trees, lambda 1.0 | -0.00104 | [-0.00327, +0.00093] | no |
| holdout 2 | context|h15 | depth 3, 50 trees, lambda 0.25 | -0.00027 | [-0.00257, +0.00168] | no |
| holdout 2 | ema|h5 | depth 2, 150 trees, lambda 0.5 | -0.00081 | [-0.00275, +0.00115] | no |
| holdout 2 | ema|h15 | depth 3, 50 trees, lambda 1.0 | -0.00145 | [-0.00870, +0.00561] | no |

## Reliability of P(up), pooled

- 5 min, context: 0.50 -> 0.517 (54,468); 0.50 -> 0.499 (190,704)
- 5 min, ema: 0.50 -> 0.517 (54,468); 0.50 -> 0.499 (190,704)
- 15 min, context: 0.50 -> 0.507 (244,590)
- 15 min, ema: 0.50 -> 0.507 (244,590)

## Diagnostics (explanation only, never a deployment rule)

CRPS difference in bps per session, bucket by bucket; extension terciles from each block's training rows.

| Horizon | Breakdown | Bucket | Rows | C - A | C - B |
|---|---|---|---|---|---|
| 5 min | phase | after_close | 8,250 | +0.00000 [+0.00000, +0.00000] | +0.00000 [+0.00000, +0.00000] |
| 5 min | phase | afternoon | 18,000 | +0.00000 [+0.00000, +0.00000] | +0.00000 [+0.00000, +0.00000] |
| 5 min | phase | midday | 31,500 | +0.00000 [+0.00000, +0.00000] | +0.00000 [+0.00000, +0.00000] |
| 5 min | phase | opening_hour | 9,000 | +0.00000 [+0.00000, +0.00000] | +0.00000 [+0.00000, +0.00000] |
| 5 min | phase | overnight | 125,999 | +0.00000 [+0.00000, +0.00000] | +0.00000 [+0.00000, +0.00000] |
| 5 min | phase | pre_open | 13,500 | +0.00000 [+0.00000, +0.00000] | +0.00000 [+0.00000, +0.00000] |
| 5 min | tf_agree | disagree | 62,955 | +0.00000 [+0.00000, +0.00000] | +0.00000 [+0.00000, +0.00000] |
| 5 min | tf_agree | down | 61,637 | +0.00000 [+0.00000, +0.00000] | +0.00000 [+0.00000, +0.00000] |
| 5 min | tf_agree | up | 81,657 | +0.00000 [+0.00000, +0.00000] | +0.00000 [+0.00000, +0.00000] |
| 5 min | extension | high | 69,410 | +0.00000 [+0.00000, +0.00000] | +0.00000 [+0.00000, +0.00000] |
| 5 min | extension | low | 69,036 | +0.00000 [+0.00000, +0.00000] | +0.00000 [+0.00000, +0.00000] |
| 5 min | extension | mid | 67,803 | +0.00000 [+0.00000, +0.00000] | +0.00000 [+0.00000, +0.00000] |
| 15 min | phase | after_close | 6,750 | +0.00000 [+0.00000, +0.00000] | +0.00000 [+0.00000, +0.00000] |
| 15 min | phase | afternoon | 18,000 | +0.00000 [+0.00000, +0.00000] | +0.00000 [+0.00000, +0.00000] |
| 15 min | phase | midday | 31,500 | +0.00000 [+0.00000, +0.00000] | +0.00000 [+0.00000, +0.00000] |
| 15 min | phase | opening_hour | 9,000 | +0.00000 [+0.00000, +0.00000] | +0.00000 [+0.00000, +0.00000] |
| 15 min | phase | overnight | 125,999 | +0.00000 [+0.00000, +0.00000] | +0.00000 [+0.00000, +0.00000] |
| 15 min | phase | pre_open | 13,500 | +0.00000 [+0.00000, +0.00000] | +0.00000 [+0.00000, +0.00000] |
| 15 min | tf_agree | disagree | 62,420 | +0.00000 [+0.00000, +0.00000] | +0.00000 [+0.00000, +0.00000] |
| 15 min | tf_agree | down | 61,188 | +0.00000 [+0.00000, +0.00000] | +0.00000 [+0.00000, +0.00000] |
| 15 min | tf_agree | up | 81,141 | +0.00000 [+0.00000, +0.00000] | +0.00000 [+0.00000, +0.00000] |
| 15 min | extension | high | 69,170 | +0.00000 [+0.00000, +0.00000] | +0.00000 [+0.00000, +0.00000] |
| 15 min | extension | low | 68,386 | +0.00000 [+0.00000, +0.00000] | +0.00000 [+0.00000, +0.00000] |
| 15 min | extension | mid | 67,193 | +0.00000 [+0.00000, +0.00000] | +0.00000 [+0.00000, +0.00000] |
