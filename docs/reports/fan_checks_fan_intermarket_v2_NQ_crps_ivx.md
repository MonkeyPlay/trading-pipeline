# Checks: crps_ivx against fan_rw_v2 (fan_intermarket_v2, NQ)

Experiment hash `5adc22ef9b82116d`; baseline fan_rw_v2 `cf1c3910d23b9b0f`; code 70ef93ebe654+dirty.

**Development, not the verdict.** The manifest's three checks: per check the candidate is trained on the development sessions before it only (rows every 5 minutes, plus the pre-open origin), then it and the baseline are scored on every origin of the check's sessions. CRPS of the log price in basis points (K = 200) on identical origins; candidate minus baseline per session, 95 % moving-block bootstrap (5-session blocks, 2000 resamples). Negative is better; ✓ marks a whole interval below zero, ✗ above.

**Features** (17 columns, hash `7c0f6e1c82ddb538`): `base.log_sigma`, `base.release_ahead`, `base.minute`, `base.weekday`, `NQ.rv5`, `NQ.rv15`, `NQ.rv60`, `NQ.rv240`, `NQ.ret15`, `NQ.ret60`, `NQ.chg`, `NQ.vol60`, `NQ.age`, `NQ.day_rv`, `NQ.rv5d`, `VXN.level`, `VXN.iv_rv`.

| Check | Trained on | Training rows | Checked | Scored | Multiplier at the primary horizon (5 / 50 / 95 %) |
|---|---|---|---|---|---|
| 1 | 2025-07-21 to 2026-03-02 (153) | 323,281 | 2026-03-03 to 2026-04-14 | 30 of 30 | 0.77 / 0.97 / 1.26 |
| 2 | 2025-07-21 to 2026-04-14 (183) | 386,671 | 2026-04-15 to 2026-05-27 | 30 of 30 | 0.86 / 1.01 / 1.27 |
| 3 | 2025-07-21 to 2026-05-27 (213) | 450,061 | 2026-05-28 to 2026-07-10 | 30 of 30 | 0.81 / 0.99 / 1.27 |

## By horizon, the checks pooled

| Horizon | Role | Sessions | Origins | Baseline CRPS | Candidate CRPS | Difference | Share | 95 % interval | Verdict |
|---|---|---|---|---|---|---|---|---|---|
| 1 min | exploratory | 90 | 124,110 | 1.7384 | 1.7306 | -0.00779 | -0.45 % | [-0.01055, -0.00540] | better |
| 5 min | secondary | 90 | 123,750 | 3.9096 | 3.8929 | -0.01672 | -0.43 % | [-0.02341, -0.01061] | better |
| 15 min | primary | 90 | 122,850 | 6.8020 | 6.7771 | -0.02488 | -0.37 % | [-0.03779, -0.01216] | better |
| 20 min | exploratory | 90 | 122,400 | 7.8530 | 7.8275 | -0.02553 | -0.33 % | [-0.04588, -0.00722] | better |
| 30 min | secondary | 90 | 121,500 | 9.6849 | 9.6522 | -0.03272 | -0.34 % | [-0.06009, -0.00759] | better |
| 60 min | secondary | 90 | 118,800 | 14.0617 | 13.9915 | -0.07019 | -0.50 % | [-0.12721, -0.01794] | better |
| 120 min | exploratory | 90 | 113,400 | 20.4402 | 20.3251 | -0.11511 | -0.56 % | [-0.23578, -0.00055] | better |
| 240 min | exploratory | 90 | 102,600 | 29.3740 | 29.2485 | -0.12551 | -0.43 % | [-0.33204, +0.09772] | inconclusive |
| pre-open, 09:29 + 16 min | secondary | 90 | 90 | 18.0857 | 18.2839 | 0.19815 | +1.10 % | [-0.06439, +0.41702] | inconclusive |
| pre-open, 09:29 + 31 min | secondary | 90 | 90 | 27.3747 | 27.5009 | 0.12623 | +0.46 % | [-0.14618, +0.35130] | inconclusive |
| pre-open, 09:29 + 61 min | secondary | 90 | 90 | 30.0667 | 30.1415 | 0.07478 | +0.25 % | [-0.31941, +0.50370] | inconclusive |

## Per check (share of the baseline's CRPS)

