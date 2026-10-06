# Checks: lin_pois_ivx_shape against fan_rw_v2 (fan_intermarket_v2, NQ)

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
| 1 min | exploratory | 90 | 124,110 | 1.7384 | 1.7332 | -0.00521 | -0.30 % | [-0.00849, -0.00288] | better |
| 5 min | secondary | 90 | 123,750 | 3.9096 | 3.8931 | -0.01643 | -0.42 % | [-0.02377, -0.00998] | better |
| 15 min | primary | 90 | 122,850 | 6.8020 | 6.7759 | -0.02604 | -0.38 % | [-0.04336, -0.01222] | better |
| 20 min | exploratory | 90 | 122,400 | 7.8530 | 7.8256 | -0.02738 | -0.35 % | [-0.05158, -0.00900] | better |
| 30 min | secondary | 90 | 121,500 | 9.6849 | 9.6520 | -0.03288 | -0.34 % | [-0.07058, -0.00064] | better |
| 60 min | secondary | 90 | 118,800 | 14.0617 | 14.0047 | -0.05694 | -0.40 % | [-0.13830, +0.01443] | inconclusive |
| 120 min | exploratory | 90 | 113,400 | 20.4402 | 20.3695 | -0.07068 | -0.35 % | [-0.22199, +0.05658] | inconclusive |
| 240 min | exploratory | 90 | 102,600 | 29.3740 | 29.4213 | 0.04728 | +0.16 % | [-0.24049, +0.32435] | inconclusive |
| pre-open, 09:29 + 16 min | secondary | 90 | 90 | 18.0857 | 18.2068 | 0.12108 | +0.67 % | [-0.13656, +0.32861] | inconclusive |
| pre-open, 09:29 + 31 min | secondary | 90 | 90 | 27.3747 | 27.5057 | 0.13099 | +0.48 % | [-0.17691, +0.37527] | inconclusive |
| pre-open, 09:29 + 61 min | secondary | 90 | 90 | 30.0667 | 30.4710 | 0.40428 | +1.34 % | [-0.08022, +0.92130] | inconclusive |

## Per check (share of the baseline's CRPS)

| Horizon | Check 1 | Check 2 | Check 3 |
|---|---|---|---|
| 1 min | -0.24 % ✓ | -0.30 % ✓ | -0.36 % ✓ |
| 5 min | -0.12 % ✓ | -0.49 % ✓ | -0.64 % ✓ |
| 15 min | -0.03 % | -0.39 % ✓ | -0.70 % ✓ |
| 20 min | +0.06 % | -0.33 % ✓ | -0.74 % ✓ |
| 30 min | +0.20 % | -0.23 % | -0.90 % ✓ |
| 60 min | +0.29 % | -0.13 % | -1.23 % ✓ |
| 120 min | +0.42 % | -0.14 % | -1.21 % ✓ |
| 240 min | +0.63 % | +1.22 % ✗ | -0.97 % |
| pre-open, 09:29 + 16 min | +2.65 % ✗ | -1.15 % | +0.96 % |
| pre-open, 09:29 + 31 min | +1.89 % | +0.20 % | -0.41 % |
| pre-open, 09:29 + 61 min | +3.29 % ✗ | -0.05 % | +0.66 % |

## By origin phase (ET), the checks pooled

| Phase | 1 min | 5 min | 15 min | 20 min | 30 min | 60 min | 120 min | 240 min |
|---|---|---|---|---|---|---|---|---|
| overnight 18:00-08:00 | -0.45 % ✓ | -0.45 % ✓ | -0.52 % ✓ | -0.51 % ✓ | -0.50 % ✓ | -0.65 % ✓ | -0.72 % ✓ | -0.09 % |
| pre open 08:00-09:30 | -0.18 % ✓ | -0.07 % | +0.31 % | +0.44 % | +0.69 % | +1.66 % ✗ | +2.87 % ✗ | +3.32 % ✗ |
| opening hour 09:30-10:30 | -0.06 % | -0.36 % ✓ | +0.05 % | +0.11 % | +0.14 % | -0.28 % | +0.25 % | -0.18 % |
| midday 10:30-14:00 | -0.55 % ✓ | -0.60 % ✓ | -0.53 % ✓ | -0.54 % | -0.58 % | -0.74 % | -1.12 % | -1.18 % |
| afternoon 14:00-16:00 | -0.36 % ✓ | -0.45 % ✓ | -0.68 % ✓ | -0.63 % ✓ | -0.64 % | -0.72 % | -1.60 % | - |
| after close 16:00-18:00 | +2.70 % ✗ | +0.39 % | +0.99 % | +1.31 % | +1.04 % | - | - | - |
| a release ahead | -0.86 % | +2.34 % ✗ | +0.37 % | +0.28 % | +0.13 % | +0.60 % | -0.24 % | -0.07 % |

## How concentrated the primary horizon's gain is

Over 90 check sessions the candidate's CRPS was lower than the baseline's in **72 %** of sessions (mean difference -0.02604 bps). Its five best sessions carry 50 % of the total gain; its five worst add +0.4788 bps. With each of the 19 calendar weeks left out in turn the mean difference stays between -0.02997 and -0.02064 bps (0 week(s) whose removal turns it to no gain). A sensitivity check: difficult days stay in the score.

## Calibration, the checks pooled

