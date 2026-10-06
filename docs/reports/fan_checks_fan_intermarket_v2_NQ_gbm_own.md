# Checks: gbm_own against fan_rw_v2 (fan_intermarket_v2, NQ)

Experiment hash `5adc22ef9b82116d`; baseline fan_rw_v2 `cf1c3910d23b9b0f`; code 70ef93ebe654+dirty.

**Development, not the verdict.** The manifest's three checks: per check the candidate is trained on the development sessions before it only (rows every 5 minutes, plus the pre-open origin), then it and the baseline are scored on every origin of the check's sessions. CRPS of the log price in basis points (K = 200) on identical origins; candidate minus baseline per session, 95 % moving-block bootstrap (5-session blocks, 2000 resamples). Negative is better; ✓ marks a whole interval below zero, ✗ above.

**Features** (14 columns, hash `ba348244f5221811`): `base.log_sigma`, `base.release_ahead`, `base.minute`, `base.weekday`, `NQ.rv5`, `NQ.rv15`, `NQ.rv60`, `NQ.rv240`, `NQ.ret15`, `NQ.ret60`, `NQ.chg`, `NQ.vol60`, `NQ.age`, `NQ.day_rv`.

| Check | Trained on | Training rows | Checked | Scored | Multiplier at the primary horizon (5 / 50 / 95 %) |
|---|---|---|---|---|---|
| 1 | 2025-07-21 to 2026-03-02 (153) | 323,281 | 2026-03-03 to 2026-04-14 | 30 of 30 | 0.79 / 0.95 / 1.20 |
| 2 | 2025-07-21 to 2026-04-14 (183) | 386,671 | 2026-04-15 to 2026-05-27 | 30 of 30 | 0.77 / 0.94 / 1.23 |
| 3 | 2025-07-21 to 2026-05-27 (213) | 450,061 | 2026-05-28 to 2026-07-10 | 30 of 30 | 0.82 / 0.96 / 1.20 |

## By horizon, the checks pooled

| Horizon | Role | Sessions | Origins | Baseline CRPS | Candidate CRPS | Difference | Share | 95 % interval | Verdict |
|---|---|---|---|---|---|---|---|---|---|
| 1 min | exploratory | 90 | 124,110 | 1.7384 | 1.7324 | -0.00608 | -0.35 % | [-0.00843, -0.00431] | better |
| 5 min | secondary | 90 | 123,750 | 3.9096 | 3.8965 | -0.01312 | -0.34 % | [-0.01802, -0.00876] | better |
| 15 min | primary | 90 | 122,850 | 6.8020 | 6.7858 | -0.01616 | -0.24 % | [-0.02708, -0.00632] | better |
| 20 min | exploratory | 90 | 122,400 | 7.8530 | 7.8357 | -0.01728 | -0.22 % | [-0.03268, -0.00466] | better |
| 30 min | secondary | 90 | 121,500 | 9.6849 | 9.6637 | -0.02118 | -0.22 % | [-0.04217, -0.00290] | better |
| 60 min | secondary | 90 | 118,800 | 14.0617 | 14.0298 | -0.03187 | -0.23 % | [-0.07010, +0.00523] | inconclusive |
| 120 min | exploratory | 90 | 113,400 | 20.4402 | 20.3658 | -0.07436 | -0.36 % | [-0.15924, +0.01160] | inconclusive |
| 240 min | exploratory | 90 | 102,600 | 29.3740 | 29.3183 | -0.05569 | -0.19 % | [-0.20182, +0.09704] | inconclusive |
| pre-open, 09:29 + 16 min | secondary | 90 | 90 | 18.0857 | 18.2730 | 0.18724 | +1.04 % | [+0.02611, +0.33645] | worse |
| pre-open, 09:29 + 31 min | secondary | 90 | 90 | 27.3747 | 27.5481 | 0.17338 | +0.63 % | [-0.00707, +0.33617] | inconclusive |
| pre-open, 09:29 + 61 min | secondary | 90 | 90 | 30.0667 | 30.1925 | 0.12576 | +0.42 % | [-0.18251, +0.53710] | inconclusive |

## Per check (share of the baseline's CRPS)