| Horizon | Check 1 | Check 2 | Check 3 |
|---|---|---|---|
| 1 min | -0.28 % ✓ | -0.48 % ✓ | -0.59 % ✓ |
| 5 min | -0.18 % ✓ | -0.47 % ✓ | -0.63 % ✓ |
| 15 min | -0.09 % | -0.32 % ✓ | -0.65 % ✓ |
| 20 min | +0.03 % | -0.25 % ✓ | -0.70 % ✓ |
| 30 min | +0.08 % | -0.22 % ✓ | -0.80 % ✓ |
| 60 min | +0.11 % | -0.37 % | -1.15 % ✓ |
| 120 min | +0.26 % | -0.54 % | -1.36 % ✓ |
| 240 min | +0.25 % | -0.13 % | -1.25 % ✓ |
| pre-open, 09:29 + 16 min | +3.69 % ✗ | -0.88 % | +1.15 % |
| pre-open, 09:29 + 31 min | +1.09 % | -0.15 % | +0.35 % |
| pre-open, 09:29 + 61 min | +2.16 % ✗ | -1.07 % | -0.46 % |

## By origin phase (ET), the checks pooled

| Phase | 1 min | 5 min | 15 min | 20 min | 30 min | 60 min | 120 min | 240 min |
|---|---|---|---|---|---|---|---|---|
| overnight 18:00-08:00 | -0.52 % ✓ | -0.47 % ✓ | -0.46 % ✓ | -0.47 % ✓ | -0.47 % ✓ | -0.60 % ✓ | -0.70 % ✓ | -0.66 % |
| pre open 08:00-09:30 | -0.33 % ✓ | -0.23 % ✓ | +0.20 % | +0.29 % | +0.20 % | +0.29 % | +0.97 % | +0.60 % |
| opening hour 09:30-10:30 | -0.10 % | -0.28 % | -0.09 % | -0.01 % | -0.06 % | -0.41 % | -0.75 % | -0.57 % |
| midday 10:30-14:00 | -0.51 % ✓ | -0.50 % ✓ | -0.43 % ✓ | -0.39 % | -0.42 % | -0.77 % | -0.87 % | -0.20 % |
| afternoon 14:00-16:00 | -0.32 % ✓ | -0.32 % ✓ | -0.46 % ✓ | -0.36 % | -0.33 % | -0.34 % | -1.11 % | - |
| after close 16:00-18:00 | -0.77 % ✓ | -0.62 % ✓ | -0.44 % | -0.20 % | -0.09 % | - | - | - |
| a release ahead | +0.54 % | +0.20 % | -0.11 % | -0.25 % | -0.71 % | -0.60 % | -0.39 % | -0.91 % |

## How concentrated the primary horizon's gain is

Over 90 check sessions the candidate's CRPS was lower than the baseline's in **78 %** of sessions (mean difference -0.02488 bps). Its five best sessions carry 36 % of the total gain; its five worst add +0.2765 bps. With each of the 19 calendar weeks left out in turn the mean difference stays between -0.02813 and -0.02127 bps (0 week(s) whose removal turns it to no gain). A sensitivity check: difficult days stay in the score.

## Calibration, the checks pooled

