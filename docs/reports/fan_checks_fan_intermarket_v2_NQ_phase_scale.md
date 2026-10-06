# Checks: phase_scale against fan_rw_v2 (fan_intermarket_v2, NQ)

Experiment hash `5adc22ef9b82116d`; baseline fan_rw_v2 `cf1c3910d23b9b0f`; code 44b99b9cb497+dirty.

**Development, not the verdict.** The manifest's three checks: per check the candidate is trained on the development sessions before it only (rows every 5 minutes, plus the pre-open origin), then it and the baseline are scored on every origin of the check's sessions. CRPS of the log price in basis points (K = 200) on identical origins; candidate minus baseline per session, 95 % moving-block bootstrap (5-session blocks, 2000 resamples). Negative is better; ✓ marks a whole interval below zero, ✗ above.

| Check | Trained on | Training rows | Checked | Scored |
|---|---|---|---|---|
| 1 | 2025-07-21 to 2026-03-02 (153) | 323,281 | 2026-03-03 to 2026-04-14 | 30 of 30 |
| 2 | 2025-07-21 to 2026-04-14 (183) | 386,671 | 2026-04-15 to 2026-05-27 | 30 of 30 |
| 3 | 2025-07-21 to 2026-05-27 (213) | 450,061 | 2026-05-28 to 2026-07-10 | 30 of 30 |

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
