# Checks: lin_pois_ivx against fan_rw_v2 (fan_intermarket_v2, NQ)

Experiment hash `5adc22ef9b82116d`; baseline fan_rw_v2 `cf1c3910d23b9b0f`; code 70ef93ebe654+dirty.

**Development, not the verdict.** The manifest's three checks: per check the candidate is trained on the development sessions before it only (rows every 5 minutes, plus the pre-open origin), then it and the baseline are scored on every origin of the check's sessions. CRPS of the log price in basis points (K = 200) on identical origins; candidate minus baseline per session, 95 % moving-block bootstrap (5-session blocks, 2000 resamples). Negative is better; ✓ marks a whole interval below zero, ✗ above.

**Features** (16 columns, hash `61b6a1e0c67b00c2`): `base.log_sigma`, `base.release_ahead`, `base.minute`, `base.weekday`, `NQ.rv5`, `NQ.rv15`, `NQ.rv60`, `NQ.rv240`, `NQ.ret15`, `NQ.ret60`, `NQ.chg`, `NQ.vol60`, `NQ.age`, `NQ.day_rv`, `NQ.rv5d`, `VXN.level`.

| Check | Trained on | Training rows | Checked | Scored | Multiplier at the primary horizon (5 / 50 / 95 %) |
|---|---|---|---|---|---|
| 1 | 2025-07-21 to 2026-03-02 (153) | 323,281 | 2026-03-03 to 2026-04-14 | 30 of 30 | 0.74 / 0.95 / 1.23 |
| 2 | 2025-07-21 to 2026-04-14 (183) | 386,671 | 2026-04-15 to 2026-05-27 | 30 of 30 | 0.77 / 1.00 / 1.28 |
| 3 | 2025-07-21 to 2026-05-27 (213) | 450,061 | 2026-05-28 to 2026-07-10 | 30 of 30 | 0.72 / 1.00 / 1.34 |

## By horizon, the checks pooled

| Horizon | Role | Sessions | Origins | Baseline CRPS | Candidate CRPS | Difference | Share | 95 % interval | Verdict |
|---|---|---|---|---|---|---|---|---|---|
| 1 min | exploratory | 90 | 124,110 | 1.7384 | 1.7320 | -0.00640 | -0.37 % | [-0.00923, -0.00438] | better |
| 5 min | secondary | 90 | 123,750 | 3.9096 | 3.8936 | -0.01600 | -0.41 % | [-0.02304, -0.00982] | better |
| 15 min | primary | 90 | 122,850 | 6.8020 | 6.7767 | -0.02528 | -0.37 % | [-0.04232, -0.01174] | better |
| 20 min | exploratory | 90 | 122,400 | 7.8530 | 7.8254 | -0.02764 | -0.35 % | [-0.05151, -0.00984] | better |
| 30 min | secondary | 90 | 121,500 | 9.6849 | 9.6484 | -0.03654 | -0.38 % | [-0.07276, -0.00678] | better |
| 60 min | secondary | 90 | 118,800 | 14.0617 | 13.9896 | -0.07206 | -0.51 % | [-0.14281, -0.00967] | better |
| 120 min | exploratory | 90 | 113,400 | 20.4402 | 20.3355 | -0.10470 | -0.51 % | [-0.23811, +0.00509] | inconclusive |
| 240 min | exploratory | 90 | 102,600 | 29.3740 | 29.1994 | -0.17464 | -0.59 % | [-0.37847, +0.02868] | inconclusive |
| pre-open, 09:29 + 16 min | secondary | 90 | 90 | 18.0857 | 18.1836 | 0.09790 | +0.54 % | [-0.11219, +0.29084] | inconclusive |
| pre-open, 09:29 + 31 min | secondary | 90 | 90 | 27.3747 | 27.4729 | 0.09822 | +0.36 % | [-0.14026, +0.29223] | inconclusive |
| pre-open, 09:29 + 61 min | secondary | 90 | 90 | 30.0667 | 30.1910 | 0.12427 | +0.41 % | [-0.18126, +0.44131] | inconclusive |

## Per check (share of the baseline's CRPS)

