# Direction: fan_direction_v1 on fan_intermarket_v2, NQ

Code b7078a7b7b55; settings `350c8e965d909919`; experiment `5adc22ef9b82116d`. Development only: the holdout blocks were examined by the scale experiment before this one existed - confirmation needs sessions after 2026-10-05.

Three centres on one scale (lin_pois_ivx, refitted per block; fan_rw_v2's shape): **zero** (no drift), **ema_damped** (0.5 x h x the EMA 14's slope per minute), **ridge** (a learned shift in the scale's sigmas, capped at +-1.0). Every origin of 150 sessions; each block trained on every session before it. Arm minus zero per session, 95 % moving-block bootstrap interval in the metric's units (* = whole interval below zero, better; ! = above, worse). Brier: P(up) against a move above zero, moves of exactly zero left out. RPS: below / within / above +-0.25 sigma.

## Pooled

| Horizon | Arm | CRPS (bps) | vs zero | Brier | vs zero | RPS | vs zero | Hit rate | 50 / 80 / 90 % coverage | Mean shift (sigma) |
|---|---|---|---|---|---|---|---|---|---|---|
| 5 min | zero | 3.6472 | - | 0.2500 | - | 0.2373 | - | - | 48.2 / 78.8 / 89.4 | 0.000 |
| 5 min | ema_damped | 3.7994 | +4.17 % ! [+0.1343, +0.1715] | 0.2642 | +5.66 % ! [+0.0134, +0.0148] | 0.2503 | +5.48 % ! [+0.0123, +0.0136] | 49.8 % | 46.4 / 77.0 / 88.3 | 0.203 |
| 5 min | ridge | 3.6475 | +0.01 % [-0.0007, +0.0011] | 0.2500 | +0.01 % [-0.0001, +0.0001] | 0.2373 | +0.01 % [-0.0000, +0.0001] | 50.2 % | 48.2 / 78.8 / 89.4 | 0.015 |
| 15 min | zero | 6.3539 | - | 0.2500 | - | 0.2380 | - | - | 47.6 / 78.6 / 89.5 | 0.000 |
| 15 min | ema_damped | 7.1167 | +12.01 % ! [+0.6664, +0.8582] | 0.2846 | +13.83 % ! [+0.0323, +0.0366] | 0.2703 | +13.56 % ! [+0.0302, +0.0341] | 49.8 % | 43.2 / 73.6 / 86.0 | 0.354 |
| 15 min | ridge | 6.3552 | +0.02 % ! [+0.0001, +0.0023] | 0.2501 | +0.02 % [-0.0000, +0.0001] | 0.2381 | +0.02 % [-0.0000, +0.0001] | 50.3 % | 47.6 / 78.6 / 89.5 | 0.008 |

## Per block (CRPS against zero)

| Block | Sessions | Trained on | ema_damped 5 / 15 min | ridge 5 / 15 min | ridge's out-of-sample correlation 5 / 15 min |
|---|---|---|---|---|---|
| check 1 (2026-03-03 to 2026-04-14) | 30 | 153 | +4.76 % ! / +13.72 % ! | -0.04 % * / -0.01 % | +0.008 / +0.017 |
| check 2 (2026-04-15 to 2026-05-27) | 30 | 183 | +4.42 % ! / +12.62 % ! | +0.07 % ! / +0.06 % ! | -0.018 / -0.024 |
| check 3 (2026-05-28 to 2026-07-10) | 30 | 213 | +4.03 % ! / +11.05 % ! | -0.00 % / -0.00 % | +0.010 / +0.010 |
| holdout 1 (2026-07-13 to 2026-08-21) | 30 | 243 | +3.80 % ! / +11.63 % ! | +0.02 % / +0.00 % | +0.015 / +0.011 |
| holdout 2 (2026-08-24 to 2026-10-05) | 30 | 273 | +3.75 % ! / +10.83 % ! | +0.00 % / +0.07 % | +0.012 / -0.010 |

## The ridge as fitted per block

Penalty chosen on the last 20 % of each block's training sessions; 'gain' is the validation MSE's improvement over predicting zero. Coefficients on standardised inputs (|coef| >= 0.002).

- check 1, 5 min: alpha 10000, gain 0.043 %; NQ.dist_ema14 -0.0037, NQ.ret5 -0.0189, NQ.ret15 +0.0047, NQ.vwap_dist +0.0090, NQ.rv15 +0.0030, phase:pre_open -0.0057, phase:midday -0.0035, phase:afternoon -0.0027, phase:after_close +0.0037
- check 1, 15 min: alpha 316228, gain 0.003 %; none
- check 2, 5 min: alpha 10000, gain 0.069 %; NQ.dist_ema14 -0.0070, NQ.ret5 -0.0198, NQ.ret15 +0.0027, NQ.vwap_dist +0.0093, phase:pre_open -0.0036, phase:opening_hour -0.0021, phase:midday -0.0035, phase:afternoon -0.0031, phase:after_close +0.0036
- check 2, 15 min: alpha 31623, gain 0.024 %; NQ.ema14_slope -0.0023, NQ.dist_ema14 -0.0027, NQ.ret1 +0.0028, NQ.ret5 -0.0082, NQ.vwap_dist +0.0090, NQ.vol60 +0.0037, phase:pre_open -0.0038, phase:midday -0.0045, phase:afternoon -0.0034, phase:after_close +0.0050
- check 3, 5 min: alpha 100000, gain 0.017 %; NQ.dist_ema14 -0.0040, NQ.ret5 -0.0067, NQ.vwap_dist +0.0020
- check 3, 15 min: alpha 316228, gain 0.005 %; none
- holdout 1, 5 min: alpha 100000, gain 0.020 %; NQ.dist_ema14 -0.0040, NQ.ret5 -0.0073, NQ.vwap_dist +0.0024
- holdout 1, 15 min: alpha 316228, gain 0.007 %; NQ.ret5 -0.0023
- holdout 2, 5 min: alpha 10000, gain 0.061 %; NQ.dist_ema14 -0.0053, NQ.ret1 +0.0029, NQ.ret5 -0.0191, NQ.vwap_dist +0.0078, NQ.rv60 +0.0020, phase:pre_open -0.0048, phase:afternoon -0.0048
- holdout 2, 15 min: alpha 10000, gain 0.044 %; NQ.ema14_slope -0.0048, NQ.dist_ema100 +0.0029, NQ.ret5 -0.0141, NQ.vwap_dist +0.0121, NQ.vol60 +0.0046, NQ.rv60 +0.0023, phase:pre_open -0.0067, phase:afternoon -0.0074, phase:after_close +0.0025

## Reliability of P(up), pooled

| Horizon | Arm | P(up) bin: forecast -> observed (n) |
|---|---|---|
| 5 min | ema_damped | 0.04 -> 0.460 (63); 0.17 -> 0.534 (251); 0.27 -> 0.523 (5,247); 0.36 -> 0.508 (31,843); 0.45 -> 0.503 (61,872); 0.55 -> 0.503 (61,157); 0.64 -> 0.504 (33,545); 0.74 -> 0.492 (8,818); 0.83 -> 0.488 (1,248); 0.95 -> 0.455 (275) |
| 5 min | ridge | 0.49 -> 0.502 (102,131); 0.51 -> 0.506 (102,188) |
| 15 min | ema_damped | 0.07 -> 0.536 (896); 0.16 -> 0.517 (6,689); 0.26 -> 0.516 (19,923); 0.35 -> 0.511 (32,316); 0.45 -> 0.508 (39,127); 0.55 -> 0.504 (38,753); 0.65 -> 0.512 (32,676); 0.74 -> 0.504 (21,399); 0.84 -> 0.509 (9,549); 0.94 -> 0.465 (2,497) |
| 15 min | ridge | 0.50 -> 0.505 (102,928); 0.50 -> 0.512 (100,897) |
