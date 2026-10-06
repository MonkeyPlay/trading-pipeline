# Holdout: fan_intermarket_v2_lin_pois_ivx_frozen against fan_rw_v2 (fan_intermarket_v2, NQ)

Model `fan_intermarket_v2_lin_pois_ivx_frozen` (definition `cb831885906a169f`), frozen on 243 development sessions; experiment `5adc22ef9b82116d`; code dde9789c0dc3 (source snapshot `408e55643a9fe58a`); data `fa45688c0eba434c`.

**Rule** (fixed before any result): the whole 95 % interval of model minus baseline, NQ at 15 minutes over the holdout's sessions, lies below zero; the interval includes zero: the model is not promoted on this evidence; the whole interval lies above zero.

**Sessions:** 60 of 60 scored. CRPS of the log price in basis points (K = 200) on identical origins; the model minus the baseline per session, 95 % moving-block bootstrap (5-session blocks, 2000 resamples).

## Verdict at the primary horizon (15 minutes): **pass**

| Horizon | Role | Sessions | Origins | Baseline CRPS | Model CRPS | Difference | Share | 95 % interval | Verdict |
|---|---|---|---|---|---|---|---|---|---|
| 1 min | exploratory | 60 | 82,739 | 1.4542 | 1.4547 | 0.00053 | +0.04 % | [-0.00065, +0.00157] | inconclusive |
| 5 min | secondary | 60 | 82,499 | 3.2820 | 3.2778 | -0.00419 | -0.13 % | [-0.00699, -0.00120] | better |
| 15 min | primary | 60 | 81,899 | 5.7284 | 5.7202 | -0.00821 | -0.14 % | [-0.01619, -0.00003] | better |
| 20 min | exploratory | 60 | 81,599 | 6.6488 | 6.6385 | -0.01034 | -0.16 % | [-0.02102, +0.00069] | inconclusive |
| 30 min | secondary | 60 | 80,999 | 8.2493 | 8.2354 | -0.01395 | -0.17 % | [-0.03274, +0.00397] | inconclusive |
| 60 min | secondary | 60 | 79,199 | 11.8621 | 11.8296 | -0.03248 | -0.27 % | [-0.08110, +0.01172] | inconclusive |
| 120 min | exploratory | 60 | 75,599 | 16.9490 | 16.9377 | -0.01129 | -0.07 % | [-0.09152, +0.06775] | inconclusive |
| 240 min | exploratory | 60 | 68,399 | 24.3601 | 24.4453 | 0.08519 | +0.35 % | [-0.10356, +0.27708] | inconclusive |
| pre-open, 09:29 + 16 min | secondary | 60 | 60 | 20.3069 | 20.2863 | -0.02059 | -0.10 % | [-0.24516, +0.28931] | inconclusive |
| pre-open, 09:29 + 31 min | secondary | 60 | 60 | 25.5178 | 25.4769 | -0.04094 | -0.16 % | [-0.48887, +0.38531] | inconclusive |
| pre-open, 09:29 + 61 min | secondary | 60 | 60 | 33.1381 | 33.0300 | -0.10811 | -0.33 % | [-0.54272, +0.33371] | inconclusive |

## By origin phase (ET)

| Phase | 1 min | 5 min | 15 min | 20 min | 30 min | 60 min | 120 min | 240 min |
|---|---|---|---|---|---|---|---|---|
| overnight 18:00-08:00 | -0.11 % | -0.23 % ✓ | -0.24 % ✓ | -0.24 % ✓ | -0.29 % ✓ | -0.47 % ✓ | -0.28 % | -0.05 % |
| pre open 08:00-09:30 | +0.19 % ✗ | +0.12 % | -0.24 % | -0.32 % | -0.40 % | -0.44 % | +0.29 % | +1.61 % ✗ |
| opening hour 09:30-10:30 | +0.10 % | -0.06 % | +0.10 % | +0.01 % | +0.04 % | -0.00 % | +0.35 % | +1.28 % |
| midday 10:30-14:00 | -0.06 % | -0.19 % ✓ | -0.18 % | -0.18 % | -0.16 % | -0.12 % | -0.17 % | +0.76 % |
| afternoon 14:00-16:00 | -0.06 % | -0.24 % | -0.13 % | -0.04 % | +0.08 % | +0.48 % | +1.03 % | - |
| after close 16:00-18:00 | +2.96 % ✗ | +1.75 % ✗ | +1.80 % | +2.04 % | +2.97 % ✗ | - | - | - |

## How concentrated the primary horizon's gain is

Over 60 check sessions the candidate's CRPS was lower than the baseline's in **62 %** of sessions (mean difference -0.00821 bps). Its five best sessions carry 69 % of the total gain; its five worst add +0.2355 bps. With each of the 13 calendar weeks left out in turn the mean difference stays between -0.01019 and -0.00625 bps (0 week(s) whose removal turns it to no gain). A sensitivity check: difficult days stay in the score.

## Calibration, the checks pooled