| Horizon | Check 1 | Check 2 | Check 3 |
|---|---|---|---|
| 1 min | -0.31 % ✓ | -0.39 % ✓ | -0.41 % ✓ |
| 5 min | -0.13 % ✓ | -0.49 % ✓ | -0.61 % ✓ |
| 15 min | -0.03 % | -0.37 % ✓ | -0.68 % ✓ |
| 20 min | +0.02 % | -0.33 % ✓ | -0.71 % ✓ |
| 30 min | +0.09 % | -0.28 % ✓ | -0.87 % ✓ |
| 60 min | +0.11 % | -0.28 % | -1.24 % ✓ |
| 120 min | +0.24 % | -0.33 % | -1.34 % ✓ |
| 240 min | -0.14 % | -0.06 % | -1.37 % ✓ |
| pre-open, 09:29 + 16 min | +1.55 % | -0.81 % | +1.04 % |
| pre-open, 09:29 + 31 min | +1.52 % ✗ | +0.03 % | -0.32 % |
| pre-open, 09:29 + 61 min | +1.64 % ✗ | -0.40 % | -0.06 % |

## By origin phase (ET), the checks pooled

| Phase | 1 min | 5 min | 15 min | 20 min | 30 min | 60 min | 120 min | 240 min |
|---|---|---|---|---|---|---|---|---|
| overnight 18:00-08:00 | -0.49 % ✓ | -0.45 % ✓ | -0.51 % ✓ | -0.50 % ✓ | -0.50 % ✓ | -0.63 % ✓ | -0.74 % ✓ | -0.82 % ✓ |
| pre open 08:00-09:30 | -0.22 % ✓ | -0.13 % | +0.22 % | +0.28 % | +0.37 % | +0.70 % | +1.63 % ✗ | +0.97 % |
| opening hour 09:30-10:30 | -0.10 % | -0.27 % ✓ | +0.09 % | +0.12 % | +0.14 % | -0.41 % | -0.14 % | -0.60 % |
| midday 10:30-14:00 | -0.53 % ✓ | -0.54 % ✓ | -0.50 % ✓ | -0.52 % | -0.62 % | -0.85 % | -1.08 % | -0.95 % |
| afternoon 14:00-16:00 | -0.38 % ✓ | -0.40 % ✓ | -0.60 % | -0.55 % | -0.56 % | -0.58 % | -1.34 % | - |
| after close 16:00-18:00 | +1.34 % ✗ | +0.01 % | +0.62 % | +0.84 % | +0.49 % | - | - | - |
| a release ahead | -1.33 % | +1.79 % ✗ | +0.15 % | -0.02 % | -0.35 % | -0.33 % | -0.48 % | -1.13 % |

## How concentrated the primary horizon's gain is

Over 90 check sessions the candidate's CRPS was lower than the baseline's in **76 %** of sessions (mean difference -0.02528 bps). Its five best sessions carry 50 % of the total gain; its five worst add +0.4498 bps. With each of the 19 calendar weeks left out in turn the mean difference stays between -0.02866 and -0.01986 bps (0 week(s) whose removal turns it to no gain). A sensitivity check: difficult days stay in the score.

## Calibration, the checks pooled

