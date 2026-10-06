# Checks: gam_ivx against fan_rw_v2 (fan_intermarket_v2, NQ)

Experiment hash `5adc22ef9b82116d`; baseline fan_rw_v2 `cf1c3910d23b9b0f`; code 70ef93ebe654+dirty.

**Development, not the verdict.** The manifest's three checks: per check the candidate is trained on the development sessions before it only (rows every 5 minutes, plus the pre-open origin), then it and the baseline are scored on every origin of the check's sessions. CRPS of the log price in basis points (K = 200) on identical origins; candidate minus baseline per session, 95 % moving-block bootstrap (5-session blocks, 2000 resamples). Negative is better; ✓ marks a whole interval below zero, ✗ above.

**Features** (17 columns, hash `7c0f6e1c82ddb538`): `base.log_sigma`, `base.release_ahead`, `base.minute`, `base.weekday`, `NQ.rv5`, `NQ.rv15`, `NQ.rv60`, `NQ.rv240`, `NQ.ret15`, `NQ.ret60`, `NQ.chg`, `NQ.vol60`, `NQ.age`, `NQ.day_rv`, `NQ.rv5d`, `VXN.level`, `VXN.iv_rv`.

| Check | Trained on | Training rows | Checked | Scored | Multiplier at the primary horizon (5 / 50 / 95 %) |
|---|---|---|---|---|---|
| 1 | 2025-07-21 to 2026-03-02 (153) | 323,281 | 2026-03-03 to 2026-04-14 | 30 of 30 | 0.79 / 0.93 / 1.19 |
| 2 | 2025-07-21 to 2026-04-14 (183) | 386,671 | 2026-04-15 to 2026-05-27 | 30 of 30 | 0.83 / 0.98 / 1.21 |
| 3 | 2025-07-21 to 2026-05-27 (213) | 450,061 | 2026-05-28 to 2026-07-10 | 30 of 30 | 0.80 / 0.99 / 1.24 |

## By horizon, the checks pooled

| Horizon | Role | Sessions | Origins | Baseline CRPS | Candidate CRPS | Difference | Share | 95 % interval | Verdict |
|---|---|---|---|---|---|---|---|---|---|
| 1 min | exploratory | 90 | 124,110 | 1.7384 | 1.7333 | -0.00518 | -0.30 % | [-0.00786, -0.00324] | better |
| 5 min | secondary | 90 | 123,750 | 3.9096 | 3.8956 | -0.01399 | -0.36 % | [-0.01978, -0.00893] | better |
| 15 min | primary | 90 | 122,850 | 6.8020 | 6.7778 | -0.02412 | -0.35 % | [-0.03834, -0.01231] | better |
| 20 min | exploratory | 90 | 122,400 | 7.8530 | 7.8251 | -0.02791 | -0.36 % | [-0.04752, -0.01277] | better |
| 30 min | secondary | 90 | 121,500 | 9.6849 | 9.6458 | -0.03912 | -0.40 % | [-0.06902, -0.01475] | better |
| 60 min | secondary | 90 | 118,800 | 14.0617 | 13.9953 | -0.06639 | -0.47 % | [-0.11862, -0.01626] | better |
| 120 min | exploratory | 90 | 113,400 | 20.4402 | 20.3329 | -0.10731 | -0.53 % | [-0.20199, -0.01628] | better |
| 240 min | exploratory | 90 | 102,600 | 29.3740 | 29.2197 | -0.15433 | -0.53 % | [-0.32695, +0.02289] | inconclusive |
| pre-open, 09:29 + 16 min | secondary | 90 | 90 | 18.0857 | 18.0580 | -0.02773 | -0.15 % | [-0.19521, +0.10781] | inconclusive |
| pre-open, 09:29 + 31 min | secondary | 90 | 90 | 27.3747 | 27.4378 | 0.06316 | +0.23 % | [-0.20265, +0.28048] | inconclusive |
| pre-open, 09:29 + 61 min | secondary | 90 | 90 | 30.0667 | 29.9780 | -0.08871 | -0.30 % | [-0.31663, +0.18292] | inconclusive |

## Per check (share of the baseline's CRPS)

