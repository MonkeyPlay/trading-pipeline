# Checks: crps_scale against fan_rw_v2 (fan_intermarket_v2, NQ)

Experiment hash `5adc22ef9b82116d`; baseline fan_rw_v2 `cf1c3910d23b9b0f`; code 70ef93ebe654+dirty.

**Development, not the verdict.** The manifest's three checks: per check the candidate is trained on the development sessions before it only (rows every 5 minutes, plus the pre-open origin), then it and the baseline are scored on every origin of the check's sessions. CRPS of the log price in basis points (K = 200) on identical origins; candidate minus baseline per session, 95 % moving-block bootstrap (5-session blocks, 2000 resamples). Negative is better; ✓ marks a whole interval below zero, ✗ above.

| Check | Trained on | Training rows | Checked | Scored | Multiplier at the primary horizon (5 / 50 / 95 %) |
|---|---|---|---|---|---|
| 1 | 2025-07-21 to 2026-03-02 (153) | 323,281 | 2026-03-03 to 2026-04-14 | 30 of 30 | 1.02 / 1.02 / 1.02 |
| 2 | 2025-07-21 to 2026-04-14 (183) | 386,671 | 2026-04-15 to 2026-05-27 | 30 of 30 | 1.01 / 1.01 / 1.01 |
| 3 | 2025-07-21 to 2026-05-27 (213) | 450,061 | 2026-05-28 to 2026-07-10 | 30 of 30 | 1.02 / 1.02 / 1.02 |

## By horizon, the checks pooled

| Horizon | Role | Sessions | Origins | Baseline CRPS | Candidate CRPS | Difference | Share | 95 % interval | Verdict |
|---|---|---|---|---|---|---|---|---|---|
| 1 min | exploratory | 90 | 124,110 | 1.7384 | 1.7392 | 0.00072 | +0.04 % | [-0.00062, +0.00223] | inconclusive |
| 5 min | secondary | 90 | 123,750 | 3.9096 | 3.9094 | -0.00022 | -0.01 % | [-0.00226, +0.00190] | inconclusive |
| 15 min | primary | 90 | 122,850 | 6.8020 | 6.8016 | -0.00032 | -0.00 % | [-0.00260, +0.00209] | inconclusive |
| 20 min | exploratory | 90 | 122,400 | 7.8530 | 7.8531 | 0.00012 | +0.00 % | [-0.00248, +0.00280] | inconclusive |
| 30 min | secondary | 90 | 121,500 | 9.6849 | 9.6854 | 0.00044 | +0.00 % | [-0.00219, +0.00325] | inconclusive |
| 60 min | secondary | 90 | 118,800 | 14.0617 | 14.0630 | 0.00128 | +0.01 % | [-0.00470, +0.00783] | inconclusive |
| 120 min | exploratory | 90 | 113,400 | 20.4402 | 20.4477 | 0.00751 | +0.04 % | [-0.00461, +0.02270] | inconclusive |
| 240 min | exploratory | 90 | 102,600 | 29.3740 | 29.3864 | 0.01239 | +0.04 % | [-0.00051, +0.03336] | inconclusive |
| pre-open, 09:29 + 16 min | secondary | 90 | 90 | 18.0857 | 18.1113 | 0.02555 | +0.14 % | [-0.01344, +0.05982] | inconclusive |
| pre-open, 09:29 + 31 min | secondary | 90 | 90 | 27.3747 | 27.3749 | 0.00023 | +0.00 % | [-0.03247, +0.02711] | inconclusive |
| pre-open, 09:29 + 61 min | secondary | 90 | 90 | 30.0667 | 30.1043 | 0.03760 | +0.13 % | [-0.00889, +0.08947] | inconclusive |

## Per check (share of the baseline's CRPS)

