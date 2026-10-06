# Baseline gate: fan_rw_v2 against fan_rw_v1 (fan_intermarket_v2)

Experiment hash `5adc22ef9b82116d`; fan_rw_v1 `d92d72a7e16b6ef9`, fan_rw_v2 `cf1c3910d23b9b0f`; code 35e63d17860f+dirty.

**Rule** (fixed in the manifest before any result): a rule-based fan_rw_v2 (development chunk 2) replaces fan_rw_v1 as the baseline when its paired difference against fan_rw_v1, NQ at 15 minutes over the three checks' sessions, has its whole 95 % interval below zero - decided before any model is frozen; otherwise fan_rw_v1 stays the baseline.

**Sessions:** the three checks, 2026-03-03 to 2026-07-10: 90 of 90 scored; target NQ. Both versions walk forward as they would have been issued: each session fitted on the sessions before it, v2's shape from the standardised errors of the 120 sessions before it. CRPS of the log price in basis points, the manifest's quantile form (K = 200) on identical origins; v2 minus v1 per session, 95 % moving-block bootstrap (5-session blocks, 2000 resamples).

## Decision: the baseline is **fan_rw_v2**

| Horizon | Role | Sessions | Origins | v1 CRPS | v2 CRPS | v2 - v1 | Share | 95 % interval | Verdict |
|---|---|---|---|---|---|---|---|---|---|
| 1 min | exploratory | 90 | 124,110 | 1.7447 | 1.7384 | -0.00625 | -0.36 % | [-0.00981, -0.00297] | better |
| 5 min | secondary | 90 | 123,750 | 3.9214 | 3.9096 | -0.01177 | -0.30 % | [-0.02008, -0.00443] | better |
| 15 min | primary | 90 | 122,850 | 6.8237 | 6.8020 | -0.02176 | -0.32 % | [-0.03853, -0.00754] | better |
| 20 min | exploratory | 90 | 122,400 | 7.8807 | 7.8530 | -0.02774 | -0.35 % | [-0.04829, -0.00946] | better |
| 30 min | secondary | 90 | 121,500 | 9.7191 | 9.6849 | -0.03418 | -0.35 % | [-0.06404, -0.00615] | better |
| 60 min | secondary | 90 | 118,800 | 14.1062 | 14.0617 | -0.04457 | -0.32 % | [-0.09431, +0.00112] | inconclusive |
| 120 min | exploratory | 90 | 113,400 | 20.5062 | 20.4402 | -0.06596 | -0.32 % | [-0.13750, -0.00749] | better |
| 240 min | exploratory | 90 | 102,600 | 29.4344 | 29.3740 | -0.06043 | -0.21 % | [-0.13763, +0.00537] | inconclusive |
| pre-open, 09:29 + 16 min | secondary | 90 | 90 | 18.3386 | 18.0857 | -0.25286 | -1.38 % | [-0.59670, +0.14373] | inconclusive |
| pre-open, 09:29 + 31 min | secondary | 90 | 90 | 27.5211 | 27.3747 | -0.14638 | -0.53 % | [-0.46925, +0.20680] | inconclusive |
| pre-open, 09:29 + 61 min | secondary | 90 | 90 | 30.4809 | 30.0667 | -0.41418 | -1.36 % | [-0.86938, +0.04593] | inconclusive |

## How v2 was chosen (development sessions before the checks)

- **Releases by name:** at the release minute, CPI moved about 140x its usual minute variance (median), payrolls about 50x, PPI about 28x, ISM Manufacturing about 2x - one 'high' multiplier fitted none of them. Per release, shrunk towards the group: CRPS of origins with a release ahead -2 % to -5 %, their 90 % band from about 82 % to 86-88 %.
- **Earnings at the close:** the store dates an earnings release by its 8-K filing, which follows the market's reaction (AMZN and GOOGL showed nothing at their filing minute) and is not known in advance. Their own group at 16:00-17:00: origins reaching 16:00-17:00 from 89.9 % to 90.7 % held; the 16:15-17:00 minutes had been forecast at under half their realised variance.
- **Fat-tailed shape:** the symmetric empirical shape of the last 120 sessions' standardised errors: CRPS -0.25 % to -0.32 % at 1, 15 and 60 minutes, every interval below zero; 40 sessions did as well as 120.
- **The open:** no rule. The 09:30-10:30 minutes were forecast at 0.9-1.0 of their realised variance; the pre-open origins' shortfall came from the 08:30 releases. Two candidates changed nothing: stopping the last hour's level at 09:30, and a phase-bounded outlier cap.
- **Combined, on those sessions:** CRPS -0.25 % (1 min), -0.40 % (15 min), -0.44 % (60 min), every interval below zero. The checks' sessions were not scored until v2 was registered.