Where each realised move fell in its fan. Coverage of the central 50 / 80 / 90 / 95 % bands (ideal: the band's own share), the 90 % band's misses below and above (ideal 5.0 % each), its mean width and its mean interval score (width plus 2 / alpha times the miss; lower is better) in basis points - baseline -> candidate.

| Horizon | Origins | Coverage 50 / 80 / 90 / 95 % | Below 90 % band | Above 90 % band | 90 % width (bps) | 90 % interval score (bps) |
|---|---|---|---|---|---|---|
| 1 min | 124,110 | 50.3 / 80.3 / 90.2 / 95.2 -> 51.0 / 81.6 / 91.4 / 95.9 | 5.0 -> 4.4 | 4.8 -> 4.2 | 10.07 -> 10.52 | 14.34 -> 14.10 |
| 5 min | 123,750 | 49.5 / 80.0 / 90.1 / 95.1 -> 49.6 / 80.7 / 90.9 / 95.7 | 5.1 -> 4.7 | 4.8 -> 4.4 | 22.31 -> 23.07 | 31.84 -> 31.32 |
| 15 min | 122,850 | 48.9 / 79.6 / 89.9 / 95.0 -> 48.4 / 79.9 / 90.4 / 95.4 | 5.4 -> 5.1 | 4.8 -> 4.6 | 38.79 -> 39.52 | 55.53 -> 54.69 |
| 20 min | 122,400 | 49.2 / 79.6 / 90.0 / 95.1 -> 48.6 / 79.6 / 90.2 / 95.4 | 5.3 -> 5.1 | 4.7 -> 4.6 | 44.90 -> 45.57 | 64.34 -> 63.54 |
| 30 min | 121,500 | 48.9 / 79.6 / 90.0 / 95.1 -> 48.2 / 79.4 / 90.0 / 95.2 | 5.4 -> 5.3 | 4.7 -> 4.7 | 55.40 -> 55.67 | 79.66 -> 78.69 |
| 60 min | 118,800 | 49.0 / 79.5 / 89.9 / 95.1 -> 48.3 / 79.7 / 90.5 / 95.6 | 5.5 -> 5.3 | 4.6 -> 4.3 | 80.83 -> 81.60 | 115.80 -> 113.55 |
| 120 min | 113,400 | 49.7 / 79.9 / 90.2 / 95.3 -> 48.9 / 80.6 / 90.9 / 95.7 | 5.5 -> 5.4 | 4.3 -> 3.7 | 121.66 -> 122.34 | 169.59 -> 166.20 |
| 240 min | 102,600 | 49.7 / 81.5 / 91.7 / 95.8 -> 48.3 / 81.0 / 91.5 / 95.9 | 4.7 -> 4.9 | 3.6 -> 3.6 | 181.58 -> 178.05 | 244.32 -> 239.61 |

By origin phase, 15 minutes ahead:

| Phase | Origins | Coverage 50 / 80 / 90 / 95 % | Below 90 % band | Above 90 % band | 90 % width (bps) | 90 % interval score (bps) |
|---|---|---|---|---|---|---|
| overnight 18:00-08:00 | 75,600 | 49.2 / 79.9 / 90.1 / 95.1 -> 48.4 / 80.1 / 90.6 / 95.5 | 5.3 -> 4.9 | 4.7 -> 4.5 | 30.28 -> 30.53 | 43.67 -> 42.84 |
| pre open 08:00-09:30 | 8,100 | 47.4 / 78.6 / 89.7 / 95.3 -> 48.2 / 79.5 / 90.1 / 95.2 | 5.3 -> 5.2 | 5.0 -> 4.7 | 48.54 -> 50.39 | 63.50 -> 64.51 |
| opening hour 09:30-10:30 | 5,400 | 46.8 / 79.1 / 90.5 / 95.7 -> 48.2 / 81.1 / 91.6 / 96.4 | 5.6 -> 5.2 | 3.9 -> 3.1 | 88.99 -> 93.32 | 118.78 -> 118.97 |
| midday 10:30-14:00 | 18,900 | 48.2 / 79.4 / 89.8 / 95.2 -> 48.5 / 80.2 / 90.7 / 95.8 | 5.5 -> 5.2 | 4.7 -> 4.1 | 53.05 -> 54.82 | 74.60 -> 72.85 |
| afternoon 14:00-16:00 | 10,800 | 47.5 / 77.8 / 88.3 / 94.0 -> 46.5 / 76.8 / 88.7 / 94.1 | 6.2 -> 5.7 | 5.6 -> 5.6 | 44.81 -> 44.94 | 69.78 -> 68.35 |
| after close 16:00-18:00 | 4,050 | 57.0 / 83.1 / 89.9 / 93.6 -> 55.0 / 80.9 / 89.2 / 93.2 | 4.6 -> 5.0 | 5.5 -> 5.7 | 28.71 -> 27.87 | 49.78 -> 49.14 |

PIT deciles, 15 minutes ahead (ideal 10.0 each; a U shape means too narrow, a hump too wide):

| | 0-10 | 10-20 | 20-30 | 30-40 | 40-50 | 50-60 | 60-70 | 70-80 | 80-90 | 90-100 |
|---|---|---|---|---|---|---|---|---|---|---|
| baseline | 10.2 | 9.8 | 9.7 | 9.5 | 9.5 | 9.8 | 10.0 | 10.6 | 10.7 | 10.2 |
| candidate | 10.2 | 10.0 | 9.8 | 9.4 | 9.4 | 9.7 | 9.9 | 10.7 | 11.0 | 10.0 |
