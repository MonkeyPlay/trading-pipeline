# Intermarket Direction: fan_im_dir_v1, NQ

Definition `2171e8f4da7adde0` (docs/fan_im_direction.md), fixed before this run; code b7078a7b7b55. 150 sessions in 5 chronological blocks, every origin; all previously examined - development.

**Conclusion: PASS** - 5 min: pass; 15 min: not below both.

Arms: A `zero`, B `own` (NQ's returns, VWAP distance, volatility, relative volume, phase), C `intermarket` (B plus ES and RTY returns, NQ relative to ES, co-movement, ES and RTY volume and volatility). Differences per session in CRPS basis points (share of the second arm's CRPS), 95 % moving-block interval; the decision uses 97.5 % (Bonferroni over two horizons).

## Pooled

| Horizon | Arm | CRPS (bps) | Brier | 50 % / 90 % coverage | Shifted origins | Mean abs shift (sigma / bps) |
|---|---|---|---|---|---|---|
| 5 min | zero | 3.6472 | 0.25002 | 48.2 / 89.4 % | 0.0 % | 0.000 / 0.00 |
| 5 min | own | 3.6472 | 0.25002 | 48.2 / 89.4 % | 0.0 % | 0.000 / 0.00 |
| 5 min | intermarket | 3.6468 | 0.24998 | 48.2 / 89.4 % | 20.0 % | 0.024 / 0.18 |
| 15 min | zero | 6.3539 | 0.25000 | 47.6 / 89.5 % | 0.0 % | 0.000 / 0.00 |
| 15 min | own | 6.3539 | 0.25000 | 47.6 / 89.5 % | 0.0 % | 0.000 / 0.00 |
| 15 min | intermarket | 6.3539 | 0.25000 | 47.6 / 89.5 % | 0.0 % | 0.000 / 0.00 |

| Horizon | Comparison | CRPS | Brier | Decision (97.5 %) |
|---|---|---|---|---|
| 5 min | intermarket - zero | -0.013 % [-0.00087, -0.00005] | -0.00004 [-0.00007, +0.00000] | -0.013 % [-0.00094, -0.00002] |
| 5 min | intermarket - own | -0.013 % [-0.00087, -0.00005] | -0.00004 [-0.00007, +0.00000] | -0.013 % [-0.00094, -0.00002] |
| 5 min | own - zero | +0.000 % [+0.00000, +0.00000] | +0.00000 [+0.00000, +0.00000] | - |
| 15 min | intermarket - zero | +0.000 % [+0.00000, +0.00000] | +0.00000 [+0.00000, +0.00000] | +0.000 % [+0.00000, +0.00000] |
| 15 min | intermarket - own | +0.000 % [+0.00000, +0.00000] | +0.00000 [+0.00000, +0.00000] | +0.000 % [+0.00000, +0.00000] |
| 15 min | own - zero | +0.000 % [+0.00000, +0.00000] | +0.00000 [+0.00000, +0.00000] | - |

## Per block

| Block | Trained on | Validation | Activated | C - A 5 / 15 min | C - B 5 / 15 min |
|---|---|---|---|---|---|
| check 1 (2026-03-03 to 2026-04-14) | 153 | 31 (2026-01-15 to 2026-03-02) | intermarket|h5 | -0.057 % / +0.000 % | -0.057 % / +0.000 % |
| check 2 (2026-04-15 to 2026-05-27) | 183 | 37 (2026-02-20 to 2026-04-14) | none | +0.000 % / +0.000 % | +0.000 % / +0.000 % |
| check 3 (2026-05-28 to 2026-07-10) | 213 | 43 (2026-03-26 to 2026-05-27) | none | +0.000 % / +0.000 % | +0.000 % / +0.000 % |
| holdout 1 (2026-07-13 to 2026-08-21) | 243 | 49 (2026-04-30 to 2026-07-10) | none | +0.000 % / +0.000 % | +0.000 % / +0.000 % |
| holdout 2 (2026-08-24 to 2026-10-05) | 273 | 55 (2026-06-04 to 2026-08-21) | none | +0.000 % / +0.000 % | +0.000 % / +0.000 % |

## Selection on validation

Best option per block, arm and horizon: its mean CRPS change against zero on the validation sessions and the 95 % interval (activation reads 99.58 %).

| Block | Arm, horizon | Best option | Validation change (bps) | Interval | Activated |
|---|---|---|---|---|---|
| check 1 | own|h5 | depth 2, 50 trees, lambda 1.0 | -0.00250 | [-0.00674, +0.00072] | no |
| check 1 | own|h15 | zero | +0.00000 | - | no |
| check 1 | intermarket|h5 | depth 3, 50 trees, lambda 1.0 | -0.00204 | [-0.00565, -0.00015] | yes |
| check 1 | intermarket|h15 | zero | +0.00000 | - | no |
| check 2 | own|h5 | depth 2, 50 trees, lambda 1.0 | -0.00172 | [-0.00520, +0.00049] | no |
| check 2 | own|h15 | depth 2, 50 trees, lambda 0.5 | -0.00034 | [-0.00301, +0.00209] | no |
| check 2 | intermarket|h5 | depth 3, 50 trees, lambda 1.0 | -0.00328 | [-0.00826, +0.00016] | no |
| check 2 | intermarket|h15 | depth 2, 50 trees, lambda 0.5 | -0.00074 | [-0.00362, +0.00268] | no |
| check 3 | own|h5 | depth 2, 50 trees, lambda 0.25 | -0.00007 | [-0.00076, +0.00060] | no |
| check 3 | own|h15 | zero | +0.00000 | - | no |
| check 3 | intermarket|h5 | depth 3, 50 trees, lambda 0.25 | -0.00025 | [-0.00091, +0.00053] | no |
| check 3 | intermarket|h15 | depth 2, 50 trees, lambda 1.0 | -0.00092 | [-0.00386, +0.00403] | no |
| holdout 1 | own|h5 | depth 3, 50 trees, lambda 0.25 | -0.00018 | [-0.00107, +0.00068] | no |
| holdout 1 | own|h15 | depth 2, 50 trees, lambda 0.5 | -0.00071 | [-0.00529, +0.00184] | no |
| holdout 1 | intermarket|h5 | depth 3, 150 trees, lambda 0.5 | -0.00101 | [-0.00544, +0.00208] | no |
| holdout 1 | intermarket|h15 | depth 2, 50 trees, lambda 1.0 | -0.00095 | [-0.00712, +0.00389] | no |
| holdout 2 | own|h5 | depth 2, 50 trees, lambda 1.0 | -0.00104 | [-0.00327, +0.00093] | no |
| holdout 2 | own|h15 | depth 3, 50 trees, lambda 0.25 | -0.00027 | [-0.00257, +0.00168] | no |
| holdout 2 | intermarket|h5 | depth 2, 150 trees, lambda 1.0 | -0.00193 | [-0.00757, +0.00325] | no |
| holdout 2 | intermarket|h15 | zero | +0.00000 | - | no |