| Horizon | Check 1 | Check 2 | Check 3 |
|---|---|---|---|
| 1 min | +0.13 % ✗ | +0.01 % | -0.02 % |
| 5 min | +0.05 % | -0.01 % | -0.05 % |
| 15 min | +0.03 % | -0.02 % | -0.03 % |
| 20 min | +0.04 % | -0.01 % | -0.03 % |
| 30 min | +0.04 % ✗ | -0.01 % | -0.02 % |
| 60 min | +0.05 % | -0.01 % | -0.02 % |
| 120 min | +0.09 % | -0.01 % | +0.02 % |
| 240 min | +0.12 % ✗ | +0.00 % | -0.00 % |
| pre-open, 09:29 + 16 min | +0.83 % ✗ | -0.13 % | -0.06 % |
| pre-open, 09:29 + 31 min | +0.08 % | -0.00 % | -0.05 % |
| pre-open, 09:29 + 61 min | +0.31 % ✗ | -0.01 % | +0.06 % |

## By origin phase (ET), the checks pooled

| Phase | 1 min | 5 min | 15 min | 20 min | 30 min | 60 min | 120 min | 240 min |
|---|---|---|---|---|---|---|---|---|
| overnight 18:00-08:00 | +0.08 % | +0.02 % | -0.01 % | -0.00 % | -0.00 % | -0.01 % | +0.00 % | +0.02 % |
| pre open 08:00-09:30 | -0.00 % | -0.02 % | +0.01 % | +0.02 % | +0.03 % | +0.10 % | +0.20 % ✗ | +0.14 % ✗ |
| opening hour 09:30-10:30 | -0.01 % | -0.02 % | +0.01 % | +0.01 % | +0.01 % | +0.01 % | +0.05 % | +0.05 % |
| midday 10:30-14:00 | -0.04 % | -0.05 % ✓ | -0.01 % | +0.00 % | +0.02 % | +0.04 % | +0.05 % | +0.03 % |
| afternoon 14:00-16:00 | +0.04 % | -0.04 % | -0.04 % | -0.02 % | -0.02 % | -0.03 % | -0.05 % | - |
| after close 16:00-18:00 | +0.26 % ✗ | +0.14 % ✗ | +0.08 % | +0.07 % | +0.05 % | - | - | - |
| a release ahead | +0.54 % ✗ | +0.12 % | -0.00 % | +0.01 % | +0.06 % | +0.09 % | +0.08 % | +0.10 % |

## How concentrated the primary horizon's gain is

Over 90 check sessions the candidate's CRPS was lower than the baseline's in **50 %** of sessions (mean difference -0.00032 bps). Its five best sessions carry 316 % of the total gain; its five worst add +0.0847 bps. With each of the 19 calendar weeks left out in turn the mean difference stays between -0.00102 and +0.00029 bps (3 week(s) whose removal turns it to no gain). A sensitivity check: difficult days stay in the score.

## Calibration, the checks pooled

