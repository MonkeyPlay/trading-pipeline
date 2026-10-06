# Checks: phase_scale against fan_rw_v2 (fan_intermarket_v2, NQ)

Experiment hash `5adc22ef9b82116d`; baseline fan_rw_v2 `cf1c3910d23b9b0f`; code 70ef93ebe654+dirty.

**Development, not the verdict.** The manifest's three checks: per check the candidate is trained on the development sessions before it only (rows every 5 minutes, plus the pre-open origin), then it and the baseline are scored on every origin of the check's sessions. CRPS of the log price in basis points (K = 200) on identical origins; candidate minus baseline per session, 95 % moving-block bootstrap (5-session blocks, 2000 resamples). Negative is better; ✓ marks a whole interval below zero, ✗ above.

| Check | Trained on | Training rows | Checked | Scored | Multiplier at the primary horizon (5 / 50 / 95 %) |
|---|---|---|---|---|---|
| 1 | 2025-07-21 to 2026-03-02 (153) | 323,281 | 2026-03-03 to 2026-04-14 | 30 of 30 | 0.96 / 0.99 / 1.09 |
| 2 | 2025-07-21 to 2026-04-14 (183) | 386,671 | 2026-04-15 to 2026-05-27 | 30 of 30 | 0.96 / 0.99 / 1.06 |
| 3 | 2025-07-21 to 2026-05-27 (213) | 450,061 | 2026-05-28 to 2026-07-10 | 30 of 30 | 0.98 / 1.00 / 1.08 |

## By horizon, the checks pooled

| Horizon | Role | Sessions | Origins | Baseline CRPS | Candidate CRPS | Difference | Share | 95 % interval | Verdict |
|---|---|---|---|---|---|---|---|---|---|
| 1 min | exploratory | 90 | 124,110 | 1.7384 | 1.7387 | 0.00031 | +0.02 % | [-0.00124, +0.00145] | inconclusive |
| 5 min | secondary | 90 | 123,750 | 3.9096 | 3.9094 | -0.00016 | -0.00 % | [-0.00271, +0.00190] | inconclusive |
| 15 min | primary | 90 | 122,850 | 6.8020 | 6.8038 | 0.00180 | +0.03 % | [-0.00127, +0.00444] | inconclusive |
| 20 min | exploratory | 90 | 122,400 | 7.8530 | 7.8552 | 0.00222 | +0.03 % | [-0.00119, +0.00587] | inconclusive |
| 30 min | secondary | 90 | 121,500 | 9.6849 | 9.6871 | 0.00218 | +0.02 % | [-0.00228, +0.00697] | inconclusive |
| 60 min | secondary | 90 | 118,800 | 14.0617 | 14.0781 | 0.01640 | +0.12 % | [+0.00027, +0.03259] | worse |
| 120 min | exploratory | 90 | 113,400 | 20.4402 | 20.5001 | 0.05989 | +0.29 % | [+0.01900, +0.09889] | worse |
| 240 min | exploratory | 90 | 102,600 | 29.3740 | 29.4594 | 0.08539 | +0.29 % | [+0.02658, +0.15446] | worse |
| pre-open, 09:29 + 16 min | secondary | 90 | 90 | 18.0857 | 18.3635 | 0.27778 | +1.54 % | [+0.01549, +0.52732] | worse |
| pre-open, 09:29 + 31 min | secondary | 90 | 90 | 27.3747 | 27.7696 | 0.39497 | +1.44 % | [-0.20502, +0.91595] | inconclusive |
| pre-open, 09:29 + 61 min | secondary | 90 | 90 | 30.0667 | 31.2842 | 1.21744 | +4.05 % | [+0.41612, +2.02055] | worse |

## Per check (share of the baseline's CRPS)

