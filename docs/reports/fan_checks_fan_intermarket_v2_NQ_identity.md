# Checks: identity against fan_rw_v2 (fan_intermarket_v2, NQ)

Experiment hash `5adc22ef9b82116d`; baseline fan_rw_v2 `cf1c3910d23b9b0f`; code 70ef93ebe654+dirty.

**Development, not the verdict.** The manifest's three checks: per check the candidate is trained on the development sessions before it only (rows every 5 minutes, plus the pre-open origin), then it and the baseline are scored on every origin of the check's sessions. CRPS of the log price in basis points (K = 200) on identical origins; candidate minus baseline per session, 95 % moving-block bootstrap (5-session blocks, 2000 resamples). Negative is better; ✓ marks a whole interval below zero, ✗ above.

| Check | Trained on | Training rows | Checked | Scored | Multiplier at the primary horizon (5 / 50 / 95 %) |
|---|---|---|---|---|---|
| 1 | 2025-07-21 to 2026-03-02 (153) | 323,281 | 2026-03-03 to 2026-04-14 | 30 of 30 | 1.00 / 1.00 / 1.00 |
| 2 | 2025-07-21 to 2026-04-14 (183) | 386,671 | 2026-04-15 to 2026-05-27 | 30 of 30 | 1.00 / 1.00 / 1.00 |
| 3 | 2025-07-21 to 2026-05-27 (213) | 450,061 | 2026-05-28 to 2026-07-10 | 30 of 30 | 1.00 / 1.00 / 1.00 |

## By horizon, the checks pooled

| Horizon | Role | Sessions | Origins | Baseline CRPS | Candidate CRPS | Difference | Share | 95 % interval | Verdict |
|---|---|---|---|---|---|---|---|---|---|
| 1 min | exploratory | 90 | 124,110 | 1.7384 | 1.7384 | 0.00000 | +0.00 % | [+0.00000, +0.00000] | inconclusive |
| 5 min | secondary | 90 | 123,750 | 3.9096 | 3.9096 | 0.00000 | +0.00 % | [+0.00000, +0.00000] | inconclusive |
| 15 min | primary | 90 | 122,850 | 6.8020 | 6.8020 | 0.00000 | +0.00 % | [+0.00000, +0.00000] | inconclusive |
| 20 min | exploratory | 90 | 122,400 | 7.8530 | 7.8530 | 0.00000 | +0.00 % | [+0.00000, +0.00000] | inconclusive |
| 30 min | secondary | 90 | 121,500 | 9.6849 | 9.6849 | 0.00000 | +0.00 % | [+0.00000, +0.00000] | inconclusive |
| 60 min | secondary | 90 | 118,800 | 14.0617 | 14.0617 | 0.00000 | +0.00 % | [+0.00000, +0.00000] | inconclusive |
| 120 min | exploratory | 90 | 113,400 | 20.4402 | 20.4402 | 0.00000 | +0.00 % | [+0.00000, +0.00000] | inconclusive |
| 240 min | exploratory | 90 | 102,600 | 29.3740 | 29.3740 | 0.00000 | +0.00 % | [+0.00000, +0.00000] | inconclusive |
| pre-open, 09:29 + 16 min | secondary | 90 | 90 | 18.0857 | 18.0857 | 0.00000 | +0.00 % | [+0.00000, +0.00000] | inconclusive |
| pre-open, 09:29 + 31 min | secondary | 90 | 90 | 27.3747 | 27.3747 | 0.00000 | +0.00 % | [+0.00000, +0.00000] | inconclusive |
| pre-open, 09:29 + 61 min | secondary | 90 | 90 | 30.0667 | 30.0667 | 0.00000 | +0.00 % | [+0.00000, +0.00000] | inconclusive |

## Per check (share of the baseline's CRPS)

| Horizon | Check 1 | Check 2 | Check 3 |
|---|---|---|---|
| 1 min | +0.00 % | +0.00 % | +0.00 % |
| 5 min | +0.00 % | +0.00 % | +0.00 % |
| 15 min | +0.00 % | +0.00 % | +0.00 % |
| 20 min | +0.00 % | +0.00 % | +0.00 % |
| 30 min | +0.00 % | +0.00 % | +0.00 % |
| 60 min | +0.00 % | +0.00 % | +0.00 % |
| 120 min | +0.00 % | +0.00 % | +0.00 % |
| 240 min | +0.00 % | +0.00 % | +0.00 % |
| pre-open, 09:29 + 16 min | +0.00 % | +0.00 % | +0.00 % |
| pre-open, 09:29 + 31 min | +0.00 % | +0.00 % | +0.00 % |
| pre-open, 09:29 + 61 min | +0.00 % | +0.00 % | +0.00 % |

## By origin phase (ET), the checks pooled

| Phase | 1 min | 5 min | 15 min | 20 min | 30 min | 60 min | 120 min | 240 min |
|---|---|---|---|---|---|---|---|---|
| overnight 18:00-08:00 | +0.00 % | +0.00 % | +0.00 % | +0.00 % | +0.00 % | +0.00 % | +0.00 % | +0.00 % |
| pre open 08:00-09:30 | +0.00 % | +0.00 % | +0.00 % | +0.00 % | +0.00 % | +0.00 % | +0.00 % | +0.00 % |
| opening hour 09:30-10:30 | +0.00 % | +0.00 % | +0.00 % | +0.00 % | +0.00 % | +0.00 % | +0.00 % | +0.00 % |
| midday 10:30-14:00 | +0.00 % | +0.00 % | +0.00 % | +0.00 % | +0.00 % | +0.00 % | +0.00 % | +0.00 % |
| afternoon 14:00-16:00 | +0.00 % | +0.00 % | +0.00 % | +0.00 % | +0.00 % | +0.00 % | +0.00 % | - |
| after close 16:00-18:00 | +0.00 % | +0.00 % | +0.00 % | +0.00 % | +0.00 % | - | - | - |
| a release ahead | +0.00 % | +0.00 % | +0.00 % | +0.00 % | +0.00 % | +0.00 % | +0.00 % | +0.00 % |

