# Checks: gbm_own_iv against fan_rw_v2 (fan_intermarket_v2, NQ)

Experiment hash `5adc22ef9b82116d`; baseline fan_rw_v2 `cf1c3910d23b9b0f`; code 70ef93ebe654+dirty.

**Development, not the verdict.** The manifest's three checks: per check the candidate is trained on the development sessions before it only (rows every 5 minutes, plus the pre-open origin), then it and the baseline are scored on every origin of the check's sessions. CRPS of the log price in basis points (K = 200) on identical origins; candidate minus baseline per session, 95 % moving-block bootstrap (5-session blocks, 2000 resamples). Negative is better; ✓ marks a whole interval below zero, ✗ above.

**Features** (15 columns, hash `6f93b3c12955a67e`): `base.log_sigma`, `base.release_ahead`, `base.minute`, `base.weekday`, `NQ.rv5`, `NQ.rv15`, `NQ.rv60`, `NQ.rv240`, `NQ.ret15`, `NQ.ret60`, `NQ.chg`, `NQ.vol60`, `NQ.age`, `NQ.day_rv`, `VXN.iv_rv`.

| Check | Trained on | Training rows | Checked | Scored | Multiplier at the primary horizon (5 / 50 / 95 %) |
|---|---|---|---|---|---|
| 1 | 2025-07-21 to 2026-03-02 (153) | 323,281 | 2026-03-03 to 2026-04-14 | 30 of 30 | 0.73 / 0.90 / 1.16 |
| 2 | 2025-07-21 to 2026-04-14 (183) | 386,671 | 2026-04-15 to 2026-05-27 | 30 of 30 | 0.81 / 0.98 / 1.26 |
| 3 | 2025-07-21 to 2026-05-27 (213) | 450,061 | 2026-05-28 to 2026-07-10 | 30 of 30 | 0.76 / 0.94 / 1.21 |

## By horizon, the checks pooled

| Horizon | Role | Sessions | Origins | Baseline CRPS | Candidate CRPS | Difference | Share | 95 % interval | Verdict |
|---|---|---|---|---|---|---|---|---|---|
| 1 min | exploratory | 90 | 124,110 | 1.7384 | 1.7322 | -0.00619 | -0.36 % | [-0.00866, -0.00426] | better |
| 5 min | secondary | 90 | 123,750 | 3.9096 | 3.8950 | -0.01456 | -0.37 % | [-0.02057, -0.00923] | better |
| 15 min | primary | 90 | 122,850 | 6.8020 | 6.7803 | -0.02164 | -0.32 % | [-0.03429, -0.01127] | better |
| 20 min | exploratory | 90 | 122,400 | 7.8530 | 7.8292 | -0.02382 | -0.30 % | [-0.04047, -0.01058] | better |
| 30 min | secondary | 90 | 121,500 | 9.6849 | 9.6552 | -0.02968 | -0.31 % | [-0.05508, -0.00832] | better |
| 60 min | secondary | 90 | 118,800 | 14.0617 | 14.0027 | -0.05898 | -0.42 % | [-0.10602, -0.01268] | better |
| 120 min | exploratory | 90 | 113,400 | 20.4402 | 20.3280 | -0.11215 | -0.55 % | [-0.21104, -0.01081] | better |
| 240 min | exploratory | 90 | 102,600 | 29.3740 | 29.2187 | -0.15534 | -0.53 % | [-0.32475, +0.04213] | inconclusive |
| pre-open, 09:29 + 16 min | secondary | 90 | 90 | 18.0857 | 18.1517 | 0.06598 | +0.36 % | [-0.11857, +0.22748] | inconclusive |
| pre-open, 09:29 + 31 min | secondary | 90 | 90 | 27.3747 | 27.5888 | 0.21407 | +0.78 % | [-0.06302, +0.49959] | inconclusive |
| pre-open, 09:29 + 61 min | secondary | 90 | 90 | 30.0667 | 30.0324 | -0.03433 | -0.11 % | [-0.31609, +0.29899] | inconclusive |

## Per check (share of the baseline's CRPS)