| Horizon | Check 1 | Check 2 | Check 3 |
|---|---|---|---|
| 1 min | -0.29 % ✓ | -0.26 % ✓ | -0.33 % ✓ |
| 5 min | -0.18 % ✓ | -0.37 % ✓ | -0.51 % ✓ |
| 15 min | -0.16 % | -0.20 % ✓ | -0.64 % ✓ |
| 20 min | -0.14 % | -0.16 % ✓ | -0.69 % ✓ |
| 30 min | -0.15 % | -0.10 % | -0.85 % ✓ |
| 60 min | -0.12 % | -0.15 % | -1.01 % ✓ |
| 120 min | -0.07 % | -0.42 % | -1.03 % ✓ |
| 240 min | -0.15 % | -0.13 % | -1.14 % ✓ |
| pre-open, 09:29 + 16 min | +0.25 % | -0.81 % | +0.14 % |
| pre-open, 09:29 + 31 min | +1.32 % | -0.38 % | -0.22 % |
| pre-open, 09:29 + 61 min | -0.19 % | +0.30 % | -0.83 % |

## By origin phase (ET), the checks pooled

| Phase | 1 min | 5 min | 15 min | 20 min | 30 min | 60 min | 120 min | 240 min |
|---|---|---|---|---|---|---|---|---|
| overnight 18:00-08:00 | -0.43 % ✓ | -0.43 % ✓ | -0.45 % ✓ | -0.44 % ✓ | -0.43 % ✓ | -0.44 % ✓ | -0.63 % ✓ | -0.64 % |
| pre open 08:00-09:30 | -0.17 % ✓ | -0.08 % | +0.05 % | -0.01 % | -0.15 % | -0.25 % | +0.26 % | -0.17 % |
| opening hour 09:30-10:30 | +0.12 % | -0.10 % | +0.16 % | +0.14 % | +0.08 % | -0.05 % | -0.20 % | -0.35 % |
| midday 10:30-14:00 | -0.34 % ✓ | -0.39 % ✓ | -0.36 % | -0.39 % | -0.53 % | -0.80 % ✓ | -0.74 % | -0.45 % |
| afternoon 14:00-16:00 | -0.23 % ✓ | -0.36 % ✓ | -0.62 % ✓ | -0.60 % ✓ | -0.67 % ✓ | -0.54 % | -0.88 % | - |
| after close 16:00-18:00 | +0.13 % | -0.61 % ✓ | -0.61 % | -0.53 % | -0.54 % | - | - | - |
| a release ahead | +0.90 % | -0.01 % | -0.29 % | -0.43 % | -0.62 % | -0.86 % ✓ | -1.02 % ✓ | -1.75 % ✓ |

## How concentrated the primary horizon's gain is

Over 90 check sessions the candidate's CRPS was lower than the baseline's in **71 %** of sessions (mean difference -0.02412 bps). Its five best sessions carry 43 % of the total gain; its five worst add +0.1901 bps. With each of the 19 calendar weeks left out in turn the mean difference stays between -0.02665 and -0.01953 bps (0 week(s) whose removal turns it to no gain). A sensitivity check: difficult days stay in the score.

## Calibration, the checks pooled