| Horizon | Check 1 | Check 2 | Check 3 |
|---|---|---|---|
| 1 min | -0.30 % ✓ | -0.37 % ✓ | -0.38 % ✓ |
| 5 min | -0.17 % ✓ | -0.39 % ✓ | -0.45 % ✓ |
| 15 min | -0.09 % | -0.12 % | -0.45 % ✓ |
| 20 min | -0.04 % | -0.09 % | -0.48 % ✓ |
| 30 min | -0.00 % | -0.01 % | -0.56 % ✓ |
| 60 min | +0.18 % | -0.09 % | -0.69 % ✓ |
| 120 min | +0.24 % | -0.36 % | -0.94 % ✓ |
| 240 min | +0.39 % | +0.14 % | -0.95 % ✓ |
| pre-open, 09:29 + 16 min | +1.49 % | +0.10 % | +1.53 % ✗ |
| pre-open, 09:29 + 31 min | +1.49 % ✗ | +0.10 % | +0.31 % |
| pre-open, 09:29 + 61 min | +2.04 % ✗ | -0.45 % | -0.37 % |

## By origin phase (ET), the checks pooled

| Phase | 1 min | 5 min | 15 min | 20 min | 30 min | 60 min | 120 min | 240 min |
|---|---|---|---|---|---|---|---|---|
| overnight 18:00-08:00 | -0.45 % ✓ | -0.39 % ✓ | -0.33 % ✓ | -0.33 % ✓ | -0.37 % ✓ | -0.37 % ✓ | -0.44 % | -0.25 % |
| pre open 08:00-09:30 | -0.23 % ✓ | -0.11 % | +0.23 % | +0.31 % ✗ | +0.33 % | +0.45 % | +0.56 % | +0.50 % |
| opening hour 09:30-10:30 | -0.05 % | -0.21 % ✓ | -0.01 % | +0.04 % | +0.09 % | -0.02 % | -0.21 % | -0.83 % |
| midday 10:30-14:00 | -0.39 % ✓ | -0.35 % ✓ | -0.25 % | -0.25 % | -0.23 % | -0.41 % | -0.70 % | -0.18 % |
| afternoon 14:00-16:00 | -0.23 % ✓ | -0.31 % ✓ | -0.37 % | -0.36 % | -0.29 % | -0.05 % | -0.62 % | - |
| after close 16:00-18:00 | -0.20 % | -0.41 % ✓ | -0.22 % | -0.12 % | +0.05 % | - | - | - |
| a release ahead | +0.77 % | +0.11 % | -0.00 % | -0.07 % | -0.34 % | -0.55 % | -0.63 % | -0.50 % |

## How concentrated the primary horizon's gain is

Over 90 check sessions the candidate's CRPS was lower than the baseline's in **64 %** of sessions (mean difference -0.01616 bps). Its five best sessions carry 46 % of the total gain; its five worst add +0.2282 bps. With each of the 19 calendar weeks left out in turn the mean difference stays between -0.01814 and -0.01239 bps (0 week(s) whose removal turns it to no gain). A sensitivity check: difficult days stay in the score.

## Calibration, the checks pooled