| Horizon | Check 1 | Check 2 | Check 3 |
|---|---|---|---|
| 1 min | -0.34 % ✓ | -0.36 % ✓ | -0.37 % ✓ |
| 5 min | -0.19 % ✓ | -0.45 % ✓ | -0.48 % ✓ |
| 15 min | -0.13 % | -0.29 % ✓ | -0.51 % ✓ |
| 20 min | -0.11 % | -0.22 % ✓ | -0.54 % ✓ |
| 30 min | -0.04 % | -0.14 % | -0.66 % ✓ |
| 60 min | +0.01 % | -0.28 % | -0.91 % ✓ |
| 120 min | +0.04 % | -0.55 % ✓ | -1.10 % ✓ |
| 240 min | -0.09 % | -0.10 % | -1.22 % ✓ |
| pre-open, 09:29 + 16 min | -0.01 % | -0.46 % | +1.27 % |
| pre-open, 09:29 + 31 min | +1.89 % | -0.03 % | +0.43 % |
| pre-open, 09:29 + 61 min | +0.01 % | -0.55 % | +0.10 % |

## By origin phase (ET), the checks pooled

| Phase | 1 min | 5 min | 15 min | 20 min | 30 min | 60 min | 120 min | 240 min |
|---|---|---|---|---|---|---|---|---|
| overnight 18:00-08:00 | -0.48 % ✓ | -0.43 % ✓ | -0.40 % ✓ | -0.39 % ✓ | -0.41 % ✓ | -0.49 % ✓ | -0.60 % ✓ | -0.62 % |
| pre open 08:00-09:30 | -0.25 % ✓ | -0.18 % | +0.09 % | +0.14 % | +0.11 % | +0.02 % | +0.13 % | -0.06 % |
| opening hour 09:30-10:30 | +0.02 % | -0.16 % | +0.03 % | +0.09 % | +0.12 % | -0.33 % | -0.45 % | -0.84 % |
| midday 10:30-14:00 | -0.40 % ✓ | -0.36 % ✓ | -0.30 % | -0.32 % | -0.39 % | -0.66 % | -0.78 % | -0.35 % |
| afternoon 14:00-16:00 | -0.24 % | -0.37 % ✓ | -0.54 % ✓ | -0.56 % ✓ | -0.42 % | -0.13 % | -0.83 % | - |
| after close 16:00-18:00 | -0.05 % | -0.70 % ✓ | -0.60 % | -0.42 % | -0.28 % | - | - | - |
| a release ahead | +0.56 % | -0.03 % | -0.17 % | -0.16 % | -0.62 % | -0.71 % | -0.77 % | -1.16 % |

## How concentrated the primary horizon's gain is

Over 90 check sessions the candidate's CRPS was lower than the baseline's in **71 %** of sessions (mean difference -0.02164 bps). Its five best sessions carry 45 % of the total gain; its five worst add +0.2475 bps. With each of the 19 calendar weeks left out in turn the mean difference stays between -0.02357 and -0.01678 bps (0 week(s) whose removal turns it to no gain). A sensitivity check: difficult days stay in the score.

## Calibration, the checks pooled

