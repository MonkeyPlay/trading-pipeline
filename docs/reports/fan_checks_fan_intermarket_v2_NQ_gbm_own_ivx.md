# Checks: gbm_own_ivx against fan_rw_v2 (fan_intermarket_v2, NQ)

Experiment hash `5adc22ef9b82116d`; baseline fan_rw_v2 `cf1c3910d23b9b0f`; code 70ef93ebe654+dirty.

**Development, not the verdict.** The manifest's three checks: per check the candidate is trained on the development sessions before it only (rows every 5 minutes, plus the pre-open origin), then it and the baseline are scored on every origin of the check's sessions. CRPS of the log price in basis points (K = 200) on identical origins; candidate minus baseline per session, 95 % moving-block bootstrap (5-session blocks, 2000 resamples). Negative is better; ✓ marks a whole interval below zero, ✗ above.

**Features** (17 columns, hash `7c0f6e1c82ddb538`): `base.log_sigma`, `base.release_ahead`, `base.minute`, `base.weekday`, `NQ.rv5`, `NQ.rv15`, `NQ.rv60`, `NQ.rv240`, `NQ.ret15`, `NQ.ret60`, `NQ.chg`, `NQ.vol60`, `NQ.age`, `NQ.day_rv`, `NQ.rv5d`, `VXN.level`, `VXN.iv_rv`.

| Check | Trained on | Training rows | Checked | Scored | Multiplier at the primary horizon (5 / 50 / 95 %) |
|---|---|---|---|---|---|
| 1 | 2025-07-21 to 2026-03-02 (153) | 323,281 | 2026-03-03 to 2026-04-14 | 30 of 30 | 0.74 / 0.90 / 1.18 |
| 2 | 2025-07-21 to 2026-04-14 (183) | 386,671 | 2026-04-15 to 2026-05-27 | 30 of 30 | 0.81 / 1.02 / 1.26 |
| 3 | 2025-07-21 to 2026-05-27 (213) | 450,061 | 2026-05-28 to 2026-07-10 | 30 of 30 | 0.78 / 0.99 / 1.29 |

## By horizon, the checks pooled

| Horizon | Role | Sessions | Origins | Baseline CRPS | Candidate CRPS | Difference | Share | 95 % interval | Verdict |
|---|---|---|---|---|---|---|---|---|---|
| 1 min | exploratory | 90 | 124,110 | 1.7384 | 1.7320 | -0.00638 | -0.37 % | [-0.00902, -0.00438] | better |
| 5 min | secondary | 90 | 123,750 | 3.9096 | 3.8934 | -0.01621 | -0.41 % | [-0.02247, -0.01069] | better |
| 15 min | primary | 90 | 122,850 | 6.8020 | 6.7745 | -0.02746 | -0.40 % | [-0.04307, -0.01453] | better |
| 20 min | exploratory | 90 | 122,400 | 7.8530 | 7.8216 | -0.03145 | -0.40 % | [-0.05341, -0.01382] | better |
| 30 min | secondary | 90 | 121,500 | 9.6849 | 9.6426 | -0.04236 | -0.44 % | [-0.07514, -0.01516] | better |
| 60 min | secondary | 90 | 118,800 | 14.0617 | 13.9875 | -0.07422 | -0.53 % | [-0.13287, -0.02013] | better |
| 120 min | exploratory | 90 | 113,400 | 20.4402 | 20.3211 | -0.11910 | -0.58 % | [-0.22575, -0.01066] | better |
| 240 min | exploratory | 90 | 102,600 | 29.3740 | 29.2121 | -0.16185 | -0.55 % | [-0.33873, +0.02985] | inconclusive |
| pre-open, 09:29 + 16 min | secondary | 90 | 90 | 18.0857 | 18.1219 | 0.03614 | +0.20 % | [-0.14962, +0.18531] | inconclusive |
| pre-open, 09:29 + 31 min | secondary | 90 | 90 | 27.3747 | 27.4764 | 0.10173 | +0.37 % | [-0.17011, +0.35007] | inconclusive |
| pre-open, 09:29 + 61 min | secondary | 90 | 90 | 30.0667 | 30.0212 | -0.04558 | -0.15 % | [-0.35340, +0.30835] | inconclusive |

## Per check (share of the baseline's CRPS)

