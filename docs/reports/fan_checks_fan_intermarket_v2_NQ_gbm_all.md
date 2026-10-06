# Checks: gbm_all against fan_rw_v2 (fan_intermarket_v2, NQ)

Experiment hash `5adc22ef9b82116d`; baseline fan_rw_v2 `cf1c3910d23b9b0f`; code 70ef93ebe654+dirty.

**Development, not the verdict.** The manifest's three checks: per check the candidate is trained on the development sessions before it only (rows every 5 minutes, plus the pre-open origin), then it and the baseline are scored on every origin of the check's sessions. CRPS of the log price in basis points (K = 200) on identical origins; candidate minus baseline per session, 95 % moving-block bootstrap (5-session blocks, 2000 resamples). Negative is better; ✓ marks a whole interval below zero, ✗ above.

**Features** (137 columns, hash `906f219fb690f87d`): `base.log_sigma`, `base.release_ahead`, `base.minute`, `base.weekday`, `DX.rv5`, `DX.rv15`, `DX.rv60`, `DX.rv240`, `DX.ret15`, `DX.ret60`, `DX.chg`, `DX.vol60`, `DX.age`, `DX.day_rv`, `DX.rv5d`, `ES.rv5`, `ES.rv15`, `ES.rv60`, `ES.rv240`, `ES.ret15`, `ES.ret60`, `ES.chg`, `ES.vol60`, `ES.age`, `ES.day_rv`, `ES.rv5d`, `NQ.rv5`, `NQ.rv15`, `NQ.rv60`, `NQ.rv240`, `NQ.ret15`, `NQ.ret60`, `NQ.chg`, `NQ.vol60`, `NQ.age`, `NQ.day_rv`, `NQ.rv5d`, `10Y.rv5`, `10Y.rv15`, `10Y.rv60`, `10Y.rv240`, `10Y.ret15`, `10Y.ret60`, `10Y.chg`, `10Y.vol60`, `10Y.age`, `10Y.day_rv`, `10Y.rv5d`, `IWM.rv5`, `IWM.rv15`, `IWM.rv60`, `IWM.rv240`, `IWM.ret15`, `IWM.ret60`, `IWM.chg`, `IWM.vol60`, `IWM.age`, `IWM.day_rv`, `IWM.rv5d`, `QQQ.rv5`, `QQQ.rv15`, `QQQ.rv60`, `QQQ.rv240`, `QQQ.ret15`, `QQQ.ret60`, `QQQ.chg`, `QQQ.vol60`, `QQQ.age`, `QQQ.day_rv`, `QQQ.rv5d`, `RTY.rv5`, `RTY.rv15`, `RTY.rv60`, `RTY.rv240`, `RTY.ret15`, `RTY.ret60`, `RTY.chg`, `RTY.vol60`, `RTY.age`, `RTY.day_rv`, `RTY.rv5d`, `SMH.rv5`, `SMH.rv15`, `SMH.rv60`, `SMH.rv240`, `SMH.ret15`, `SMH.ret60`, `SMH.chg`, `SMH.vol60`, `SMH.age`, `SMH.day_rv`, `SMH.rv5d`, `SPY.rv5`, `SPY.rv15`, `SPY.rv60`, `SPY.rv240`, `SPY.ret15`, `SPY.ret60`, `SPY.chg`, `SPY.vol60`, `SPY.age`, `SPY.day_rv`, `SPY.rv5d`, `TNX.rv5`, `TNX.rv15`, `TNX.rv60`, `TNX.rv240`, `TNX.ret15`, `TNX.ret60`, `TNX.chg`, `TNX.age`, `TNX.day_rv`, `TNX.rv5d`, `VIX.rv5`, `VIX.rv15`, `VIX.rv60`, `VIX.rv240`, `VIX.ret15`, `VIX.ret60`, `VIX.chg`, `VIX.age`, `VIX.day_rv`, `VIX.rv5d`, `VIX.level`, `VIX.iv_rv`, `VXN.rv5`, `VXN.rv15`, `VXN.rv60`, `VXN.rv240`, `VXN.ret15`, `VXN.ret60`, `VXN.chg`, `VXN.age`, `VXN.day_rv`, `VXN.rv5d`, `VXN.level`, `VXN.iv_rv`.