Where each realised move fell in its fan. Coverage of the central 50 / 80 / 90 / 95 % bands (ideal: the band's own share), the 90 % band's misses below and above (ideal 5.0 % each), its mean width and its mean interval score (width plus 2 / alpha times the miss; lower is better) in basis points - baseline -> candidate.

| Horizon | Origins | Coverage 50 / 80 / 90 / 95 % | Below 90 % band | Above 90 % band | 90 % width (bps) | 90 % interval score (bps) |
|---|---|---|---|---|---|---|
| 1 min | 124,110 | 50.3 / 80.3 / 90.2 / 95.2 -> 49.0 / 79.5 / 89.9 / 95.0 | 5.0 -> 5.1 | 4.8 -> 5.0 | 10.07 -> 10.06 | 14.34 -> 14.12 |
| 5 min | 123,750 | 49.5 / 80.0 / 90.1 / 95.1 -> 48.6 / 79.7 / 90.2 / 95.2 | 5.1 -> 5.1 | 4.8 -> 4.7 | 22.31 -> 22.42 | 31.84 -> 31.30 |
| 15 min | 122,850 | 48.9 / 79.6 / 89.9 / 95.0 -> 47.9 / 79.4 / 90.1 / 95.3 | 5.4 -> 5.2 | 4.8 -> 4.7 | 38.79 -> 38.96 | 55.53 -> 54.52 |
| 20 min | 122,400 | 49.2 / 79.6 / 90.0 / 95.1 -> 48.1 / 79.2 / 90.0 / 95.4 | 5.3 -> 5.2 | 4.7 -> 4.8 | 44.90 -> 45.05 | 64.34 -> 63.19 |
| 30 min | 121,500 | 48.9 / 79.6 / 90.0 / 95.1 -> 48.0 / 79.4 / 90.1 / 95.3 | 5.4 -> 5.3 | 4.7 -> 4.7 | 55.40 -> 55.71 | 79.66 -> 78.45 |
| 60 min | 118,800 | 49.0 / 79.5 / 89.9 / 95.1 -> 47.6 / 78.9 / 90.0 / 95.3 | 5.5 -> 5.4 | 4.6 -> 4.5 | 80.83 -> 80.79 | 115.80 -> 113.07 |
| 120 min | 113,400 | 49.7 / 79.9 / 90.2 / 95.3 -> 47.9 / 79.1 / 90.0 / 95.4 | 5.5 -> 5.7 | 4.3 -> 4.3 | 121.66 -> 121.01 | 169.59 -> 165.27 |
| 240 min | 102,600 | 49.7 / 81.5 / 91.7 / 95.8 -> 46.9 / 79.1 / 90.3 / 95.3 | 4.7 -> 5.3 | 3.6 -> 4.4 | 181.58 -> 173.27 | 244.32 -> 235.75 |

By origin phase, 15 minutes ahead:

| Phase | Origins | Coverage 50 / 80 / 90 / 95 % | Below 90 % band | Above 90 % band | 90 % width (bps) | 90 % interval score (bps) |
|---|---|---|---|---|---|---|
| overnight 18:00-08:00 | 75,600 | 49.2 / 79.9 / 90.1 / 95.1 -> 47.7 / 79.6 / 90.3 / 95.4 | 5.3 -> 5.0 | 4.7 -> 4.6 | 30.28 -> 30.05 | 43.67 -> 42.67 |
| pre open 08:00-09:30 | 8,100 | 47.4 / 78.6 / 89.7 / 95.3 -> 50.4 / 81.1 / 91.5 / 96.2 | 5.3 -> 4.4 | 5.0 -> 4.1 | 48.54 -> 52.40 | 63.50 -> 64.76 |
| opening hour 09:30-10:30 | 5,400 | 46.8 / 79.1 / 90.5 / 95.7 -> 44.6 / 77.3 / 88.9 / 94.8 | 5.6 -> 6.2 | 3.9 -> 4.9 | 88.99 -> 87.17 | 118.78 -> 118.96 |
| midday 10:30-14:00 | 18,900 | 48.2 / 79.4 / 89.8 / 95.2 -> 46.6 / 78.6 / 89.5 / 95.2 | 5.5 -> 5.8 | 4.7 -> 4.7 | 53.05 -> 52.99 | 74.60 -> 72.49 |
| afternoon 14:00-16:00 | 10,800 | 47.5 / 77.8 / 88.3 / 94.0 -> 46.0 / 76.7 / 88.0 / 94.0 | 6.2 -> 6.1 | 5.6 -> 6.0 | 44.81 -> 44.76 | 69.78 -> 67.83 |
| after close 16:00-18:00 | 4,050 | 57.0 / 83.1 / 89.9 / 93.6 -> 61.3 / 85.7 / 92.3 / 95.4 | 4.6 -> 3.2 | 5.5 -> 4.5 | 28.71 -> 33.24 | 49.78 -> 49.83 |

PIT deciles, 15 minutes ahead (ideal 10.0 each; a U shape means too narrow, a hump too wide):

| | 0-10 | 10-20 | 20-30 | 30-40 | 40-50 | 50-60 | 60-70 | 70-80 | 80-90 | 90-100 |
|---|---|---|---|---|---|---|---|---|---|---|
| baseline | 10.2 | 9.8 | 9.7 | 9.5 | 9.5 | 9.8 | 10.0 | 10.6 | 10.7 | 10.2 |
| candidate | 10.3 | 10.1 | 9.7 | 9.4 | 9.3 | 9.6 | 9.8 | 10.5 | 11.1 | 10.3 |