Where each realised move fell in its fan. Coverage of the central 50 / 80 / 90 / 95 % bands (ideal: the band's own share), the 90 % band's misses below and above (ideal 5.0 % each), its mean width and its mean interval score (width plus 2 / alpha times the miss; lower is better) in basis points - baseline -> candidate.

| Horizon | Origins | Coverage 50 / 80 / 90 / 95 % | Below 90 % band | Above 90 % band | 90 % width (bps) | 90 % interval score (bps) |
|---|---|---|---|---|---|---|
| 1 min | 124,110 | 50.3 / 80.3 / 90.2 / 95.2 -> 48.6 / 79.1 / 89.6 / 94.9 | 5.0 -> 5.3 | 4.8 -> 5.1 | 10.07 -> 9.95 | 14.34 -> 14.11 |
| 5 min | 123,750 | 49.5 / 80.0 / 90.1 / 95.1 -> 48.0 / 78.9 / 89.6 / 94.9 | 5.1 -> 5.4 | 4.8 -> 5.0 | 22.31 -> 22.12 | 31.84 -> 31.36 |
| 15 min | 122,850 | 48.9 / 79.6 / 89.9 / 95.0 -> 47.0 / 78.3 / 89.2 / 94.6 | 5.4 -> 5.6 | 4.8 -> 5.2 | 38.79 -> 38.16 | 55.53 -> 54.89 |
| 20 min | 122,400 | 49.2 / 79.6 / 90.0 / 95.1 -> 47.3 / 78.2 / 89.2 / 94.7 | 5.3 -> 5.6 | 4.7 -> 5.2 | 44.90 -> 44.18 | 64.34 -> 63.72 |
| 30 min | 121,500 | 48.9 / 79.6 / 90.0 / 95.1 -> 47.2 / 78.3 / 89.2 / 94.8 | 5.4 -> 5.6 | 4.7 -> 5.2 | 55.40 -> 54.54 | 79.66 -> 78.98 |
| 60 min | 118,800 | 49.0 / 79.5 / 89.9 / 95.1 -> 47.0 / 78.1 / 89.2 / 94.7 | 5.5 -> 5.9 | 4.6 -> 5.0 | 80.83 -> 79.22 | 115.80 -> 114.86 |
| 120 min | 113,400 | 49.7 / 79.9 / 90.2 / 95.3 -> 47.3 / 78.5 / 89.6 / 95.0 | 5.5 -> 6.0 | 4.3 -> 4.4 | 121.66 -> 118.34 | 169.59 -> 166.96 |
| 240 min | 102,600 | 49.7 / 81.5 / 91.7 / 95.8 -> 45.9 / 77.8 / 89.6 / 94.9 | 4.7 -> 6.0 | 3.6 -> 4.4 | 181.58 -> 170.37 | 244.32 -> 239.66 |

By origin phase, 15 minutes ahead:

| Phase | Origins | Coverage 50 / 80 / 90 / 95 % | Below 90 % band | Above 90 % band | 90 % width (bps) | 90 % interval score (bps) |
|---|---|---|---|---|---|---|
| overnight 18:00-08:00 | 75,600 | 49.2 / 79.9 / 90.1 / 95.1 -> 46.7 / 78.2 / 89.2 / 94.6 | 5.3 -> 5.6 | 4.7 -> 5.2 | 30.28 -> 29.44 | 43.67 -> 43.05 |
| pre open 08:00-09:30 | 8,100 | 47.4 / 78.6 / 89.7 / 95.3 -> 47.5 / 78.7 / 89.5 / 95.0 | 5.3 -> 5.4 | 5.0 -> 5.1 | 48.54 -> 49.23 | 63.50 -> 64.40 |
| opening hour 09:30-10:30 | 5,400 | 46.8 / 79.1 / 90.5 / 95.7 -> 45.3 / 77.9 / 89.6 / 95.4 | 5.6 -> 5.9 | 3.9 -> 4.5 | 88.99 -> 87.62 | 118.78 -> 117.98 |
| midday 10:30-14:00 | 18,900 | 48.2 / 79.4 / 89.8 / 95.2 -> 46.5 / 78.1 / 89.3 / 94.8 | 5.5 -> 5.8 | 4.7 -> 4.9 | 53.05 -> 52.17 | 74.60 -> 73.51 |
| afternoon 14:00-16:00 | 10,800 | 47.5 / 77.8 / 88.3 / 94.0 -> 46.5 / 76.8 / 88.4 / 94.1 | 6.2 -> 5.9 | 5.6 -> 5.8 | 44.81 -> 44.89 | 69.78 -> 68.71 |
| after close 16:00-18:00 | 4,050 | 57.0 / 83.1 / 89.9 / 93.6 -> 57.7 / 83.3 / 90.4 / 94.1 | 4.6 -> 4.5 | 5.5 -> 5.1 | 28.71 -> 29.64 | 49.78 -> 49.19 |

PIT deciles, 15 minutes ahead (ideal 10.0 each; a U shape means too narrow, a hump too wide):

| | 0-10 | 10-20 | 20-30 | 30-40 | 40-50 | 50-60 | 60-70 | 70-80 | 80-90 | 90-100 |
|---|---|---|---|---|---|---|---|---|---|---|
| baseline | 10.2 | 9.8 | 9.7 | 9.5 | 9.5 | 9.8 | 10.0 | 10.6 | 10.7 | 10.2 |
| candidate | 10.8 | 10.1 | 9.5 | 9.2 | 9.1 | 9.4 | 9.6 | 10.4 | 11.0 | 10.9 |