Where each realised move fell in its fan. Coverage of the central 50 / 80 / 90 / 95 % bands (ideal: the band's own share), the 90 % band's misses below and above (ideal 5.0 % each), its mean width and its mean interval score (width plus 2 / alpha times the miss; lower is better) in basis points - baseline -> candidate.

| Horizon | Origins | Coverage 50 / 80 / 90 / 95 % | Below 90 % band | Above 90 % band | 90 % width (bps) | 90 % interval score (bps) |
|---|---|---|---|---|---|---|
| 1 min | 124,110 | 50.3 / 80.3 / 90.2 / 95.2 -> 49.0 / 79.4 / 89.8 / 95.0 | 5.0 -> 5.2 | 4.8 -> 5.0 | 10.07 -> 10.05 | 14.34 -> 14.15 |
| 5 min | 123,750 | 49.5 / 80.0 / 90.1 / 95.1 -> 48.4 / 79.3 / 89.9 / 95.1 | 5.1 -> 5.2 | 4.8 -> 4.9 | 22.31 -> 22.26 | 31.84 -> 31.32 |
| 15 min | 122,850 | 48.9 / 79.6 / 89.9 / 95.0 -> 47.6 / 79.0 / 89.7 / 95.0 | 5.4 -> 5.4 | 4.8 -> 4.9 | 38.79 -> 38.54 | 55.53 -> 54.56 |
| 20 min | 122,400 | 49.2 / 79.6 / 90.0 / 95.1 -> 47.8 / 78.7 / 89.7 / 95.0 | 5.3 -> 5.4 | 4.7 -> 5.0 | 44.90 -> 44.38 | 64.34 -> 63.27 |
| 30 min | 121,500 | 48.9 / 79.6 / 90.0 / 95.1 -> 47.7 / 78.9 / 89.7 / 95.0 | 5.4 -> 5.4 | 4.7 -> 4.9 | 55.40 -> 54.65 | 79.66 -> 78.40 |
| 60 min | 118,800 | 49.0 / 79.5 / 89.9 / 95.1 -> 47.6 / 78.7 / 89.7 / 95.1 | 5.5 -> 5.6 | 4.6 -> 4.7 | 80.83 -> 79.54 | 115.80 -> 113.41 |
| 120 min | 113,400 | 49.7 / 79.9 / 90.2 / 95.3 -> 48.0 / 79.1 / 90.1 / 95.4 | 5.5 -> 5.6 | 4.3 -> 4.4 | 121.66 -> 119.02 | 169.59 -> 165.06 |
| 240 min | 102,600 | 49.7 / 81.5 / 91.7 / 95.8 -> 46.2 / 78.5 / 90.1 / 95.1 | 4.7 -> 5.5 | 3.6 -> 4.5 | 181.58 -> 168.94 | 244.32 -> 235.50 |

By origin phase, 15 minutes ahead:

| Phase | Origins | Coverage 50 / 80 / 90 / 95 % | Below 90 % band | Above 90 % band | 90 % width (bps) | 90 % interval score (bps) |
|---|---|---|---|---|---|---|
| overnight 18:00-08:00 | 75,600 | 49.2 / 79.9 / 90.1 / 95.1 -> 47.9 / 79.6 / 90.1 / 95.3 | 5.3 -> 5.1 | 4.7 -> 4.7 | 30.28 -> 30.10 | 43.67 -> 42.78 |
| pre open 08:00-09:30 | 8,100 | 47.4 / 78.6 / 89.7 / 95.3 -> 46.9 / 78.5 / 89.2 / 94.8 | 5.3 -> 5.6 | 5.0 -> 5.2 | 48.54 -> 48.48 | 63.50 -> 64.17 |
| opening hour 09:30-10:30 | 5,400 | 46.8 / 79.1 / 90.5 / 95.7 -> 45.3 / 77.7 / 89.1 / 95.2 | 5.6 -> 6.4 | 3.9 -> 4.5 | 88.99 -> 87.81 | 118.78 -> 119.06 |
| midday 10:30-14:00 | 18,900 | 48.2 / 79.4 / 89.8 / 95.2 -> 46.4 / 78.1 / 89.2 / 95.0 | 5.5 -> 5.9 | 4.7 -> 4.8 | 53.05 -> 52.34 | 74.60 -> 72.91 |
| afternoon 14:00-16:00 | 10,800 | 47.5 / 77.8 / 88.3 / 94.0 -> 46.2 / 76.8 / 88.3 / 94.1 | 6.2 -> 5.9 | 5.6 -> 5.8 | 44.81 -> 44.89 | 69.78 -> 67.64 |
| after close 16:00-18:00 | 4,050 | 57.0 / 83.1 / 89.9 / 93.6 -> 56.4 / 82.1 / 90.3 / 93.9 | 4.6 -> 4.4 | 5.5 -> 5.3 | 28.71 -> 29.14 | 49.78 -> 48.64 |

PIT deciles, 15 minutes ahead (ideal 10.0 each; a U shape means too narrow, a hump too wide):

| | 0-10 | 10-20 | 20-30 | 30-40 | 40-50 | 50-60 | 60-70 | 70-80 | 80-90 | 90-100 |
|---|---|---|---|---|---|---|---|---|---|---|
| baseline | 10.2 | 9.8 | 9.7 | 9.5 | 9.5 | 9.8 | 10.0 | 10.6 | 10.7 | 10.2 |
| candidate | 10.5 | 10.1 | 9.7 | 9.3 | 9.2 | 9.5 | 9.7 | 10.6 | 11.0 | 10.5 |