Where each realised move fell in its fan. Coverage of the central 50 / 80 / 90 / 95 % bands (ideal: the band's own share), the 90 % band's misses below and above (ideal 5.0 % each), its mean width and its mean interval score (width plus 2 / alpha times the miss; lower is better) in basis points - baseline -> candidate.

| Horizon | Origins | Coverage 50 / 80 / 90 / 95 % | Below 90 % band | Above 90 % band | 90 % width (bps) | 90 % interval score (bps) |
|---|---|---|---|---|---|---|
| 1 min | 124,110 | 50.3 / 80.3 / 90.2 / 95.2 -> 48.6 / 79.1 / 89.6 / 94.9 | 5.0 -> 5.3 | 4.8 -> 5.1 | 10.07 -> 9.91 | 14.34 -> 14.11 |
| 5 min | 123,750 | 49.5 / 80.0 / 90.1 / 95.1 -> 47.5 / 78.5 / 89.4 / 94.8 | 5.1 -> 5.5 | 4.8 -> 5.1 | 22.31 -> 21.77 | 31.84 -> 31.27 |
| 15 min | 122,850 | 48.9 / 79.6 / 89.9 / 95.0 -> 46.3 / 77.8 / 88.9 / 94.5 | 5.4 -> 5.7 | 4.8 -> 5.3 | 38.79 -> 37.40 | 55.53 -> 54.61 |
| 20 min | 122,400 | 49.2 / 79.6 / 90.0 / 95.1 -> 46.5 / 77.6 / 88.8 / 94.5 | 5.3 -> 5.8 | 4.7 -> 5.4 | 44.90 -> 43.20 | 64.34 -> 63.40 |
| 30 min | 121,500 | 48.9 / 79.6 / 90.0 / 95.1 -> 46.5 / 77.7 / 88.8 / 94.6 | 5.4 -> 5.8 | 4.7 -> 5.3 | 55.40 -> 53.30 | 79.66 -> 78.59 |
| 60 min | 118,800 | 49.0 / 79.5 / 89.9 / 95.1 -> 46.6 / 77.8 / 89.1 / 94.8 | 5.5 -> 5.9 | 4.6 -> 5.0 | 80.83 -> 77.85 | 115.80 -> 113.76 |
| 120 min | 113,400 | 49.7 / 79.9 / 90.2 / 95.3 -> 46.5 / 78.0 / 89.2 / 94.8 | 5.5 -> 6.1 | 4.3 -> 4.7 | 121.66 -> 115.75 | 169.59 -> 165.03 |
| 240 min | 102,600 | 49.7 / 81.5 / 91.7 / 95.8 -> 45.5 / 77.6 / 89.7 / 95.0 | 4.7 -> 5.5 | 3.6 -> 4.8 | 181.58 -> 167.08 | 244.32 -> 234.95 |

By origin phase, 15 minutes ahead:

| Phase | Origins | Coverage 50 / 80 / 90 / 95 % | Below 90 % band | Above 90 % band | 90 % width (bps) | 90 % interval score (bps) |
|---|---|---|---|---|---|---|
| overnight 18:00-08:00 | 75,600 | 49.2 / 79.9 / 90.1 / 95.1 -> 46.3 / 78.0 / 89.1 / 94.6 | 5.3 -> 5.6 | 4.7 -> 5.3 | 30.28 -> 29.04 | 43.67 -> 42.88 |
| pre open 08:00-09:30 | 8,100 | 47.4 / 78.6 / 89.7 / 95.3 -> 46.1 / 77.3 / 88.6 / 94.5 | 5.3 -> 5.8 | 5.0 -> 5.6 | 48.54 -> 47.41 | 63.50 -> 63.81 |
| opening hour 09:30-10:30 | 5,400 | 46.8 / 79.1 / 90.5 / 95.7 -> 44.3 / 77.3 / 89.2 / 95.0 | 5.6 -> 6.3 | 3.9 -> 4.4 | 88.99 -> 85.61 | 118.78 -> 118.14 |
| midday 10:30-14:00 | 18,900 | 48.2 / 79.4 / 89.8 / 95.2 -> 45.6 / 77.3 / 88.7 / 94.6 | 5.5 -> 6.3 | 4.7 -> 5.0 | 53.05 -> 50.97 | 74.60 -> 73.11 |
| afternoon 14:00-16:00 | 10,800 | 47.5 / 77.8 / 88.3 / 94.0 -> 45.7 / 76.0 / 88.2 / 93.9 | 6.2 -> 5.9 | 5.6 -> 5.9 | 44.81 -> 43.93 | 69.78 -> 67.94 |
| after close 16:00-18:00 | 4,050 | 57.0 / 83.1 / 89.9 / 93.6 -> 55.3 / 81.3 / 89.4 / 93.4 | 4.6 -> 4.8 | 5.5 -> 5.8 | 28.71 -> 28.50 | 49.78 -> 48.61 |

PIT deciles, 15 minutes ahead (ideal 10.0 each; a U shape means too narrow, a hump too wide):

| | 0-10 | 10-20 | 20-30 | 30-40 | 40-50 | 50-60 | 60-70 | 70-80 | 80-90 | 90-100 |
|---|---|---|---|---|---|---|---|---|---|---|
| baseline | 10.2 | 9.8 | 9.7 | 9.5 | 9.5 | 9.8 | 10.0 | 10.6 | 10.7 | 10.2 |
| candidate | 11.1 | 10.2 | 9.5 | 9.1 | 8.9 | 9.2 | 9.5 | 10.3 | 11.1 | 11.1 |