| Horizon | Check 1 | Check 2 | Check 3 |
|---|---|---|---|
| 1 min | -0.35 % ✓ | -0.38 % ✓ | -0.37 % ✓ |
| 5 min | -0.21 % ✓ | -0.45 % ✓ | -0.58 % ✓ |
| 15 min | -0.14 % | -0.31 % ✓ | -0.71 % ✓ |
| 20 min | -0.10 % | -0.23 % ✓ | -0.80 % ✓ |
| 30 min | -0.09 % | -0.13 % | -0.96 % ✓ |
| 60 min | -0.03 % | -0.21 % | -1.20 % ✓ |
| 120 min | +0.09 % | -0.42 % | -1.33 % ✓ |
| 240 min | -0.10 % | -0.17 % | -1.22 % ✓ |
| pre-open, 09:29 + 16 min | -0.04 % | -0.52 % | +0.94 % |
| pre-open, 09:29 + 31 min | +1.48 % | -0.06 % | -0.21 % |
| pre-open, 09:29 + 61 min | +0.08 % | -0.44 % | -0.15 % |

## By origin phase (ET), the checks pooled

| Phase | 1 min | 5 min | 15 min | 20 min | 30 min | 60 min | 120 min | 240 min |
|---|---|---|---|---|---|---|---|---|
| overnight 18:00-08:00 | -0.49 % ✓ | -0.48 % ✓ | -0.47 % ✓ | -0.48 % ✓ | -0.50 % ✓ | -0.52 % ✓ | -0.69 % ✓ | -0.70 % |
| pre open 08:00-09:30 | -0.24 % ✓ | -0.20 % ✓ | -0.05 % | -0.07 % | -0.13 % | -0.08 % | +0.22 % | -0.19 % |
| opening hour 09:30-10:30 | -0.00 % | -0.13 % | +0.05 % | +0.18 % | +0.13 % | -0.32 % | -0.53 % | -0.49 % |
| midday 10:30-14:00 | -0.45 % ✓ | -0.43 % ✓ | -0.43 % ✓ | -0.48 % ✓ | -0.60 % | -0.96 % ✓ | -0.80 % | -0.26 % |
| afternoon 14:00-16:00 | -0.25 % ✓ | -0.46 % ✓ | -0.70 % ✓ | -0.69 % ✓ | -0.59 % | -0.37 % | -0.55 % | - |
| after close 16:00-18:00 | +0.22 % | -0.58 % ✓ | -0.52 % | -0.43 % | -0.38 % | - | - | - |
| a release ahead | +0.65 % | -0.16 % | -0.56 % | -0.51 % | -0.95 % ✓ | -0.79 % | -0.76 % | -1.52 % ✓ |

## How concentrated the primary horizon's gain is

Over 90 check sessions the candidate's CRPS was lower than the baseline's in **78 %** of sessions (mean difference -0.02746 bps). Its five best sessions carry 42 % of the total gain; its five worst add +0.2358 bps. With each of the 19 calendar weeks left out in turn the mean difference stays between -0.02998 and -0.02286 bps (0 week(s) whose removal turns it to no gain). A sensitivity check: difficult days stay in the score.

## Calibration, the checks pooled