| Horizon | Check 1 | Check 2 | Check 3 |
|---|---|---|---|
| 1 min | +0.14 % ✗ | -0.03 % | -0.06 % |
| 5 min | +0.10 % ✗ | -0.05 % | -0.07 % |
| 15 min | +0.10 % ✗ | -0.01 % | -0.01 % |
| 20 min | +0.10 % ✗ | -0.01 % | -0.02 % |
| 30 min | +0.11 % ✗ | -0.02 % | -0.03 % |
| 60 min | +0.33 % ✗ | -0.01 % | +0.01 % |
| 120 min | +0.60 % ✗ | -0.02 % | +0.22 % ✗ |
| 240 min | +0.55 % ✗ | +0.10 % | +0.17 % |
| pre-open, 09:29 + 16 min | +6.94 % ✗ | -0.40 % | -0.16 % |
| pre-open, 09:29 + 31 min | +4.07 % | +1.17 % | -0.36 % |
| pre-open, 09:29 + 61 min | +7.48 % ✗ | +1.21 % | +3.13 % |

## By origin phase (ET), the checks pooled

| Phase | 1 min | 5 min | 15 min | 20 min | 30 min | 60 min | 120 min | 240 min |
|---|---|---|---|---|---|---|---|---|
| overnight 18:00-08:00 | +0.01 % | -0.00 % | +0.01 % | +0.00 % | -0.00 % | -0.01 % | +0.00 % | +0.11 % |
| pre open 08:00-09:30 | -0.01 % | +0.07 % | +0.12 % | +0.14 % | +0.21 % | +1.06 % ✗ | +2.24 % ✗ | +1.76 % ✗ |
| opening hour 09:30-10:30 | +0.14 % | +0.17 % | +0.10 % | +0.10 % | +0.03 % | +0.15 % | +0.19 % | +0.01 % |
| midday 10:30-14:00 | -0.02 % | -0.07 % | +0.00 % | -0.00 % | -0.00 % | -0.02 % | +0.03 % | -0.01 % |
| afternoon 14:00-16:00 | +0.05 % | -0.05 % ✓ | +0.08 % | +0.10 % ✗ | +0.04 % | +0.05 % | +0.08 % | - |
| after close 16:00-18:00 | -0.06 % | -0.19 % | -0.20 % | -0.20 % | -0.21 % | - | - | - |
| a release ahead | +0.62 % | +0.66 % ✗ | -0.08 % | -0.06 % | -0.03 % | +0.52 % | +0.85 % | +0.09 % |

## How concentrated the primary horizon's gain is

Over 90 check sessions the candidate's CRPS was lower than the baseline's in **38 %** of sessions (mean difference +0.00180 bps). There is no total gain; its five worst add +0.1214 bps. With each of the 19 calendar weeks left out in turn the mean difference stays between +0.00126 and +0.00289 bps. A sensitivity check: difficult days stay in the score.

## Calibration, the checks pooled