| Check | Trained on | Training rows | Checked | Scored | Multiplier at the primary horizon (5 / 50 / 95 %) |
|---|---|---|---|---|---|
| 1 | 2025-07-21 to 2026-03-02 (153) | 323,281 | 2026-03-03 to 2026-04-14 | 30 of 30 | 0.74 / 0.96 / 1.30 |
| 2 | 2025-07-21 to 2026-04-14 (183) | 386,671 | 2026-04-15 to 2026-05-27 | 30 of 30 | 0.84 / 0.98 / 1.21 |
| 3 | 2025-07-21 to 2026-05-27 (213) | 450,061 | 2026-05-28 to 2026-07-10 | 30 of 30 | 0.75 / 0.94 / 1.20 |

## By horizon, the checks pooled

| Horizon | Role | Sessions | Origins | Baseline CRPS | Candidate CRPS | Difference | Share | 95 % interval | Verdict |
|---|---|---|---|---|---|---|---|---|---|
| 1 min | exploratory | 90 | 124,110 | 1.7384 | 1.7322 | -0.00620 | -0.36 % | [-0.00893, -0.00419] | better |
| 5 min | secondary | 90 | 123,750 | 3.9096 | 3.8974 | -0.01213 | -0.31 % | [-0.01744, -0.00726] | better |
| 15 min | primary | 90 | 122,850 | 6.8020 | 6.7870 | -0.01492 | -0.22 % | [-0.03024, -0.00293] | better |
| 20 min | exploratory | 90 | 122,400 | 7.8530 | 7.8405 | -0.01252 | -0.16 % | [-0.03541, +0.00491] | inconclusive |
| 30 min | secondary | 90 | 121,500 | 9.6849 | 9.6758 | -0.00914 | -0.09 % | [-0.04143, +0.01564] | inconclusive |
| 60 min | secondary | 90 | 118,800 | 14.0617 | 14.0490 | -0.01264 | -0.09 % | [-0.05452, +0.02825] | inconclusive |
| 120 min | exploratory | 90 | 113,400 | 20.4402 | 20.4334 | -0.00676 | -0.03 % | [-0.07990, +0.08112] | inconclusive |
| 240 min | exploratory | 90 | 102,600 | 29.3740 | 29.3271 | -0.04694 | -0.16 % | [-0.18110, +0.11870] | inconclusive |
| pre-open, 09:29 + 16 min | secondary | 90 | 90 | 18.0857 | 18.2908 | 0.20505 | +1.13 % | [-0.10207, +0.45780] | inconclusive |
| pre-open, 09:29 + 31 min | secondary | 90 | 90 | 27.3747 | 27.7281 | 0.35343 | +1.29 % | [+0.03413, +0.62763] | worse |
| pre-open, 09:29 + 61 min | secondary | 90 | 90 | 30.0667 | 30.2773 | 0.21060 | +0.70 % | [-0.10959, +0.60454] | inconclusive |

## Per check (share of the baseline's CRPS)

| Horizon | Check 1 | Check 2 | Check 3 |
|---|---|---|---|
| 1 min | -0.33 % ✓ | -0.37 % ✓ | -0.37 % ✓ |
| 5 min | -0.21 % ✓ | -0.37 % ✓ | -0.35 % ✓ |
| 15 min | -0.04 % | -0.14 % | -0.44 % ✓ |
| 20 min | +0.10 % | -0.05 % | -0.47 % ✓ |
| 30 min | +0.21 % | +0.09 % | -0.50 % ✓ |
| 60 min | +0.24 % | +0.11 % | -0.53 % ✓ |
| 120 min | +0.18 % | +0.17 % | -0.37 % |
| 240 min | -0.17 % | +0.18 % | -0.37 % |
| pre-open, 09:29 + 16 min | +3.51 % | -0.85 % | +1.32 % |
| pre-open, 09:29 + 31 min | +1.88 % | +0.30 % | +1.43 % |
| pre-open, 09:29 + 61 min | +1.52 % ✗ | -0.55 % | +0.91 % |