## Reliability of P(up), pooled

- 5 min, own: 0.50 -> 0.517 (54,468); 0.50 -> 0.499 (190,704)
- 5 min, intermarket: 0.49 -> 0.500 (68,012); 0.50 -> 0.504 (177,160)
- 15 min, own: 0.50 -> 0.507 (244,590)
- 15 min, intermarket: 0.50 -> 0.507 (244,590)

## Diagnostics (explanation only, never a deployment rule)

CRPS difference in bps per session, bucket by bucket; divergence terciles of |NQ.rel_es15| from each block's training rows.

| Horizon | Breakdown | Bucket | Rows | C - A | C - B |
|---|---|---|---|---|---|
| 5 min | phase | after_close | 8,250 | -0.00040 [-0.00174, +0.00036] | -0.00040 [-0.00174, +0.00036] |
| 5 min | phase | afternoon | 18,000 | +0.00019 [-0.00136, +0.00205] | +0.00019 [-0.00136, +0.00205] |
| 5 min | phase | midday | 31,500 | -0.00027 [-0.00165, +0.00086] | -0.00027 [-0.00165, +0.00086] |
| 5 min | phase | opening_hour | 9,000 | -0.00311 [-0.00712, +0.00050] | -0.00311 [-0.00712, +0.00050] |
| 5 min | phase | overnight | 125,999 | -0.00041 [-0.00090, +0.00011] | -0.00041 [-0.00090, +0.00011] |
| 5 min | phase | pre_open | 13,500 | -0.00071 [-0.00311, +0.00111] | -0.00071 [-0.00311, +0.00111] |
| 5 min | comove15 | diverging | 27,309 | -0.00083 [-0.00167, +0.00001] | -0.00083 [-0.00167, +0.00001] |
| 5 min | comove15 | together down | 87,537 | -0.00035 [-0.00071, +0.00015] | -0.00035 [-0.00071, +0.00015] |
| 5 min | comove15 | together up | 91,403 | -0.00057 [-0.00114, -0.00005] | -0.00057 [-0.00114, -0.00005] |
| 5 min | divergence | high | 83,523 | -0.00107 [-0.00218, +0.00012] | -0.00107 [-0.00218, +0.00012] |
| 5 min | divergence | low | 59,527 | -0.00021 [-0.00064, +0.00032] | -0.00021 [-0.00064, +0.00032] |
| 5 min | divergence | mid | 63,199 | -0.00024 [-0.00083, +0.00024] | -0.00024 [-0.00083, +0.00024] |
| 15 min | phase | after_close | 6,750 | +0.00000 [+0.00000, +0.00000] | +0.00000 [+0.00000, +0.00000] |
| 15 min | phase | afternoon | 18,000 | +0.00000 [+0.00000, +0.00000] | +0.00000 [+0.00000, +0.00000] |
| 15 min | phase | midday | 31,500 | +0.00000 [+0.00000, +0.00000] | +0.00000 [+0.00000, +0.00000] |
| 15 min | phase | opening_hour | 9,000 | +0.00000 [+0.00000, +0.00000] | +0.00000 [+0.00000, +0.00000] |
| 15 min | phase | overnight | 125,999 | +0.00000 [+0.00000, +0.00000] | +0.00000 [+0.00000, +0.00000] |
| 15 min | phase | pre_open | 13,500 | +0.00000 [+0.00000, +0.00000] | +0.00000 [+0.00000, +0.00000] |
| 15 min | comove15 | diverging | 27,018 | +0.00000 [+0.00000, +0.00000] | +0.00000 [+0.00000, +0.00000] |
| 15 min | comove15 | together down | 86,897 | +0.00000 [+0.00000, +0.00000] | +0.00000 [+0.00000, +0.00000] |
| 15 min | comove15 | together up | 90,834 | +0.00000 [+0.00000, +0.00000] | +0.00000 [+0.00000, +0.00000] |
| 15 min | divergence | high | 83,123 | +0.00000 [+0.00000, +0.00000] | +0.00000 [+0.00000, +0.00000] |
| 15 min | divergence | low | 58,976 | +0.00000 [+0.00000, +0.00000] | +0.00000 [+0.00000, +0.00000] |
| 15 min | divergence | mid | 62,650 | +0.00000 [+0.00000, +0.00000] | +0.00000 [+0.00000, +0.00000] |