Where each realised move fell in its fan. Coverage of the central 50 / 80 / 90 / 95 % bands (ideal: the band's own share), the 90 % band's misses below and above (ideal 5.0 % each), its mean width and its mean interval score (width plus 2 / alpha times the miss; lower is better) in basis points - baseline -> candidate.

| Horizon | Origins | Coverage 50 / 80 / 90 / 95 % | Below 90 % band | Above 90 % band | 90 % width (bps) | 90 % interval score (bps) |
|---|---|---|---|---|---|---|
| 1 min | 124,110 | 50.3 / 80.3 / 90.2 / 95.2 -> 52.5 / 82.3 / 91.5 / 96.0 | 5.0 -> 4.3 | 4.8 -> 4.2 | 10.07 -> 10.62 | 14.34 -> 14.37 |
| 5 min | 123,750 | 49.5 / 80.0 / 90.1 / 95.1 -> 50.8 / 81.1 / 91.0 / 95.6 | 5.1 -> 4.7 | 4.8 -> 4.4 | 22.31 -> 23.04 | 31.84 -> 31.87 |
| 15 min | 122,850 | 48.9 / 79.6 / 89.9 / 95.0 -> 49.7 / 80.3 / 90.4 / 95.3 | 5.4 -> 5.1 | 4.8 -> 4.5 | 38.79 -> 39.54 | 55.53 -> 55.55 |
| 20 min | 122,400 | 49.2 / 79.6 / 90.0 / 95.1 -> 49.9 / 80.3 / 90.4 / 95.3 | 5.3 -> 5.1 | 4.7 -> 4.5 | 44.90 -> 45.71 | 64.34 -> 64.37 |
| 30 min | 121,500 | 48.9 / 79.6 / 90.0 / 95.1 -> 49.5 / 80.1 / 90.4 / 95.3 | 5.4 -> 5.2 | 4.7 -> 4.5 | 55.40 -> 56.16 | 79.66 -> 79.72 |
| 60 min | 118,800 | 49.0 / 79.5 / 89.9 / 95.1 -> 49.6 / 80.2 / 90.3 / 95.3 | 5.5 -> 5.3 | 4.6 -> 4.4 | 80.83 -> 82.23 | 115.80 -> 115.91 |
| 120 min | 113,400 | 49.7 / 79.9 / 90.2 / 95.3 -> 50.6 / 80.7 / 90.7 / 95.6 | 5.5 -> 5.2 | 4.3 -> 4.1 | 121.66 -> 124.27 | 169.59 -> 169.98 |
| 240 min | 102,600 | 49.7 / 81.5 / 91.7 / 95.8 -> 50.0 / 81.9 / 91.9 / 95.9 | 4.7 -> 4.6 | 3.6 -> 3.5 | 181.58 -> 183.32 | 244.32 -> 245.10 |

By origin phase, 15 minutes ahead:

| Phase | Origins | Coverage 50 / 80 / 90 / 95 % | Below 90 % band | Above 90 % band | 90 % width (bps) | 90 % interval score (bps) |
|---|---|---|---|---|---|---|
| overnight 18:00-08:00 | 75,600 | 49.2 / 79.9 / 90.1 / 95.1 -> 49.9 / 80.6 / 90.6 / 95.4 | 5.3 -> 5.0 | 4.7 -> 4.4 | 30.28 -> 30.87 | 43.67 -> 43.67 |
| pre open 08:00-09:30 | 8,100 | 47.4 / 78.6 / 89.7 / 95.3 -> 48.3 / 79.3 / 90.3 / 95.7 | 5.3 -> 5.0 | 5.0 -> 4.7 | 48.54 -> 49.48 | 63.50 -> 63.65 |
| opening hour 09:30-10:30 | 5,400 | 46.8 / 79.1 / 90.5 / 95.7 -> 47.6 / 80.0 / 91.0 / 96.0 | 5.6 -> 5.3 | 3.9 -> 3.7 | 88.99 -> 90.71 | 118.78 -> 119.08 |
| midday 10:30-14:00 | 18,900 | 48.2 / 79.4 / 89.8 / 95.2 -> 49.0 / 80.1 / 90.3 / 95.6 | 5.5 -> 5.3 | 4.7 -> 4.4 | 53.05 -> 54.07 | 74.60 -> 74.63 |
| afternoon 14:00-16:00 | 10,800 | 47.5 / 77.8 / 88.3 / 94.0 -> 48.3 / 78.5 / 89.0 / 94.3 | 6.2 -> 5.8 | 5.6 -> 5.3 | 44.81 -> 45.67 | 69.78 -> 69.66 |
| after close 16:00-18:00 | 4,050 | 57.0 / 83.1 / 89.9 / 93.6 -> 57.7 / 83.4 / 90.3 / 93.8 | 4.6 -> 4.3 | 5.5 -> 5.4 | 28.71 -> 29.25 | 49.78 -> 49.81 |

PIT deciles, 15 minutes ahead (ideal 10.0 each; a U shape means too narrow, a hump too wide):

| | 0-10 | 10-20 | 20-30 | 30-40 | 40-50 | 50-60 | 60-70 | 70-80 | 80-90 | 90-100 |
|---|---|---|---|---|---|---|---|---|---|---|
| baseline | 10.2 | 9.8 | 9.7 | 9.5 | 9.5 | 9.8 | 10.0 | 10.6 | 10.7 | 10.2 |
| candidate | 9.9 | 9.8 | 9.8 | 9.6 | 9.7 | 10.0 | 10.2 | 10.7 | 10.7 | 9.8 |