Where each realised move fell in its fan. Coverage of the central 50 / 80 / 90 / 95 % bands (ideal: the band's own share), the 90 % band's misses below and above (ideal 5.0 % each), its mean width and its mean interval score (width plus 2 / alpha times the miss; lower is better) in basis points - baseline -> candidate.

| Horizon | Origins | Coverage 50 / 80 / 90 / 95 % | Below 90 % band | Above 90 % band | 90 % width (bps) | 90 % interval score (bps) |
|---|---|---|---|---|---|---|
| 1 min | 124,110 | 50.3 / 80.3 / 90.2 / 95.2 -> 49.1 / 79.6 / 90.0 / 95.1 | 5.0 -> 5.1 | 4.8 -> 4.9 | 10.07 -> 10.08 | 14.34 -> 14.11 |
| 5 min | 123,750 | 49.5 / 80.0 / 90.1 / 95.1 -> 48.5 / 79.5 / 90.1 / 95.2 | 5.1 -> 5.2 | 4.8 -> 4.8 | 22.31 -> 22.28 | 31.84 -> 31.24 |
| 15 min | 122,850 | 48.9 / 79.6 / 89.9 / 95.0 -> 47.6 / 79.0 / 89.8 / 95.1 | 5.4 -> 5.3 | 4.8 -> 4.8 | 38.79 -> 38.49 | 55.53 -> 54.46 |
| 20 min | 122,400 | 49.2 / 79.6 / 90.0 / 95.1 -> 47.9 / 79.0 / 89.9 / 95.2 | 5.3 -> 5.3 | 4.7 -> 4.8 | 44.90 -> 44.61 | 64.34 -> 63.18 |
| 30 min | 121,500 | 48.9 / 79.6 / 90.0 / 95.1 -> 47.8 / 79.1 / 89.9 / 95.1 | 5.4 -> 5.4 | 4.7 -> 4.8 | 55.40 -> 55.06 | 79.66 -> 78.37 |
| 60 min | 118,800 | 49.0 / 79.5 / 89.9 / 95.1 -> 47.8 / 79.1 / 90.0 / 95.4 | 5.5 -> 5.4 | 4.6 -> 4.6 | 80.83 -> 80.05 | 115.80 -> 113.15 |
| 120 min | 113,400 | 49.7 / 79.9 / 90.2 / 95.3 -> 47.8 / 79.3 / 90.0 / 95.4 | 5.5 -> 5.7 | 4.3 -> 4.3 | 121.66 -> 118.74 | 169.59 -> 164.95 |
| 240 min | 102,600 | 49.7 / 81.5 / 91.7 / 95.8 -> 46.4 / 78.6 / 90.4 / 95.3 | 4.7 -> 5.2 | 3.6 -> 4.4 | 181.58 -> 169.62 | 244.32 -> 235.14 |

By origin phase, 15 minutes ahead:

| Phase | Origins | Coverage 50 / 80 / 90 / 95 % | Below 90 % band | Above 90 % band | 90 % width (bps) | 90 % interval score (bps) |
|---|---|---|---|---|---|---|
| overnight 18:00-08:00 | 75,600 | 49.2 / 79.9 / 90.1 / 95.1 -> 47.6 / 79.2 / 90.0 / 95.2 | 5.3 -> 5.2 | 4.7 -> 4.9 | 30.28 -> 29.85 | 43.67 -> 42.78 |
| pre open 08:00-09:30 | 8,100 | 47.4 / 78.6 / 89.7 / 95.3 -> 47.3 / 78.5 / 89.8 / 95.0 | 5.3 -> 5.2 | 5.0 -> 5.0 | 48.54 -> 48.69 | 63.50 -> 63.67 |
| opening hour 09:30-10:30 | 5,400 | 46.8 / 79.1 / 90.5 / 95.7 -> 45.3 / 78.2 / 89.6 / 95.3 | 5.6 -> 6.4 | 3.9 -> 4.1 | 88.99 -> 87.28 | 118.78 -> 118.61 |
| midday 10:30-14:00 | 18,900 | 48.2 / 79.4 / 89.8 / 95.2 -> 46.9 / 78.6 / 89.6 / 95.3 | 5.5 -> 5.9 | 4.7 -> 4.5 | 53.05 -> 52.68 | 74.60 -> 72.64 |
| afternoon 14:00-16:00 | 10,800 | 47.5 / 77.8 / 88.3 / 94.0 -> 46.8 / 77.8 / 89.1 / 94.4 | 6.2 -> 5.5 | 5.6 -> 5.4 | 44.81 -> 45.40 | 69.78 -> 67.60 |
| after close 16:00-18:00 | 4,050 | 57.0 / 83.1 / 89.9 / 93.6 -> 57.0 / 83.0 / 90.5 / 94.1 | 4.6 -> 4.3 | 5.5 -> 5.2 | 28.71 -> 29.78 | 49.78 -> 48.58 |

PIT deciles, 15 minutes ahead (ideal 10.0 each; a U shape means too narrow, a hump too wide):

| | 0-10 | 10-20 | 20-30 | 30-40 | 40-50 | 50-60 | 60-70 | 70-80 | 80-90 | 90-100 |
|---|---|---|---|---|---|---|---|---|---|---|
| baseline | 10.2 | 9.8 | 9.7 | 9.5 | 9.5 | 9.8 | 10.0 | 10.6 | 10.7 | 10.2 |
| candidate | 10.5 | 10.1 | 9.7 | 9.3 | 9.2 | 9.5 | 9.8 | 10.5 | 11.0 | 10.5 |