## By origin phase (ET), the checks pooled

| Phase | 1 min | 5 min | 15 min | 20 min | 30 min | 60 min | 120 min | 240 min |
|---|---|---|---|---|---|---|---|---|
| overnight 18:00-08:00 | -0.47 % ✓ | -0.39 % ✓ | -0.39 % ✓ | -0.38 % ✓ | -0.34 % ✓ | -0.39 % | -0.37 % | -0.35 % |
| pre open 08:00-09:30 | -0.26 % ✓ | -0.12 % | +0.27 % | +0.42 % | +0.69 % | +0.68 % | +0.91 % | +0.25 % |
| opening hour 09:30-10:30 | +0.04 % | -0.12 % | +0.44 % ✗ | +0.48 % | +0.56 % ✗ | +0.39 % | -0.04 % | +0.04 % |
| midday 10:30-14:00 | -0.44 % ✓ | -0.34 % ✓ | -0.28 % | -0.18 % | -0.10 % | +0.00 % | +0.22 % | +0.20 % |
| afternoon 14:00-16:00 | -0.15 % | -0.16 % | -0.29 % | -0.17 % | -0.18 % | +0.02 % | +0.33 % | - |
| after close 16:00-18:00 | -0.25 % | -0.52 % ✓ | -0.34 % | -0.13 % | -0.16 % | - | - | - |
| a release ahead | +2.10 % | +0.40 % | +0.53 % | +0.07 % | +0.11 % | -0.46 % | -0.48 % | -0.97 % |

## How concentrated the primary horizon's gain is

Over 90 check sessions the candidate's CRPS was lower than the baseline's in **64 %** of sessions (mean difference -0.01492 bps). Its five best sessions carry 59 % of the total gain; its five worst add +0.4562 bps. With each of the 19 calendar weeks left out in turn the mean difference stays between -0.01789 and -0.00919 bps (0 week(s) whose removal turns it to no gain). A sensitivity check: difficult days stay in the score.

## Calibration, the checks pooled