Where each realised move fell in its fan. Coverage of the central 50 / 80 / 90 / 95 % bands (ideal: the band's own share), the 90 % band's misses below and above (ideal 5.0 % each), its mean width and its mean interval score (width plus 2 / alpha times the miss; lower is better) in basis points - baseline -> candidate.

| Horizon | Origins | Coverage 50 / 80 / 90 / 95 % | Below 90 % band | Above 90 % band | 90 % width (bps) | 90 % interval score (bps) |
|---|---|---|---|---|---|---|
| 1 min | 124,110 | 50.3 / 80.3 / 90.2 / 95.2 -> 51.6 / 81.5 / 91.0 / 95.6 | 5.0 -> 4.6 | 4.8 -> 4.4 | 10.07 -> 10.53 | 14.34 -> 14.39 |
| 5 min | 123,750 | 49.5 / 80.0 / 90.1 / 95.1 -> 50.1 / 80.6 / 90.5 / 95.3 | 5.1 -> 4.9 | 4.8 -> 4.6 | 22.31 -> 22.98 | 31.84 -> 31.93 |
| 15 min | 122,850 | 48.9 / 79.6 / 89.9 / 95.0 -> 49.1 / 79.8 / 90.0 / 95.0 | 5.4 -> 5.3 | 4.8 -> 4.7 | 38.79 -> 39.33 | 55.53 -> 55.70 |
| 20 min | 122,400 | 49.2 / 79.6 / 90.0 / 95.1 -> 49.3 / 79.7 / 90.0 / 95.1 | 5.3 -> 5.3 | 4.7 -> 4.7 | 44.90 -> 45.33 | 64.34 -> 64.53 |
| 30 min | 121,500 | 48.9 / 79.6 / 90.0 / 95.1 -> 49.0 / 79.7 / 90.0 / 95.1 | 5.4 -> 5.3 | 4.7 -> 4.7 | 55.40 -> 55.81 | 79.66 -> 79.88 |
| 60 min | 118,800 | 49.0 / 79.5 / 89.9 / 95.1 -> 49.6 / 80.1 / 90.3 / 95.2 | 5.5 -> 5.3 | 4.6 -> 4.4 | 80.83 -> 82.92 | 115.80 -> 116.65 |
| 120 min | 113,400 | 49.7 / 79.9 / 90.2 / 95.3 -> 50.6 / 80.4 / 90.5 / 95.4 | 5.5 -> 5.3 | 4.3 -> 4.2 | 121.66 -> 125.21 | 169.59 -> 172.16 |
| 240 min | 102,600 | 49.7 / 81.5 / 91.7 / 95.8 -> 51.2 / 82.3 / 92.3 / 95.9 | 4.7 -> 4.5 | 3.6 -> 3.2 | 181.58 -> 188.21 | 244.32 -> 247.67 |

By origin phase, 15 minutes ahead:

| Phase | Origins | Coverage 50 / 80 / 90 / 95 % | Below 90 % band | Above 90 % band | 90 % width (bps) | 90 % interval score (bps) |
|---|---|---|---|---|---|---|
| overnight 18:00-08:00 | 75,600 | 49.2 / 79.9 / 90.1 / 95.1 -> 48.8 / 79.6 / 89.9 / 95.0 | 5.3 -> 5.4 | 4.7 -> 4.8 | 30.28 -> 30.04 | 43.67 -> 43.67 |
| pre open 08:00-09:30 | 8,100 | 47.4 / 78.6 / 89.7 / 95.3 -> 50.5 / 81.6 / 91.7 / 96.3 | 5.3 -> 4.3 | 5.0 -> 4.0 | 48.54 -> 52.37 | 63.50 -> 64.37 |
| opening hour 09:30-10:30 | 5,400 | 46.8 / 79.1 / 90.5 / 95.7 -> 49.6 / 82.0 / 92.2 / 96.5 | 5.6 -> 4.6 | 3.9 -> 3.1 | 88.99 -> 95.26 | 118.78 -> 120.24 |
| midday 10:30-14:00 | 18,900 | 48.2 / 79.4 / 89.8 / 95.2 -> 50.1 / 81.1 / 91.0 / 95.9 | 5.5 -> 5.0 | 4.7 -> 4.0 | 53.05 -> 55.39 | 74.60 -> 74.72 |
| afternoon 14:00-16:00 | 10,800 | 47.5 / 77.8 / 88.3 / 94.0 -> 46.2 / 76.3 / 87.3 / 93.4 | 6.2 -> 6.7 | 5.6 -> 6.0 | 44.81 -> 43.34 | 69.78 -> 70.03 |
| after close 16:00-18:00 | 4,050 | 57.0 / 83.1 / 89.9 / 93.6 -> 53.6 / 80.5 / 88.1 / 92.3 | 4.6 -> 5.8 | 5.5 -> 6.0 | 28.71 -> 26.28 | 49.78 -> 49.85 |

PIT deciles, 15 minutes ahead (ideal 10.0 each; a U shape means too narrow, a hump too wide):

| | 0-10 | 10-20 | 20-30 | 30-40 | 40-50 | 50-60 | 60-70 | 70-80 | 80-90 | 90-100 |
|---|---|---|---|---|---|---|---|---|---|---|
| baseline | 10.2 | 9.8 | 9.7 | 9.5 | 9.5 | 9.8 | 10.0 | 10.6 | 10.7 | 10.2 |
| candidate | 10.2 | 9.8 | 9.7 | 9.5 | 9.5 | 9.8 | 10.0 | 10.6 | 10.7 | 10.0 |