Where each realised move fell in its fan. Coverage of the central 50 / 80 / 90 / 95 % bands (ideal: the band's own share), the 90 % band's misses below and above (ideal 5.0 % each), its mean width and its mean interval score (width plus 2 / alpha times the miss; lower is better) in basis points - baseline -> candidate.

| Horizon | Origins | Coverage 50 / 80 / 90 / 95 % | Below 90 % band | Above 90 % band | 90 % width (bps) | 90 % interval score (bps) |
|---|---|---|---|---|---|---|
| 1 min | 124,110 | 50.3 / 80.3 / 90.2 / 95.2 -> 52.7 / 82.4 / 91.3 / 95.8 | 5.0 -> 4.4 | 4.8 -> 4.3 | 10.07 -> 10.62 | 14.34 -> 14.17 |
| 5 min | 123,750 | 49.5 / 80.0 / 90.1 / 95.1 -> 50.5 / 80.9 / 90.9 / 95.5 | 5.1 -> 4.7 | 4.8 -> 4.4 | 22.31 -> 23.03 | 31.84 -> 31.33 |
| 15 min | 122,850 | 48.9 / 79.6 / 89.9 / 95.0 -> 49.5 / 80.6 / 90.6 / 95.5 | 5.4 -> 4.9 | 4.8 -> 4.4 | 38.79 -> 39.75 | 55.53 -> 54.55 |
| 20 min | 122,400 | 49.2 / 79.6 / 90.0 / 95.1 -> 50.2 / 80.9 / 90.8 / 95.7 | 5.3 -> 4.9 | 4.7 -> 4.3 | 44.90 -> 46.32 | 64.34 -> 63.27 |
| 30 min | 121,500 | 48.9 / 79.6 / 90.0 / 95.1 -> 50.9 / 81.6 / 91.5 / 95.9 | 5.4 -> 4.6 | 4.7 -> 3.9 | 55.40 -> 58.42 | 79.66 -> 78.73 |
| 60 min | 118,800 | 49.0 / 79.5 / 89.9 / 95.1 -> 52.1 / 82.3 / 92.0 / 96.4 | 5.5 -> 4.5 | 4.6 -> 3.5 | 80.83 -> 86.67 | 115.80 -> 114.01 |
| 120 min | 113,400 | 49.7 / 79.9 / 90.2 / 95.3 -> 52.2 / 82.4 / 91.8 / 96.3 | 5.5 -> 4.8 | 4.3 -> 3.4 | 121.66 -> 129.16 | 169.59 -> 166.66 |
| 240 min | 102,600 | 49.7 / 81.5 / 91.7 / 95.8 -> 55.3 / 86.4 / 94.4 / 97.1 | 4.7 -> 3.4 | 3.6 -> 2.2 | 181.58 -> 206.12 | 244.32 -> 246.95 |

By origin phase, 15 minutes ahead:

| Phase | Origins | Coverage 50 / 80 / 90 / 95 % | Below 90 % band | Above 90 % band | 90 % width (bps) | 90 % interval score (bps) |
|---|---|---|---|---|---|---|
| overnight 18:00-08:00 | 75,600 | 49.2 / 79.9 / 90.1 / 95.1 -> 49.4 / 80.8 / 90.9 / 95.6 | 5.3 -> 4.8 | 4.7 -> 4.3 | 30.28 -> 30.66 | 43.67 -> 42.71 |
| pre open 08:00-09:30 | 8,100 | 47.4 / 78.6 / 89.7 / 95.3 -> 52.1 / 82.3 / 92.0 / 96.4 | 5.3 -> 4.1 | 5.0 -> 3.9 | 48.54 -> 53.45 | 63.50 -> 64.98 |
| opening hour 09:30-10:30 | 5,400 | 46.8 / 79.1 / 90.5 / 95.7 -> 46.5 / 78.7 / 89.6 / 95.0 | 5.6 -> 6.0 | 3.9 -> 4.4 | 88.99 -> 88.92 | 118.78 -> 119.01 |
| midday 10:30-14:00 | 18,900 | 48.2 / 79.4 / 89.8 / 95.2 -> 48.2 / 79.8 / 90.1 / 95.4 | 5.5 -> 5.5 | 4.7 -> 4.4 | 53.05 -> 54.06 | 74.60 -> 72.48 |
| afternoon 14:00-16:00 | 10,800 | 47.5 / 77.8 / 88.3 / 94.0 -> 47.4 / 78.1 / 88.7 / 94.3 | 6.2 -> 5.7 | 5.6 -> 5.6 | 44.81 -> 45.67 | 69.78 -> 67.67 |
| after close 16:00-18:00 | 4,050 | 57.0 / 83.1 / 89.9 / 93.6 -> 62.8 / 86.7 / 92.6 / 95.4 | 4.6 -> 3.0 | 5.5 -> 4.4 | 28.71 -> 33.95 | 49.78 -> 50.07 |

PIT deciles, 15 minutes ahead (ideal 10.0 each; a U shape means too narrow, a hump too wide):

| | 0-10 | 10-20 | 20-30 | 30-40 | 40-50 | 50-60 | 60-70 | 70-80 | 80-90 | 90-100 |
|---|---|---|---|---|---|---|---|---|---|---|
| baseline | 10.2 | 9.8 | 9.7 | 9.5 | 9.5 | 9.8 | 10.0 | 10.6 | 10.7 | 10.2 |
| candidate | 9.8 | 9.8 | 9.8 | 9.7 | 9.6 | 9.9 | 10.2 | 10.7 | 10.8 | 9.6 |