Where each realised move fell in its fan. Coverage of the central 50 / 80 / 90 / 95 % bands (ideal: the band's own share), the 90 % band's misses below and above (ideal 5.0 % each), its mean width and its mean interval score (width plus 2 / alpha times the miss; lower is better) in basis points - baseline -> candidate.

| Horizon | Origins | Coverage 50 / 80 / 90 / 95 % | Below 90 % band | Above 90 % band | 90 % width (bps) | 90 % interval score (bps) |
|---|---|---|---|---|---|---|
| 1 min | 82,739 | 50.1 / 79.2 / 89.5 / 94.6 -> 48.1 / 77.4 / 88.2 / 93.8 | 5.2 -> 5.9 | 5.3 -> 5.9 | 8.37 -> 8.17 | 11.79 -> 11.84 |
| 5 min | 82,499 | 49.0 / 78.7 / 89.2 / 94.7 -> 47.7 / 77.5 / 88.3 / 94.1 | 5.3 -> 5.8 | 5.5 -> 5.9 | 18.66 -> 18.35 | 25.63 -> 25.48 |
| 15 min | 81,899 | 48.6 / 78.8 / 89.6 / 95.0 -> 47.1 / 77.6 / 88.7 / 94.6 | 5.2 -> 5.6 | 5.2 -> 5.7 | 32.51 -> 31.91 | 44.17 -> 43.82 |
| 20 min | 81,599 | 48.4 / 78.8 / 89.5 / 95.0 -> 46.8 / 77.5 / 88.7 / 94.5 | 5.3 -> 5.6 | 5.2 -> 5.7 | 37.68 -> 36.92 | 51.45 -> 51.08 |
| 30 min | 80,999 | 47.8 / 78.8 / 89.5 / 95.0 -> 46.2 / 77.4 / 88.5 / 94.5 | 5.3 -> 5.7 | 5.2 -> 5.7 | 46.32 -> 45.33 | 63.91 -> 63.36 |
| 60 min | 79,199 | 48.6 / 79.2 / 89.4 / 95.0 -> 46.7 / 77.8 / 88.8 / 94.4 | 5.2 -> 5.5 | 5.3 -> 5.7 | 67.83 -> 66.17 | 92.79 -> 91.84 |
| 120 min | 75,599 | 48.4 / 79.2 / 90.0 / 95.4 -> 45.8 / 77.5 / 88.5 / 94.2 | 5.0 -> 5.8 | 4.9 -> 5.7 | 99.79 -> 96.14 | 130.40 -> 131.69 |
| 240 min | 68,399 | 47.8 / 78.7 / 89.5 / 95.2 -> 44.4 / 76.0 / 87.8 / 93.9 | 5.5 -> 6.5 | 4.9 -> 5.7 | 143.09 -> 135.21 | 186.67 -> 190.93 |

By origin phase, 15 minutes ahead:

| Phase | Origins | Coverage 50 / 80 / 90 / 95 % | Below 90 % band | Above 90 % band | 90 % width (bps) | 90 % interval score (bps) |
|---|---|---|---|---|---|---|
| overnight 18:00-08:00 | 50,399 | 48.9 / 78.5 / 89.4 / 94.9 -> 47.0 / 77.0 / 88.2 / 94.5 | 5.3 -> 5.8 | 5.3 -> 5.9 | 26.56 -> 25.75 | 35.97 -> 35.56 |
| pre open 08:00-09:30 | 5,400 | 44.2 / 76.0 / 88.3 / 94.6 -> 45.3 / 77.6 / 89.4 / 95.2 | 5.7 -> 5.0 | 5.9 -> 5.6 | 41.85 -> 43.33 | 57.78 -> 57.33 |
| opening hour 09:30-10:30 | 3,600 | 46.9 / 79.4 / 90.3 / 95.7 -> 45.0 / 77.5 / 88.9 / 95.0 | 5.8 -> 6.4 | 3.9 -> 4.8 | 76.48 -> 74.10 | 98.79 -> 98.93 |
| midday 10:30-14:00 | 12,600 | 48.9 / 79.4 / 90.4 / 95.2 -> 46.8 / 77.6 / 89.0 / 94.4 | 4.4 -> 5.0 | 5.3 -> 5.9 | 41.06 -> 39.75 | 55.47 -> 54.93 |
| afternoon 14:00-16:00 | 7,200 | 49.5 / 80.8 / 90.5 / 94.9 -> 47.5 / 78.8 / 89.4 / 94.6 | 5.1 -> 5.4 | 4.4 -> 5.1 | 34.26 -> 34.09 | 50.13 -> 49.43 |
| after close 16:00-18:00 | 2,700 | 51.3 / 80.2 / 90.5 / 95.5 -> 56.0 / 84.3 / 93.8 / 96.8 | 4.9 -> 3.5 | 4.6 -> 2.7 | 21.57 -> 25.38 | 28.55 -> 30.66 |

PIT deciles, 15 minutes ahead (ideal 10.0 each; a U shape means too narrow, a hump too wide):

| | 0-10 | 10-20 | 20-30 | 30-40 | 40-50 | 50-60 | 60-70 | 70-80 | 80-90 | 90-100 |
|---|---|---|---|---|---|---|---|---|---|---|
| baseline | 10.6 | 10.0 | 9.7 | 9.5 | 9.3 | 10.0 | 9.9 | 9.9 | 10.4 | 10.6 |
| candidate | 11.1 | 10.3 | 9.7 | 9.2 | 9.0 | 9.6 | 9.6 | 9.7 | 10.4 | 11.4 |