Where each realised move fell in its fan. Coverage of the central 50 / 80 / 90 / 95 % bands (ideal: the band's own share), the 90 % band's misses below and above (ideal 5.0 % each), its mean width and its mean interval score (width plus 2 / alpha times the miss; lower is better) in basis points - baseline -> candidate.

| Horizon | Origins | Coverage 50 / 80 / 90 / 95 % | Below 90 % band | Above 90 % band | 90 % width (bps) | 90 % interval score (bps) |
|---|---|---|---|---|---|---|
| 1 min | 124,110 | 50.3 / 80.3 / 90.2 / 95.2 -> 48.9 / 79.5 / 89.8 / 95.0 | 5.0 -> 5.2 | 4.8 -> 5.0 | 10.07 -> 10.06 | 14.34 -> 14.12 |
| 5 min | 123,750 | 49.5 / 80.0 / 90.1 / 95.1 -> 47.7 / 78.7 / 89.5 / 94.8 | 5.1 -> 5.5 | 4.8 -> 5.1 | 22.31 -> 22.03 | 31.84 -> 31.38 |
| 15 min | 122,850 | 48.9 / 79.6 / 89.9 / 95.0 -> 47.2 / 78.5 / 89.4 / 94.8 | 5.4 -> 5.5 | 4.8 -> 5.1 | 38.79 -> 38.44 | 55.53 -> 54.98 |
| 20 min | 122,400 | 49.2 / 79.6 / 90.0 / 95.1 -> 47.4 / 78.3 / 89.3 / 94.8 | 5.3 -> 5.6 | 4.7 -> 5.1 | 44.90 -> 44.55 | 64.34 -> 63.91 |
| 30 min | 121,500 | 48.9 / 79.6 / 90.0 / 95.1 -> 47.4 / 78.3 / 89.3 / 94.7 | 5.4 -> 5.6 | 4.7 -> 5.1 | 55.40 -> 55.14 | 79.66 -> 79.49 |
| 60 min | 118,800 | 49.0 / 79.5 / 89.9 / 95.1 -> 47.2 / 78.0 / 89.0 / 94.6 | 5.5 -> 6.0 | 4.6 -> 5.0 | 80.83 -> 79.64 | 115.80 -> 115.38 |
| 120 min | 113,400 | 49.7 / 79.9 / 90.2 / 95.3 -> 47.4 / 78.1 / 89.3 / 94.6 | 5.5 -> 6.0 | 4.3 -> 4.7 | 121.66 -> 117.99 | 169.59 -> 169.09 |
| 240 min | 102,600 | 49.7 / 81.5 / 91.7 / 95.8 -> 45.6 / 77.9 / 89.4 / 94.6 | 4.7 -> 5.8 | 3.6 -> 4.8 | 181.58 -> 166.42 | 244.32 -> 242.19 |

By origin phase, 15 minutes ahead:

| Phase | Origins | Coverage 50 / 80 / 90 / 95 % | Below 90 % band | Above 90 % band | 90 % width (bps) | 90 % interval score (bps) |
|---|---|---|---|---|---|---|
| overnight 18:00-08:00 | 75,600 | 49.2 / 79.9 / 90.1 / 95.1 -> 46.9 / 78.6 / 89.5 / 94.9 | 5.3 -> 5.5 | 4.7 -> 5.0 | 30.28 -> 29.67 | 43.67 -> 42.92 |
| pre open 08:00-09:30 | 8,100 | 47.4 / 78.6 / 89.7 / 95.3 -> 47.5 / 78.4 / 89.3 / 94.8 | 5.3 -> 5.4 | 5.0 -> 5.3 | 48.54 -> 49.45 | 63.50 -> 64.67 |
| opening hour 09:30-10:30 | 5,400 | 46.8 / 79.1 / 90.5 / 95.7 -> 45.4 / 78.5 / 89.4 / 95.0 | 5.6 -> 6.4 | 3.9 -> 4.2 | 88.99 -> 88.24 | 118.78 -> 121.08 |
| midday 10:30-14:00 | 18,900 | 48.2 / 79.4 / 89.8 / 95.2 -> 47.2 / 78.7 / 89.4 / 94.8 | 5.5 -> 5.8 | 4.7 -> 4.8 | 53.05 -> 52.80 | 74.60 -> 73.62 |
| afternoon 14:00-16:00 | 10,800 | 47.5 / 77.8 / 88.3 / 94.0 -> 46.2 / 76.7 / 88.3 / 93.8 | 6.2 -> 5.8 | 5.6 -> 6.0 | 44.81 -> 45.12 | 69.78 -> 68.69 |
| after close 16:00-18:00 | 4,050 | 57.0 / 83.1 / 89.9 / 93.6 -> 56.6 / 82.3 / 90.3 / 93.8 | 4.6 -> 4.4 | 5.5 -> 5.4 | 28.71 -> 29.11 | 49.78 -> 49.06 |

PIT deciles, 15 minutes ahead (ideal 10.0 each; a U shape means too narrow, a hump too wide):

| | 0-10 | 10-20 | 20-30 | 30-40 | 40-50 | 50-60 | 60-70 | 70-80 | 80-90 | 90-100 |
|---|---|---|---|---|---|---|---|---|---|---|
| baseline | 10.2 | 9.8 | 9.7 | 9.5 | 9.5 | 9.8 | 10.0 | 10.6 | 10.7 | 10.2 |
| candidate | 10.8 | 10.1 | 9.6 | 9.2 | 9.1 | 9.4 | 9.7 | 10.4 | 11.1 | 10.7 |