## How concentrated the primary horizon's gain is

Over 90 check sessions the candidate's CRPS was lower than the baseline's in **0 %** of sessions (mean difference +0.00000 bps). There is no total gain; its five worst add +0.0000 bps. With each of the 19 calendar weeks left out in turn the mean difference stays between +0.00000 and +0.00000 bps. A sensitivity check: difficult days stay in the score.

## Calibration, the checks pooled

Where each realised move fell in its fan. Coverage of the central 50 / 80 / 90 / 95 % bands (ideal: the band's own share), the 90 % band's misses below and above (ideal 5.0 % each) and its mean width in basis points - baseline -> candidate.

| Horizon | Origins | Coverage 50 / 80 / 90 / 95 % | Below 90 % band | Above 90 % band | 90 % width (bps) |
|---|---|---|---|---|---|
| 1 min | 124,110 | 50.3 / 80.3 / 90.2 / 95.2 -> 50.3 / 80.3 / 90.2 / 95.2 | 5.0 -> 5.0 | 4.8 -> 4.8 | 10.07 -> 10.07 |
| 5 min | 123,750 | 49.5 / 80.0 / 90.1 / 95.1 -> 49.5 / 80.0 / 90.1 / 95.1 | 5.1 -> 5.1 | 4.8 -> 4.8 | 22.31 -> 22.31 |
| 15 min | 122,850 | 48.9 / 79.6 / 89.9 / 95.0 -> 48.9 / 79.6 / 89.9 / 95.0 | 5.4 -> 5.4 | 4.8 -> 4.8 | 38.79 -> 38.79 |
| 20 min | 122,400 | 49.2 / 79.6 / 90.0 / 95.1 -> 49.2 / 79.6 / 90.0 / 95.1 | 5.3 -> 5.3 | 4.7 -> 4.7 | 44.90 -> 44.90 |
| 30 min | 121,500 | 48.9 / 79.6 / 90.0 / 95.1 -> 48.9 / 79.6 / 90.0 / 95.1 | 5.4 -> 5.4 | 4.7 -> 4.7 | 55.40 -> 55.40 |
| 60 min | 118,800 | 49.0 / 79.5 / 89.9 / 95.1 -> 49.0 / 79.5 / 89.9 / 95.1 | 5.5 -> 5.5 | 4.6 -> 4.6 | 80.83 -> 80.83 |
| 120 min | 113,400 | 49.7 / 79.9 / 90.2 / 95.3 -> 49.7 / 79.9 / 90.2 / 95.3 | 5.5 -> 5.5 | 4.3 -> 4.3 | 121.66 -> 121.66 |
| 240 min | 102,600 | 49.7 / 81.5 / 91.7 / 95.8 -> 49.7 / 81.5 / 91.7 / 95.8 | 4.7 -> 4.7 | 3.6 -> 3.6 | 181.58 -> 181.58 |

By origin phase, 15 minutes ahead:

| Phase | Origins | Coverage 50 / 80 / 90 / 95 % | Below 90 % band | Above 90 % band | 90 % width (bps) |
|---|---|---|---|---|---|
| overnight 18:00-08:00 | 75,600 | 49.2 / 79.9 / 90.1 / 95.1 -> 49.2 / 79.9 / 90.1 / 95.1 | 5.3 -> 5.3 | 4.7 -> 4.7 | 30.28 -> 30.28 |
| pre open 08:00-09:30 | 8,100 | 47.4 / 78.6 / 89.7 / 95.3 -> 47.4 / 78.6 / 89.7 / 95.3 | 5.3 -> 5.3 | 5.0 -> 5.0 | 48.54 -> 48.54 |
| opening hour 09:30-10:30 | 5,400 | 46.8 / 79.1 / 90.5 / 95.7 -> 46.8 / 79.1 / 90.5 / 95.7 | 5.6 -> 5.6 | 3.9 -> 3.9 | 88.99 -> 88.99 |
| midday 10:30-14:00 | 18,900 | 48.2 / 79.4 / 89.8 / 95.2 -> 48.2 / 79.4 / 89.8 / 95.2 | 5.5 -> 5.5 | 4.7 -> 4.7 | 53.05 -> 53.05 |
| afternoon 14:00-16:00 | 10,800 | 47.5 / 77.8 / 88.3 / 94.0 -> 47.5 / 77.8 / 88.3 / 94.0 | 6.2 -> 6.2 | 5.6 -> 5.6 | 44.81 -> 44.81 |
| after close 16:00-18:00 | 4,050 | 57.0 / 83.1 / 89.9 / 93.6 -> 57.0 / 83.1 / 89.9 / 93.6 | 4.6 -> 4.6 | 5.5 -> 5.5 | 28.71 -> 28.71 |

PIT deciles, 15 minutes ahead (ideal 10.0 each; a U shape means too narrow, a hump too wide):

| | 0-10 | 10-20 | 20-30 | 30-40 | 40-50 | 50-60 | 60-70 | 70-80 | 80-90 | 90-100 |
|---|---|---|---|---|---|---|---|---|---|---|
| baseline | 10.2 | 9.8 | 9.7 | 9.5 | 9.5 | 9.8 | 10.0 | 10.6 | 10.7 | 10.2 |
| candidate | 10.2 | 9.8 | 9.7 | 9.5 | 9.5 | 9.8 | 10.0 | 10.6 | 10.7 | 10.2 |
